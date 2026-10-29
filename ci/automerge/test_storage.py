"""Unit tests for the storage gate (rule 8). Run: python3 -m unittest discover -s ci/automerge -v

Fixtures in fixtures/zpool/ are `zpool status -p` output:
  2026-10-29-mpool-degraded-resilvered.txt  the real pool on 2026-10-29: raidz2-1 with dev12
                                            FAULTED, the resilver finished, redundancy left
  2026-10-18-mpool-resilvering.txt          a replace resilvering in raidz2-0, dev12 FAULTED
                                            in raidz2-1 (trimmed)
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import policy  # noqa: E402
from policy import STORAGE_BLOCKED, STORAGE_NO_HEAVY, STORAGE_OPEN  # noqa: E402


def fixture(name: str) -> str:
    return (HERE / "fixtures" / "zpool" / name).read_text()


TODAY = fixture("2026-10-29-mpool-degraded-resilvered.txt")
RESILVERING = fixture("2026-10-18-mpool-resilvering.txt")
DEGRADED = "mpool\tDEGRADED\n"
ONLINE = "mpool\tONLINE\n"

HEALTHY = """  pool: mpool
 state: ONLINE
  scan: scrub repaired 0B in 1 days 02:00:00 with 0 errors on Sun Oct 11 02:00:00 2026
config:

\tNAME        STATE     READ WRITE CKSUM
\tmpool       ONLINE       0     0     0
\t  raidz2-0  ONLINE       0     0     0
\t    dev1    ONLINE       0     0     0
\t    dev2    ONLINE       0     0     0
\t    dev3    ONLINE       0     0     0
\t    dev4    ONLINE       0     0     0

errors: No known data errors
"""

SCRUB_LINE = ("  scan: scrub in progress since Sun Nov  8 00:24:01 2026\n"
              "\t120T / 366T scanned at 1.1G/s, 40T / 366T issued at 400M/s\n"
              "\t0B repaired, 10.93% done, 3 days 01:02:03 to go")
TODAY_SCAN = "  scan: resilvered 17.4T in 3 days 00:56:49 with 0 errors on Wed Oct 21 22:07:21 2026"


def edit(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def fault(text: str, dev: str, state: str = "FAULTED") -> str:
    """Set a leaf vdev's state, keeping the column layout."""
    for line in text.splitlines():
        toks = line.split()
        if toks and toks[0] == dev:
            return edit(text, line, line.replace(f" {toks[1]}", f" {state.ljust(len(toks[1]))}", 1))
    raise AssertionError(dev)


class ParseTests(unittest.TestCase):
    def test_today_tree(self):
        st = policy.parse_zpool_status(TODAY)["mpool"]
        self.assertEqual(st.state, "DEGRADED")
        self.assertFalse(st.repairing)
        self.assertFalse(st.resilvering)
        self.assertEqual([v.name for v in st.vdevs], ["raidz2-0", "raidz2-1", "raidz2-2"])
        self.assertEqual([len(v.children) for v in st.vdevs], [8, 8, 7])
        self.assertEqual(st.rows["dev12"], ("FAULTED", (55, 0, 0)))

    def test_resilvering_tree(self):
        st = policy.parse_zpool_status(RESILVERING)["mpool"]
        self.assertTrue(st.repairing)
        self.assertTrue(st.resilvering)
        rz0 = st.vdevs[0]
        self.assertEqual([c.name for c in rz0.children], ["dev1", "replacing-7"])
        self.assertEqual([c.name for c in rz0.children[1].children], ["dev22", "dev17"])
        spares = [v for v in st.vdevs if v.section == "spares"]
        self.assertEqual([v.name for v in spares], ["dev30"])
        self.assertNotIn("spares", st.rows)


