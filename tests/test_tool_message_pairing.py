"""Offline graph regressions that enforce the provider's pairing contract."""
import unittest
from unittest.mock import patch

import test_openai_flow as flow
from src.model_request_diagnostics import request_structure


class ToolMessagePairingTests(unittest.IsolatedAsyncioTestCase):
    async def investigate(self, plans, results):
        fixture = flow.AzureDevOpsInvestigationTests()
        original = flow.FakeOpenAIClient

        def checked_client(responses):
            client = original(responses)
            self.last_client = client
            replies = iter(responses)

            async def create(**kwargs):
                self.assertTrue(request_structure(kwargs["messages"])["tool_pairing_valid"])
                # Independently check exact multiplicity, IDs and immediate ordering.
                pending, seen = set(), set()
                for message in kwargs["messages"]:
                    if message["role"] == "tool":
                        identifier = message["tool_call_id"]
                        self.assertIn(identifier, pending)
                        pending.remove(identifier)
                    else:
                        self.assertFalse(pending)
                        for call in message.get("tool_calls", []):
                            self.assertNotIn(call["id"], seen)
                            seen.add(call["id"])
                            pending.add(call["id"])
                self.assertFalse(pending)
                return next(replies)

            client.create.side_effect = create
            return client

        with patch.object(flow, "FakeOpenAIClient", side_effect=checked_client):
            return await fixture.investigate(plans, results, answer="Only fresh evidence inspected.")

    async def test_live_sequence_discovery_rejection_retains_assistant_call(self):
        fixture = flow.AzureDevOpsInvestigationTests()
        name, args = fixture.file_call
        execute, client, _ = await self.investigate(
            [fixture.pipeline_call, fixture.repository_call, (name, {**args, "repositoryId": "invented"}), fixture.file_call],
            [fixture.pipelines, fixture.repositories, fixture.yaml])
        history = client.create.await_args_list[3].kwargs["messages"]
        self.assertEqual(len(history), 8)
        self.assertEqual([m["role"] for m in history[-2:]], ["assistant", "tool"])
        self.assertEqual(history[-2]["tool_calls"][0]["function"]["name"], "repo_file")
        self.assertEqual(len(history[-1]["content"][0]["text"]), 128)
        self.assertIn("Repository discovery required", history[-1]["content"][0]["text"])
        self.assertEqual([c.args[0] for c in execute.await_args_list],
                         ["pipelines_definition", "repo_repository", "repo_file"])
        self.assertEqual(client.create.await_count, 5)

    async def test_schema_validation_error_is_paired_and_can_recover(self):
        fixture = flow.AzureDevOpsInvestigationTests()
        name, args = fixture.file_call
        execute, client, _ = await self.investigate(
            [fixture.pipeline_call, fixture.repository_call, (name, {**args, "unknown": True}), fixture.file_call],
            [fixture.pipelines, fixture.repositories, fixture.yaml])
        history = client.create.await_args_list[3].kwargs["messages"]
        self.assertIn("Tool arguments", history[-1]["content"][0]["text"])
        self.assertEqual(execute.await_count, 3)

    async def test_recoverable_tool_error_remains_paired(self):
        fixture = flow.AzureDevOpsInvestigationTests()
        execute, client, _ = await self.investigate(
            [fixture.pipeline_call, fixture.repository_call, fixture.file_call],
            [fixture.pipelines, fixture.repositories, RuntimeError("temporary failure")])
        self.assertEqual(execute.await_count, 3)
        self.assertIn("failed", client.create.await_args.kwargs["messages"][-1]["content"][0]["text"])

    async def test_multiple_calls_duplicate_and_budget_stops_do_not_orphan(self):
        fixture = flow.AzureDevOpsInvestigationTests()
        for plans, results, dispatches in (
            ([[fixture.pipeline_call, fixture.repository_call], fixture.pipeline_call], [fixture.pipelines], 1),
            ([fixture.pipeline_call, fixture.pipeline_call], [fixture.pipelines], 1),
            ([("pipelines_definition", {"action": "list", "name": str(i)}) for i in range(6)],
             [fixture.pipelines] * 5, 5),
        ):
            with self.subTest(dispatches=dispatches):
                execute, _, _ = await self.investigate(plans, results)
                self.assertEqual(execute.await_count, dispatches)

    async def test_policy_rejection_remains_hard_failure_without_followup(self):
        with self.assertRaises(PermissionError):
            await self.investigate([("pipelines_definition", {"action": "list_revisions"})], [])
        self.assertEqual(self.last_client.create.await_count, 1)

    async def test_allowed_but_unavailable_tool_error_is_paired(self):
        client = flow.FakeOpenAIClient([
            flow.completion(tool_calls=[flow.model_tool_call("pipelines_definition", '{"action":"list"}', "missing")]),
            flow.completion("Tool unavailable.")])
        fixture = flow.GenericAgentFlowTests()
        with fixture.generic_patches(client, flow.FakeRuntime([])), patch("builtins.print"):
            await flow.main("List pipelines")
        history = client.create.await_args.kwargs["messages"]
        self.assertTrue(request_structure(history)["tool_pairing_valid"])
        self.assertEqual([m["role"] for m in history[-2:]], ["assistant", "tool"])
        self.assertEqual(history[-2]["tool_calls"][0]["id"], history[-1]["tool_call_id"])
