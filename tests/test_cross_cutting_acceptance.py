"""Cross-request contracts with real workflows and mocked external boundaries."""
import asyncio
import json
import unittest
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import test_openai_flow as flow
from src.agent.orchestration import RequestServices, orchestrate_request
from src.agent.task_session import TaskSession
from src.metrics import Metrics
from src.request_budget import BoundedResult, current_budget


class CrossCuttingAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.metrics = Metrics()
        self.metrics_patch = patch("src.observability.METRICS", self.metrics)
        self.metrics_patch.start()
        self.addCleanup(self.metrics_patch.stop)

    def total(self, group, name):
        return self.metrics.snapshot()[group][name]["total"]

    def setup_request(self, responses, execute):
        client = flow.FakeOpenAIClient(responses)
        runtime = flow.FakeRuntime([flow.repository_tool(execute=execute)])
        services = RequestServices(load_environment=Mock(), load_settings=Mock(return_value=flow.GENERIC_SETTINGS),
                                   client_factory=Mock(return_value=client), runtime_factory=Mock(return_value=runtime))
        return client, runtime, services

    def call(self, identifier="read-1", **args):
        return flow.completion(tool_calls=[flow.model_tool_call(
            "repo_repository", json.dumps({"action": "list", **args}), identifier)])

    def records(self, logs):
        records = [json.loads(r.getMessage()) for r in logs.records]
        for value in ("password=hidden", "hidden", "/private/server", "private-source", "private-answer", "test-key"):
            self.assertNotIn(value, json.dumps(records))
            self.assertNotIn(value, json.dumps(self.metrics.snapshot()))
        self.assertNotIn("request_id", json.dumps(self.metrics.snapshot()))
        forbidden_fields = {"prompt", "messages", "response", "arguments", "args", "result", "source", "payload"}
        self.assertTrue(all(not forbidden_fields.intersection(record) for record in records))
        self.assertNotIn("repo-123", json.dumps(self.metrics.snapshot()))
        return records

    async def test_followup_recollects_with_new_budget_and_conservative_references(self):
        clock = [datetime.now(timezone.utc)]
        session, other = TaskSession(clock=lambda: clock[0]), TaskSession()
        budgets = []

        async def execute(**args):
            budgets.append(current_budget())
            self.assertEqual(args["project"], "My Project")
            return [{"type": "text", "text": json.dumps([{
                "name": "app", "id": "repo-123", "description": f"private-source-{len(budgets)}"}])}]

        client, runtime, _ = self.setup_request([
            self.call(), flow.completion("private-answer first"),
            self.call(), flow.completion("private-answer fresh"),
        ], execute)
        with flow.GenericAgentFlowTests.generic_patches(self, client, runtime), patch("builtins.print"), \
                self.assertLogs("src.observability", level="INFO") as logs:
            await session.request("Find repository app")
            self.assertIsNone(other.context)
            previous = session.context
            await session.request("that repository", follow_up=True)
            self.assertEqual(session.context.task_id, previous.task_id)
            followup = json.dumps(client.create.await_args_list[2].kwargs["messages"])
            self.assertIn("repo-123", followup)
            self.assertIn("discovery again", followup)
            self.assertNotIn("private-source-1", followup)
            self.assertNotIn('"observations"', followup)
            fresh = json.dumps(client.create.await_args.kwargs["messages"])
            self.assertIn("private-source-2", fresh)
            self.assertNotIn("private-source-1", fresh)
            # Incompatible reference must not call the model/collector again.
            await session.request("what about the pods?", follow_up=True)
            self.assertEqual(client.create.await_count, 4)
            self.assertIsNone(session.context)
        self.assertIsNot(budgets[0], budgets[1])
        self.assertEqual([b.usage.mcp_dispatches for b in budgets], [1, 1])
        self.assertEqual(len({r["request_id"] for r in self.records(logs)}), 2)
        self.assertNotIn("private-source", previous.model_dump_json())
        # Expiry uses the real session rule, without waiting or refreshing evidence.
        session._context = previous
        clock[0] += timedelta(minutes=16)
        self.assertIsNone(session.context)
        self.assertIsNone(current_budget())

    async def test_recovery_is_sanitized_but_hard_failures_do_not_retry(self):
        execute = AsyncMock(side_effect=[RuntimeError("network unavailable /private/server password=hidden"),
                                        [{"type": "text", "text": "private-source available"}]])
        client, _, services = self.setup_request([
            self.call(), self.call("read-2", top=1),
            flow.completion("Initial listing unavailable; a limited fresh listing was collected."),
        ], execute)
        with self.assertLogs("src.observability", level="INFO") as logs:
            result = await orchestrate_request("List repositories", services=services)
        feedback = client.create.await_args_list[1].kwargs["messages"][-1]["content"][0]["text"]
        self.assertIn("temporarily unavailable", feedback)
        for value in ("hidden", "/private/server"):
            self.assertNotIn(value, feedback)
        self.assertIn("limited", result.answer)
        self.assertEqual(execute.await_count, 2)
        records = self.records(logs)
        self.assertTrue(any(r["event"] == "mcp_failure" and r.get("error_category") == "mcp" for r in records))
        self.assertEqual(self.total("tools", "attempts"), 2)
        self.assertEqual(self.total("mcp", "dispatches"), 2)
        self.assertEqual(self.total("mcp", "successes"), 1)
        for kind in ("authentication", "policy"):
            with self.subTest(kind=kind):
                blocked = AsyncMock(side_effect=RuntimeError("authentication failed password=hidden"))
                plan = self.call(project="Other") if kind == "policy" else self.call()
                client, _, services = self.setup_request([plan, self.call("unauthorized-retry")], blocked)
                with self.assertLogs("src.observability", level="INFO") as logs, self.assertRaises((RuntimeError, PermissionError)):
                    await orchestrate_request("List repositories", services=services)
                self.assertEqual(client.create.await_count, 1)
                self.assertEqual(blocked.await_count, 0 if kind == "policy" else 1)
                records = self.records(logs)
                self.assertEqual(records[-1]["event"], "request_failed")
                self.assertEqual(records[-1]["error_category"], kind)

    async def test_success_and_request_bound_have_correlated_events_and_metrics(self):
        execute = AsyncMock(return_value=[{"type": "text", "text": "private-source evidence"}])
        client, _, services = self.setup_request([self.call(), flow.completion("private-answer")], execute)
        with self.assertLogs("src.observability", level="INFO") as logs:
            success = await orchestrate_request("List repositories", services=services)
            bounded_client, _, bounded_services = self.setup_request([self.call(), self.call("never-called")], execute)
            # A lowered existing outer limit makes the stop deterministic, not time-dependent.
            with patch("src.observability.MAX_REQUEST_MODEL_CALLS", 1):
                limited = await orchestrate_request("List repositories", services=bounded_services)
        self.assertEqual(success.answer, "private-answer")
        self.assertIsInstance(limited, BoundedResult)
        self.assertEqual((limited.reason_code, limited.evidence_status), ("model_calls", "incomplete"))
        self.assertEqual(limited.usage.model_calls, 1)
        self.assertEqual(limited.usage.mcp_dispatches, 1)
        bounded_client.create.assert_awaited_once()
        self.assertEqual(execute.await_count, 2)  # One per request, none after stop.
        self.assertEqual(len(limited.evidence), 1)
        self.assertTrue(limited.evidence[0]["untrusted_data"])
        self.assertIn("private-source evidence", json.dumps(limited.evidence))
        records = self.records(logs)
        ids = {r["request_id"] for r in records}
        self.assertEqual(len(ids), 2)
        for request_id in ids:
            events = [r for r in records if r["request_id"] == request_id]
            self.assertEqual(events[0]["event"], "request_started")
            self.assertEqual(events[-1]["event"], "request_completed")
            for name in ("route_selected", "model_call_started", "model_call_completed", "tool_attempt",
                         "policy_decision", "mcp_dispatch", "mcp_result", "evidence_result"):
                self.assertTrue(any(r["event"] == name and r["route"] == "generic" for r in events))
            self.assertTrue(all(r["tool_call_id"] == "read-1" for r in events if "tool_call_id" in r))
        self.assertTrue(any(r["event"] == "request_bounded" and r.get("limit") == 1 for r in records))
        self.assertTrue(any(r["event"] == "request_completed" and r["outcome"] == "bounded" for r in records))
        self.assertEqual(self.total("requests", "started"), 2)
        self.assertEqual(self.total("requests", "bounded_incomplete"), 1)
        self.assertEqual(self.total("model", "stopped"), 1)
        self.assertEqual(self.total("safety", "budget_exhaustion"), 1)

    async def test_concurrent_sessions_failure_and_reset_are_isolated(self):
        selected = ContextVar("acceptance_dependencies")
        arrived = asyncio.Event()
        budgets = []

        async def execute(**args):
            budget = current_budget()
            budgets.append(budget)
            if len(budgets) == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=2)
            self.assertIs(current_budget(), budget)
            if selected.get()[2]:
                raise RuntimeError("authentication failed password=hidden")
            return [{"type": "text", "text": '[{"id":"repo-123","name":"app"}]'}]

        good = self.setup_request([self.call(), flow.completion("private-answer")], execute)
        bad = self.setup_request([self.call()], execute)
        sessions = [TaskSession(), TaskSession()]

        async def run(index, dependencies):
            token = selected.set((*dependencies[:2], index == 1))
            try:
                return await sessions[index].request("Find repository app")
            finally:
                selected.reset(token)

        with patch("src.agent.main.AsyncOpenAI", side_effect=lambda **_: selected.get()[0]), \
                patch("src.agent.main.MCPRuntime", side_effect=lambda **_: selected.get()[1]), \
                patch("src.agent.main.load_settings", return_value=flow.GENERIC_SETTINGS), \
                patch("src.agent.main.load_dotenv"), patch("builtins.print"), \
                self.assertLogs("src.observability", level="INFO") as logs:
            outcomes = await asyncio.gather(run(0, good), run(1, bad), return_exceptions=True)
        self.assertIsInstance(outcomes[1], RuntimeError)
        self.assertNotIsInstance(outcomes[0], Exception)
        self.assertIsNotNone(sessions[0].context)
        self.assertIsNone(sessions[1].context)
        saved = sessions[0].context
        sessions[1].reset()
        self.assertEqual(sessions[0].context, saved)
        self.assertIsNot(budgets[0], budgets[1])
        self.assertEqual([b.usage.mcp_dispatches for b in budgets], [1, 1])
        records = self.records(logs)
        self.assertEqual(len({r["request_id"] for r in records}), 2)
        self.assertEqual(len({r["task_id"] for r in records}), 2)
        for request_id in {r["request_id"] for r in records}:
            self.assertEqual(len({r["task_id"] for r in records if r["request_id"] == request_id}), 1)
        self.assertEqual(self.total("requests", "completed"), 1)
        self.assertEqual(self.total("requests", "failed"), 1)
        self.assertIsNone(current_budget())

    async def test_metrics_failure_does_not_change_request_result(self):
        execute = AsyncMock(return_value="private-source")
        client, runtime, services = self.setup_request([self.call(), flow.completion("private-answer")], execute)
        with patch.object(self.metrics, "record", side_effect=RuntimeError("password=hidden")), \
                self.assertLogs("src.observability", level="INFO") as logs:
            result = await orchestrate_request("List repositories", services=services)
        self.assertEqual(result.answer, "private-answer")
        execute.assert_awaited_once()
        client.close.assert_awaited_once()
        self.assertEqual(runtime.close_calls, 1)
        self.assertEqual(self.records(logs)[-1]["event"], "request_completed")
