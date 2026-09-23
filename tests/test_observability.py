import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
import test_openai_flow as flow
from src.agent.main import main
from src.observability import event_record, observed_request, emit, select_route, mcp_call, model_call
from src.observability import tool_metadata
from src.policy.policy import enforce_read_only_policy


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def records(self, captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    async def test_complete_generic_lifecycle_and_payload_exclusion(self):
        payload = 'password=hunter2; resource "sensitive" {}'

        @tool("pipelines_definition")
        async def pipelines(action: str, project: str) -> str:
            """Read pipeline definitions."""
            return payload

        client = flow.FakeOpenAIClient([
            flow.completion(tool_calls=[flow.model_tool_call("pipelines_definition", '{"action":"list"}', "call-1")]),
            flow.completion("final private answer"),
        ])
        with flow.GenericAgentFlowTests.generic_patches(self, client, flow.FakeRuntime([pipelines])), \
                patch("builtins.print") as output, self.assertLogs("src.observability", level="INFO") as captured:
            await main("List pipelines: private question")
        records = self.records(captured)
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertEqual(records[0]["event"], "request_started")
        self.assertEqual(records[-1]["event"], "request_completed")
        for event in ("route_selected", "model_call_started", "model_call_completed", "tool_attempt",
                      "policy_decision", "mcp_dispatch", "mcp_result", "evidence_result"):
            self.assertTrue(any(r["event"] == event and r["route"] == "generic" for r in records))
        for record in records:
            if record["event"] in {"tool_attempt", "policy_decision", "mcp_dispatch", "mcp_result", "evidence_result"}:
                self.assertEqual(record["tool_call_id"], "call-1")
        serialized = json.dumps(records)
        for value in (payload, "hunter2", "private question", "final private answer", "My Project", "test-key"):
            self.assertNotIn(value, serialized)
        output.assert_called_once_with("final private answer")

    async def test_policy_rejection_no_dispatch(self):
        @observed_request
        async def request():
            enforce_read_only_policy({"name": "repo_repository", "args": {"action": "delete"}, "id": "deny-1"})
        with self.assertLogs("src.observability", level="INFO") as captured, self.assertRaises(PermissionError):
            await request()
        records = self.records(captured)
        self.assertTrue(any(r.get("policy_decision") == "reject" for r in records))
        self.assertFalse(any(r["event"] == "mcp_dispatch" for r in records))
        self.assertEqual(records[-1]["event"], "request_failed")

    async def test_model_and_mcp_failures_do_not_leak(self):
        call = {"name": "repo_repository", "args": {"action": "list"}, "id": "fail-1"}
        @observed_request
        async def request():
            select_route("generic")
            with self.assertRaises(RuntimeError):
                await mcp_call(call, AsyncMock(side_effect=RuntimeError("password=private")))
            client = flow.FakeOpenAIClient([RuntimeError("Bearer private")])
            with self.assertRaises(RuntimeError):
                await model_call(client, messages=["private prompt"])
            await mcp_call(call, AsyncMock(return_value=ToolMessage(content="password=private", status="error", tool_call_id="fail-1")))
        with self.assertLogs("src.observability", level="INFO") as captured:
            await request()
        records = self.records(captured)
        self.assertEqual(sum(r["event"] == "mcp_failure" for r in records), 2)
        self.assertTrue(any(r["event"] == "model_call_failed" for r in records))
        self.assertNotIn("private", json.dumps(records))

    async def test_concurrent_request_task_isolation_and_reset(self):
        @observed_request
        async def request(*, task_context):
            select_route(task_context.topic)
            await asyncio.sleep(0)
            emit("evidence_result", outcome="collected")
        with self.assertLogs("src.observability", level="INFO") as captured:
            await asyncio.gather(request(task_context=SimpleNamespace(task_id="task-a", topic="aks")),
                                 request(task_context=SimpleNamespace(task_id="task-b", topic="terraform")))
            emit("outside_request", outcome="ignored")
        records = self.records(captured)
        self.assertEqual(len({r["request_id"] for r in records}), 2)
        for record in records:
            if "route" in record:
                self.assertEqual(record["route"], "aks" if record["task_id"] == "task-a" else "terraform")
        self.assertFalse(any(r["event"] == "outside_request" for r in records))

    def test_safe_serialization_and_strict_metadata_boundary(self):
        for field in ("prompt", "arguments", "result", "source", "approval", "payload"):
            with self.assertRaises(ValueError):
                event_record("test", **{field: "sensitive"})
        for secret in ("password=x", "Bearer abc", "Authorization: abc", "AccountKey=abc", "stringData", "ＡＰＩ＿ＫＥＹ=x"):
            record = event_record("test", operation=secret, duration_ms=float("nan"), truncated=True)
            self.assertNotIn("operation", record)
            self.assertNotIn("duration_ms", record)
            self.assertTrue(record["truncated"])
            json.dumps(record, allow_nan=False)

    def test_untrusted_tool_operation_is_not_logged(self):
        metadata = tool_metadata({"name": "repo_file", "id": "call-1",
                                  "args": {"action": "private_source_text", "content": "private_source_text"}})
        self.assertNotIn("private_source_text", json.dumps(metadata))

    async def test_sink_failure_does_not_change_result(self):
        @observed_request
        async def request():
            return 42
        with patch("src.observability.LOGGER.info", side_effect=RuntimeError("sink failure")):
            self.assertEqual(await request(), 42)
