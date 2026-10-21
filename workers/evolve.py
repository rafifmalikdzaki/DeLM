"""Cross-session lessons: harvest board knowledge, seed the next session.

When a multi-agent run finishes, the board is archived and its lessons
(conventions, warnings) would be lost. This tool harvests FACT/FAIL notes
into a persistent ``knowledge.db`` SQLite store and prints a seed block the
next session pastes into its board context.

Standard library only. Never touches the board's live tables beyond reading.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from board.protected_board import ProtectedBoard

SCHEMA = """CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    basis TEXT NOT NULL,
    source TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE
)"""


def fingerprint(kind: str, scope: str, text: str) -> str:
    return hashlib.sha256(
        f"{kind}\x00{scope.strip().lower()}\x00{text.strip()}".encode("utf-8")
    ).hexdigest()


def open_knowledge(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.execute(SCHEMA)
    db.commit()
    return db


def harvest(board_path: Path, knowledge_path: Path,
            source: Optional[str] = None) -> Dict[str, Any]:
    """Copy new FACT/FAIL notes from a board into the knowledge store."""
    board = ProtectedBoard(str(board_path), [], original=lambda path: None)
    with board.connect() as db:
        view = board.view(db, "evolve")
    notes = [n for n in view.get("notes", [])
             if n.get("type") in ("FACT", "FAIL")]
    source = source or str(board_path)
    added, skipped = 0, 0
    db = open_knowledge(knowledge_path)
    try:
        for note in notes:
            fp = fingerprint(note.get("type", ""), note.get("scope", ""),
                             note.get("text", ""))
            try:
                db.execute(
                    "INSERT INTO lessons(kind, scope, text, basis, source, "
                    "created_ns, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (note.get("type"), note.get("scope"), note.get("text"),
                     note.get("basis"), source, time.monotonic_ns(), fp),
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        db.commit()
    finally:
        db.close()
    return {"notes_seen": len(notes), "added": added, "skipped_duplicates": skipped}


def seed_block(knowledge_path: Path, limit: int = 20) -> str:
    """Render accumulated lessons as markdown for the next session."""
    db = open_knowledge(knowledge_path)
    try:
        rows = db.execute(
            "SELECT kind, scope, text, basis FROM lessons "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        db.close()
    if not rows:
        return "(No accumulated team lessons yet.)"
    lines = ["## Team memory (from past DeLM sessions)"]
    for kind, scope, text, basis in reversed(rows):
        icon = "💡" if kind == "FACT" else "⚠️"
        lines.append(f"- {icon} **[{kind}] {scope}**: {text}")
        if basis:
            lines.append(f"  *Basis:* {basis}")
    return "\n".join(lines)


def list_lessons(knowledge_path: Path) -> List[Dict[str, Any]]:
    db = open_knowledge(knowledge_path)
    try:
        return [dict(zip(["kind", "scope", "text", "basis", "source"], row))
                for row in db.execute(
                    "SELECT kind, scope, text, basis, source FROM lessons "
                    "ORDER BY id")]
    finally:
        db.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default=".delm/board.sqlite")
    parser.add_argument("--knowledge", default=".delm/knowledge.db")
    parser.add_argument("--harvest", action="store_true",
                        help="Copy new FACT/FAIL notes into the knowledge store")
    parser.add_argument("--print-seed", action="store_true",
                        help="Print the seed block for the next session")
    args = parser.parse_args(argv)
    if not (args.harvest or args.print_seed):
        parser.error("pass --harvest and/or --print-seed")
    if args.harvest:
        result = harvest(Path(args.board), Path(args.knowledge))
        print(f"Harvested {result['added']} new lessons "
              f"({result['skipped_duplicates']} duplicates skipped, "
              f"{result['notes_seen']} notes seen).")
    if args.print_seed:
        print(seed_block(Path(args.knowledge)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
