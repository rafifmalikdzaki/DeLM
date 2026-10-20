"""Protocol and regression tests for the DeLM MCP server.

Covers the JSON-RPC surface, input validation, the publish/claim flow,
inspect diffs, conflict-safe apply, path safety, concurrent reservation,
and idempotent retries.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp_server.server import DelmMcpServer

ROOT = Path(__file__).resolve().parent.parent


def make_servers(actor_a="agent-alpha", actor_b="agent-beta"):
    temp_dir = Path(tempfile.mkdtemp())
    workspace_1 = temp_dir / "workspace_1"
    workspace_2 = temp_dir / "workspace_2"
    workspace_1.mkdir()
    workspace_2.mkdir()
    board_path = temp_dir / "shared_board.sqlite"
    agent_a = DelmMcpServer(
        actor=actor_a,
        board_path=board_path,
        workspace_path=workspace_1,
        peers=[actor_a, actor_b],
    )
    agent_b = DelmMcpServer(
        actor=actor_b,
        board_path=board_path,
        workspace_path=workspace_2,
        peers=[actor_a, actor_b],
    )
    return temp_dir, workspace_1, workspace_2, agent_a, agent_b


class TestDelmMcpServer(unittest.TestCase):
    def setUp(self):
        self.temp_dir, self.workspace_1, self.workspace_2, self.agent_a, self.agent_b = make_servers()

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
        self.assertIn("delm_publish_items", tool_names)
        self.assertIn("delm_claim_task", tool_names)
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
        b_view = self.agent_b.sc.read("agent-beta", full=True)
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
        self.assertIn("Applied 1 file(s)", resp["result"]["content"][0]["text"])

        # Verify the file was physically written to workspace_2
        copied_file = self.workspace_2 / "schema.sql"
        self.assertTrue(copied_file.is_file())
        self.assertIn("CREATE TABLE users", copied_file.read_text())


class TestProtocolCompliance(unittest.TestCase):
    def setUp(self):
        self.temp_dir, _, _, self.agent_a, _ = make_servers()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def test_initialize_negotiates_client_version(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18"):
            resp = self.agent_a.handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": version, "capabilities": {}}}
            )
            self.assertEqual(resp["result"]["protocolVersion"], version)

    def test_initialize_falls_back_for_unknown_version(self):
        resp = self.agent_a.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "1999-01-01", "capabilities": {}}}
        )
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")

    def test_resources_and_prompts_lists_are_empty(self):
        for method, key in (("resources/list", "resources"), ("prompts/list", "prompts")):
            resp = self.agent_a.handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
            )
            self.assertEqual(resp["result"], {key: []})

    def test_unknown_method_returns_method_not_found(self):
        resp = self.agent_a.handle_request(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/bogus", "params": {}}
        )
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertEqual(resp["id"], 7)

    def test_unknown_tool_returns_invalid_params(self):
        resp = self.agent_a.handle_request(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "delm_nope", "arguments": {}}}
        )
        self.assertEqual(resp["error"]["code"], -32602)

    def test_notification_returns_no_response(self):
        resp = self.agent_a.handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        self.assertIsNone(resp)

    def test_batch_mixed_requests_and_notifications(self):
        resp = self.agent_a.handle_message([
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "bogus/method", "params": {}},
        ])
        self.assertEqual(len(resp), 2)
        self.assertEqual(resp[0]["id"], 1)
        self.assertEqual(resp[1]["error"]["code"], -32601)


class TestToolValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir, _, _, self.agent_a, _ = make_servers()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def call(self, name, args):
        return self.agent_a.call_tool(name, args)

    def test_reserve_rejects_empty_subject(self):
        self.assertTrue(self.call("delm_reserve_task", {"subject": "  ", "detail": "x"})["isError"])

    def test_reserve_rejects_oversized_detail(self):
        self.assertTrue(
            self.call("delm_reserve_task", {"subject": "s", "detail": "x" * 2001})["isError"]
        )

    def test_note_rejects_bad_type(self):
        self.assertTrue(
            self.call("delm_publish_note", {
                "note_type": "MAYBE", "text": "t", "scope": "s", "basis": "b",
            })["isError"]
        )

    def test_status_rejects_oversized_text(self):
        self.assertTrue(self.call("delm_update_status", {"text": "x" * 501})["isError"])

    def test_claim_unknown_item_is_refused(self):
        self.assertTrue(self.call("delm_claim_task", {"item_id": "no-such-item"})["isError"])

    def test_read_negative_wait_returns_immediately(self):
        result = self.call("delm_read_board", {"wait_seconds": -5})
        self.assertFalse(result["isError"])

    def test_unknown_tool_name_is_refused(self):
        self.assertTrue(self.call("delm_nope", {})["isError"])

    def test_invalid_actor_name_rejected(self):
        with self.assertRaises(ValueError):
            DelmMcpServer(
                actor="bad actor!",
                board_path=self.temp_dir / "b.sqlite",
                workspace_path=self.temp_dir,
            )


class TestPublishAndClaimFlow(unittest.TestCase):
    def setUp(self):
        self.temp_dir, _, _, self.agent_a, self.agent_b = make_servers()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def test_publish_items_then_claim(self):
        res = self.agent_a.call_tool("delm_publish_items", {"items": [
            {"subject": "Build parser", "detail": "Write the TOML parser"},
            {"subject": "Build renderer", "detail": "Write the HTML renderer"},
        ]})
        self.assertFalse(res["isError"])
        self.assertIn("Published queue items", res["content"][0]["text"])

        view = self.agent_b.sc.read("agent-beta", full=True)
        open_items = [i for i in view["queue"] if i["state"] == "open"]
        self.assertEqual(len(open_items), 2)

        claim = self.agent_b.call_tool("delm_claim_task", {"item_id": open_items[0]["id"]})
        self.assertFalse(claim["isError"])

        # Second agent cannot claim the same item.
        steal = self.agent_a.call_tool("delm_claim_task", {"item_id": open_items[0]["id"]})
        self.assertTrue(steal["isError"])

    def test_concurrent_reserve_single_winner(self):
        first = self.agent_a.call_tool(
            "delm_reserve_task", {"subject": "Shared Scope", "detail": "do it"})
        second = self.agent_b.call_tool(
            "delm_reserve_task", {"subject": "  shared   scope ", "detail": "do it"})
        self.assertFalse(first["isError"])
        self.assertFalse(second["isError"])  # returns existing ownership, no crash
        self.assertIn("Won claim", first["content"][0]["text"])
        self.assertIn("agent-alpha", second["content"][0]["text"])


class TestInspectAndApply(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.baseline = self.temp_dir / "baseline"
        self.ws_a = self.temp_dir / "ws_a"
        self.ws_b = self.temp_dir / "ws_b"
        for d in (self.baseline, self.ws_a, self.ws_b):
            d.mkdir()
        self.board = self.temp_dir / "board.sqlite"
        self.agent_a = DelmMcpServer(
            actor="aa", board_path=self.board, workspace_path=self.ws_a,
            baseline_path=self.baseline, peers=["aa", "bb"],
        )
        self.agent_b = DelmMcpServer(
            actor="bb", board_path=self.board, workspace_path=self.ws_b,
            peers=["aa", "bb"],
        )
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def _publish(self, filename="f.txt", content="hello\n", attach=True):
        self.agent_a.call_tool("delm_reserve_task", {"subject": "S", "detail": "D"})
        item_id = self.agent_a.sc.read("aa", full=True)["queue"][0]["id"]
        (self.ws_a / filename).write_text(content)
        args = {"item_id": item_id, "summary": "done"}
        if attach:
            args["attach_paths"] = [filename]
        res = self.agent_a.call_tool("delm_complete_task", args)
        self.assertFalse(res["isError"], res)
        view = self.agent_b.sc.read("bb", full=True)
        return view["queue"][0]["publication_id"]

    def test_inspect_shows_baseline_diff(self):
        (self.baseline / "f.txt").write_text("old\n")
        pub_id = self._publish(content="new\n")
        res = self.agent_b.call_tool("delm_inspect_files", {"publication_id": pub_id})
        self.assertFalse(res["isError"], res)
        text = res["content"][0]["text"]
        self.assertIn("as a diff against the baseline", text)
        self.assertIn("-old", text)
        self.assertIn("+new", text)

    def test_apply_refuses_conflict_and_keeps_local_file(self):
        pub_id = self._publish(content="peer version\n")
        (self.ws_b / "f.txt").write_text("my own conflicting edits\n")
        res = self.agent_b.call_tool("delm_apply_files", {"publication_id": pub_id})
        self.assertTrue(res["isError"])
        self.assertIn("CONFLICT", res["content"][0]["text"])
        self.assertIn("Nothing was written", res["content"][0]["text"])
        self.assertEqual((self.ws_b / "f.txt").read_text(), "my own conflicting edits\n")

    def test_apply_clean_then_reapply_is_stable(self):
        pub_id = self._publish(content="peer version\n")
        first = self.agent_b.call_tool("delm_apply_files", {"publication_id": pub_id})
        self.assertFalse(first["isError"], first)
        self.assertEqual((self.ws_b / "f.txt").read_text(), "peer version\n")
        # Re-applying the same publication is a no-op, not an error.
        second = self.agent_b.call_tool("delm_apply_files", {"publication_id": pub_id})
        self.assertFalse(second["isError"], second)

    def test_board_text_carries_usable_ids(self):
        """A peer working only from rendered board text can inspect and apply."""
        self.agent_a.call_tool("delm_reserve_task", {"subject": "Text ids", "detail": "d"})
        item_id = self.agent_a.sc.read("aa", full=True)["queue"][0]["id"]
        (self.ws_a / "doc.txt").write_text("hello\n")
        done = self.agent_a.call_tool("delm_complete_task", {
            "item_id": item_id, "summary": "s", "attach_paths": ["doc.txt"]})
        self.assertFalse(done["isError"], done)
        text = self.agent_b.call_tool(
            "delm_read_board", {"full": True})["content"][0]["text"]
        match = re.search(r"`([0-9a-f]{32})`", text)
        self.assertIsNotNone(match, text)
        found = match.group(1)
        insp = self.agent_b.call_tool("delm_inspect_files", {"publication_id": found})
        self.assertFalse(insp["isError"], insp)
        self.assertIn("doc.txt", insp["content"][0]["text"])
        appl = self.agent_b.call_tool("delm_apply_files", {"publication_id": found})
        self.assertFalse(appl["isError"], appl)
        self.assertEqual((self.ws_b / "doc.txt").read_text(), "hello\n")

    def test_apply_subset_paths(self):
        self.agent_a.call_tool("delm_reserve_task", {"subject": "S2", "detail": "D"})
        item_id = self.agent_a.sc.read("aa", full=True)["queue"][0]["id"]
        (self.ws_a / "one.txt").write_text("one\n")
        (self.ws_a / "two.txt").write_text("two\n")
        res = self.agent_a.call_tool("delm_complete_task", {
            "item_id": item_id, "summary": "two files",
            "attach_paths": ["one.txt", "two.txt"],
        })
        self.assertFalse(res["isError"], res)
        pub_id = self.agent_b.sc.read("bb", full=True)["queue"][0]["publication_id"]
        res = self.agent_b.call_tool(
            "delm_apply_files", {"publication_id": pub_id, "paths": ["one.txt"]})
        self.assertFalse(res["isError"], res)
        self.assertTrue((self.ws_b / "one.txt").is_file())
        self.assertFalse((self.ws_b / "two.txt").exists())


class TestPathSafety(unittest.TestCase):
    def setUp(self):
        self.temp_dir, self.ws_a, _, self.agent_a, _ = make_servers()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def _reserve(self):
        self.agent_a.call_tool("delm_reserve_task", {"subject": "S", "detail": "D"})
        return self.agent_a.sc.read("agent-alpha", full=True)["queue"][0]["id"]

    def test_attach_rejects_parent_traversal(self):
        item_id = self._reserve()
        res = self.agent_a.call_tool("delm_complete_task", {
            "item_id": item_id, "summary": "x", "attach_paths": ["../evil.txt"]})
        self.assertTrue(res["isError"])

    def test_attach_rejects_symlink_escape(self):
        outside = self.temp_dir / "secret.txt"
        outside.write_text("secret")
        link = self.ws_a / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        item_id = self._reserve()
        res = self.agent_a.call_tool("delm_complete_task", {
            "item_id": item_id, "summary": "x", "attach_paths": ["link.txt"]})
        self.assertTrue(res["isError"])

    def test_attach_rejects_credentials(self):
        (self.ws_a / ".env").write_text("KEY=secret")
        item_id = self._reserve()
        res = self.agent_a.call_tool("delm_complete_task", {
            "item_id": item_id, "summary": "x", "attach_paths": [".env"]})
        self.assertTrue(res["isError"])

    def test_apply_rejects_traversal_paths(self):
        (self.ws_a / "ok.txt").write_text("ok\n")
        item_id = self._reserve()
        done = self.agent_a.call_tool("delm_complete_task", {
            "item_id": item_id, "summary": "x", "attach_paths": ["ok.txt"]})
        self.assertFalse(done["isError"], done)
        pub_id = self.agent_a.sc.read("agent-alpha", full=True)["queue"][0]["publication_id"]
        res = self.agent_a.call_tool(
            "delm_apply_files", {"publication_id": pub_id, "paths": ["../evil.txt"]})
        self.assertTrue(res["isError"])


class TestIdempotentRetries(unittest.TestCase):
    def setUp(self):
        self.temp_dir, self.ws_a, _, self.agent_a, _ = make_servers()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def test_complete_same_request_id_returns_same_publication(self):
        self.agent_a.call_tool("delm_reserve_task", {"subject": "S", "detail": "D"})
        item_id = self.agent_a.sc.read("agent-alpha", full=True)["queue"][0]["id"]
        args = {"item_id": item_id, "summary": "done", "request_id": "retry-1"}
        first = self.agent_a.call_tool("delm_complete_task", args)
        second = self.agent_a.call_tool("delm_complete_task", args)
        self.assertFalse(first["isError"], first)
        self.assertFalse(second["isError"], second)
        self.assertIn(first["content"][0]["text"].split("Publication ID: `")[1].split("`")[0],
                        second["content"][0]["text"])

    def test_same_text_new_content_creates_new_publication(self):
        """Re-running a note with updated attachments must publish, not error."""
        log = self.ws_a / "log.txt"
        log.write_text("run 1\n")
        args = {"note_type": "FACT", "text": "bench", "scope": "s",
                "basis": "b", "attach_paths": ["log.txt"]}
        first = self.agent_a.call_tool("delm_publish_note", dict(args))
        self.assertFalse(first["isError"], first)
        log.write_text("run 2 - different output\n")
        second = self.agent_a.call_tool("delm_publish_note", dict(args))
        self.assertFalse(second["isError"], second)
        id1 = first["content"][0]["text"].split("`")[1]
        id2 = second["content"][0]["text"].split("`")[1]
        self.assertNotEqual(id1, id2)
        # The second publication stores the new content (not a replay of the first).
        with self.agent_a.sc.connect() as db:
            _, _, stored = self.agent_a.sc.body(db, id2)
        self.assertIn("run 2 - different output", stored[0]["content"])

    def test_empty_target_treated_as_no_target(self):
        res = self.agent_a.call_tool("delm_read_board", {"target": ""})
        self.assertFalse(res["isError"], res)


class TestCliWiring(unittest.TestCase):
    def test_version_flag(self):
        proc = subprocess.run(
            [sys.executable, "-m", "mcp_server", "--version"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("1.0.0", proc.stdout + proc.stderr)

    def test_invalid_actor_rejected(self):
        proc = subprocess.run(
            [sys.executable, "-m", "mcp_server", "--actor", "bad actor!",
             "--board", str(Path(tempfile.mkdtemp()) / "b.sqlite"),
             "--workspace", str(ROOT)],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 2)

    def test_board_directory_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        proc = subprocess.run(
            [sys.executable, "-m", "mcp_server", "--actor", "probe",
             "--board", str(tmp), "--workspace", str(tmp)],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a regular file", proc.stderr)

    def test_both_databases_use_wal(self):
        import sqlite3
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        DelmMcpServer(actor="w", board_path=tmp / "b.sqlite",
                      workspace_path=tmp, peers=["w"])
        for name in ("b.sqlite", "context-bodies.sqlite"):
            db = sqlite3.connect(tmp / name)
            try:
                self.assertEqual(
                    db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                db.close()

    def test_stdio_tools_list_roundtrip(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        proc = subprocess.run(
            [sys.executable, "-m", "mcp_server", "--actor", "probe",
             "--board", str(tmp / "b.sqlite"), "--workspace", str(tmp)],
            input='{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}\n',
            capture_output=True, text=True, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = [t["name"] for t in json.loads(proc.stdout.strip())["result"]["tools"]]
        self.assertIn("delm_apply_files", names)
        self.assertIn("delm_publish_items", names)


if __name__ == "__main__":
    unittest.main()
