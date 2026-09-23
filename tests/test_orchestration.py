import asyncio
import json
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import test_openai_flow as fixtures
import test_context_hardening as context_fixtures
import test_mcp_runtime as runtime_fixtures
from src.agent.orchestration import RequestServices, orchestrate_request
from src.agent.workflows import WorkflowResult, route_question
from src.config import ConfigurationError
from src.request_budget import BoundedResult, current_budget
from src.mcp.runtime import MCPRuntime, MCPRuntimeError


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def services(self):
        runtime = fixtures.FakeRuntime([])
        client = fixtures.FakeOpenAIClient([fixtures.completion("answer")])
        services = RequestServices(load_environment=Mock(), load_settings=Mock(return_value=fixtures.GENERIC_SETTINGS),
                                   runtime_factory=Mock(return_value=runtime), client_factory=Mock(return_value=client))
        return services, runtime

    async def test_existing_router_dispatches_all_three_handlers_and_returns_without_printing(self):
        for question, route in (("List pipelines", "generic"), ("Please troubleshoot task-manager", "aks"),
                                ("Review Terraform configuration", "terraform")):
            with self.subTest(route=route):
                services, runtime = self.services()
                expected = WorkflowResult("private answer")
                handlers = {name: AsyncMock(return_value=expected) for name in ("generic", "aks", "terraform")}
                with patch.multiple("src.agent.workflows", **{"handle_"+name: handler for name, handler in handlers.items()}), \
                        patch("builtins.print") as output, self.assertLogs("src.observability", level="INFO") as logs:
                    result = await orchestrate_request(question, services=services)
                self.assertIs(result, expected)
                self.assertEqual(route_question(question), route)
                handlers[route].assert_awaited_once()
                for name, handler in handlers.items():
                    if name != route:
                        handler.assert_not_awaited()
                output.assert_not_called()
                self.assertEqual(runtime.initialize_calls, 1)
                self.assertEqual(runtime.close_calls, 1)
                records = [json.loads(r.getMessage()) for r in logs.records]
                self.assertEqual(sum(r["event"] == "request_started" for r in records), 1)
                self.assertEqual(sum(r["event"] == "request_completed" for r in records), 1)
                self.assertEqual(len({r["request_id"] for r in records}), 1)
                self.assertNotIn("private answer", str(logs.output))

    async def test_real_generic_workflow_returns_same_answer(self):
        services, runtime = self.services()
        with patch("builtins.print") as output:
            result = await orchestrate_request("List pipelines", services=services)
        self.assertEqual(result.answer, "answer")
        self.assertIsNone(result.progress)
        output.assert_not_called()
        self.assertEqual(runtime.close_calls, 1)

    async def test_handler_failure_propagates_but_events_are_safe_and_runtime_closes(self):
        services, runtime = self.services()
        with patch("src.agent.workflows.handle_generic", side_effect=PermissionError("password=private")), \
                self.assertLogs("src.observability", level="INFO") as logs:
            with self.assertRaises(PermissionError):
                await orchestrate_request("List pipelines", services=services)
        self.assertEqual(runtime.close_calls, 1)
        records = [json.loads(r.getMessage()) for r in logs.records]
        self.assertEqual(records[-1]["event"], "request_failed")
        self.assertEqual(records[-1]["error_category"], "authorization")
        self.assertNotIn("private", str(logs.output))
        self.assertIsNone(current_budget())

    async def test_configuration_and_startup_failures_keep_existing_cleanup(self):
        services, runtime = self.services()
        services.load_settings.side_effect = ConfigurationError("invalid settings")
        with self.assertRaises(ConfigurationError):
            await orchestrate_request("List pipelines", services=services)
        services.runtime_factory.assert_not_called()
        services, runtime = self.services()
        runtime.initialize = AsyncMock(side_effect=RuntimeError("startup"))
        with self.assertRaises(RuntimeError):
            await orchestrate_request("List pipelines", services=services)
        self.assertEqual(runtime.close_calls, 1)

    async def test_concurrent_requests_keep_budget_and_lifecycle_isolated(self):
        budgets = []
        async def handler(*args, **kwargs):
            budget = current_budget()
            budgets.append(budget)
            await asyncio.sleep(0)
            self.assertIs(current_budget(), budget)
            return WorkflowResult("answer")
        first, runtime_a = self.services()
        second, runtime_b = self.services()
        with patch("src.agent.workflows.handle_generic", new=handler), self.assertLogs("src.observability", level="INFO") as logs:
            await asyncio.gather(orchestrate_request("A", services=first), orchestrate_request("B", services=second))
        self.assertIsNot(budgets[0], budgets[1])
        self.assertEqual(len({json.loads(r.getMessage())["request_id"] for r in logs.records}), 2)
        self.assertEqual((runtime_a.close_calls, runtime_b.close_calls), (1, 1))
        self.assertIsNone(current_budget())

    async def test_followup_hint_fallback_and_expiry_do_not_duplicate_router(self):
        fixture = context_fixtures.ContextHardeningTests()
        fixture.setUp()
        for expired in (False, True):
            with self.subTest(expired=expired):
                task = fixture.task
                if expired:
                    task = task.model_copy(update={"created_at": task.created_at-timedelta(minutes=16),
                                                   "last_activity_at": task.last_activity_at-timedelta(minutes=16)})
                services, _ = self.services()
                router = Mock(wraps=route_question)
                services = replace(services, router=router)
                with patch("src.agent.workflows.handle_aks", new=AsyncMock(return_value=WorkflowResult("aks"))) as aks, \
                        patch("src.agent.workflows.handle_generic", new=AsyncMock(return_value=WorkflowResult("generic"))) as generic:
                    result = await orchestrate_request("Now check the service", task_context=task, context_enabled=True, services=services)
                router.assert_called_once_with("Now check the service")
                self.assertEqual(result.answer, "generic" if expired else "aks")
                used = generic if expired else aks
                self.assertEqual(used.await_args.kwargs["task_context"] is None, expired)

    async def test_bounded_outcome_is_returned_not_printed_or_turned_into_progress(self):
        services, runtime = self.services()
        with patch("src.observability.MAX_REQUEST_MODEL_CALLS", 0), patch("builtins.print") as output:
            result = await orchestrate_request("List pipelines", context_enabled=True, services=services)
        self.assertIsInstance(result, BoundedResult)
        self.assertEqual(result.reason_code, "model_calls")
        self.assertEqual(result.evidence_status, "incomplete")
        output.assert_not_called()
        self.assertEqual(runtime.close_calls, 1)

    async def test_openai_closes_once_after_normal_completion(self):
        services, runtime = self.services()
        client = services.client_factory.return_value
        result = await orchestrate_request("List pipelines", services=services)
        self.assertEqual(result.answer, "answer")
        client.close.assert_awaited_once_with()
        self.assertEqual(runtime.close_calls, 1)

    async def test_openai_closes_for_each_mcp_startup_failure(self):
        for stage in ("azure-devops", "aks", "discovery", "binding"):
            with self.subTest(stage=stage):
                services, _ = self.services()
                client = services.client_factory.return_value
                mcp_client = runtime_fixtures.FakeClient(fail_server=stage if stage in {"azure-devops", "aks"} else None)
                async def loader(session, *, server_name):
                    if stage == "discovery":
                        raise RuntimeError("private discovery failure")
                    return [runtime_fixtures.Tool("repo_file")] if server_name == "aks" else []
                runtime = MCPRuntime(client=mcp_client, tool_loader=loader, log=lambda _: None)
                services = replace(services, runtime_factory=Mock(return_value=runtime))
                with self.assertRaises(MCPRuntimeError):
                    await orchestrate_request("List pipelines", services=services)
                client.close.assert_awaited_once_with()
                self.assertEqual(runtime.state, "closed")
                self.assertEqual(mcp_client.closes, [] if stage == "azure-devops" else ["azure-devops"] if stage == "aks" else ["aks", "azure-devops"])

    async def test_openai_closes_if_runtime_construction_fails(self):
        services, _ = self.services()
        original = RuntimeError("construction failed")
        services.runtime_factory.side_effect = original
        with self.assertRaises(RuntimeError) as caught:
            await orchestrate_request("List pipelines", services=services)
        self.assertIs(caught.exception, original)
        services.client_factory.return_value.close.assert_awaited_once_with()

    async def test_openai_closes_on_router_and_workflow_failures(self):
        for stage in ("router", "workflow"):
            with self.subTest(stage=stage):
                services, runtime = self.services()
                original = RuntimeError("execution failed")
                if stage == "router":
                    services = replace(services, router=Mock(side_effect=original))
                with patch("src.agent.workflows.handle_generic", side_effect=original), self.assertRaises(RuntimeError) as caught:
                    await orchestrate_request("List pipelines", services=services)
                self.assertIs(caught.exception, original)
                services.client_factory.return_value.close.assert_awaited_once_with()
                self.assertEqual(runtime.close_calls, 1)

    async def test_both_cleanup_failures_preserve_original_error_and_safe_events(self):
        for stage in ("startup", "workflow"):
            with self.subTest(stage=stage):
                services, runtime = self.services()
                original = PermissionError("original authorization failure")
                services.client_factory.return_value.close.side_effect = RuntimeError("password=private-client")
                runtime.close = AsyncMock(side_effect=RuntimeError("password=private-runtime"))
                if stage == "startup":
                    runtime.initialize = AsyncMock(side_effect=original)
                with patch("src.agent.workflows.handle_generic", side_effect=original), \
                        self.assertLogs("src.observability", level="INFO") as logs, self.assertRaises(PermissionError) as caught:
                    await orchestrate_request("List pipelines", services=services)
                self.assertIs(caught.exception, original)
                services.client_factory.return_value.close.assert_awaited_once_with()
                runtime.close.assert_awaited_once_with()
                records = [json.loads(r.getMessage()) for r in logs.records]
                self.assertEqual(sum(r["event"] == "cleanup_failed" for r in records), 2)
                self.assertEqual(records[-1]["error_category"], "authorization")
                self.assertNotIn("private", str(logs.output))

    async def test_cleanup_failure_without_execution_failure_is_not_hidden(self):
        services, runtime = self.services()
        original = RuntimeError("client cleanup failed")
        services.client_factory.return_value.close.side_effect = original
        runtime.close = AsyncMock(side_effect=RuntimeError("second cleanup failure"))
        with self.assertRaises(RuntimeError) as caught:
            await orchestrate_request("List pipelines", services=services)
        self.assertIs(caught.exception, original)
        runtime.close.assert_awaited_once_with()

    async def test_cancellation_during_startup_or_workflow_closes_both_resources(self):
        for stage in ("startup", "workflow"):
            with self.subTest(stage=stage):
                services, runtime = self.services()
                entered = asyncio.Event()
                async def wait(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()
                if stage == "startup":
                    runtime.initialize = wait
                with patch("src.agent.workflows.handle_generic", new=wait):
                    task = asyncio.create_task(orchestrate_request("List pipelines", services=services))
                    await asyncio.wait_for(entered.wait(), timeout=1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                services.client_factory.return_value.close.assert_awaited_once_with()
                self.assertEqual(runtime.close_calls, 1)
