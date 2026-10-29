"""Checks on the real CODEOWNERS file.

Run: python3 -m unittest discover -s ci/automerge -v
"""

import fnmatch
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TEXT = (REPO / "CODEOWNERS").read_text()
RULES = policy.parse_codeowners(TEXT)

# Paths that are never deployed and may change without code-owner review.
UNOWNED = ["docs/**", "README.md", "CLAUDE.md", "DISASTER_RECOVERY.md", ".gitignore"]
# .git is a file in a worktree checkout.
SKIP_DIRS = {".git", ".claude", "__pycache__", "mpool"}


def owners(path):
    """Owners of the last matching rule (GitLab semantics), or None."""
    found = None
    for pat, own in RULES:
        if policy.codeowners_match(pat, path):
            found = own
    return found


def repo_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in (f for f in files if f not in SKIP_DIRS):
            yield str((Path(root) / name).relative_to(REPO))


class RealCodeownersTests(unittest.TestCase):
    def test_every_deployed_path_is_owned(self):
        missing = [p for p in repo_files()
                   if not any(fnmatch.fnmatchcase(p, u) or
                              (u.endswith("/**") and p.startswith(u[:-2])) for u in UNOWNED)
                   and not owners(p)]
        self.assertEqual(missing, [])

    def test_owned_paths_require_root(self):
        for p in ["docker/pihole/x.conf", "docker/homeassistant/packages/a.yaml",
                  "networking/dns/x", "scripts/bootstrap.sh", "base/nut/x", "ci/automerge/policy.py"]:
            self.assertIn("@root", owners(p) or [], p)

    def test_compose_keeps_automerge_bot(self):
        # scripts/approve-root-mr.sh and the sweep approve compose MRs as the
        # automerge bot; the last rule matching compose must still list it.
        own = owners("docker/docker-compose.yml")
        self.assertIn("@root", own)
        self.assertTrue(any(o.startswith("@project_4_bot_") for o in own), own)

    def test_docs_unowned(self):
        self.assertIsNone(owners("docs/AUTOMERGE.md"))


if __name__ == "__main__":
    unittest.main()
