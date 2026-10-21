"""Offline tests for the Mercury-3 board worker (HTTP fully mocked, no spend)."""

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from workers.mercury_board_worker import (
    assistant_text,
    chat_completion,
    run_worker,
)


def fake_body(content="plan text here"):
    return {"model": "mercury-3",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                      "total_tokens": 30}}


def fake_urlopen(body):
    raw = io.BytesIO(json.dumps(body).encode("utf-8"))
    response = mock.Mock()
    response.__enter__ = mock.Mock(return_value=raw)
    response.__exit__ = mock.Mock(return_value=False)
    return mock.Mock(return_value=response)


class TestMercuryWorker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.board = self.temp_dir / "board.sqlite"
        self.ws = self.temp_dir / "ws"
        self.ws.mkdir()
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def run_case(self, **over):
        kwargs = dict(actor="m1", board=self.board, workspace=self.ws,
                      subject="S", detail="D")
        kwargs.update(over)
        return run_worker(**kwargs)

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        with mock.patch("urllib.request.urlopen") as http:
            result = self.run_case(dry_run=True)
        http.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "dry-run")
        self.assertIn("implementation plan", result["messages"][1]["content"])
        self.assertFalse(self.board.exists())

    def test_full_flow_publishes_and_completes(self):
        # >1200 chars: the file must keep it all (note text is only a pointer).
        long_analysis = ("do X then Y. " * 300).strip()
        with mock.patch("urllib.request.urlopen",
                        fake_urlopen(fake_body(long_analysis))):
            result = self.run_case()
        self.assertTrue(result, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["usage"]["total_tokens"], 30)
        # Full analysis travels as an attached file, untruncated.
        attached = self.ws / result["analysis_file"]
        self.assertTrue(attached.is_file())
        self.assertIn(long_analysis, attached.read_text())
        from mcp_server.server import DelmMcpServer
        reader = DelmMcpServer(actor="reader", board_path=self.board,
                               workspace_path=self.ws, peers=["reader", "m1"])
        with reader.sc.connect() as db:
            _, _, stored = reader.sc.body(db, result["publication_id"])
        self.assertIn(long_analysis, stored[0]["content"])

    def test_second_run_reports_already_done_without_api_call(self):
        http = fake_urlopen(fake_body("plan"))
        with mock.patch("urllib.request.urlopen", http):
            first = self.run_case()
            second = self.run_case()
        self.assertEqual(first["stage"], "done")
        self.assertEqual(second["stage"], "already-done")
        self.assertTrue(second["ok"])
        self.assertEqual(http.call_count, 1)

    def test_peer_held_scope_is_not_duplicated(self):
        from mcp_server.server import DelmMcpServer
        peer = DelmMcpServer(actor="peer", board_path=self.board,
                             workspace_path=self.ws, peers=["peer", "m1"])
        self.assertFalse(peer.call_tool(
            "delm_reserve_task",
            {"subject": "S", "detail": "D"})["isError"])
        http = fake_urlopen(fake_body("plan"))
        with mock.patch("urllib.request.urlopen", http):
            result = self.run_case()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "already-claimed")
        http.assert_not_called()

    def test_request_shape(self):
        seen = {}

        def capture(request, timeout=None):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization", "")
            seen["payload"] = json.loads(request.data.decode())
            return fake_urlopen(fake_body())()

        with mock.patch("urllib.request.urlopen", side_effect=capture):
            with mock.patch.dict(os.environ, {"INCEPTION_API_KEY": "test-key"}):
                with mock.patch("workers.mercury_board_worker.load_dotenv"):
                    self.run_case(model="mercury-3", reasoning_effort="low")
        self.assertEqual(seen["url"],
                         "https://api.inceptionlabs.ai/v1/chat/completions")
        self.assertTrue(seen["auth"].startswith("Bearer "))
        self.assertNotIn("test-key", json.dumps(seen["payload"]))
        self.assertEqual(seen["payload"]["model"], "mercury-3")
        self.assertEqual(seen["payload"]["reasoning_effort"], "low")

    def test_missing_key_fails_cleanly(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INCEPTION_API_KEY", None)
            with mock.patch("workers.mercury_board_worker.load_dotenv"):
                with self.assertRaises(ValueError):
                    self.run_case()

    def test_bad_effort_and_temperature_rejected(self):
        with self.assertRaises(ValueError):
            chat_completion("k", [], reasoning_effort="ultra")
        with self.assertRaises(ValueError):
            chat_completion("k", [], temperature=0.1)
        with self.assertRaises(ValueError):
            chat_completion("k", [], temperature=1.5)

    def test_http_error_becomes_value_error(self):
        error = urllib.error.HTTPError(
            "https://x", 429, "Too Many Requests", {}, io.BytesIO(b"slow down"))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ValueError) as ctx:
                chat_completion("k", [{"role": "user", "content": "hi"}])
        self.assertIn("429", str(ctx.exception))

    def test_unexpected_shapes_rejected(self):
        with self.assertRaises(ValueError):
            assistant_text({"no": "choices"})
        with self.assertRaises(ValueError):
            assistant_text({"choices": []})


if __name__ == "__main__":
    unittest.main()
