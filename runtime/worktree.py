"""Git-worktree peer spawning for local multi-agent DeLM sessions.

Full Docker isolation is heavy for local runs. This module spawns each peer
agent into its own **detached** git worktree under
``<repo>/.delm/worktrees/<actor>``. Detached mode registers zero branches, so
aborted or finished sessions never linger in ``git branch`` output; removing
the folder leaves git state completely clean. Peers share the object database
(cheap) but have fully isolated working files. The shared board SQLite file
lives outside the worktrees so all peers coordinate through it.

Standard library only.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

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


def worktree_path(repo: Path, actor: str) -> Path:
    return repo / WORKTREES_DIR / check_name("actor", actor)


def spawn_peer(repo: Path, session: str, actor: str, base: str = "HEAD") -> Dict[str, Any]:
    """Create a detached, zero-branch worktree sandbox for one peer agent."""
    repo = repo_root(repo)
    check_name("session", session)
    target = worktree_path(repo, actor)
    if target.exists():
        raise ValueError(f"worktree already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    run_git(repo, "worktree", "add", "--detach", str(target), base)
    return {"actor": actor, "branch": "", "detached": True, "path": str(target)}


def remove_peer(repo: Path, session: str, actor: str) -> Dict[str, Any]:
    """Remove a peer worktree. Nothing else to clean: no branch was created."""
    repo = repo_root(repo)
    check_name("session", session)
    target = worktree_path(repo, actor)
    removed = False
    if target.exists():
        run_git(repo, "worktree", "remove", "--force", str(target))
        removed = True
    run_git(repo, "worktree", "prune")
    return {"actor": actor, "removed": removed}


def list_peers(repo: Path) -> List[Dict[str, Any]]:
    """Peer worktrees currently registered under .delm/worktrees/."""
    repo = repo_root(repo)
    out = run_git(repo, "worktree", "list", "--porcelain")
    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):]}
        elif line == "detached" and current:
            current["detached"] = True
        elif line == "" and current:
            entries.append(current)
            current = {}
    if current:
        entries.append(current)
    prefix = str(repo / WORKTREES_DIR)
    return [e for e in entries if e.get("path", "").startswith(prefix)]
