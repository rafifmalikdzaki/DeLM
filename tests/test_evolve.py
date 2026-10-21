"""Tests for the cross-session lessons harvester (temp boards, no network)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from mcp_server.server import DelmMcpServer
from workers.evolve import harvest, list_lessons, seed_block


class TestEvolve(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.board = self.temp_dir / "board.sqlite"
        self.knowledge = self.temp_dir / "knowledge.db"
        self.agent = DelmMcpServer(actor="a", board_path=self.board,
                                   workspace_path=self.temp_dir, peers=["a"])
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def _note(self, kind="FACT", text="Use WAL mode", scope="db",
              basis="measured 3x"):
        res = self.agent.call_tool("delm_publish_note", {
            "note_type": kind, "text": text, "scope": scope, "basis": basis})
        self.assertFalse(res["isError"], res)

    def test_harvest_and_seed_roundtrip(self):
        self._note()
        self._note(kind="FAIL", text="Flag X OOMs the GPU",
                   scope="scripts/run.sh", basis="crashed twice")
        result = harvest(self.board, self.knowledge, source="run-1")
        self.assertEqual(result, {"notes_seen": 2, "added": 2,
                                  "skipped_duplicates": 0})
        seed = seed_block(self.knowledge)
        self.assertIn("Use WAL mode", seed)
        self.assertIn("OOMs the GPU", seed)
        self.assertIn("Team memory", seed)

    def test_harvest_is_idempotent(self):
        self._note()
        harvest(self.board, self.knowledge)
        second = harvest(self.board, self.knowledge)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["skipped_duplicates"], 1)
        self.assertEqual(len(list_lessons(self.knowledge)), 1)

    def test_empty_board_seeds_placeholder(self):
        harvest(self.board, self.knowledge)
        self.assertIn("No accumulated team lessons", seed_block(self.knowledge))


if __name__ == "__main__":
    unittest.main()
