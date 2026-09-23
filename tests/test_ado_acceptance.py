"""Request-level acceptance with scripted model choices, never live reasoning/API tests."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mcp.types import CallToolResult, Tool as MCPTool

import test_openai_flow as fixtures
from src.agent.orchestration import RequestServices, orchestrate_request
from src.agent.workflows import MAX_TOOL_EXECUTIONS, TOOL_BUDGET_EXHAUSTED_RESPONSE, route_question
from src.request_budget import current_budget
from src.tool_results import MAX_TOOL_RESULT_CHARS


class ADOAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    question = "Find Task Manager's pipeline and show its YAML."
    data = fixtures.AzureDevOpsInvestigationTests

    async def run_request(self, plans, results, answer=None):
        session = SimpleNamespace(call_tool=AsyncMock(side_effect=[
            CallToolResult(content=[{"type": "text", "text": value if isinstance(value, str)
                                     else json.dumps(value)}]) for value in results
        ]))
        schemas = {"pipelines_definition": fixtures.PIPELINE_DEFINITION_SCHEMA,
                   "repo_repository": fixtures.REPOSITORY_SCHEMA,
                   "repo_file": fixtures.RepositoryFileReadTests.schema}
        runtime = fixtures.FakeRuntime([
            convert_mcp_tool_to_langchain_tool(session, MCPTool(name=name, inputSchema=schema))
            for name, schema in schemas.items()
        ])
        responses = [fixtures.completion(tool_calls=[fixtures.model_tool_call(
            name, json.dumps(args), f"acceptance-{index}")])
            for index, (name, args) in enumerate(plans)]
        if answer is not None:
            responses.append(fixtures.completion(answer))
        client = fixtures.FakeOpenAIClient(responses)
        budgets = []

        def route(question):
            budgets.append(current_budget())
            return route_question(question)

        router = Mock(side_effect=route)
        services = RequestServices(
            load_environment=Mock(), load_settings=Mock(return_value=fixtures.GENERIC_SETTINGS),
            client_factory=Mock(return_value=client), runtime_factory=Mock(return_value=runtime),
            router=router,
        )
        with patch("src.agent.workflows.handle_aks", new=AsyncMock()) as aks, \
                patch("src.agent.workflows.handle_terraform", new=AsyncMock()) as terraform, \
                self.assertLogs("src.observability", level="INFO") as captured:
            result = await orchestrate_request(self.question, services=services)
        aks.assert_not_awaited()
        terraform.assert_not_awaited()
        router.assert_called_once_with(self.question)
        client.close.assert_awaited_once()
        self.assertEqual((runtime.initialize_calls, runtime.close_calls), (1, 1))
        self.assertIsNone(current_budget())
        records = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual(records[0]["event"], "request_started")
        self.assertEqual(records[-1]["event"], "request_completed")
        self.assertEqual(len({record["request_id"] for record in records}), 1)
        self.assertTrue(records[0]["request_id"])
        self.assertEqual([r["route"] for r in records if r["event"] == "route_selected"], ["generic"])
        dispatches = [r for r in records if r["event"] == "mcp_dispatch"]
        self.assertEqual(len(dispatches), session.call_tool.await_count)
        for index, (event, call) in enumerate(zip(dispatches, session.call_tool.await_args_list)):
            name, args = call.args[:2]
            self.assertEqual(event["tool_call_id"], f"acceptance-{index}")
            self.assertEqual(event["tool_name"], name)
            self.assertEqual(event["mcp_server"], "azure-devops")
            self.assertEqual(event["route"], "generic")
            self.assertEqual(args["project"], "My Project")
            self.assertIn(args["action"], {"list", "list_directory", "get_content"})
            if name == "repo_file":
                self.assertEqual(args["repositoryId"], "discovered-id")
                self.assertEqual((args["version"], args["versionType"]), ("main", "Branch"))
            correlated = [r for r in records if r.get("tool_call_id") == event["tool_call_id"]]
            for expected in ("tool_attempt", "policy_decision", "mcp_result", "evidence_result"):
                self.assertTrue(any(r["event"] == expected for r in correlated), expected)
            self.assertTrue(any(r.get("policy_decision") == "allow" for r in correlated))
        serialized = json.dumps(records)
        for private in (self.question, self.data.yaml, "discovered-id", "/pipelines/deploy.yml", "test-key"):
            self.assertNotIn(private, serialized)
        if answer:
            self.assertNotIn(answer, serialized)
        return result, session.call_tool, client, records, budgets[0]

    def envelopes(self, client):
        return [json.loads(message["content"][0]["text"])
                for message in client.create.await_args.kwargs["messages"] if message["role"] == "tool"]

    async def test_success_pipeline_discovery_to_main_yaml(self):
        listing = {"items": [{"path": self.data.file_call[1]["path"], "isFolder": False}]}
        plans = [self.data.pipeline_call, self.data.repository_call,
                 self.data.directory_call, self.data.file_call]
        result, execute, client, records, budget = await self.run_request(
            plans, [self.data.pipelines, self.data.repositories, listing, self.data.yaml], self.data.answer)
        self.assertEqual([c.args[0] for c in execute.await_args_list], [p[0] for p in plans])
        self.assertEqual([c.args[1]["action"] for c in execute.await_args_list],
                         ["list", "list", "list_directory", "get_content"])
        evidence = self.envelopes(client)
        self.assertEqual(json.loads(evidence[0]["content"][0]["text"]), self.data.pipelines)
        discovery = json.loads(evidence[1]["content"][-1]["text"])["repository_discovery"]
        self.assertEqual(discovery["repository_id"], "discovered-id")
        self.assertEqual(json.loads(evidence[2]["content"][0]["text"]), listing)
        self.assertEqual(evidence[3]["content"][0]["text"], self.data.yaml)
        for envelope in evidence:
            self.assertEqual(envelope["status"], "success")
            self.assertTrue(envelope["untrusted_data"])
            self.assertFalse(envelope["truncated"])
            self.assertLessEqual(len(json.dumps(envelope["content"])), MAX_TOOL_RESULT_CHARS)
        # Scripted answer assertions check evidence plumbing/qualification, not model reasoning.
        for fact in (self.data.pipelines[0]["name"], self.data.file_call[1]["path"], "main", "trigger: none"):
            self.assertIn(fact, result.answer)
        self.assertIn("could not be established", result.answer)
        self.assertEqual(client.create.await_count, 5)
        self.assertEqual(budget.usage.mcp_dispatches, 4)
        self.assertEqual(sum(r["event"] == "model_call_completed" for r in records), 5)

    async def test_ambiguous_repository_blocks_directory_and_file_attempts(self):
        ambiguous = [*self.data.repositories, {"id": "other-id", "name": "task-manager"}]
        answer = "Repository selection is ambiguous; clarify the repository. No YAML was inspected."
        result, execute, client, records, _ = await self.run_request(
            [self.data.pipeline_call, self.data.repository_call, self.data.directory_call, self.data.file_call],
            [self.data.pipelines, ambiguous], answer)
        self.assertEqual([c.args[0] for c in execute.await_args_list], ["pipelines_definition", "repo_repository"])
        self.assertEqual(sum(c.args[0] == "repo_file" for c in execute.await_args_list), 0)
        feedback = [call.kwargs["messages"][-1]["content"][0]["text"]
                    for call in client.create.await_args_list[2:]]
        self.assertIn("ambiguous", feedback[0])
        self.assertTrue(all("discovery required" in text.lower() for text in feedback[1:]))
        self.assertIn("ambiguous", result.answer.lower())
        self.assertIn("No YAML", result.answer)
        self.assertEqual(client.create.await_count, 5)
        self.assertFalse(any(r["event"] == "mcp_dispatch" and r.get("tool_name") == "repo_file" for r in records))

    async def test_five_tool_limit_retains_evidence_and_blocks_yaml_dispatch(self):
        directories = [("repo_file", {**self.data.directory_call[1], "path": f"/candidate-{i}"})
                       for i in range(MAX_TOOL_EXECUTIONS - 2)]
        result, execute, client, records, budget = await self.run_request(
            [self.data.pipeline_call, self.data.repository_call, *directories, self.data.file_call],
            [self.data.pipelines, self.data.repositories, *({"items": []} for _ in directories)])
        self.assertEqual(execute.await_count, MAX_TOOL_EXECUTIONS)
        self.assertEqual([c.args[0] for c in execute.await_args_list],
                         ["pipelines_definition", "repo_repository", *["repo_file"] * len(directories)])
        self.assertEqual([c.args[1]["path"] for c in execute.await_args_list[2:]],
                         [args["path"] for _, args in directories])
        self.assertEqual(budget.usage.mcp_dispatches, MAX_TOOL_EXECUTIONS)
        self.assertEqual(len([r for r in records if r["event"] == "mcp_dispatch"]), MAX_TOOL_EXECUTIONS)
        self.assertFalse(any(c.args[1]["action"] == "get_content" for c in execute.await_args_list))
        self.assertEqual(client.create.await_count, MAX_TOOL_EXECUTIONS + 1)
        self.assertEqual(result.answer, TOOL_BUDGET_EXHAUSTED_RESPONSE)
        self.assertEqual(len(budget.evidence), MAX_TOOL_EXECUTIONS)
        self.assertIn("Task Manager deploy", json.dumps(budget.evidence))
        self.assertNotIn("echo deploy-task-manager", json.dumps(budget.evidence))
        for envelope in budget.evidence:
            self.assertTrue(envelope["untrusted_data"])
            self.assertEqual(envelope["status"], "success")
        self.assertEqual(len(self.envelopes(client)), MAX_TOOL_EXECUTIONS)
        self.assertTrue(any(r["event"] == "execution_stopped" and r.get("reason_code") == "budget_exhausted"
                            and r.get("remaining_budget") == 0 for r in records))
