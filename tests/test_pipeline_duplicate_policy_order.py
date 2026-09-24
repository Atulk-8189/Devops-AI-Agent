"""Pipeline repeat behavior: policy validation precedes duplicate detection."""
import json
import unittest
from unittest.mock import patch

import test_openai_flow as fixtures
from src.agent.main import DUPLICATE_TOOL_CALL_RESPONSE, main


class PipelineDuplicatePolicyOrderTests(unittest.IsolatedAsyncioTestCase):
    async def run_pair(self, first, second, *, expect_duplicate=False, expected_reason=None):
        executions = []

        async def execute_pipeline(action, project):
            executions.append((action, project))
            return "pipeline: Task Manager - DB Bootstrap"

        from langchain_core.tools import StructuredTool

        pipeline_tool = StructuredTool(
            name="pipelines_definition",
            description="List pipeline definitions for a project.",
            args_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "project": {"type": "string"},
                },
                "required": ["action", "project"],
                "additionalProperties": False,
            },
            coroutine=execute_pipeline,
        )
        client = fixtures.FakeOpenAIClient([
            fixtures.completion(tool_calls=[fixtures.model_tool_call(
                "pipelines_definition", json.dumps(first), "first",
            )]),
            fixtures.completion(tool_calls=[fixtures.model_tool_call(
                "pipelines_definition", json.dumps(second), "second",
            )]),
        ])
        runtime = fixtures.FakeRuntime([pipeline_tool])
        fixture = fixtures.GenericAgentFlowTests()

        with fixture.generic_patches(client, runtime), patch("builtins.print") as output:
            with self.assertLogs("src.observability", level="INFO") as logs:
                if expected_reason:
                    with self.assertRaises(PermissionError):
                        await main("Find the Task Manager pipeline")
                else:
                    await main("Find the Task Manager pipeline")

        records = [json.loads(record.getMessage()) for record in logs.records]
        dispatches = [record for record in records if record["event"] == "mcp_dispatch"]
        decisions = [record for record in records if record["event"] == "policy_decision"]
        if expect_duplicate:
            output.assert_called_once_with(DUPLICATE_TOOL_CALL_RESPONSE)
            self.assertEqual([record["policy_decision"] for record in decisions], ["allow", "allow"])
            self.assertEqual(
                [record["reason_code"] for record in records if record["event"] == "execution_stopped"],
                ["duplicate_call"],
            )
        else:
            output.assert_not_called()
            self.assertEqual(decisions[-1]["reason_code"], expected_reason)
            self.assertEqual(decisions[-1]["policy_decision"], "reject")
        self.assertEqual(executions, [("list", "My Project")])
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(client.create.await_count, 2)
        self.assertEqual(runtime.close_calls, 1)
        return records

    async def test_identical_valid_call_reaches_duplicate_detection(self):
        arguments = {"action": "list", "project": "My Project"}
        await self.run_pair(arguments, arguments, expect_duplicate=True)

    async def test_list_revisions_is_rejected_before_dispatch(self):
        await self.run_pair(
            {"action": "list", "project": "My Project"},
            {"action": "list_revisions", "project": "My Project"},
            expected_reason="action_not_allowed",
        )

    async def test_missing_action_is_rejected_before_dispatch(self):
        await self.run_pair(
            {"action": "list", "project": "My Project"},
            {"project": "My Project"},
            expected_reason="action_not_allowed",
        )

    async def test_wrong_project_is_rejected_before_dispatch(self):
        await self.run_pair(
            {"action": "list", "project": "My Project"},
            {"action": "list", "project": "Other Project"},
            expected_reason="project_mismatch",
        )

    async def test_duplicate_detection_uses_policy_normalized_arguments(self):
        await self.run_pair(
            {"action": "list"},
            {"action": "list", "project": "My Project"},
            expect_duplicate=True,
        )
