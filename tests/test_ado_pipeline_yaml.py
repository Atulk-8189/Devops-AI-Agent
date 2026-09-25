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
from src.agent.ado_pipeline_yaml import pipeline_yaml_request


class PipelineYAMLTests(unittest.IsolatedAsyncioTestCase):
    question = ('Find the Azure DevOps pipeline named "Task Manager - DB Bootstrap" in project '
                '"My Project", then show me the YAML pipeline file it uses from the main branch.')
    pipeline = {"name": "Task Manager - DB Bootstrap", "repository": {
        "name": "application", "id": "verified-id", "type": "TfsGit"},
        "process": {"yamlFilename": "pipelines/bootstrap.yml"}}
    repositories = [{"name": "application", "id": "verified-id"}]

    async def run_request(self, results, question=None, answer=None):
        session = SimpleNamespace(call_tool=AsyncMock(side_effect=[
            result if isinstance(result, CallToolResult) else CallToolResult(
                content=[{"type": "text", "text": result if isinstance(result, str) else json.dumps(result)}])
            for result in results]))
        schemas = {"pipelines_definition": fixtures.PIPELINE_DEFINITION_SCHEMA,
                   "repo_repository": fixtures.REPOSITORY_SCHEMA,
                   "repo_file": fixtures.RepositoryFileReadTests.schema}
        runtime = fixtures.FakeRuntime([convert_mcp_tool_to_langchain_tool(
            session, Tool(name=name, inputSchema=schema)) for name, schema in schemas.items()])
        model_answer = answer if answer is not None else "YAML evidence inspected from main."
        client = fixtures.FakeOpenAIClient([fixtures.completion(model_answer)])
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
        _, _, client = await self.run_request([[self.pipeline], self.repositories, "x" * 70000])
        data = json.loads(client.create.await_args.kwargs["messages"][-1]["content"])
        self.assertTrue(data["evidence"][-1]["truncated"])
        self.assertTrue(all(item["untrusted_data"] for item in data["evidence"]))
        self.assertLess(len(json.dumps(data)), 75000)

    async def test_sufficient_yaml_evidence_within_request_budget(self):
        content = "steps:\n- script: echo build\n" + "# padding\n" * 2500
        result, _, client = await self.run_request([[self.pipeline], self.repositories, content])
        data = json.loads(client.create.await_args.kwargs["messages"][-1]["content"])
        self.assertFalse(data["evidence"][-1]["truncated"])
        self.assertFalse(data["yaml_truncated"])
        self.assertNotIn("truncation_warning", data)
        self.assertEqual(data["evidence"][-1]["content"][0]["text"], content)
        self.assertIn("main", result.answer)

    async def test_truncated_evidence_identifies_unverified_operations(self):
        result, _, client = await self.run_request(
            [[self.pipeline], self.repositories, "steps:\n- script: echo build\n" + "#" * 70000],
            answer="The pipeline contains an initial build step running echo build.",
        )
        data = json.loads(client.create.await_args.kwargs["messages"][-1]["content"])
        self.assertTrue(data["yaml_truncated"])
        self.assertIn("truncation_warning", data)
        self.assertIn("truncated", result.answer.lower())
        self.assertIn("could not be verified", result.answer.lower())
        self.assertIn("SQL operations", result.answer)

    async def test_truncated_evidence_model_identifies_unverified_operations(self):
        answer = ("Observed behavior: Build step running echo build. "
                  "Truncated evidence notice: Evidence was truncated; later stages and SQL operations "
                  "could not be verified.")
        result, _, client = await self.run_request(
            [[self.pipeline], self.repositories, "steps:\n- script: echo build\n" + "#" * 70000],
            answer=answer,
        )
        self.assertEqual(result.answer, answer)

    async def test_unsupported_claims_unobserved_sql_rejected(self):
        unsupported_answer = "Confirmed: The pipeline executes SQL operations to initialize the database."
        result, _, _ = await self.run_request(
            [[self.pipeline], self.repositories, "steps:\n- script: echo build"],
            answer=unsupported_answer,
        )
        self.assertIn("Investigation incomplete", result.answer)
        self.assertIn("unobserved SQL operations", result.answer)
        self.assertIn("No missing evidence was inferred", result.answer)

    async def test_unsupported_claims_unobserved_resource_changes_rejected(self):
        unsupported_answer = "The pipeline is confirmed to perform infrastructure changes and provision resources."
        result, _, _ = await self.run_request(
            [[self.pipeline], self.repositories, "steps:\n- script: echo build"],
            answer=unsupported_answer,
        )
        self.assertIn("Investigation incomplete", result.answer)
        self.assertIn("unobserved resource changes", result.answer)
        self.assertIn("No missing evidence was inferred", result.answer)

    async def test_distinguish_verified_behavior_from_inference_accepted(self):
        grounded_answer = (
            "Verified YAML behavior: The pipeline runs a build script echo build on the main branch.\n"
            "Inferences: Database operations and infrastructure changes were not observed in the YAML evidence "
            "and could not be verified."
        )
        result, _, _ = await self.run_request(
            [[self.pipeline], self.repositories, "steps:\n- script: echo build"],
            answer=grounded_answer,
        )
        self.assertEqual(result.answer, grounded_answer)

    async def test_grounding_system_prompt_rules(self):
        _, _, client = await self.run_request([[self.pipeline], self.repositories, "steps:\n- script: echo build"])
        messages = client.create.await_args.kwargs["messages"]
        system_content = messages[0]["content"]
        self.assertIn("distinguish verified yaml behavior from inference", system_content.lower())
        self.assertIn("never describe unobserved pipeline stages, sql operations, or resource changes as confirmed facts", system_content.lower())
        self.assertIn("if evidence is truncated, explicitly identify what could not be verified", system_content.lower())

    def test_narrow_route_preserves_generic_requests(self):
        self.assertEqual(route_question(self.question), "azure_devops")
        self.assertEqual(route_question("List pipelines"), "generic")
        self.assertEqual(route_question("Find Task Manager's pipeline and show its YAML."), "generic")

    async def test_live_investigation_request_uses_deterministic_workflow(self):
        question = ("Investigate the Task Manager - DB Bootstrap pipeline in my Azure DevOps project. "
                    "Find its YAML file and summarize what it does, including its main stages "
                    "and any infrastructure changes it is configured to perform.")
        self.assertEqual(route_question(question), "azure_devops")
        self.assertEqual(pipeline_yaml_request(question), (self.pipeline["name"], "My Project"))
        _, execute, client = await self.run_request(
            [[self.pipeline], self.repositories, "trigger: none"], question)
        self.assertEqual([call.args[0] for call in execute.await_args_list],
                         ["pipelines_definition", "repo_repository", "repo_file"])
        self.assertEqual(execute.await_args_list[0].args[1]["name"], self.pipeline["name"])
        client.create.assert_awaited_once()

    def test_quoted_investigation_preserves_exact_name_and_project(self):
        question = ('Investigate the "Task Manager - DB Bootstrap" pipeline in project "Other Project". '
                    'Find its YAML file and summarize what it does.')
        self.assertEqual(pipeline_yaml_request(question), (self.pipeline["name"], "Other Project"))
        self.assertEqual(route_question(question), "azure_devops")

    def test_investigation_false_positives_remain_generic(self):
        base = ("Investigate the Task Manager - DB Bootstrap pipeline in my Azure DevOps project. "
                "Find its YAML file and summarize what it does")
        for question in (
            "Investigate the Task Manager - DB Bootstrap pipeline runs and logs.",
            "List all pipelines and their YAML files.",
            "Investigate the My Project repository and summarize its README.",
            base + " from the develop branch.",
            base + ". Then run the pipeline.",
            base.replace("Task Manager - DB Bootstrap", "any"),
        ):
            with self.subTest(question=question):
                self.assertIsNone(pipeline_yaml_request(question))
                self.assertEqual(route_question(question), "generic")

    async def test_project_policy_still_blocks_before_dispatch(self):
        with self.assertRaises(PermissionError):
            await self.run_request([], self.question.replace('"My Project"', '"Other Project"'))
