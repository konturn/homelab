"""Tests for revert_guard.py against throwaway git repositories (fake data).

Run: python3 -m unittest discover -s ci/revert-guard -p 'test_*.py' -v
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import revert_guard as rg  # noqa: E402

DAY = 86400
T0 = 1_800_000_000  # fixed clock for the fake history


class Repo:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="revert-guard-test-")
        self.clock = T0
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def git(self, *args):
        env = dict(os.environ,
                   GIT_AUTHOR_DATE=f"@{self.clock} +0000",
                   GIT_COMMITTER_DATE=f"@{self.clock} +0000")
        return subprocess.run(["git", *args], cwd=self.dir, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def tick(self, seconds=60):
        self.clock += seconds

    def write(self, path, text):
        full = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def read(self, path):
        with open(os.path.join(self.dir, path)) as f:
            return f.read()

    def commit(self, msg, **files):
        for path, text in files.items():
            self.write(path.replace("__", "/"), text)
        self.git("add", "-A")
        self.tick()
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def branch(self, name, start="main"):
        self.git("checkout", "-q", "-b", name, start)

    def merge(self, name, iid, title):
        self.git("checkout", "-q", "main")
        self.tick()
        self.git("merge", "-q", "--no-ff", name, "-m",
                 f"Merge branch '{name}' into 'main'\n\n{title}\n\n"
                 f"See merge request root/homelab!{iid}")

    def mr(self, name, iid, title, **files):
        """Branch from main, commit, merge: a whole MR."""
        self.branch(name)
        self.commit(title, **files)
        self.merge(name, iid, title)


CONFIG = "".join(f"setting_{i}: value_{i}\n" for i in range(20))
TASKS = "".join(f"- name: task number {i}\n  command: run-thing-{i}\n" for i in range(10))


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.r.commit("initial", **{"app/config.yml": CONFIG, "app/tasks.yml": TASKS,
                                    "docs/notes.md": "Some notes about the app.\n"})
        # Push the initial history out of the window.
        self.r.tick(30 * DAY)
        self.addCleanup(self.r.cleanup)

    def analyse(self, head, own_iid="99", days=7):
        return rg.analyse("main", head, days, own_iid, now=self.r.clock + 60, cwd=self.r.dir)

    def run_main(self, head, labels=""):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rg.main(["--target", "main", "--head", head, "--mr-iid", "99",
                            "--labels", labels, "--now", str(self.r.clock + 60),
                            "-C", self.r.dir])
        return code, buf.getvalue()

    def recent_mr_adding_block(self):
        cfg = CONFIG + "".join(f"memory_cgroup_line_{i}: enabled\n" for i in range(5))
        self.r.mr("feature/cgroup", 101, "Enable the memory cgroup",
                  **{"app__config.yml": cfg})
        return cfg

    def test_stale_tree_revert_fails(self):
        self.recent_mr_adding_block()
        # MR 99 is rebased on main, but its commit carries the old config.yml
        # (stale working tree) next to its real change.
        self.r.branch("feature/mine")
        self.r.commit("my change", **{"app__config.yml": CONFIG,
                                     "docs__notes.md": "Some notes about the app.\nMore.\n"})
        findings, _ = self.analyse("feature/mine")
        blocking = [f for f in findings if f.blocking]
        self.assertEqual([(f.path, f.mr.iid, f.kind) for f in blocking],
                         [("app/config.yml", "101", "reverts")])
        self.assertEqual(len(blocking[0].lines), 5)
        code, out = self.run_main("feature/mine")
        self.assertEqual(code, 1)
        self.assertIn("app/config.yml: removes lines added by !101", out)
        self.assertIn("Enable the memory cgroup", out)
        self.assertIn("lines (at merge base): 21-25", out)

    def test_override_label(self):
        self.recent_mr_adding_block()
        self.r.branch("feature/mine")
        self.r.commit("revert it on purpose", **{"app__config.yml": CONFIG})
        code, out = self.run_main("feature/mine", labels="docs,revert-intended")
        self.assertEqual(code, 0)
        self.assertIn("label present", out)

    def test_restoring_deleted_lines_fails(self):
        trimmed = "".join(ln + "\n" for ln in TASKS.splitlines()[:-4])
        self.r.mr("chore/drop-tasks", 102, "Drop two tasks", **{"app__tasks.yml": trimmed})
        self.r.branch("feature/mine")
        self.r.commit("stale", **{"app__tasks.yml": TASKS,
                                 "docs__notes.md": "Other notes.\n"})
        findings, _ = self.analyse("feature/mine")
        self.assertEqual([(f.path, f.mr.iid, f.kind, f.blocking) for f in findings],
                         [("app/tasks.yml", "102", "restores", True)])

    def test_inverse_replacement_fails(self):
        self.r.mr("fix/mode", 103, "Unit files 0644",
                  **{"app__config.yml": CONFIG.replace("value_3", "mode_0644")})
        self.r.branch("feature/mine")
        self.r.commit("stale", **{"app__config.yml": CONFIG, "docs__notes.md": "x y z\n"})
        findings, _ = self.analyse("feature/mine")
        kinds = sorted((f.kind, f.blocking) for f in findings if f.mr.iid == "103")
        self.assertEqual(kinds, [("restores", True), ("reverts", True)])

    def test_follow_up_edit_is_not_flagged(self):
        cfg = self.recent_mr_adding_block()
        self.r.branch("feature/tune")
        self.r.commit("tune one value",
                      **{"app__config.yml": cfg.replace("memory_cgroup_line_2: enabled",
                                                        "memory_cgroup_line_2: disabled")})
        findings, _ = self.analyse("feature/tune")
        self.assertEqual(findings, [])
        self.assertEqual(self.run_main("feature/tune")[0], 0)

    def test_partial_removal_is_a_note(self):
        cfg = self.recent_mr_adding_block()
        self.r.branch("feature/trim")
        drop = "".join(f"memory_cgroup_line_{i}: enabled\n" for i in (3, 4))
        self.r.commit("drop two of the new lines", **{"app__config.yml": cfg.replace(drop, "")})
        findings, _ = self.analyse("feature/trim")
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0].blocking)
        self.assertAlmostEqual(findings[0].coverage, 0.4)
        code, out = self.run_main("feature/trim")
        self.assertEqual(code, 0)
        self.assertIn("Note (not failing)", out)

    def test_revert_mixed_with_own_edits_is_a_note(self):
        cfg = self.recent_mr_adding_block()
        self.r.branch("feature/rewrite")
        self.r.commit("rewrite config",
                      **{"app__config.yml": CONFIG.replace("setting_0: value_0",
                                                           "setting_0: something_new")})
        findings, _ = self.analyse("feature/rewrite")
        self.assertTrue(findings)
        self.assertFalse(any(f.blocking for f in findings))

    def test_moved_block_is_not_flagged(self):
        cfg = self.recent_mr_adding_block()
        block = "".join(f"memory_cgroup_line_{i}: enabled\n" for i in range(5))
        moved = block + cfg.replace(block, "")
        self.r.branch("feature/move")
        self.r.commit("move the block to the top", **{"app__config.yml": moved})
        findings, _ = self.analyse("feature/move")
        self.assertEqual(findings, [])

    def test_old_changes_are_out_of_window(self):
        self.recent_mr_adding_block()
        self.r.tick(8 * DAY)
        self.r.branch("feature/mine")
        self.r.commit("drop it, a week later", **{"app__config.yml": CONFIG})
        findings, _ = self.analyse("feature/mine", days=7)
        self.assertEqual(findings, [])
        findings, _ = self.analyse("feature/mine", days=30)
        self.assertEqual(len(findings), 1)

    def test_own_mr_is_excluded(self):
        self.recent_mr_adding_block()
        self.r.branch("feature/mine")
        self.r.commit("undo my own earlier MR", **{"app__config.yml": CONFIG})
        findings, _ = self.analyse("feature/mine", own_iid="101")
        self.assertEqual(findings, [])

    def test_behind_branch_is_not_a_revert(self):
        # Branch from the old main, then another MR lands: a plain merge keeps
        # main's side, so only the two-dot note fires.
        self.r.branch("feature/old")
        self.r.commit("docs only", **{"docs__notes.md": "Rewritten notes.\n"})
        self.r.git("checkout", "-q", "main")
        self.recent_mr_adding_block()
        findings, untouched = self.analyse("feature/old")
        self.assertEqual(findings, [])
        self.assertEqual(untouched, ["app/config.yml"])
        code, out = self.run_main("feature/old")
        self.assertEqual(code, 0)
        self.assertIn("none of this MR's commits touch", out)
        self.assertIn("  app/config.yml", out)

    def test_direct_push_is_attributed(self):
        self.r.tick()
        self.r.commit("hotfix on main",
                      **{"app__config.yml": CONFIG + "hotfix_line_one: yes\nhotfix_line_two: yes\n"})
        self.r.branch("feature/mine")
        self.r.commit("stale", **{"app__config.yml": CONFIG, "docs__notes.md": "n\n"})
        findings, _ = self.analyse("feature/mine")
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].mr.iid.startswith("direct:"))
        self.assertTrue(findings[0].blocking)

    def test_deleted_file_revert(self):
        self.r.mr("feature/new-file", 104, "Add a role",
                  **{"roles__new__main.yml": "- name: brand new task\n  shell: do-the-thing\n"})
        self.r.branch("feature/mine")
        self.r.git("rm", "-q", "roles/new/main.yml")
        self.r.commit("stale", **{"docs__notes.md": "n\n"})
        findings, _ = self.analyse("feature/mine")
        self.assertEqual([(f.path, f.mr.iid, f.blocking) for f in findings],
                         [("roles/new/main.yml", "104", True)])

    def test_trivial_lines_are_ignored(self):
        self.r.mr("feature/braces", 105, "Add braces",
                  **{"app__x.json": "{\n}\n"})
        self.r.branch("feature/mine")
        self.r.commit("stale", **{"app__x.json": "\n"})
        findings, _ = self.analyse("feature/mine")
        self.assertEqual(findings, [])


class ParseTest(unittest.TestCase):
    def test_parse_diff(self):
        text = ("diff --git a/x b/y\nsimilarity index 90%\nrename from x\nrename to y\n"
                "--- a/x\n+++ b/y\n@@ -3,2 +3 @@ ctx\n-old one\n-old two\n+new\n"
                "diff --git a/z b/z\nnew file mode 100644\n--- /dev/null\n+++ b/z\n"
                "@@ -0,0 +1 @@\n+--- not a header\n")
        h = rg.parse_diff(text)
        self.assertEqual((h[0].old_path, h[0].new_path, h[0].old_start), ("x", "y", 3))
        self.assertEqual(h[0].removed, ["old one", "old two"])
        self.assertEqual(h[0].added, ["new"])
        self.assertEqual((h[1].old_path, h[1].new_path), (None, "z"))
        self.assertEqual(h[1].added, ["--- not a header"])

    def test_ranges(self):
        self.assertEqual(rg._ranges([5, 1, 2, 3, 9, 10]), "1-3, 5, 9-10")

    def test_mr_ref(self):
        self.assertEqual(rg.MR_RE.search("x\n\nSee merge request root/homelab!1048").group(1),
                         "1048")


if __name__ == "__main__":
    unittest.main()
