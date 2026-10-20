"""Integration and protocol tests for the DeLM MCP Server."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from mcp_server.server import DelmMcpServer


class TestDelmMcpServer(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_1 = Path(self.temp_dir) / "workspace_1"
        self.workspace_2 = Path(self.temp_dir) / "workspace_2"
        self.workspace_1.mkdir()
        self.workspace_2.mkdir()
        self.board_path = Path(self.temp_dir) / "shared_board.sqlite"

        # Initialize two servers for two cooperating agents sharing the same board
        self.agent_a = DelmMcpServer(
            actor="agent-alpha",
            board_path=self.board_path,
            workspace_path=self.workspace_1,
            peers=["agent-alpha", "agent-beta"]
        )
        self.agent_b = DelmMcpServer(
            actor="agent-beta",
            board_path=self.board_path,
            workspace_path=self.workspace_2,
            peers=["agent-alpha", "agent-beta"]
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mcp_initialize_and_tools_list(self):
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}}
        }
        resp = self.agent_a.handle_request(init_req)
        self.assertEqual(resp["id"], 1)
        self.assertIn("tools", resp["result"]["capabilities"])

        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = self.agent_a.handle_request(list_req)
        tools = resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("delm_read_board", tool_names)
        self.assertIn("delm_reserve_task", tool_names)
        self.assertIn("delm_complete_task", tool_names)
        self.assertIn("delm_publish_note", tool_names)
        self.assertIn("delm_update_status", tool_names)
        self.assertIn("delm_inspect_files", tool_names)
        self.assertIn("delm_apply_files", tool_names)

    def test_multi_agent_coordination_workflow(self):
        # 1. Agent Alpha updates status
        call_req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "delm_update_status",
                "arguments": {"text": "Exploring database migrations"}
            }
        }
        resp = self.agent_a.handle_request(call_req)
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("Exploring database migrations", resp["result"]["content"][0]["text"])

        # 2. Agent Alpha reserves a task
        call_req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "delm_reserve_task",
                "arguments": {
                    "subject": "Implement users table schema",
                    "detail": "Define user id, email, and created_at columns"
                }
            }
        }
        resp = self.agent_a.handle_request(call_req)
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("Won claim", resp["result"]["content"][0]["text"])

        # 3. Agent Beta reads the board and sees Alpha's claim
        read_req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "delm_read_board",
                "arguments": {"full": True}
            }
        }
        resp = self.agent_b.handle_request(read_req)
        board_text = resp["result"]["content"][0]["text"]
        self.assertIn("Implement users table schema", board_text)
        self.assertIn("CLAIMED by agent-alpha", board_text)

        # 4. Agent Alpha publishes a FACT note with reproducible evidence
        note_req = {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "delm_publish_note",
                "arguments": {
                    "note_type": "FACT",
                    "text": "SQLite WAL mode reduces lock contention by 3x",
                    "scope": "db/config",
                    "basis": "Executed 100 concurrent transactions benchmark"
                }
            }
        }
        resp = self.agent_a.handle_request(note_req)
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("Published FACT", resp["result"]["content"][0]["text"])

        # 5. Agent Alpha completes the task and publishes a new file
        test_file = self.workspace_1 / "schema.sql"
        test_file.write_text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL);\n")

        # Extract item ID from Alpha's claimed task
        view = self.agent_a.sc.read("agent-alpha", full=True)
        item_id = view["queue"][0]["id"]

        done_req = {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "delm_complete_task",
                "arguments": {
                    "item_id": item_id,
                    "summary": "Completed schema.sql and added migration",
                    "attach_paths": ["schema.sql"]
                }
            }
        }
        resp = self.agent_a.handle_request(done_req)
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("Completed task", resp["result"]["content"][0]["text"])

        # 6. Agent Beta applies the file published by Alpha into its own workspace
        # First read the board to get the publication ID
        b_view = self.agent_b.sc.read("agent-beta", full=True)
        pub_id = b_view["queue"][0]["files"][0]["path"]  # verify files attached
        # The publication ID is associated with the done item
        pub_id = b_view["queue"][0]["publication_id"]

        apply_req = {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {
                "name": "delm_apply_files",
                "arguments": {
                    "publication_id": pub_id
                }
            }
        }
        resp = self.agent_b.handle_request(apply_req)
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("Successfully applied", resp["result"]["content"][0]["text"])

        # Verify the file was physically written to workspace_2
        copied_file = self.workspace_2 / "schema.sql"
        self.assertTrue(copied_file.is_file())
        self.assertIn("CREATE TABLE users", copied_file.read_text())


if __name__ == "__main__":
    unittest.main()
