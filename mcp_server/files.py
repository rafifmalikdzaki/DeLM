"""Workspace file handling for the DeLM MCP server (standard library only).

These helpers port the safety rules of the Docker harness client
(:mod:`board.context_client`) to MCP mode, where each agent attaches files
from its own workspace and applies files published by peers:

* paths stay inside the agent workspace (or under the scratch root),
* ``..`` segments, symlinks and credential/cache directories are rejected,
* per-file and total bundle size limits are enforced before anything is sent,
* peer files are written atomically (stage + rename) with rollback, so a
  failed apply never leaves a half-written workspace.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

from board.shared_context import BoardError, FILE_LIMITS, SCRATCH_ROOT

SKIP_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    ".cache", ".pytestcache", ".codex", ".ssh", ".aws", ".config",
    ".claude", "records",
})
CREDENTIAL_NAMES = frozenset({
    "auth.json", "credentials.json", "id_rsa", "id_ed25519",
    "grading.log", "reward.txt", "reward.json",
})


def is_excluded_board_path(name: str) -> bool:
    """True when a board-relative path must never be published or written."""
    parts = Path(name).parts
    basename = Path(name).name.lower()
    return (
        bool(set(parts) & SKIP_DIRS)
        or basename in CREDENTIAL_NAMES
        or basename == ".env"
        or basename.startswith(".env.")
        or name.startswith(("/tests/", "/solution/", "/records/"))
    )


def locate(path: str, root: Path) -> Tuple[str, Path]:
    """Map a user-supplied path to (board name, absolute location).

    Relative paths resolve under the agent workspace; absolute paths are
    accepted only under the scratch root. ``..`` segments are rejected and
    parent directories are resolved so symlinks cannot escape the workspace.
    """
    if not isinstance(path, str) or not path or path != path.strip():
        raise BoardError("every attached file needs a path")
    given = Path(path)
    if ".." in given.parts:
        raise BoardError(f"{path}: path must not contain '..'")
    root = root.resolve()
    absolute = given if given.is_absolute() else root / given
    absolute = absolute.parent.resolve() / absolute.name
    try:
        return absolute.relative_to(root).as_posix(), absolute
    except ValueError:
        pass
    try:
        scratch = Path(SCRATCH_ROOT).resolve()
        return f"{SCRATCH_ROOT}/{absolute.relative_to(scratch).as_posix()}", absolute
    except ValueError:
        raise BoardError(
            f"{path}: only files in your workspace ({root}) or under "
            f"{SCRATCH_ROOT} can be attached"
        ) from None


def bundle_files(paths: List[str], root: Path) -> List[Dict]:
    """Read workspace files into board attach payloads, enforcing all limits."""
    if len(paths) > FILE_LIMITS["files"]:
        raise BoardError(
            f"{len(paths)} files given; at most {FILE_LIMITS['files']} can be attached"
        )
    files, total, seen = [], 0, set()
    for path in paths:
        name, absolute = locate(path, root)
        if name in seen:
            raise BoardError(f"{path}: given twice")
        seen.add(name)
        if is_excluded_board_path(name):
            raise BoardError(
                f"{path}: credentials, dependency/cache directories and "
                "grading records cannot be published"
            )
        if absolute.is_symlink() or not absolute.is_file():
            raise BoardError(
                f"{path}: not a regular file\nResolved path: {absolute}\n"
                f"Relative attachment paths use workspace root: {root.resolve()}"
            )
        raw = absolute.read_bytes()
        if len(raw) > FILE_LIMITS["file_bytes"]:
            raise BoardError(
                f"{path} is {len(raw):,} bytes; at most "
                f"{FILE_LIMITS['file_bytes']:,} per file"
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BoardError(
                f"{path}: not UTF-8 text; only text files can be attached"
            ) from None
        total += len(raw)
        files.append(dict(
            path=name,
            content=content,
            mode="x" if absolute.stat().st_mode & 0o111 else "-",
        ))
    if total > FILE_LIMITS["total_bytes"]:
        raise BoardError(
            f"attached files total {total:,} bytes; at most "
            f"{FILE_LIMITS['total_bytes']:,} allowed"
        )
    return files


def local_hashes(names: List[str], root: Path) -> Dict[str, str | None]:
    """sha256 of the caller's current copy of each board path (None if absent)."""
    hashes: Dict[str, str | None] = {}
    for name in names:
        _, absolute = locate(name, root)
        hashes[name] = (
            hashlib.sha256(absolute.read_bytes()).hexdigest()
            if absolute.is_file() and not absolute.is_symlink()
            else None
        )
    return hashes


def atomic_write_files(files: List[Dict], root: Path) -> List[str]:
    """Write board-granted files into the workspace atomically.

    Each file is staged to a temporary sibling and renamed over the target,
    preserving the executable bit convention. On failure, already-renamed
    files are rolled back to their backups and a BoardError is raised, so
    the workspace is never left half-written.
    """
    written: List[str] = []
    prepared, temporary_paths = [], []

    def temporary(parent: Path) -> Path:
        fd, name = tempfile.mkstemp(prefix=".delm-apply-", dir=parent)
        os.close(fd)
        path = Path(name)
        temporary_paths.append(path)
        return path

    try:
        for entry in files:
            _, absolute = locate(entry["path"], root)
            if is_excluded_board_path(entry["path"]):
                raise BoardError(
                    f"{entry['path']}: peer published a protected path; "
                    "refusing to write it"
                )
            if absolute.exists() and not absolute.is_file():
                raise BoardError(
                    f"{entry['path']}: exists and is not a regular file"
                )
            absolute.parent.mkdir(parents=True, exist_ok=True)
            backup = temporary(absolute.parent) if absolute.exists() else None
            if backup is not None:
                shutil.copy2(absolute, backup, follow_symlinks=False)
            staged = temporary(absolute.parent)
            staged.write_text(entry["content"], encoding="utf-8")
            mode = absolute.stat().st_mode if backup is not None else 0o644
            if entry.get("mode") == "x":
                staged.chmod(mode | 0o111)
            else:
                staged.chmod(mode & ~0o111)
            prepared.append((entry["path"], absolute, staged, backup))
        for name, absolute, staged, _ in prepared:
            os.replace(staged, absolute)
            written.append(name)
    except (OSError, BoardError) as error:
        failures = []
        for name, absolute, _, backup in reversed(prepared):
            if name not in written:
                continue
            try:
                if backup is not None:
                    os.replace(backup, absolute)
                else:
                    absolute.unlink()
                written.remove(name)
            except OSError as rollback_error:
                if backup is not None and backup in temporary_paths:
                    temporary_paths.remove(backup)
                failures.append(f"{name}: {rollback_error}; backup: {backup}")
        if failures:
            raise BoardError(
                f"{error}; rollback failed: {'; '.join(failures)}"
            ) from None
        if isinstance(error, BoardError):
            raise
        raise BoardError(f"failed to write peer files: {error}") from None
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
    return written
