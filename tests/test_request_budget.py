import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import ToolMessage
import test_openai_flow as flow
import test_aks_troubleshooting as aks_fixtures
import test_terraform_evidence as terraform_fixtures
from src.agent.main import main, MAX_TOOL_EXECUTIONS
from src.agent.aks_troubleshooting import MAX_AKS_COLLECTION_CALLS
from src.config import load_settings, ConfigurationError
from src.observability import observed_request, model_call, mcp_call, select_route
from src.policy.policy import enforce_read_only_policy
from src.request_budget import (RequestBudget, RequestBoundExceeded, BoundedResult,
    current_budget, checkpoint, measured_size, MAX_REQUEST_INPUT_CHARS, MAX_REQUEST_RESULT_CHARS)
from src.safe_diagnostics import classify_error


CALL = {"name": "repo_repository", "args": {"action": "list", "project": "My Project"}, "id": "call-1"}


class RequestBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_clock_remaining_and_expired(self):
        with patch("src.request_budget.monotonic", return_value=101):
            budget = RequestBudget(started=100, deadline_seconds=5)
            self.assertEqual(budget.elapsed, 1)
            self.assertEqual(budget.remaining, 4)
            budget.check()
        with patch("src.request_budget.monotonic", return_value=105):
            with self.assertRaises(RequestBoundExceeded) as caught:
                budget.check()
        self.assertEqual(classify_error(caught.exception).category, "timeout")
        self.assertNotIn("Traceback", str(caught.exception))

    async def test_deadline_before_model_policy_and_dispatch(self):
        for boundary in ("model", "policy", "mcp"):
            with self.subTest(boundary=boundary):
                invoke = AsyncMock()
                client = flow.FakeOpenAIClient([flow.completion("answer")])
                @observed_request
                async def request():
                    current_budget().started -= 301
                    if boundary == "model":
                        await model_call(client, messages=[])
                    elif boundary == "policy":
                        enforce_read_only_policy(CALL)
                    else:
                        await mcp_call(CALL, invoke)
                with patch("builtins.print"), self.assertLogs("src.observability", level="INFO") as logs:
                    result = await request()
                self.assertIsInstance(result, BoundedResult)
                self.assertEqual(result.reason_code, "deadline")
                client.create.assert_not_awaited()
                invoke.assert_not_awaited()
                self.assertTrue(any(json.loads(r.getMessage())["event"] == "request_bounded" for r in logs.records))

    async def test_model_limit_shared_across_components_and_routes(self):
        client = flow.FakeOpenAIClient([flow.completion("answer")]*4)
        @observed_request
        async def request():
            select_route("generic")
            await model_call(client, messages=[])
            select_route("terraform")
            await model_call(client, messages=[])
            await model_call(client, messages=[])
        with patch("src.observability.MAX_REQUEST_MODEL_CALLS", 2), patch("builtins.print"):
            result = await request()
        self.assertEqual(client.create.await_count, 2)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertEqual(result.reason_code, "model_calls")

    async def test_tool_attempt_limit_and_dispatch_counts(self):
        invoke = AsyncMock(return_value=ToolMessage(content="[]", tool_call_id="call-1"))
        @observed_request
        async def request():
            enforce_read_only_policy(CALL)
            await mcp_call(CALL, invoke)
            enforce_read_only_policy(CALL)
            enforce_read_only_policy(CALL)
        with patch("src.observability.MAX_REQUEST_TOOL_ATTEMPTS", 2), patch("builtins.print"):
            result = await request()
        self.assertEqual(result.usage.tool_attempts, 2)
        self.assertEqual(result.usage.mcp_dispatches, 1)
        self.assertEqual(result.reason_code, "tool_attempts")

    async def test_partial_evidence_preserved_no_oversized_result_or_more_work(self):
        invoke = AsyncMock(side_effect=[ToolMessage(content="useful", tool_call_id="call-1"),
                                       ToolMessage(content="private"*50, tool_call_id="call-2")])
        @observed_request
        async def request():
            for _ in range(3):
                await mcp_call(CALL, invoke)
        with patch("src.request_budget.MAX_REQUEST_RESULT_CHARS", 100), patch("builtins.print") as output:
            result = await request()
        self.assertEqual(invoke.await_count, 2)
        self.assertEqual(result.evidence_status, "incomplete")
        self.assertEqual(result.reason_code, "result_chars")
        self.assertEqual(len(result.evidence), 1)
        self.assertIn("useful", json.dumps(result.evidence))
        self.assertNotIn("private", repr(result))
        self.assertNotIn("useful", str(output.call_args))
        self.assertIn("incomplete", str(output.call_args))

    async def test_aggregate_input_blocks_model_and_tool(self):
        for boundary in ("model", "mcp"):
            with self.subTest(boundary=boundary):
                invoke = AsyncMock()
                client = flow.FakeOpenAIClient([flow.completion("answer")])
                @observed_request
                async def request():
                    if boundary == "model":
                        await model_call(client, messages=["x"*100])
                    else:
                        await mcp_call({**CALL, "args": {"path": "x"*100}}, invoke)
                with patch("src.request_budget.MAX_REQUEST_INPUT_CHARS", 50), patch("builtins.print"):
                    result = await request()
                self.assertEqual(result.reason_code, "input_chars")
                client.create.assert_not_awaited()
                invoke.assert_not_awaited()

    async def test_input_accumulates_and_model_output_counts(self):
        client = flow.FakeOpenAIClient([flow.completion("answer")]*3)
        @observed_request
        async def request():
            await model_call(client, messages=["x"*20])
            await model_call(client, messages=["x"*20])
        with patch("src.request_budget.MAX_REQUEST_INPUT_CHARS", 50), patch("builtins.print"):
            result = await request()
        self.assertEqual(result.reason_code, "input_chars")
        self.assertGreater(result.usage.result_chars, 0)
        self.assertEqual(client.create.await_count, 1)

    async def test_inflight_timeout_cancels_and_cleans_up(self):
        cleaned = []
        @observed_request
        async def request():
            current_budget().configure(0.01)
            try:
                await asyncio.sleep(10)
            finally:
                cleaned.append(True)
        with patch("builtins.print"):
            result = await request()
        self.assertEqual(result.reason_code, "deadline")
        self.assertEqual(cleaned, [True])
        self.assertIsNone(current_budget())

    async def test_request_and_concurrent_budget_isolation(self):
        @observed_request
        async def request():
            budget = current_budget()
            await asyncio.sleep(0)
            self.assertIs(current_budget(), budget)
            return budget.usage
        first, second = await asyncio.gather(request(), request())
        self.assertIsNot(first, second)
        self.assertEqual(first.model_calls, 0)
        self.assertIsNone(current_budget())

    async def test_main_bounded_model_closes_runtime_and_no_task_update(self):
        client = flow.FakeOpenAIClient([flow.completion("answer")])
        runtime = flow.FakeRuntime([])
        with flow.GenericAgentFlowTests.generic_patches(self, client, runtime), \
                patch("src.observability.MAX_REQUEST_MODEL_CALLS", 0), patch("builtins.print"):
            result = await main("List pipelines", context_enabled=True)
        self.assertIsNone(result)
        self.assertEqual(runtime.close_calls, 1)
        client.create.assert_not_awaited()

    async def test_dedicated_collectors_share_outer_budget_and_stop_cleanly(self):
        for route in ("aks", "terraform"):
            with self.subTest(route=route):
                calls = []
                if route == "aks":
                    tool = aks_fixtures.AKSHardeningTests().healthy_tool()
                    tools = [tool]
                    calls = tool.calls
                    collector = aks_fixtures.collect_task_manager_evidence
                else:
                    tools, _ = terraform_fixtures.EvidenceTests().tools(calls)
                    collector = terraform_fixtures.gather_terraform_evidence
                @observed_request
                async def request():
                    select_route(route)
                    return await collector(tools, log=lambda _: None)
                with patch("src.observability.MAX_REQUEST_TOOL_ATTEMPTS", 2), patch("builtins.print"):
                    result = await request()
                self.assertEqual(result.reason_code, "tool_attempts")
                self.assertEqual(len(calls), 2)
                self.assertEqual(result.usage.mcp_dispatches, 2)
                self.assertEqual(len(result.evidence), 2)

    async def test_fresh_terraform_exact_source_unmodified_with_outer_budget(self):
        calls = []
        tools, source = terraform_fixtures.EvidenceTests().tools(calls)
        @observed_request
        async def request():
            messages = await terraform_fixtures.gather_terraform_evidence(tools, log=lambda _: None)
            return messages, current_budget().usage
        messages, usage = await request()
        files = terraform_fixtures.retrieved_files(messages)
        self.assertTrue(files)
        self.assertTrue(all(value == source for value in files.values()))
        self.assertEqual(usage.mcp_dispatches, len(calls))
        self.assertGreater(usage.result_chars, 0)

    def test_configuration_and_existing_limits(self):
        env = {"AZURE_OPENAI_ENDPOINT": "https://example.test", "AZURE_OPENAI_API_KEY": "private", "AKS_MCP_PATH": "/tmp/aks"}
        self.assertEqual(load_settings(env).request_deadline_seconds, 300)
        self.assertEqual(load_settings({**env, "REQUEST_DEADLINE_SECONDS": "120"}).request_deadline_seconds, 120)
        for value in ("0", "1801", "nan", "password=private"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError) as caught:
                load_settings({**env, "REQUEST_DEADLINE_SECONDS": value})
            self.assertNotIn(value, str(caught.exception))
        self.assertEqual(MAX_TOOL_EXECUTIONS, 5)
        self.assertEqual(MAX_AKS_COLLECTION_CALLS, 12)
        self.assertEqual(MAX_REQUEST_RESULT_CHARS, 512_000)
        self.assertEqual(MAX_REQUEST_INPUT_CHARS, 1_000_000)

    def test_measurement_does_not_stringify_unknown_objects(self):
        class Unsafe:
            def __str__(self):
                raise AssertionError("Must not stringify")
        self.assertEqual(measured_size(Unsafe(), 10), 11)
        circular = []
        circular.append(circular)
        self.assertEqual(measured_size(circular, 1000), 1001)
