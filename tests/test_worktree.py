"""Tests for git-worktree peer spawning (uses temp repos, no network)."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from runtime.worktree import (
    is_git_repo,
    list_peers,
    remove_peer,
    repo_root,
    spawn_peer,
)


def init_repo() -> Path:
    tmp = Path(tempfile.mkdtemp())
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"],
                 ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(tmp), *args], check=True,
                       capture_output=True)
    (tmp / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(tmp), "add", "."], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "init"],
                   check=True, capture_output=True)
    return tmp


class TestWorktree(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.addCleanup(shutil.rmtree, self.repo, True)

    def test_spawn_isolates_peers(self):
        a = spawn_peer(self.repo, "s1", "alpha")
        b = spawn_peer(self.repo, "s1", "beta")
        self.assertTrue(a["detached"])
        (Path(a["path"]) / "a.txt").write_text("alpha work\n")
        (Path(b["path"]) / "b.txt").write_text("beta work\n")
        self.assertFalse((Path(a["path"]) / "b.txt").exists())
        self.assertFalse((Path(b["path"]) / "a.txt").exists())
        peers = list_peers(self.repo)
        self.assertEqual({p["path"] for p in peers}, {a["path"], b["path"]})
        self.assertTrue(all(p.get("detached") for p in peers))

    def test_no_branches_ever_registered(self):
        branches_before = self._branches()
        a = spawn_peer(self.repo, "s1", "alpha")
        self.assertEqual(self._branches(), branches_before)
        remove_peer(self.repo, "s1", "alpha")
        self.assertEqual(self._branches(), branches_before)
        self.assertFalse(Path(a["path"]).exists())

    def _branches(self):
        out = subprocess.run(
            ["git", "-C", str(self.repo), "branch", "--list"],
            capture_output=True, text=True, check=True)
        return sorted(line.strip().lstrip("* ") for line in out.stdout.splitlines()
                      if line.strip())

    def test_spawn_twice_is_refused(self):
        spawn_peer(self.repo, "s1", "alpha")
        with self.assertRaises(ValueError):
            spawn_peer(self.repo, "s1", "alpha")

    def test_remove_cleans_up(self):
        spawn_peer(self.repo, "s1", "alpha")
        out = remove_peer(self.repo, "s1", "alpha")
        self.assertTrue(out["removed"])
        self.assertEqual(list_peers(self.repo), [])

    def test_remove_missing_is_noop(self):
        out = remove_peer(self.repo, "s1", "ghost")
        self.assertFalse(out["removed"])

    def test_bad_names_rejected(self):
        with self.assertRaises(ValueError):
            spawn_peer(self.repo, "s1", "bad actor!")
        with self.assertRaises(ValueError):
            spawn_peer(self.repo, "../evil", "alpha")

    def test_non_repo_detected(self):
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plain, True)
        self.assertTrue(is_git_repo(self.repo))
        self.assertFalse(is_git_repo(plain))
        self.assertEqual(repo_root(self.repo), self.repo)


if __name__ == "__main__":
    unittest.main()
