import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_openai_flow as flow
from src.metrics import Metrics
from src.observability import emit, observed_request, select_route, model_call


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.metrics = Metrics()

    def total(self, group, metric):
        return self.metrics.snapshot()[group][metric]["total"]

    def test_request_counters_and_dimensions(self):
        for event, fields in [("request_started", {}), ("request_completed", {"route": "aks"}),
                              ("request_failed", {"route": "terraform"}),
                              ("request_completed", {"route": "generic", "outcome": "bounded"})]:
            self.metrics.record({"event": event, **fields})
        self.assertEqual(self.total("requests", "started"), 1)
        self.assertEqual(self.total("requests", "completed"), 2)
        self.assertEqual(self.total("requests", "failed"), 1)
        self.assertEqual(self.total("requests", "bounded_incomplete"), 1)
        self.assertEqual(self.metrics.snapshot()["requests"]["completed"]["dimensions"]["route"], {"aks": 1, "generic": 1})

    def test_model_tool_mcp_and_policy_counters(self):
        events = ["model_call_started", "model_call_failed", "model_call_stopped", "tool_attempt", "mcp_dispatch", "mcp_result", "mcp_failure", "policy_decision"]
        for event in events:
            self.metrics.record({"event": event, "policy_decision": "reject", "tool_name": "repo_file", "mcp_server": "azure-devops"})
        for group in ("model", "tools", "mcp"):
            for metric in self.metrics.snapshot()[group]:
                self.assertEqual(self.total(group, metric), 1)

    def test_safety_and_evidence_counters(self):
        records = [
            {"event": "schema_validation_failed", "error_category": "validation"},
            {"event": "execution_stopped", "reason_code": "duplicate_call"},
            {"event": "request_bounded", "reason_code": "aggregate_limit"},
            {"event": "request_bounded", "reason_code": "deadline_exceeded"},
            {"event": "evidence_collection"},
            {"event": "evidence_result", "outcome": "collected", "truncated": True, "evidence_status": "partial"},
            {"event": "evidence_result", "outcome": "failed"},
        ]
        for record in records:
            self.metrics.record(record)
        for group in ("safety", "evidence"):
            for metric in self.metrics.snapshot()[group]:
                self.assertEqual(self.total(group, metric), 1)

    def test_cardinality_and_sensitive_content_excluded(self):
        for index in range(1000):
            self.metrics.record({"event": "mcp_dispatch", "route": "private-"+str(index),
                "tool_name": "Bearer private", "mcp_server": "password=x", "request_id": str(index),
                "task_id": str(index), "arguments": {"content": "resource private {}"},
                "result": "Authorization: private", "reason_code": "AccountKey=private", "path": "/private/file"})
        encoded = json.dumps(self.metrics.snapshot())
        for text in ("private", "Bearer", "password", "AccountKey", "request_id", "task_id", "arguments", "Authorization"):
            self.assertNotIn(text, encoded)
        self.assertEqual(self.total("mcp", "dispatches"), 1000)
        self.assertEqual(self.metrics.snapshot()["mcp"]["dispatches"]["dimensions"], {})
        self.assertEqual(len(self.metrics._counts), 2)

    def test_concurrent_recording_snapshot_and_reset(self):
        def record(_):
            for _ in range(100):
                self.metrics.record({"event": "request_started", "route": "aks"})
                self.metrics.snapshot()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(record, range(8)))
        snapshot = self.metrics.snapshot()
        self.assertEqual(self.total("requests", "started"), 800)
        json.dumps(snapshot, allow_nan=False)
        self.metrics.reset()
        self.assertEqual(self.total("requests", "started"), 0)
        self.assertEqual(snapshot["requests"]["started"]["total"], 800)
        self.assertEqual(Metrics().snapshot(), self.metrics.snapshot())

    async def test_emitter_integration_partial_and_concurrent_requests(self):
        @observed_request
        async def request(fail=False):
            select_route("aks")
            await asyncio.sleep(0)
            emit("evidence_result", outcome="collected", evidence_status="partial")
            if fail:
                raise ValueError("private")
        with patch("src.observability.METRICS", self.metrics):
            await asyncio.gather(request(), request(True), return_exceptions=True)
        self.assertEqual(self.total("requests", "started"), 2)
        self.assertEqual(self.total("requests", "completed"), 1)
        self.assertEqual(self.total("requests", "failed"), 1)
        self.assertEqual(self.total("requests", "bounded_incomplete"), 2)
        self.assertNotIn("private", json.dumps(self.metrics.snapshot()))

    async def test_failure_isolation_between_logging_metrics_and_execution(self):
        @observed_request
        async def request():
            return 42
        with patch("src.observability.METRICS", self.metrics), patch("src.observability.LOGGER.info", side_effect=RuntimeError("sink")):
            self.assertEqual(await request(), 42)
        self.assertEqual(self.total("requests", "completed"), 1)
        with patch("src.observability.METRICS.record", side_effect=RuntimeError("metrics")), self.assertLogs("src.observability", level="INFO") as logs:
            self.assertEqual(await request(), 42)
        self.assertEqual(len(logs.records), 2)

    async def test_model_stop_budget_and_inflight_deadline(self):
        @observed_request
        async def request():
            return await model_call(flow.FakeOpenAIClient([flow.completion("private answer")]), messages=[])
        with patch("src.observability.METRICS", self.metrics), patch("src.observability.MAX_REQUEST_MODEL_CALLS", 0), patch("builtins.print"):
            await request()
        self.assertEqual(self.total("model", "calls"), 0)
        self.assertEqual(self.total("model", "stopped"), 1)
        self.assertEqual(self.total("safety", "budget_exhaustion"), 1)
        self.assertEqual(self.total("requests", "bounded_incomplete"), 1)

        from src.request_budget import current_budget
        from types import SimpleNamespace
        async def slow(**kwargs):
            await asyncio.sleep(5)
        @observed_request
        async def deadline_request():
            current_budget().configure(0.01)
            await model_call(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=slow))))
        with patch("src.observability.METRICS", self.metrics), patch("builtins.print"):
            await deadline_request()
        self.assertEqual(self.total("model", "stopped"), 2)
        self.assertEqual(self.total("safety", "deadline_exhaustion"), 1)

    async def test_real_generic_schema_event_updates_metrics(self):
        from langchain_core.tools import tool
        from src.agent.main import main
        @tool("pipelines_definition")
        async def pipeline(action: str, project: str, count: int) -> str:
            """List pipelines."""
            raise AssertionError("Invalid schema must not reach MCP")
        client = flow.FakeOpenAIClient([
            flow.completion(tool_calls=[flow.model_tool_call("pipelines_definition", '{"action":"list"}', "call-1")]),
            flow.completion("No evidence collected."),
        ])
        with flow.GenericAgentFlowTests.generic_patches(self, client, flow.FakeRuntime([pipeline])), \
                patch("src.observability.METRICS", self.metrics), patch("builtins.print"):
            await main("List pipelines")
        self.assertEqual(self.total("safety", "schema_validation_failures"), 1)
        self.assertEqual(self.total("mcp", "dispatches"), 0)
        self.assertEqual(self.total("model", "calls"), 2)
