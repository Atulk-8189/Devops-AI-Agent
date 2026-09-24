"""Offline coverage for the verified repository ID file-access boundary."""
import json
import unittest
from unittest.mock import patch

import test_openai_flow as flow
from src.agent.main import main


class RepositoryIdBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, repository_id=None, *, discover=True, expected_feedback=None):
        file_executions = []
        tools = [flow.json_schema_repo_file_tool(file_executions)]
        responses = []
        if discover:
            tools.insert(0, flow.repository_tool([{"id": "repo-123", "name": "My Project"}]))
            responses.append(flow.discovery_completion())
        file_arguments = {"action": "get_content"}
        if repository_id is not None:
            file_arguments["repositoryId"] = repository_id
        responses.append(flow.completion(tool_calls=[flow.model_tool_call(
            "repo_file", json.dumps(file_arguments), "file-call",
        )]))
        responses.append(flow.completion("No file was accessed."))
        client = flow.FakeOpenAIClient(responses)
        runtime = flow.FakeRuntime(tools)
        fixture = flow.GenericAgentFlowTests()

        with fixture.generic_patches(client, runtime), patch("builtins.print") as output:
            with self.assertLogs("src.observability", level="INFO") as logs:
                await main("Read the repository file")

        records = [json.loads(record.getMessage()) for record in logs.records]
        dispatches = [record for record in records if record["event"] == "mcp_dispatch"]
        policy_decisions = [record for record in records if record["event"] == "policy_decision"]
        feedback = client.create.await_args_list[-1].kwargs["messages"][-1]["content"][0]["text"]
        return file_executions, dispatches, policy_decisions, records, feedback, output

    async def test_exact_verified_id_reaches_mcp(self):
        executions, dispatches, decisions, records, _, output = await self.run_case("repo-123")
        self.assertEqual(executions, [("get_content", "My Project", "repo-123", "main", "Branch")])
        self.assertEqual([record["tool_name"] for record in dispatches], ["repo_repository", "repo_file"])
        self.assertEqual([record["policy_decision"] for record in decisions], ["allow", "allow"])
        self.assertFalse(any(record["event"] == "schema_validation_failed" for record in records))
        output.assert_called_once_with("No file was accessed.")

    async def test_repository_name_is_blocked_before_mcp(self):
        executions, dispatches, decisions, records, feedback, _ = await self.run_case("My Project")
        self.assertEqual(executions, [])
        self.assertEqual([record["tool_name"] for record in dispatches], ["repo_repository"])
        self.assertEqual([record["policy_decision"] for record in decisions], ["allow", "allow"])
        self.assertFalse(any(record["tool_name"] == "repo_file" for record in dispatches))
        self.assertIn("Repository discovery required", feedback)
        self.assertFalse(any(record["event"] == "schema_validation_failed" for record in records))

    async def test_wrong_id_is_blocked_before_mcp(self):
        executions, dispatches, _, records, feedback, _ = await self.run_case("repo-999")
        self.assertEqual(executions, [])
        self.assertEqual([record["tool_name"] for record in dispatches], ["repo_repository"])
        self.assertIn("Repository discovery required", feedback)
        self.assertFalse(any(record["event"] == "schema_validation_failed" for record in records))

    async def test_missing_id_fails_schema_validation_before_prerequisite(self):
        executions, dispatches, _, records, feedback, _ = await self.run_case()
        self.assertEqual(executions, [])
        self.assertEqual([record["tool_name"] for record in dispatches], ["repo_repository"])
        self.assertIn("arguments for 'repo_file' are invalid", feedback)
        self.assertTrue(any(record["event"] == "schema_validation_failed" for record in records))

    async def test_empty_discovery_state_blocks_verified_id_before_mcp(self):
        executions, dispatches, _, records, feedback, _ = await self.run_case("repo-123", discover=False)
        self.assertEqual(executions, [])
        self.assertEqual(dispatches, [])
        self.assertIn("Repository discovery required", feedback)
        self.assertFalse(any(record["event"] == "schema_validation_failed" for record in records))
