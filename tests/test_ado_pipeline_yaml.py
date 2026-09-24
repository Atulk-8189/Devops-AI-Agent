"""Mocked deterministic workflow; no live model or MCP server."""
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mcp.types import CallToolResult, Tool

import test_openai_flow as fixtures
from src.agent.orchestration import RequestServices, orchestrate_request
from src.agent.workflows import route_question


class PipelineYAMLTests(unittest.IsolatedAsyncioTestCase):
    question = ('Find the Azure DevOps pipeline named "Task Manager - DB Bootstrap" in project '
                '"My Project", then show me the YAML pipeline file it uses from the main branch.')
    pipeline = {"name": "Task Manager - DB Bootstrap", "repository": {
        "name": "application", "id": "verified-id", "type": "TfsGit"},
        "process": {"yamlFilename": "pipelines/bootstrap.yml"}}
    repositories = [{"name": "application", "id": "verified-id"}]

    async def run_request(self, results, question=None):
        session = SimpleNamespace(call_tool=AsyncMock(side_effect=[
            result if isinstance(result, CallToolResult) else CallToolResult(
                content=[{"type": "text", "text": result if isinstance(result, str) else json.dumps(result)}])
            for result in results]))
        schemas = {"pipelines_definition": fixtures.PIPELINE_DEFINITION_SCHEMA,
                   "repo_repository": fixtures.REPOSITORY_SCHEMA,
                   "repo_file": fixtures.RepositoryFileReadTests.schema}
        runtime = fixtures.FakeRuntime([convert_mcp_tool_to_langchain_tool(
            session, Tool(name=name, inputSchema=schema)) for name, schema in schemas.items()])
        client = fixtures.FakeOpenAIClient([fixtures.completion("YAML evidence inspected from main.")])
        services = RequestServices(load_environment=Mock(),
            load_settings=Mock(return_value=fixtures.GENERIC_SETTINGS),
            client_factory=Mock(return_value=client), runtime_factory=Mock(return_value=runtime))
        with self.assertLogs("src.observability", level="INFO") as logs:
            result = await orchestrate_request(question or self.question, services=services)
        events = [json.loads(record.getMessage()) for record in logs.records]
        self.assertEqual(len({event["request_id"] for event in events}), 1)
        self.assertIn("azure_devops", [event.get("route") for event in events])
        client.close.assert_awaited_once()
        return result, session.call_tool, client

    async def test_exact_selection_application_id_and_one_final_model_call(self):
        other = dict(self.pipeline, name="other")
        result, execute, client = await self.run_request(
            [[other, self.pipeline], self.repositories, "trigger: none"])
        self.assertIn("main", result.answer)
        self.assertEqual([call.args[0] for call in execute.await_args_list],
                         ["pipelines_definition", "repo_repository", "repo_file"])
        args = execute.await_args_list[-1].args[1]
        self.assertEqual(args["repositoryId"], "verified-id")
        self.assertNotEqual(args["repositoryId"], "application")
        self.assertEqual((args["project"], args["version"], args["versionType"]),
                         ("My Project", "main", "Branch"))
        self.assertEqual(args["path"], "/pipelines/bootstrap.yml")
        client.create.assert_awaited_once()
        request = client.create.await_args.kwargs
        self.assertNotIn("tools", request)
        data = json.loads(request["messages"][-1]["content"])
        self.assertEqual(len(data["evidence"]), 3)
        self.assertIn("trigger: none", json.dumps(data))

    async def test_missing_or_ambiguous_pipeline_stops_before_repository(self):
        for rows, reason in [([], "pipeline_not_found"),
                             ([self.pipeline, self.pipeline], "pipeline_ambiguous")]:
            with self.subTest(reason=reason):
                result, execute, client = await self.run_request([rows])
                self.assertIn(reason, result.answer)
                self.assertEqual(execute.await_count, 1)
                client.create.assert_not_awaited()

    async def test_repository_must_be_complete_exact_and_consistent(self):
        for rows in [[], [{"name": "other", "id": "verified-id"}],
                     self.repositories + [{"name": "application", "id": "different"}],
                     [{"name": "application", "id": "different"}]]:
            with self.subTest(rows=rows):
                result, execute, client = await self.run_request([[self.pipeline], rows])
                self.assertIn("incomplete", result.answer)
                self.assertEqual(execute.await_count, 2)
                client.create.assert_not_awaited()

    async def test_pipeline_id_alone_cannot_authorize_file_read(self):
        pipeline = deepcopy(self.pipeline)
        del pipeline["repository"]["name"]
        result, execute, client = await self.run_request([[pipeline]])
        self.assertIn("metadata_unavailable", result.answer)
        self.assertEqual(execute.await_count, 1)
        client.create.assert_not_awaited()

    async def test_discovery_pages_supply_id_without_pipeline_id(self):
        pipeline = deepcopy(self.pipeline)
        del pipeline["repository"]["id"]
        page = [{"name": "application", "id": "verified-id"}] * 100
        _, execute, client = await self.run_request([[pipeline], page, [], "trigger: none"])
        self.assertEqual(execute.await_count, 4)
        self.assertEqual(execute.await_args_list[2].args[1]["skip"], 100)
        self.assertEqual(execute.await_args_list[-1].args[1]["repositoryId"], "verified-id")
        client.create.assert_awaited_once()

    async def test_file_failure_is_safe_and_does_not_summarize_success(self):
        error = CallToolResult(isError=True, content=[{"type": "text", "text": "not found /private/internal"}])
        result, execute, client = await self.run_request([[self.pipeline], self.repositories, error])
        self.assertIn("tool_failed", result.answer)
        self.assertNotIn("/private", result.answer)
        self.assertEqual(execute.await_count, 3)
        client.create.assert_not_awaited()

    async def test_final_evidence_bounded_and_untrusted(self):
        _, _, client = await self.run_request([[self.pipeline], self.repositories, "x" * 50000])
        data = json.loads(client.create.await_args.kwargs["messages"][-1]["content"])
        self.assertTrue(data["evidence"][-1]["truncated"])
        self.assertTrue(all(item["untrusted_data"] for item in data["evidence"]))
        self.assertLess(len(json.dumps(data)), 12000)

    def test_narrow_route_preserves_generic_requests(self):
        self.assertEqual(route_question(self.question), "azure_devops")
        self.assertEqual(route_question("List pipelines"), "generic")
        self.assertEqual(route_question("Find Task Manager's pipeline and show its YAML."), "generic")

    async def test_project_policy_still_blocks_before_dispatch(self):
        with self.assertRaises(PermissionError):
            await self.run_request([], self.question.replace('"My Project"', '"Other Project"'))
