"""Real event contracts: starts are never mistaken for successful completion."""
import asyncio
import json
import logging
from pathlib import Path
import tempfile
import unittest

from frontend.activity import Activity
from frontend.data_layer import SQLiteDataLayer
from frontend.investigation import EventHandler, Investigation, _active


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.activity = Activity(clock=lambda: self.now)

    def emit(self, name, **fields):
        return self.activity.accept({"event": name, **fields})

    def test_repeated_calls_order_and_matched_completion(self):
        self.emit("request_started")
        self.emit("model_call_started", model_call_sequence_number=1)
        self.now = 2.0
        self.assertEqual(self.activity.entries[-1]["state"], "active")
        self.emit("model_call_completed", model_call_sequence_number=1)
        self.assertEqual(self.activity.entries[1]["duration_seconds"], 2.0)
        self.emit("mcp_dispatch", dispatch_count=1, tool_name="kubectl_resources", operation="get")
        self.emit("mcp_result", dispatch_count=1, tool_name="kubectl_resources", operation="get")
        self.emit("model_call_started", model_call_sequence_number=2)
        self.assertIn("additional analysis", self.activity.current)
        self.emit("model_call_completed", model_call_sequence_number=2)
        self.emit("mcp_dispatch", dispatch_count=2, tool_name="repo_file", operation="get_content")
        self.emit("mcp_result", dispatch_count=2, tool_name="repo_file", operation="get_content")
        self.emit("cleanup_completed")
        self.emit("request_completed", outcome="completed")
        titles = [row["title"] for row in self.activity.entries]
        self.assertEqual(len(titles), 11)
        self.assertIn("Sending request", titles[1])
        self.assertIn("LLM response received", titles[2])
        self.assertIn("Executing MCP tool: kubectl_resources", titles[3])
        self.assertIn("Receiving MCP response: kubectl_resources", titles[4])
        self.assertIn("additional analysis", titles[5])
        self.assertIn("Executing MCP tool: repo_file", titles[7])
        self.assertEqual(titles[-1], "Investigation completed")
        self.assertTrue(all(row["state"] == "completed" for row in self.activity.entries))

    def test_unmatched_result_does_not_complete_active_call(self):
        self.emit("mcp_dispatch", dispatch_count=1)
        self.emit("mcp_result", dispatch_count=2)
        self.assertEqual(self.activity.entries[0]["state"], "active")
        self.emit("request_completed", outcome="completed")
        self.assertEqual(self.activity.entries[0]["state"], "unconfirmed")

    def test_simultaneous_calls_complete_only_matching_id(self):
        self.emit("mcp_dispatch", dispatch_count=1)
        self.emit("mcp_dispatch", dispatch_count=2)
        self.emit("mcp_result", dispatch_count=2)
        self.assertEqual(self.activity.entries[0]["state"], "active")
        self.assertEqual(self.activity.entries[1]["state"], "completed")
        self.emit("mcp_failure", dispatch_count=1)
        self.assertEqual(self.activity.entries[0]["state"], "failed")

    def test_mcp_failure_and_safe_request_failure(self):
        self.emit("mcp_dispatch", dispatch_count=1, tool_name="repo_repository")
        self.emit("mcp_failure", dispatch_count=1, tool_name="repo_repository", error_category="authentication", error="password=secret")
        self.assertEqual(self.activity.entries[0]["state"], "failed")
        self.assertIn("Authentication failed", self.activity.current)
        self.emit("cleanup_failed")
        self.emit("request_failed", error_category="mcp", reason_code="private-value")
        snapshot = self.activity.snapshot()
        self.assertEqual(snapshot["outcome"], "failed")
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertNotIn("private-value", json.dumps(snapshot))
        self.assertEqual(snapshot["entries"][-2]["state"], "failed")

    def test_llm_failure_does_not_show_response_success(self):
        self.emit("model_call_started", model_call_sequence_number=1)
        self.emit("model_call_failed", model_call_sequence_number=1, error_category="timeout")
        self.assertEqual(self.activity.entries[0]["state"], "failed")
        self.assertIn("timed out", self.activity.current)
        self.assertNotIn("response received", self.activity.current)

    def test_bounded_and_cancelled_are_not_success(self):
        self.emit("model_call_started", model_call_sequence_number=1)
        self.emit("model_call_stopped", outcome="bounded")
        self.emit("request_completed", outcome="bounded")
        self.assertEqual(self.activity.outcome, "stopped")
        self.assertEqual(self.activity.entries[0]["state"], "unconfirmed")
        self.assertNotIn("Investigation completed", json.dumps(self.activity.snapshot()))
        other = Activity()
        other.accept({"event": "mcp_dispatch", "dispatch_count": 1})
        other.end("cancelled", "Investigation cancelled")
        self.assertEqual(other.entries[0]["state"], "unconfirmed")
        self.assertEqual(other.outcome, "cancelled")

    def test_no_unobserved_stages_or_raw_metadata(self):
        self.emit("request_started")
        before = self.activity.snapshot()
        self.assertFalse(self.emit("processing_evidence", output="secret"))
        self.assertFalse(self.emit("service_connected"))
        self.assertEqual(before, self.activity.snapshot())
        self.emit("mcp_dispatch", tool_name="password=secret", operation="private-value", args={"token": "secret"})
        self.assertIn("tool name unavailable", self.activity.current)
        self.assertNotIn("secret", json.dumps(self.activity.snapshot()))
        self.assertNotIn("private-value", json.dumps(self.activity.snapshot()))

    def test_snapshots_do_not_change_and_time_freezes_after_finish(self):
        self.emit("model_call_started")
        snapshot = self.activity.snapshot()
        self.now = 3.5
        self.emit("model_call_completed")
        self.assertEqual(snapshot["entries"][0]["state"], "active")
        self.emit("request_completed", outcome="completed")
        final = self.activity.snapshot()
        self.now = 60
        self.emit("model_call_started")
        self.assertEqual(self.activity.snapshot(), final)
        self.assertEqual(final["elapsed_seconds"], 3.5)

    def test_startup_and_cleanup_log_templates_only(self):
        investigation = Investigation()
        token = _active.set(investigation)
        handler = EventHandler()
        try:
            def log(name, message, args=()):
                handler.emit(logging.LogRecord(name, logging.INFO, "", 0, message, args, None))
            log("src.agent.orchestration", "agent startup: initializing Azure OpenAI and MCP runtime")
            self.assertEqual(investigation.activity.entries[0]["state"], "active")
            log("src.agent.orchestration", "tool discovery complete: %d allowed tools", (3,))
            self.assertEqual(investigation.activity.entries[0]["state"], "completed")
            log("src.mcp.subprocess", "password=secret")
            log("src.agent.orchestration", "Unknown message: %s", ("private-value",))
            log("src.agent.orchestration", "agent shutdown complete")
            self.assertEqual(len(investigation.activity.entries), 3)
            self.assertEqual(investigation.activity.current, "MCP and model client cleanup completed")
            self.assertNotIn("private-value", json.dumps(investigation.activity.snapshot()))
        finally:
            _active.reset(token)


class ActivityPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_activity_survives_restart_and_is_thread_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            layer = SQLiteDataLayer(path)
            activity = Activity()
            activity.accept({"event": "model_call_started", "model_call_sequence_number": 1})
            activity.accept({"event": "model_call_completed", "model_call_sequence_number": 1})
            activity.accept({"event": "request_completed", "outcome": "completed"})
            snapshot = activity.snapshot()
            await layer.create_step({"id": "status", "threadId": "one", "metadata": {"activity": snapshot}})
            await layer.create_element({"id": "panel", "threadId": "one", "forId": "status", "name": "Activity", "type": "custom", "props": snapshot})
            await layer.create_step({"id": "other", "threadId": "two"})
            layer._conn.close()
            restored = SQLiteDataLayer(path)
            try:
                thread = await restored.get_thread("one")
                self.assertEqual(thread["steps"][0]["metadata"]["activity"], snapshot)
                self.assertEqual(thread["elements"][0]["props"], snapshot)
                self.assertEqual((await restored.get_thread("two"))["elements"], [])
            finally:
                restored._conn.close()
