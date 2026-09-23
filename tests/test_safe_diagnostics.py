import json
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

import test_openai_flow as fixtures
from src.agent.context import ContextScope
from src.agent.main import cli, main
from src.config import Settings, ConfigurationError
from src.mcp.runtime import MCPRuntimeError
from src.safe_diagnostics import classify_error, diagnostic_line, safe_fields, sensitive
from src.review.terraform_evidence import gather_terraform_evidence
from src.review.review_validation import retrieved_files


class SafeDiagnosticTests(unittest.TestCase):
    def test_categories_and_no_raw_error_details(self):
        cases = [(ValueError("secret=unique-leak"), None, "validation"),
                 (PermissionError("Tool call blocked by read-only policy secret=unique-leak"), None, "policy"),
                 (RuntimeError("401 Authorization: Bearer unique-leak"), "tool", "authentication"),
                 (PermissionError("403 unique-leak"), None, "authorization"),
                 (ConfigurationError("unique-leak"), None, "configuration"),
                 (MCPRuntimeError("unique-leak"), None, "mcp"),
                 (TimeoutError("unique-leak"), None, "timeout"),
                 (RuntimeError("unique-leak"), "model", "model"),
                 (RuntimeError("unique-leak"), "tool", "tool"),
                 (RuntimeError("unique-leak"), None, "unexpected")]
        for error, boundary, category in cases:
            with self.subTest(category=category):
                safe = classify_error(error, boundary=boundary)
                self.assertEqual(safe.category, category)
                output = diagnostic_line(event="request_failed", request_id="request-123", error_category=safe.category, reason_code=safe.reason_code)
                self.assertIn(category, output)
                self.assertNotIn("unique-leak", output + safe.user_message() + repr(safe))

    def test_settings_repr_never_contains_values(self):
        settings = Settings(azure_openai_endpoint="https://user:unique-leak@host", azure_openai_api_key="unique-leak",
                            aks_mcp_path="/private/unique-leak", azure_config_dir="unique-leak", model="unique-leak")
        self.assertNotIn("unique-leak", repr(settings) + str(settings) + json.dumps(settings.safe_summary()))
        self.assertEqual(settings.azure_openai_api_key, "unique-leak")

    def test_sensitive_values_omitted_from_diagnostic_fields(self):
        values = ["api_key=unique-leak", "Bearer unique-leak", "password=unique-leak", "ConnectionString=unique-leak",
                  "Authorization: unique-leak", 'kind: Secret data: unique-leak', "stringData=unique-leak",
                  "ｐａｓｓｗｏｒｄ=unique-leak", "pass\u200bword=unique-leak", "Basic dXNlcjpwYXNz",
                  "AccountKey=unique-leak", "postgresql://user:unique-leak@host/db"]
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(sensitive(value))
                self.assertEqual(safe_fields(event=value, task_id=value), {})
                with self.assertRaises(ValidationError):
                    ContextScope(project=value)
        for key in ("headers", "content", "payload", "exception", "password", "raw_mcp"):
            with self.assertRaises(ValueError):
                safe_fields(**{key: "unique-leak"})
        self.assertEqual(safe_fields(reason_code={"secret": "unique-leak"}, duration=float("nan")), {})

    def test_cli_errors_are_safe(self):
        for error in (RuntimeError("Authorization: Bearer unique-leak /private/internal"), ConfigurationError("unique-leak")):
            with patch("src.agent.main.main", new=AsyncMock(side_effect=error)), \
                 patch("src.agent.main.parse_cli_question", return_value="question"), \
                 patch("builtins.print") as output, self.assertLogs("src.agent.main", level="ERROR") as logs, \
                 self.assertRaises(SystemExit):
                cli()
            combined = str(output.call_args_list) + str(logs.output)
            self.assertNotIn("unique-leak", combined)
            self.assertNotIn("/private/internal", combined)
            self.assertIn("reason_code", combined)


class ToolErrorBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_logs_exclude_arguments_and_keep_exact_source(self):
        source = 'variable "example" { default = "password=unique-leak" }\r\n'
        class ReadTool:
            def __init__(self, name):
                self.name = name
            async def ainvoke(self, call):
                if self.name == "repo_repository":
                    content = '[{"name":"My Project","id":"repo-private"}]'
                elif call["args"]["action"] == "list_directory":
                    content = '{"items":[{"path":"/terraform/private.tf","isFolder":false}]}'
                else:
                    content = source
                return ToolMessage(content=content, tool_call_id=call["id"])
        logs = []
        messages = await gather_terraform_evidence([ReadTool("repo_repository"), ReadTool("repo_file")], log=logs.append)
        self.assertEqual(retrieved_files(messages)["/terraform/private.tf"], source)
        for text in ("unique-leak", "repo-private", "/terraform/private.tf", "arguments"):
            self.assertNotIn(text, "\n".join(logs))

    async def test_returned_errors_sanitized_for_pipeline_and_repository(self):
        for name in ("pipelines_definition", "repo_repository"):
            async def invoke(**kwargs):
                return ToolMessage(content="network unavailable password=unique-leak /private/internal\nTraceback", tool_call_id="call-1", name=name, status="error")
            tool = StructuredTool(name=name, description="Read tool", args_schema={"type": "object", "properties": {
                "action": {"type": "string"}, "project": {"type": "string"}}}, coroutine=invoke)
            client = fixtures.FakeOpenAIClient([fixtures.completion(tool_calls=[fixtures.model_tool_call(name, '{"action":"list"}', "call-1")]), fixtures.completion("Unavailable")])
            with fixtures.GenericAgentFlowTests.generic_patches(self, client, fixtures.FakeRuntime([tool])), patch("builtins.print"):
                await main("List repositories and pipelines")
            text = client.create.await_args.kwargs["messages"][-1]["content"][0]["text"]
            self.assertNotIn("unique-leak", text)
            self.assertNotIn("/private/internal", text)
            self.assertIn("temporarily unavailable", text)

    async def test_returned_authentication_error_stops_without_leaking(self):
        async def invoke(**kwargs):
            return ToolMessage(content="401 Unauthorized Authorization: Bearer unique-leak", tool_call_id="call-1", status="error")
        tool = StructuredTool(name="pipelines_definition", description="Read tool", args_schema={"type":"object"}, coroutine=invoke)
        client = fixtures.FakeOpenAIClient([fixtures.completion(tool_calls=[fixtures.model_tool_call("pipelines_definition", '{"action":"list"}', "call-1")])])
        with fixtures.GenericAgentFlowTests.generic_patches(self, client, fixtures.FakeRuntime([tool])), self.assertRaises(RuntimeError) as raised:
            await main("List pipelines")
        self.assertNotIn("unique-leak", str(raised.exception))
        self.assertIn("authentication", str(raised.exception))
        self.assertEqual(client.create.await_count, 1)
