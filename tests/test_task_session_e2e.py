import json
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, ToolMessage
import test_openai_flow as fixtures
from src.agent.aks_troubleshooting import TroubleshootingEvidence
from src.agent.task_session import TaskSession, progress
from src.review.terraform_evidence import TerraformEvidence


class TaskSessionFinalTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_grammar_and_route_isolation(self):
        for initial, followup, topic, repo in (
            ("Investigate Task Manager", "what about the pods?", "aks", None),
            ("Investigate Task Manager", "the service", "aks", None),
            ("Find repository app", "that repository", "azure_devops", "repo-A"),
            ("Review Terraform", "the Terraform module", "terraform", "repo-A")):
            with self.subTest(followup=followup):
                session = TaskSession()
                with patch("src.agent.main.main", new=AsyncMock(return_value=progress(topic, repository_id=repo))) as run:
                    await session.request(initial)
                    identity = session.context.task_id
                    await session.request(followup, follow_up=True)
                    hints = run.await_args.kwargs["task_context"]
                    self.assertEqual(hints.topic, topic)
                    self.assertEqual(hints.task_id, identity)
                    self.assertEqual(session.context.current_objective, followup)
                    await session.request("Find repository other", follow_up=True)
                    selected = run.await_args.kwargs["task_context"]
                    self.assertIsNone(selected.scope.repository_id_hint)
                    self.assertIsNone(selected.scope.workload_hint)

    async def test_ambiguous_reference_does_not_call_model_or_collector(self):
        session = TaskSession()
        with patch("src.agent.main.main", new=AsyncMock(return_value=progress("aks"))) as run, patch("builtins.print") as output:
            await session.request("Investigate Task Manager")
            await session.request("What about it?", follow_up=True)
            self.assertEqual(run.await_count, 1)
            self.assertIsNone(session.context)
            self.assertIn("Please specify", output.call_args.args[0])
        with patch("src.agent.main.main", new=AsyncMock(return_value=progress("azure_devops"))) as run, patch("builtins.print"):
            await session.request("Find repositories")
            await session.request("that repository", follow_up=True)
            self.assertEqual(run.await_count, 1)
            self.assertIsNone(session.context)
        with patch("src.agent.main.main", new=AsyncMock(return_value=progress("terraform"))) as run, patch("builtins.print"):
            await session.request("Review Terraform")
            await session.request("what about the pods?", follow_up=True)
            self.assertEqual(run.await_count, 1)
            self.assertIsNone(session.context)

    async def test_malformed_aks_response_discards_context(self):
        client = fixtures.FakeOpenAIClient([fixtures.completion("not-json")])
        session = TaskSession()
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, fixtures.FakeRuntime([])), \
             patch("src.agent.main.collect_task_manager_evidence", new=AsyncMock(return_value=TroubleshootingEvidence())) as collect, \
             patch("builtins.print"):
            await session.request("Investigate Task Manager")
        self.assertEqual(collect.await_count, 1)
        self.assertIsNone(session.context)

    async def test_mcp_failure_then_followup_cannot_reuse_old_scope(self):
        session = TaskSession()
        with patch("src.agent.main.main", new=AsyncMock(return_value=progress("terraform", repository_id="repo-old"))):
            await session.request("Review Terraform")
        with fixtures.GenericAgentFlowTests.generic_patches(self, fixtures.FakeOpenAIClient([]), fixtures.FakeRuntime([])), \
             patch("src.agent.main.gather_terraform_evidence", new=AsyncMock(side_effect=RuntimeError("transport failure"))), \
             self.assertRaises(RuntimeError):
            await session.request("Now look specifically at the networking", follow_up=True)
        self.assertIsNone(session.context)
        with patch("src.agent.main.main", new=AsyncMock(return_value=None)) as run:
            await session.request("Show that file", follow_up=True)
        self.assertIsNone(run.await_args.kwargs["task_context"].scope.repository_id_hint)

    async def test_terraform_followup_cannot_cite_file_from_prior_request(self):
        def evidence(path):
            data = TerraformEvidence()
            data.discovery.update(repository_id="repo-1", selected_files=[path])
            data.extend([AIMessage(content="", tool_calls=[{"name": "repo_file", "id": "read", "args": {"action": "get_content", "path": path}}]),
                         ToolMessage(content='variable "example" {}', tool_call_id="read")])
            return data
        finding = {"Finding": "Example", "File": "/terraform/old.tf", "Evidence line": 1,
                   "Why it matters": "Possible risk", "Verification needed": "Check requirements", "Confidence": "Inference"}
        client = fixtures.FakeOpenAIClient([fixtures.completion('{"findings":[]}'), fixtures.completion(json.dumps({"findings": [finding]}))])
        session = TaskSession()
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, fixtures.FakeRuntime([])), \
             patch("src.agent.main.gather_terraform_evidence", new=AsyncMock(side_effect=[evidence("/terraform/old.tf"), evidence("/terraform/new.tf")])) as collect, \
             patch("builtins.print") as output:
            await session.request("Review Terraform")
            await session.request("the Terraform module", follow_up=True)
        self.assertEqual(collect.await_count, 2)
        self.assertIn("Review could not be completed", output.call_args.args[0])
        self.assertIsNone(session.context)

    async def test_five_ado_calls_per_request_start_with_fresh_budget(self):
        executions = []
        runtime = fixtures.FakeRuntime([fixtures.repository_tool(), fixtures.json_schema_repo_file_tool(executions)])
        responses = []
        for _ in range(2):
            for index in range(5):
                responses.append(fixtures.completion(tool_calls=[fixtures.model_tool_call("repo_repository",
                    json.dumps({"action": "list", "top": index + 1}), f"read-{index}")]))
            responses.append(fixtures.completion("Fresh repositories listed"))
        client = fixtures.FakeOpenAIClient(responses)
        session = TaskSession()
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, runtime), patch("builtins.print"):
            await session.request("Find repository app")
            await session.request("that repository", follow_up=True)
        self.assertEqual(client.create.await_count, 12)
        self.assertEqual(runtime.close_calls, 2)
