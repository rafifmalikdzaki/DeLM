"""Git-worktree peer spawning for local multi-agent DeLM sessions.

Full Docker isolation is heavy for local runs. This module spawns each peer
agent into its own git worktree under ``<repo>/.delm/worktrees/<actor>`` on a
dedicated branch ``delm/<session>/<actor>``. Peers share the object database
(cheap) but have fully isolated working files. The shared board SQLite file
lives outside the worktrees so all peers coordinate through it.

Standard library only.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List

NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
WORKTREES_DIR = Path(".delm/worktrees")


def check_name(kind: str, value: str) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"invalid {kind} name {value!r}: use 1-64 letters, digits, '.', '_' or '-'"
        )
    return value


def run_git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"git failed: {error}") from None
    if proc.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def is_git_repo(path: Path) -> bool:
    try:
        run_git(path, "rev-parse", "--git-dir")
        return True
    except ValueError:
        return False


def repo_root(path: Path) -> Path:
    return Path(run_git(path, "rev-parse", "--show-toplevel").strip())


def branch_name(session: str, actor: str) -> str:
    return f"delm/{check_name('session', session)}/{check_name('actor', actor)}"


def worktree_path(repo: Path, actor: str) -> Path:
    return repo / WORKTREES_DIR / check_name("actor", actor)


def spawn_peer(repo: Path, session: str, actor: str, base: str = "HEAD") -> Dict[str, str]:
    """Create an isolated worktree + branch for one peer agent."""
    repo = repo_root(repo)
    branch = branch_name(session, actor)
    target = worktree_path(repo, actor)
    if target.exists():
        raise ValueError(f"worktree already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = run_git(repo, "branch", "--list", branch).strip()
    if existing:
        run_git(repo, "worktree", "add", str(target), branch)
    else:
        run_git(repo, "worktree", "add", "-b", branch, str(target), base)
    return {"actor": actor, "branch": branch, "path": str(target)}


def remove_peer(repo: Path, session: str, actor: str,
                delete_branch: bool = True) -> Dict[str, str]:
    """Remove a peer worktree (and its branch unless it has unique commits)."""
    repo = repo_root(repo)
    branch = branch_name(session, actor)
    target = worktree_path(repo, actor)
    removed = False
    if target.exists():
        run_git(repo, "worktree", "remove", "--force", str(target))
        removed = True
    run_git(repo, "worktree", "prune")
    deleted_branch = False
    if delete_branch and run_git(repo, "branch", "--list", branch).strip():
        merged = run_git(repo, "branch", "--merged", "HEAD").strip().splitlines()
        if any(line.strip().lstrip("* ") == branch for line in merged):
            run_git(repo, "branch", "-d", branch)
            deleted_branch = True
    return {"actor": actor, "branch": branch, "removed": removed,
            "deleted_branch": deleted_branch}


def list_peers(repo: Path) -> List[Dict[str, str]]:
    """Peer worktrees currently registered under .delm/worktrees/."""
    repo = repo_root(repo)
    out = run_git(repo, "worktree", "list", "--porcelain")
    entries: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):]}
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line[len("branch refs/heads/"):]
        elif line == "" and current:
            entries.append(current)
            current = {}
    if current:
        entries.append(current)
    prefix = str(repo / WORKTREES_DIR)
    return [e for e in entries if e.get("path", "").startswith(prefix)]
