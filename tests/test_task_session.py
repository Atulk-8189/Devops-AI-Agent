import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, ToolMessage

import test_openai_flow as fixtures
from src.agent.aks_troubleshooting import AKSDiagnosis, TroubleshootingEvidence
from src.agent.task_session import TaskSession, progress, hint_text
from src.agent.context import Observation
from src.review.terraform_evidence import TerraformEvidence


class TaskSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_aks_followup_collects_fresh_and_passes_only_hints(self):
        session = TaskSession()
        runtime = fixtures.FakeRuntime([])
        client = fixtures.FakeOpenAIClient([])
        diagnosis = AKSDiagnosis(summary="Unknown", observed_evidence=[], likely_root_causes=[], recommended_next_diagnostic_step="Collect evidence")
        evidence = [TroubleshootingEvidence(), TroubleshootingEvidence()]
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, runtime), \
             patch("src.agent.main.collect_task_manager_evidence", new=AsyncMock(side_effect=evidence)) as collect, \
             patch("src.agent.main.diagnose_aks", new=AsyncMock(return_value=diagnosis)) as diagnose, patch("builtins.print"):
            await session.request("Investigate Task Manager")
            first_id = session.context.task_id
            await session.request("Now check the service", follow_up=True)
        self.assertEqual(collect.await_count, 2)
        self.assertIs(diagnose.await_args_list[1].args[2], evidence[1])
        hints = diagnose.await_args.kwargs["task_hints"]
        self.assertIn("task-manager", hints)
        self.assertNotIn('"observations"', hints)
        self.assertEqual(session.context.task_id, first_id)
        self.assertEqual(session.context.current_objective, "Now check the service")
        self.assertEqual(runtime.close_calls, 2)

    async def test_terraform_followup_recollects_current_source(self):
        def messages(source):
            evidence = TerraformEvidence()
            evidence.discovery.update(repository_id="repo-1", selected_files=["/terraform/main.tf"])
            evidence.extend([AIMessage(content="", tool_calls=[{"id": "read", "name": "repo_file", "args": {"action": "get_content", "path": "/terraform/main.tf"}}]),
                             ToolMessage(content=source, tool_call_id="read")])
            return evidence
        client = fixtures.FakeOpenAIClient([fixtures.completion('{"findings":[]}')] * 2)
        runtime = fixtures.FakeRuntime([])
        session = TaskSession()
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, runtime), \
             patch("src.agent.main.gather_terraform_evidence", new=AsyncMock(side_effect=[messages('variable "old" {}'), messages('variable "fresh" {}')])) as collect, \
             patch("builtins.print"):
            await session.request("Review the Terraform configuration")
            await session.request("Now look specifically at the networking", follow_up=True)
        self.assertEqual(collect.await_count, 2)
        prompt = client.create.await_args.kwargs["messages"][1]["content"][0]["text"]
        self.assertIn('variable "fresh" {}', prompt)
        self.assertNotIn('variable "old" {}', prompt)
        self.assertIn("Review the Terraform configuration", prompt)
        self.assertIn("/terraform/main.tf", prompt)
        self.assertNotIn("variable", session.context.model_dump_json())

    async def test_ado_fresh_discovery_and_file_reads_each_request(self):
        listing = AsyncMock(return_value=[{"type": "text", "text": '[{"name":"app","id":"repo-123"}]'}])
        executions = []
        runtime = fixtures.FakeRuntime([fixtures.repository_tool(execute=listing), fixtures.json_schema_repo_file_tool(executions)])
        responses = []
        for _ in range(2):
            responses.extend([fixtures.discovery_completion(), fixtures.completion(tool_calls=[fixtures.model_tool_call("repo_file", '{"action":"get_content","repositoryId":"repo-123"}', "file")]), fixtures.completion("Fresh file read")])
        client = fixtures.FakeOpenAIClient(responses)
        session = TaskSession()
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, runtime), patch("builtins.print"):
            await session.request("Find the repository configuration")
            await session.request("Show that file again", follow_up=True)
        self.assertEqual(listing.await_count, 2)
        self.assertEqual(len(executions), 2)
        second_prompt = client.create.await_args_list[3].kwargs["messages"][1]["content"][0]["text"]
        self.assertIn("repo-123", second_prompt)
        self.assertIn("discovery again", second_prompt)

    async def test_remembered_id_cannot_authorize_file_access(self):
        session = TaskSession()
        with patch("src.agent.main.main", new=AsyncMock(return_value=progress("azure_devops", repository_id="repo-123", completed=("repository_discovery",)))):
            await session.request("Find the repository")
        executions = []
        runtime = fixtures.FakeRuntime([fixtures.json_schema_repo_file_tool(executions)])
        client = fixtures.FakeOpenAIClient([fixtures.completion(tool_calls=[fixtures.model_tool_call("repo_file", '{"action":"get_content","repositoryId":"repo-123"}', "file")]), fixtures.completion("No evidence")])
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, runtime), patch("builtins.print") as output:
            await session.request("Show that file", follow_up=True)
        self.assertEqual(executions, [])
        self.assertIn("No fresh evidence", output.call_args.args[0])

    async def test_context_cannot_bypass_project_policy(self):
        session = TaskSession()
        client = fixtures.FakeOpenAIClient([fixtures.completion(tool_calls=[fixtures.model_tool_call("repo_repository", '{"action":"list","project":"Other"}', "bad")])])
        runtime = fixtures.FakeRuntime([fixtures.repository_tool()])
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, runtime), self.assertRaises(PermissionError):
            await session.request("Find the repository")
        self.assertIsNone(session.context)

    async def test_isolation_reset_expiry_and_unrelated_topic(self):
        now = datetime.now(timezone.utc)
        clock = [now]
        session, other = TaskSession(clock=lambda: clock[0]), TaskSession()
        run = AsyncMock(return_value=progress("aks", completed=("deployment_health",)))
        with patch("src.agent.main.main", new=run):
            await session.request("Investigate Task Manager")
            self.assertIsNone(other.context)
            session.reset()
            await session.request("Now check the service", follow_up=True)
            self.assertEqual(run.await_args.kwargs["task_context"].topic, "generic")
            await session.request("Investigate Task Manager")
            clock[0] += timedelta(minutes=15)
            await session.request("Now check the service", follow_up=True)
            self.assertEqual(run.await_args.kwargs["task_context"].topic, "generic")
            await session.request("Investigate Task Manager")
            await session.request("Review Terraform", follow_up=True)
            selected = run.await_args.kwargs["task_context"]
            self.assertEqual(selected.topic, "terraform")
            self.assertIsNone(selected.scope.workload_hint)

    async def test_sensitive_question_not_retained_and_history_not_passed(self):
        session = TaskSession()
        with patch("src.agent.main.main", new=AsyncMock(return_value=progress("aks", completed=("pod_health",)))) as run:
            await session.request("Investigate Task Manager")
            context = session.context
            stale = Observation(kind="observed_fact", check="pod_health", outcome="healthy", source="aks_collector",
                scope=context.scope, observed_at=context.last_activity_at, completeness="complete")
            context = context.model_copy(update={"observations": (stale,)})
            self.assertNotIn("healthy", hint_text(context))
            await session.request("password=private-value", follow_up=True)
            self.assertIsNone(session.context)
            self.assertEqual(run.await_args.kwargs, {})

    async def test_failure_does_not_keep_or_refresh_context(self):
        session = TaskSession()
        with patch("src.agent.main.main", new=AsyncMock(return_value=None)):
            await session.request("Review Terraform")
        self.assertIsNone(session.context)
