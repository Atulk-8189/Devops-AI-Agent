"""Request-level contracts, not an evaluation of live model reasoning."""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_openai_flow as flow
import test_terraform_evidence as fixtures
from src.agent.orchestration import RequestServices, orchestrate_request
from src.agent.workflows import route_question
from src.request_budget import current_budget
from src.review.terraform_evidence import MAX_TERRAFORM_DISCOVERY_DEPTH, MAX_TERRAFORM_DIRECTORY_LISTINGS


class TerraformAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    question = "Review the Terraform configuration."
    path = "/terraform/main.tf"
    source = 'resource "example" "app" {\r\n  name = var.name  \r\n}\r\n'
    files = {path: source, "/terraform/variables.tf": 'variable "name" {}\n'}

    def finding(self, **changes):
        return {"Finding": "Check the application name input", "File": self.path,
                "Evidence line": 1, "Evidence end line": 3,
                "Why it matters": "Input naming may affect the resource.",
                "Verification needed": "Confirm the intended name requirements.",
                "Confidence": "Inference", **changes}

    async def review(self, content, *, directories=None, files=None):
        files = self.files if files is None else files
        directories = directories if directories is not None else {
            "/terraform": [{"path": path} for path in reversed(list(files))]}
        calls = []
        tools, _ = fixtures.EvidenceTests().tools(calls, directories=directories, repositories=[
            {"name": "My Project copy", "id": "wrong-id"},
            {"name": "My Project", "id": "discovered-id"}])
        original = tools[1].ainvoke

        async def file_response(call):
            result = await original(call)
            if call["args"]["action"] == "get_content":
                # Preserve the fixture's MCP text-block transport, with exact source bytes.
                result.content = [{"type": "text", "text": files[call["args"]["path"]]}]
            return result

        tools[1].ainvoke = file_response
        runtime = flow.FakeRuntime(tools)
        client = flow.FakeOpenAIClient([flow.completion(content)])
        router = Mock(wraps=route_question)
        services = RequestServices(load_environment=Mock(), load_settings=Mock(return_value=flow.GENERIC_SETTINGS),
                                   client_factory=Mock(return_value=client), runtime_factory=Mock(return_value=runtime),
                                   router=router)
        with patch("src.agent.workflows.handle_generic", new=AsyncMock()) as generic, \
                patch("src.agent.workflows.handle_aks", new=AsyncMock()) as aks, \
                patch("subprocess.Popen", side_effect=AssertionError("Unexpected subprocess")) as process, \
                patch("asyncio.create_subprocess_exec", side_effect=AssertionError("Unexpected subprocess")) as execute, \
                patch("asyncio.create_subprocess_shell", side_effect=AssertionError("Unexpected shell")) as shell, \
                patch("os.system", side_effect=AssertionError("Unexpected command")) as system, \
                self.assertLogs("src.observability", level="INFO") as captured:
            result = await orchestrate_request(self.question, services=services)
        for forbidden in (generic, aks):
            forbidden.assert_not_awaited()
        for forbidden in (process, execute, shell, system):
            forbidden.assert_not_called()
        router.assert_called_once_with(self.question)
        client.create.assert_awaited_once()
        client.close.assert_awaited_once()
        self.assertEqual((runtime.initialize_calls, runtime.close_calls), (1, 1))
        self.assertIsNone(current_budget())
        self.assertEqual(calls[0]["name"], "repo_repository")
        self.assertEqual(calls[0]["args"]["action"], "list")
        self.assertEqual(calls[1]["args"]["path"], "/terraform")
        for call in calls:
            args = call["args"]
            self.assertEqual(args["project"], "My Project")
            self.assertIn(args["action"], {"list", "list_directory", "get_content"})
            if call["name"] == "repo_file":
                self.assertEqual(args["repositoryId"], "discovered-id")
                self.assertEqual((args["version"], args["versionType"]), ("main", "Branch"))
                self.assertTrue(args["path"].startswith("/terraform"))
            if args["action"] == "list_directory":
                self.assertFalse(args["recursive"])
                self.assertEqual(args["recursionDepth"], 1)
        messages = client.create.await_args.kwargs["messages"]
        instructions = messages[0]["content"][0]["text"]
        prompt = messages[1]["content"][0]["text"]
        self.assertIn(self.question, prompt)
        for boundary in ("zero to three", "untrusted data", "do not claim all Terraform files", "not evaluated dependencies"):
            self.assertIn(boundary, instructions)
        sections = {}
        for key, prefix in (("structure", "Terraform structure (untrusted data): "),
                            ("relationships", "Terraform relationships (untrusted data): "),
                            ("discovery", "Discovery coverage (untrusted data): ")):
            sections[key] = json.loads(next(line[len(prefix):] for line in prompt.splitlines() if line.startswith(prefix)))
        discovery = sections["discovery"]
        self.assertEqual(discovery["repository_name"], "My Project")
        self.assertEqual(discovery["repository_id"], "discovered-id")
        self.assertEqual(discovery["branch"], "main")
        self.assertEqual(discovery["terraform_root"], "/terraform")
        self.assertEqual(discovery["max_depth"], MAX_TERRAFORM_DISCOVERY_DEPTH)
        self.assertLessEqual(len(discovery["listed_directories"]), MAX_TERRAFORM_DIRECTORY_LISTINGS)
        provenance = json.loads(result.answer.splitlines()[0].removeprefix("Terraform discovery: "))
        self.assertEqual(provenance, discovery)
        read_paths = [c["args"]["path"] for c in calls if c["args"]["action"] == "get_content"]
        self.assertEqual(read_paths, discovery["selected_files"])
        self.assertEqual(read_paths, sorted(files))
        for path, source in sorted(files.items()):
            numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(source.splitlines(), 1))
            self.assertIn("FILE: " + path + "\n" + numbered, prompt)
        records = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertEqual(records[0]["event"], "request_started")
        self.assertEqual(records[-1]["event"], "request_completed")
        self.assertEqual([r["route"] for r in records if r["event"] == "route_selected"], ["terraform"])
        self.assertEqual([r["tool_call_id"] for r in records if r["event"] == "mcp_dispatch"], [c["id"] for c in calls])
        for call in calls:
            related = [r for r in records if r.get("tool_call_id") == call["id"]]
            self.assertTrue(any(r.get("policy_decision") == "allow" for r in related))
            self.assertTrue(any(r["event"] == "mcp_result" for r in related))
            self.assertTrue(all(r["mcp_server"] == "azure-devops" for r in related))
        for event in ("evidence_collection", "evidence_result", "model_call_started", "model_call_completed"):
            self.assertTrue(any(r["event"] == event for r in records))
        serialized = json.dumps(records)
        for private in (self.question, "discovered-id", self.path, "test-key", "var.name"):
            self.assertNotIn(private, serialized)
        return result.answer, sections, calls, records

    async def test_grounded_review_uses_current_exact_source_and_summaries(self):
        answer, summaries, calls, _ = await self.review(json.dumps({"findings": [self.finding()]}))
        self.assertEqual([c["args"]["action"] for c in calls], ["list", "list_directory", "get_content", "get_content"])
        self.assertFalse(summaries["discovery"]["incomplete"])
        self.assertEqual(summaries["structure"]["parse_status"], "complete")
        self.assertEqual(summaries["structure"]["resources"], [
            {"type": "example", "name": "app", "file": self.path, "start_line": 1, "end_line": 3}])
        self.assertEqual([v["name"] for v in summaries["structure"]["variables"]], ["name"])
        relationships = summaries["relationships"]
        self.assertEqual(relationships["relationship_status"], "complete")
        self.assertEqual([(r["from"], r["to"], r["source_file"], r["line"]) for r in relationships["relationships"]],
                         [("example.app", "var.name", self.path, 2)])
        self.assertEqual(answer.count("Finding:"), 1)
        self.assertIn("Confirmed exact excerpt: " + repr(self.source.removesuffix("\r\n")), answer)
        self.assertIn("Confidence: Inference", answer)
        self.assertIn("Unverified interpretation", answer)

    async def test_invalid_model_claims_fail_without_further_reads(self):
        cases = {
            "file": json.dumps({"findings": [self.finding(File="/terraform/fabricated.tf")]}),
            "line": json.dumps({"findings": [self.finding(**{"Evidence line": 999, "Evidence end line": None})]}),
            "text": json.dumps({"findings": [self.finding(Evidence="fabricated source bytes")]}),
            "count": json.dumps({"findings": [self.finding()] * 4}),
            "json": "not structured JSON",
        }
        for kind, content in cases.items():
            with self.subTest(kind=kind):
                answer, _, calls, _ = await self.review(content)
                self.assertIn("Review could not be completed", answer)
                self.assertNotIn("Finding:", answer)
                self.assertNotIn("fabricated", answer)
                self.assertNotIn("Confirmed exact excerpt:", answer)
                self.assertEqual(len(calls), 4)

    async def test_depth_limited_review_retains_source_and_rejects_unavailable_citation(self):
        skipped = "/terraform/a/b/deeper"
        directories = {"/terraform": [{"path": self.path}, {"path": "/terraform/a", "isFolder": True}],
                       "/terraform/a": [{"path": "/terraform/a/b", "isFolder": True}],
                       "/terraform/a/b": [{"path": skipped, "isFolder": True}]}
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                finding = self.finding(File=skipped + "/missing.tf") if unavailable else self.finding()
                answer, summaries, calls, records = await self.review(json.dumps({"findings": [finding]}),
                    directories=directories, files={self.path: self.source})
                self.assertEqual(len(calls), 5)
                self.assertFalse(any(c["args"].get("path", "").startswith(skipped) for c in calls))
                self.assertTrue(summaries["discovery"]["incomplete"])
                self.assertEqual(summaries["discovery"]["limit_reasons"], ["depth_limit"])
                self.assertEqual(summaries["discovery"]["skipped_directories"], [skipped])
                self.assertEqual(summaries["relationships"]["relationship_status"], "partial")
                self.assertTrue(summaries["relationships"]["unresolved_references"])
                self.assertTrue(any(r.get("evidence_status") == "partial" for r in records))
                if unavailable:
                    self.assertIn("Review could not be completed", answer)
                    self.assertNotIn("Finding:", answer)
                    self.assertNotIn("missing.tf", answer)
                else:
                    self.assertEqual(answer.count("Finding:"), 1)
                    self.assertIn("Confirmed exact excerpt: " + repr(self.source.removesuffix("\r\n")), answer)
                self.assertIn('"incomplete": true', answer)