class StorageVerdictTests(unittest.TestCase):
    def level(self, status, lst=DEGRADED):
        return policy.storage_verdict(lst, status)

    # --- the real state on 2026-10-29 ---------------------------------------

    def test_today_degraded_with_redundancy_is_open(self):
        level, msg = self.level(TODAY)
        self.assertEqual(level, STORAGE_OPEN, msg)
        self.assertIn("raidz2-1: dev12 unavailable, redundancy 1 left", msg)

    # --- rule 4: DEGRADED with redundancy, nothing running ------------------

    def test_healthy_pool_is_open(self):
        self.assertEqual(self.level(HEALTHY, ONLINE), (STORAGE_OPEN, "storage pools: mpool ONLINE"))

    def test_one_down_in_each_raidz2_vdev_is_open(self):
        st = fault(fault(TODAY, "dev3", "UNAVAIL"), "dev9", "REMOVED")
        self.assertEqual(self.level(st)[0], STORAGE_OPEN)

    def test_error_counters_alone_do_not_block(self):
        st = edit(TODAY, "dev11   ONLINE       0     0     0", "dev11   ONLINE       3     0     7")
        self.assertEqual(self.level(st)[0], STORAGE_OPEN)

    def test_log_cache_and_spare_vdevs_do_not_count(self):
        st = edit(TODAY, "\nerrors:", "\tlogs\n\t  dev40   FAULTED      0     0     0\n"
                                     "\tcache\n\t  dev41   UNAVAIL      0     0     0\n"
                                     "\tspares\n\t  dev30   AVAIL\n\nerrors:")
        self.assertEqual(self.level(st)[0], STORAGE_OPEN)

    # --- rule 1: block everything -------------------------------------------

    def test_raidz2_two_down_in_same_vdev_blocks(self):
        for state in ("FAULTED", "UNAVAIL", "OFFLINE", "REMOVED"):
            with self.subTest(state=state):
                level, msg = self.level(fault(TODAY, "dev11", state))
                self.assertEqual(level, STORAGE_BLOCKED)
                self.assertIn("raidz2-1 has no redundancy left", msg)
                self.assertIn("dev12, dev11", msg)

    def test_pool_health_blocks(self):
        for health in ("SUSPENDED", "UNAVAIL", "FAULTED", "OFFLINE", "REMOVED"):
            with self.subTest(health=health):
                level, msg = self.level(TODAY, f"mpool\t{health}\n")
                self.assertEqual(level, STORAGE_BLOCKED)
                self.assertIn(health, msg)
        st = edit(TODAY, " state: DEGRADED", " state: UNAVAIL")
        self.assertEqual(self.level(st)[0], STORAGE_BLOCKED)

    def test_top_level_vdev_unavailable_blocks(self):
        st = edit(TODAY, "raidz2-2  ONLINE  ", "raidz2-2  UNAVAIL ")
        self.assertEqual(self.level(st)[0], STORAGE_BLOCKED)

    def test_suspended_io_blocks(self):
        st = edit(TODAY, "status: One or more devices are faulted in response to persistent errors.",
                  "status: One or more devices are faulted in response to IO failures.\n\tThe pool I/O is suspended.")
        level, msg = self.level(st)
        self.assertEqual(level, STORAGE_BLOCKED)
        self.assertIn("suspended", msg)

    def test_data_errors_block(self):
        level, msg = self.level(edit(TODAY, "No known data errors", "3 data errors, use '-v' for a list"))
        self.assertEqual(level, STORAGE_BLOCKED)
        self.assertIn("data errors", msg)

    def test_mirror_and_single_disk_vdevs(self):
        mirror = edit(HEALTHY, "\t  raidz2-0  ONLINE       0     0     0\n\t    dev1    ONLINE       0     0     0\n"
                               "\t    dev2    ONLINE       0     0     0\n\t    dev3    ONLINE       0     0     0\n"
                               "\t    dev4    ONLINE       0     0     0\n",
                      "\t  mirror-0  ONLINE       0     0     0\n\t    dev1    ONLINE       0     0     0\n"
                      "\t    dev2    ONLINE       0     0     0\n")
        self.assertEqual(self.level(mirror, ONLINE)[0], STORAGE_OPEN)
        self.assertEqual(self.level(fault(mirror, "dev2"))[0], STORAGE_BLOCKED)   # 2-way, one left
        three = edit(mirror, "\t    dev2    ONLINE       0     0     0\n",
                     "\t    dev2    ONLINE       0     0     0\n\t    dev3    ONLINE       0     0     0\n")
        self.assertEqual(self.level(fault(three, "dev2"))[0], STORAGE_OPEN)       # 3-way, one down
        single = edit(HEALTHY, "\t  raidz2-0  ONLINE       0     0     0\n\t    dev1    ONLINE       0     0     0\n"
                               "\t    dev2    ONLINE       0     0     0\n\t    dev3    ONLINE       0     0     0\n"
                               "\t    dev4    ONLINE       0     0     0\n",
                      "\t  dev1      ONLINE       0     0     0\n")
        level, msg = self.level(single, ONLINE)
        self.assertEqual(level, STORAGE_BLOCKED)
        self.assertIn("no parity", msg)

    def test_unparsable_or_missing_output_blocks(self):
        self.assertEqual(self.level(edit(TODAY, "FAULTED     55", "FAULTED     5.5K"))[0], STORAGE_BLOCKED)
        self.assertEqual(self.level(TODAY, "")[0], STORAGE_BLOCKED)
        self.assertEqual(self.level("")[0], STORAGE_BLOCKED)
        self.assertEqual(self.level(TODAY, "mpool\tDEGRADED\ntank\tONLINE\n")[0], STORAGE_BLOCKED)
        self.assertEqual(self.level(TODAY, "mpool DEGRADED extra\n")[0], STORAGE_BLOCKED)
        cut = TODAY[:TODAY.index("config:")] + "config:\n\nerrors: No known data errors\n"
        self.assertEqual(self.level(cut)[0], STORAGE_BLOCKED)

    # --- rule 2: resilver / scrub running -> heavy changes wait --------------

    def test_resilver_with_redundancy_blocks_heavy_only(self):
        level, msg = self.level(RESILVERING)
        self.assertEqual(level, STORAGE_NO_HEAVY, msg)
        self.assertIn("resilvering", msg)
        # the replacing slot counts as unavailable until its resilver finishes
        self.assertIn("raidz2-0: replacing-7 unavailable, redundancy 1 left", msg)

    def test_scrub_in_progress_blocks_heavy_only(self):
        level, msg = self.level(edit(TODAY, TODAY_SCAN, SCRUB_LINE))
        self.assertEqual(level, STORAGE_NO_HEAVY, msg)
        self.assertIn("scrubbing", msg)
        self.assertEqual(self.level(edit(HEALTHY, "  scan: scrub repaired 0B in 1 days 02:00:00 with 0 errors on Sun Oct 11 02:00:00 2026",
                                         SCRUB_LINE), ONLINE)[0], STORAGE_NO_HEAVY)

    def test_resilver_on_online_pool_blocks_heavy_only(self):
        st = edit(HEALTHY, "  scan: scrub repaired 0B in 1 days 02:00:00 with 0 errors on Sun Oct 11 02:00:00 2026",
                  "  scan: resilver in progress since Thu Oct 29 10:00:00 2026")
        self.assertEqual(self.level(st, ONLINE)[0], STORAGE_NO_HEAVY)

    def test_resilver_into_a_vdev_with_no_redundancy_blocks(self):
        # raidz2-1: dev12 FAULTED and a second member being replaced -> the replacement
        # does not count until it has resilvered, so nothing is left
        st = edit(TODAY, TODAY_SCAN, "  scan: resilver in progress since Thu Oct 29 10:00:00 2026")
        st = edit(st, "\t    dev11   ONLINE       0     0     0\n",
                  "\t    replacing-1  DEGRADED     0     0     0\n"
                  "\t      dev11   FAULTED      9     0     0  too many errors\n"
                  "\t      dev25   ONLINE       0     0     0  (resilvering)\n")
        level, msg = self.level(st)
        self.assertEqual(level, STORAGE_BLOCKED)
        self.assertIn("raidz2-1 has no redundancy left", msg)

    def test_planned_replace_of_healthy_disk_keeps_redundancy(self):
        st = edit(TODAY, TODAY_SCAN, "  scan: resilver in progress since Thu Oct 29 10:00:00 2026")
        st = edit(st, "\t    dev11   ONLINE       0     0     0\n",
                  "\t    replacing-1  ONLINE       0     0     0\n"
                  "\t      dev11   ONLINE       0     0     0\n"
                  "\t      dev25   ONLINE       0     0     0  (resilvering)\n")
        self.assertEqual(self.level(st)[0], STORAGE_NO_HEAVY)

    def test_finished_replace_after_resilver_is_open(self):
        # spare-N left in place after the resilver: available while one child holds the data
        st = edit(TODAY, "\t    dev11   ONLINE       0     0     0\n",
                  "\t    spare-1  DEGRADED     0     0     0\n"
                  "\t      dev11   FAULTED      9     0     0  too many errors\n"
                  "\t      dev30   ONLINE       0     0     0\n")
        self.assertEqual(self.level(st)[0], STORAGE_OPEN)


if __name__ == "__main__":
    unittest.main()
