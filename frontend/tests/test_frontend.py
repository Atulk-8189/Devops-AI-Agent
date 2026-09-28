"""Offline adapter tests; no Chainlit server, model or MCP is started."""
import asyncio
import importlib.util
import json
import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import tomllib

# Load real dependencies before patch.dict restores sys.modules between cases.
from src.agent.orchestration import orchestrate_request


FRONTEND = Path(__file__).resolve().parents[1]


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []
        self.messages = []
        sent = self.sent
        messages = self.messages

        class Message:
            def __init__(self, content, elements=None, author=None, language=None):
                self.content = content
                self.elements = elements or []
                self.author = author
                self.language = language

            async def send(self):
                sent.append(self.content)
                messages.append(self)
                return self

            async def update(self):
                sent.append(self.content)
                return self

        class Text:
            def __init__(self, name, content, display="side"):
                self.name = name
                self.content = content
                self.display = display

        fake = ModuleType("chainlit")
        fake.Message = Message
        fake.Text = Text
        session_data = {}
        fake.user_session = SimpleNamespace(
            get=lambda k, d=None: session_data.get(k, d),
            set=lambda k, v: session_data.__setitem__(k, v)
        )
        fake.User = lambda identifier, metadata=None: SimpleNamespace(identifier=identifier, metadata=metadata or {})
        fake.on_message = fake.on_chat_start = fake.on_chat_resume = fake.header_auth_callback = fake.data_layer = lambda fn: fn
        config_module = ModuleType("chainlit.config")
        self.settings = SimpleNamespace(host="127.0.0.1", watch=False, debug=False)
        config_module.config = SimpleNamespace(run=self.settings)
        with patch.dict("sys.modules", {"chainlit": fake, "chainlit.config": config_module}):
            spec = importlib.util.spec_from_file_location("frontend_under_test", FRONTEND / "chainlit_app.py")
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.Message = Message

    def tearDown(self):
        self.app._DATA_LAYER._conn.close()

    async def test_progress_metadata_attached_to_status(self):
        from src.observability import observed_request, emit, select_route

        @observed_request(return_outcome=True)
        async def run(question):
            select_route("aks")
            emit("mcp_dispatch", outcome="started")
            await asyncio.sleep(0)
            return self.app.WorkflowResult('{"missing_evidence": ["logs"]}')

        with patch.object(self.app, "orchestrate_request", run):
            await self.app.on_message(self.Message("Check AKS cluster health."))
        status = self.messages[0]
        self.assertIn("Executing MCP tool: tool name unavailable", self.sent)
        self.assertEqual(status.metadata["investigation"]["category"], "AKS")
        self.assertIn('"missing_evidence"', status.elements[0].content)
        self.assertFalse(self.app._REQUEST_LOCK.locked())

    async def test_activity_panel_keeps_answer_and_evidence_separate(self):
        from src.observability import observed_request, emit
        self.app.cl.CustomElement = lambda **kwargs: SimpleNamespace(**kwargs)
        answer = "Exact answer with whitespace.  \n"

        @observed_request(return_outcome=True)
        async def run(question):
            emit("model_call_started", model_call_sequence_number=1)
            await asyncio.sleep(0)
            emit("model_call_completed", model_call_sequence_number=1)
            return self.app.WorkflowResult(answer)

        with patch.object(self.app, "orchestrate_request", run):
            await self.app.on_message(self.Message("Inspect target"))
        status, final = self.messages
        self.assertIn(answer, final.content)
        self.assertIn("<details>", final.content)  # Original block is present
        panel = status.elements[0]
        self.assertEqual(panel.name, "Activity")
        self.assertEqual(panel.props["outcome"], "completed")
        self.assertEqual(status.metadata["activity"], panel.props)
        self.assertNotIn(answer, str(panel.props))
        trace_panel = status.elements[1]
        self.assertEqual(trace_panel.name, "ExecutionTrace")
        self.assertEqual(trace_panel.display, "side")
        self.assertEqual(status.metadata["trace"]["summary"]["outcome"], "completed")

    async def test_consecutive_investigations_update_single_sidebar_and_preserve_history(self):
        from src.observability import observed_request, emit, select_route
        self.app.cl.CustomElement = lambda **kwargs: SimpleNamespace(**kwargs)

        # First run
        @observed_request(return_outcome=True)
        async def run1(question):
            select_route("aks")
            emit("model_call_started", model_call_sequence_number=1)
            await asyncio.sleep(0)
            emit("model_call_completed", model_call_sequence_number=1)
            return self.app.WorkflowResult("First answer")

        with patch.object(self.app, "orchestrate_request", run1):
            await self.app.on_message(self.Message("First question"))

        self.assertEqual(len(self.messages), 2)
        self.assertIn("First answer", self.messages[1].content)
        first_trace = self.app.cl.user_session.get("current_trace_panel")
        self.assertIsNotNone(first_trace)
        self.assertEqual(first_trace.props["summary"]["category"], "AKS")

        # Second run
        @observed_request(return_outcome=True)
        async def run2(question):
            select_route("terraform")
            emit("model_call_started", model_call_sequence_number=1)
            await asyncio.sleep(0)
            emit("model_call_completed", model_call_sequence_number=1)
            return self.app.WorkflowResult("Second answer")

        with patch.object(self.app, "orchestrate_request", run2):
            await self.app.on_message(self.Message("Second question"))

        # Both answers are preserved in conversation history
        self.assertEqual(len(self.messages), 4)
        self.assertIn("First answer", self.messages[1].content)
        self.assertIn("Second answer", self.messages[3].content)

        # Only the second investigation's trace is in current session
        second_trace = self.app.cl.user_session.get("current_trace_panel")
        self.assertIsNotNone(second_trace)
        self.assertEqual(second_trace.props["summary"]["category"], "TERRAFORM")

    async def test_execution_trace_side_panel_rendering_and_metadata(self):
        from src.observability import observed_request, emit, select_route
        self.app.cl.CustomElement = lambda **kwargs: SimpleNamespace(**kwargs)

        @observed_request(return_outcome=True)
        async def run(question):
            select_route("aks")
            emit("connections_start")
            emit("connections_ready")
            emit("model_call_started", model_call_sequence_number=1)
            await asyncio.sleep(0)
            emit("model_call_completed", model_call_sequence_number=1)
            emit("mcp_dispatch", dispatch_count=1, tool_name="kubectl_resources", operation="get")
            emit("mcp_result", dispatch_count=1, tool_name="kubectl_resources", operation="get")
            emit("cleanup_completed")
            return self.app.WorkflowResult('{"cluster_scope": {"cluster": "test-cluster"}}')

        with patch.object(self.app, "orchestrate_request", run):
            await self.app.on_message(self.Message("Check AKS cluster health."))
        status = self.messages[0]
        self.assertIn("trace", status.metadata)
        trace_data = status.metadata["trace"]
        self.assertEqual(trace_data["summary"]["category"], "AKS")
        self.assertEqual(trace_data["summary"]["model_call_count"], 1)
        self.assertEqual(trace_data["summary"]["tool_call_count"], 1)
        self.assertEqual(trace_data["summary"]["outcome"], "completed")

        trace_element = next(el for el in status.elements if getattr(el, "name", None) == "ExecutionTrace")
        self.assertEqual(trace_element.display, "side")
        self.assertEqual(trace_element.props["summary"]["outcome"], "completed")

    def test_execution_trace_jsx_collapse_reopen_accessibility(self):
        jsx_path = FRONTEND / "public" / "elements" / "ExecutionTrace.jsx"
        self.assertTrue(jsx_path.exists())
        content = jsx_path.read_text(encoding="utf-8")
        self.assertIn("Collapse Investigation Details", content)
        self.assertIn("Investigation Details Sidebar", content)
        self.assertIn("__DEVOPS_TRACE_STORE__", content)
        self.assertIn("DetailsTab", content)
        self.assertIn("Scope & Evidence", content)

    def test_custom_js_and_css_configured_and_present(self):
        js_path = FRONTEND / "public" / "custom.js"
        css_path = FRONTEND / "public" / "custom.css"
        config_path = FRONTEND / ".chainlit" / "config.toml"
        self.assertTrue(js_path.exists())
        self.assertTrue(css_path.exists())
        self.assertTrue(config_path.exists())

        js_content = js_path.read_text(encoding="utf-8")
        self.assertIn("__DEVOPS_TRACE_STORE__", js_content)
        self.assertIn("__DEVOPS_COLLAPSE_SIDEBAR__", js_content)
        self.assertIn("__DEVOPS_REOPEN_SIDEBAR__", js_content)
        self.assertIn("devops-sidebar-rail", js_content)
        self.assertIn("Expand Investigation Details", js_content)
        self.assertIn("tracesByThread", js_content)

        css_content = css_path.read_text(encoding="utf-8")
        self.assertIn("#devops-sidebar-rail", css_content)
        self.assertIn("#side-view-content", css_content)

        config_content = config_path.read_text(encoding="utf-8")
        self.assertIn('custom_js = "/public/custom.js"', config_content)
        self.assertIn('custom_css = "/public/custom.css"', config_content)

    async def test_listener_cleanup_on_failure(self):
        from frontend.investigation import _active
        loggers = [logging.getLogger(name) for name in
                   ("src.observability", "src.agent.orchestration")]
        original = [(logger.level, list(logger.handlers)) for logger in loggers]
        with patch.object(self.app, "orchestrate_request", AsyncMock(side_effect=RuntimeError("password=private-value"))):
            await self.app.on_message(self.Message("Inspect target"))
        for logger, (level, handlers) in zip(loggers, original):
            self.assertEqual(logger.level, level)
            self.assertEqual(logger.handlers, handlers)
        self.assertIsNone(_active.get())
        self.assertFalse(self.app._REQUEST_LOCK.locked())
        self.assertEqual(self.messages[0].metadata["activity"]["outcome"], "failed")
        self.assertNotIn("private-value", str(self.messages[0].metadata))

    async def test_cancellation_activity_and_listener_cleanup(self):
        from frontend.investigation import _active
        self.app.cl.CustomElement = lambda **kwargs: SimpleNamespace(**kwargs)
        entered = asyncio.Event()
        logger = logging.getLogger("src.agent.orchestration")
        old_handlers = list(logger.handlers)
        old_level = logger.level

        async def run(question):
            logger.info("agent startup: initializing Azure OpenAI and MCP runtime")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                logger.info("agent shutdown complete")

        with patch.object(self.app, "orchestrate_request", run):
            task = asyncio.create_task(self.app.on_message(self.Message("Inspect target")))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        activity = self.messages[0].metadata["activity"]
        self.assertEqual(activity["outcome"], "cancelled")
        self.assertEqual(activity["entries"][0]["state"], "unconfirmed")
        self.assertEqual(activity["entries"][-2]["title"], "MCP and model client cleanup completed")
        self.assertEqual(logger.handlers, old_handlers)
        self.assertEqual(logger.level, old_level)
        self.assertIsNone(_active.get())
        self.assertFalse(self.app._REQUEST_LOCK.locked())

    async def test_welcome_cards(self):
        from frontend.investigation import CAPABILITIES
        self.app.cl.CustomElement = lambda **kwargs: SimpleNamespace(**kwargs)
        await self.app.on_chat_start()
        welcome = self.messages[-1]
        self.assertEqual(welcome.author, "Frontend welcome")
        self.assertEqual(welcome.elements[0].props["cards"], CAPABILITIES)

    async def test_answer_and_exact_question_handoff_without_history(self):
        answer = "## Observed facts\nDeployment ready.\n\n## Missing evidence\nConnectivity not tested."
        run = AsyncMock(return_value=self.app.WorkflowResult(answer))
        with patch.object(self.app, "orchestrate_request", run):
            await self.app.on_message(self.Message("Inspect Task Manager"))
        run.assert_awaited_once_with("Inspect Task Manager")
        # The formatter wraps responses; the original answer is embedded in the formatted output.
        self.assertIn(answer, self.sent[-1])
        self.assertIn("Read-only agent execution started", self.sent[1])

    async def test_safe_error_without_exception_text(self):
        with patch.object(self.app, "orchestrate_request", AsyncMock(side_effect=RuntimeError("password=private-value"))):
            await self.app.on_message(self.Message("Inspect deployment"))
        self.assertIn("No sensitive diagnostic details", self.sent[-1])
        self.assertNotIn("private-value", str(self.sent))

    async def test_bounded_result_does_not_display_retained_evidence(self):
        bounded = self.app.BoundedResult("deadline", None, ({"raw": "private-source"},))
        with patch.object(self.app, "orchestrate_request", AsyncMock(return_value=bounded)):
            await self.app.on_message(self.Message("Inspect deployment"))
        self.assertIn("incomplete", self.sent[-1])
        self.assertNotIn("private-source", str(self.sent))

    async def test_sensitive_final_answer_is_explicitly_redacted(self):
        for value in ("password=hunter2", "Authorization: Bearer abc", "api_key=abc", "AccountKey=abc"):
            with self.subTest(value=value):
                answer = self.app.prepare_result(self.app.WorkflowResult(value))
                self.assertIn("[REDACTED]", answer.content)
                self.assertIn("not an exact source excerpt", answer.notice)
                self.assertNotIn(value, answer.content)
        self.assertNotIn("private-source", self.app.prepare_result({"raw": "private-source"}).notice)

    async def test_empty_and_upload_messages_do_not_execute(self):
        run = AsyncMock()
        with patch.object(self.app, "orchestrate_request", run):
            await self.app.on_message(self.Message("  "))
            await self.app.on_message(self.Message("Inspect", elements=[object()]))
        run.assert_not_called()

    async def test_unsafe_final_answer_reaches_ui_only_as_safe_explanation(self):
        result = self.app.WorkflowResult("-----BEGIN PRIVATE KEY-----\nprivate-synthetic-value")
        with patch.object(self.app, "orchestrate_request", AsyncMock(return_value=result)):
            await self.app.on_message(self.Message("Inspect deployment"))
        self.assertEqual(self.sent[-1], self.app.response_safety.WITHHELD)
        self.assertNotIn("private-synthetic-value", str(self.sent))

    async def test_handler_uses_current_filter_for_health_and_credentials(self):
        legacy_message = "The final response was withheld because it contains sensitive-looking text."
        normal = (
            "## Observed facts\nNodes Ready=True; MemoryPressure=False. "
            "Pods are Running in default. Secrets were not inspected.\n"
            "## Hypotheses\nNo current availability failure established.\n"
            "## Missing evidence\nExternal connectivity has not been tested."
        )
        resource = (
            "AKS task-manager in /subscriptions/12345678-1234-1234-1234-123456789abc/"
            "resourceGroups/rg-app/providers/Microsoft.ContainerService/managedClusters/task-manager. "
            "Service 10.0.0.1; see https://portal.azure.com."
        )
        cases = [
            (normal, "displayed"), (resource, "displayed"),
            ('Node Ready=True\npassword="synthetic-handler-credential"', "redacted"),
            ("-----BEGIN PRIVATE KEY-----\nsynthetic-key-material", "withheld"),
            ("kind: Secret\nstringData:\n  value: synthetic-secret-data", "withheld"),
        ]
        for answer, action in cases:
            with self.subTest(action=action):
                run = AsyncMock(return_value=self.app.WorkflowResult(answer))
                safety = self.app.response_safety
                with (patch.object(self.app, "orchestrate_request", run),
                      patch.object(safety, "filter_response", wraps=safety.filter_response) as filter_call,
                      self.assertLogs("frontend.response_safety", level="INFO") as logs):
                    await self.app.on_message(self.Message("Check AKS cluster health"))
                filter_call.assert_called_once_with(answer)
                run.assert_awaited_once_with("Check AKS cluster health")
                event = json.loads(logs.records[-1].getMessage())
                self.assertEqual(event["implementation"], safety.FILTER_IMPLEMENTATION)
                self.assertEqual(event["revision"], safety.FILTER_REVISION)
                self.assertEqual(event["display_action"], action)
                final = self.sent[-1]
                self.assertNotIn(legacy_message, final)
                if action == "displayed":
                    # Formatter wraps all responses — original answer is embedded in the formatted output.
                    self.assertIn(answer, final)
                elif action == "redacted":
                    self.assertIn("[REDACTED]", final)
                    self.assertIn("Node Ready=True", final)
                    self.assertIn("not an exact source excerpt", self.sent[-2])
                    self.assertEqual(self.messages[-2].author, "Frontend notice")
                else:
                    self.assertEqual(final, safety.WITHHELD)
                for private in ("synthetic-handler-credential", "synthetic-key-material", "synthetic-secret-data"):
                    self.assertNotIn(private, final)
                    self.assertNotIn(private, str(logs.output))

    async def test_requests_are_serialized_across_chats(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def run(question):
            calls.append(question)
            if question == "first":
                entered.set()
                await release.wait()
            return self.app.WorkflowResult("Completed read-only investigation.")

        with patch.object(self.app, "orchestrate_request", run):
            first = asyncio.create_task(self.app.on_message(self.Message("first")))
            await entered.wait()
            second = asyncio.create_task(self.app.on_message(self.Message("second")))
            await asyncio.sleep(0)
            self.assertEqual(calls, ["first"])
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(calls, ["first", "second"])

    async def assert_verbatim_handler_response(self, answer, language=None):
        result = self.app.WorkflowResult(answer)
        with patch.object(self.app, "orchestrate_request", AsyncMock(return_value=result)):
            await self.app.on_message(self.Message("Investigate the specified target"))
        final = self.messages[-1]
        # The formatter wraps all responses with a contextual header and original block.
        # The full original content must be present somewhere in the formatted output.
        self.assertIn(answer, final.content)
        self.assertEqual(final.language, language)
        self.assertEqual(result.answer, answer)
        self.assertIsNone(final.author)  # Agent answer, not a status/notice message.
        self.assertEqual(self.messages[-2].author, "Execution status")
        return final

    async def test_all_domains_preserve_sections_order_and_evidence(self):
        answers = {
            "aks": "# Summary\nReady.\n## Evidence\nPods 2/2.\n## Hypotheses\nNo cause established.\n"
                   "## Missing evidence\nConnectivity untested.\n## Next step\nRead service status.\n",
            "ado": "Pipeline: DB Bootstrap\n1. Build\n2. Deploy\nEvidence: /pipelines/db.yml, main.\n"
                   "Warning: YAML describes configured behavior, not proof of execution.\n",
            "terraform": "## Finding\nCheck subnet configuration.\n```hcl\nresource \"azurerm_subnet\" \"app\" {}\n```\n"
                         "Source: /terraform/main.tf:1\nInference: intent is unverified.\nLimitation: no plan executed.\n",
            "generic": "This response deliberately has no report template.\n"
                       "Evidence precedes the summary here.\nSummary: additional evidence is needed.\n",
        }
        for domain, answer in answers.items():
            with self.subTest(domain=domain):
                await self.assert_verbatim_handler_response(answer)

    async def test_markdown_code_whitespace_and_unicode_preserved(self):
        answer = (
            "  # Health report\r\n\r\n| Node | Ready |\r\n|---|---|\r\n| α | True |\r\n"
            "\r\n- Observed ✅\r\n  - Hypothesis—not confirmed\r\n\r\n"
            "```yaml\r\nstages:\r\n  - stage: Build\r\n    displayName: 'Keep spacing'\r\n```\r\n"
            "\r\n> Warning: incomplete evidence.  \r\n\r\n"
        )
        await self.assert_verbatim_handler_response(answer)

    async def test_json_every_field_value_and_original_representation_preserved(self):
        answer = (' {"custom_section": {"items": [1, false, null, "α"], "unknown": "missing"}, '
                  '"precision": 1.23000000000000000001, "large_id": 9007199254740993, '
                  '"duplicate": 1, "duplicate": 2, "empty": {}, "recommendations": [], '
                  '"evidence": "line1\\nline2", "warnings": ["Not complete"]}\n')
        result = self.app.WorkflowResult(answer)
        with patch.object(self.app, "orchestrate_request", AsyncMock(return_value=result)):
            await self.app.on_message(self.Message("Investigate the specified target"))
        final = self.messages[-1]

        # Backend result and exact response are untouched
        self.assertEqual(result.answer, answer)

        # Visual hierarchy sections are present — large/nested fields may be in collapsible blocks
        # custom_section is a dict → collapsed into <details>
        self.assertIn("<strong>custom_section</strong>", final.content)
        self.assertIn("### precision\n`1.23`", final.content)
        self.assertIn("### large_id\n`9007199254740993`", final.content)
        # warnings is a list → may be in collapsible block
        self.assertIn("warnings", final.content)
        self.assertIn("- Not complete", final.content)
        # evidence is a short string → rendered as ### section
        self.assertIn("evidence", final.content)

        # Raw response details block preserves exact content
        details_header = "<details>\n<summary><strong>Raw response</strong></summary>\n\n```json\n"
        self.assertIn(details_header, final.content)
        raw_start = final.content.index(details_header) + len(details_header)
        raw_end = final.content.rindex("\n```\n</details>")
        raw_extracted = final.content[raw_start:raw_end]
        self.assertEqual(raw_extracted, answer.rstrip("\n"))
        self.assertEqual(json.loads(raw_extracted, object_pairs_hook=list),
                         json.loads(answer, object_pairs_hook=list))
        self.assertEqual(raw_extracted.count('"duplicate"'), 2)

        # Side element contains the exact untouched answer
        if final.elements:
            self.assertEqual(final.elements[0].content, answer)
            self.assertEqual(final.elements[0].name, "Raw response")

        # Array JSON formatting and raw response preservation
        array_answer = '[{"arbitrary": "kept"}, null, 1.00]\n'
        result_arr = self.app.WorkflowResult(array_answer)
        with patch.object(self.app, "orchestrate_request", AsyncMock(return_value=result_arr)):
            await self.app.on_message(self.Message("Investigate array target"))
        final_arr = self.messages[-1]
        self.assertIn(details_header, final_arr.content)
        raw_arr_start = final_arr.content.index(details_header) + len(details_header)
        raw_arr_end = final_arr.content.rindex("\n```\n</details>")
        self.assertEqual(final_arr.content[raw_arr_start:raw_arr_end], array_answer.rstrip("\n"))

        # Partial/invalid JSON is treated as plain text without template
        await self.assert_verbatim_handler_response('{"partial": ', None)

    async def test_aks_diagnosis_visual_hierarchy_and_code_blocks(self):
        aks_payload = {
            "summary": "Task Manager deployment is degraded due to image pull failure.",
            "observed_evidence": [
                "Pod task-manager-658b9cf6d-5z72x status is Waiting (ImagePullBackOff)",
                "Failed to pull image devopsacr.azurecr.io/task-manager:v2.0.0",
                {"container": "task-manager", "restart_count": 0, "ready": False},
            ],
            "likely_root_causes": [
                {
                    "hypothesis": "Image tag v2.0.0 does not exist in registry",
                    "supporting_evidence": "RPC error: NotFound desc = failed to pull and unpack image",
                }
            ],
            "recommended_next_diagnostic_step": (
                "Run `az acr repository show-tags --name devopsacr --repository task-manager --output table` "
                "to list valid image tags."
            ),
            "cluster_scope": {
                "cluster": "aks-cluster-prod",
                "namespace": "default",
            },
        }
        answer = json.dumps(aks_payload, indent=2)
        result = self.app.WorkflowResult(answer)
        with patch.object(self.app, "orchestrate_request", AsyncMock(return_value=result)):
            await self.app.on_message(self.Message("Diagnose AKS issue"))
        final = self.messages[-1]

        # Visual hierarchy headers - now starts with Executive Summary
        self.assertIn("### 📋 Executive Summary", final.content)
        self.assertIn("Task Manager deployment is degraded", final.content)

        self.assertIn("### 🔍 Observed Evidence", final.content)
        self.assertIn("- Pod task-manager-658b9cf6d-5z72x status is Waiting", final.content)
        self.assertIn("- **container**: task-manager", final.content)

        self.assertIn("### ⚠️ Likely Root Causes", final.content)
        self.assertIn("> #### Hypothesis 1: Image tag v2.0.0 does not exist in registry", final.content)
        self.assertIn("> **Supporting evidence:**", final.content)
        self.assertIn("> - RPC error: NotFound desc", final.content)

        self.assertIn("### 💡 Recommended Next Diagnostic Step", final.content)
        self.assertIn("```sh\naz acr repository show-tags --name devopsacr --repository task-manager --output table\n```", final.content)

        # cluster_scope is a small dict — now rendered in a collapsible block
        self.assertIn("<strong>cluster_scope</strong>", final.content)
        self.assertIn("- **cluster**: aks-cluster-prod", final.content)

        # Raw response contains the exact JSON
        self.assertIn("<details>\n<summary><strong>Raw response</strong></summary>", final.content)
        self.assertIn(answer, final.content)

    async def test_large_structured_response_no_truncation(self):
        items = [f"Observed event {i}: Pod healthy at timestamp 2026-09-26T12:{i % 60:02d}:00Z" for i in range(1500)]
        payload = {
            "summary": "Large AKS inspection report with many events.",
            "observed_evidence": items,
            "recommended_next_diagnostic_step": "Run `kubectl get events -n default` to monitor further.",
        }
        answer = json.dumps(payload)
        result = self.app.WorkflowResult(answer)
        with patch.object(self.app, "orchestrate_request", AsyncMock(return_value=result)):
            await self.app.on_message(self.Message("Inspect cluster events"))
        final = self.messages[-1]

        self.assertIn("### 📋 Executive Summary", final.content)
        self.assertEqual(final.content.count("Observed event "), 1500 * 2)  # In formatted list AND raw JSON
        self.assertIn("Observed event 0:", final.content)
        self.assertIn("Observed event 1499:", final.content)
        self.assertIn("```sh\nkubectl get events -n default\n```", final.content)

    async def test_unstructured_text_no_template_artifacts(self):
        answer = "Plain conversational answer without any JSON formatting. All systems operational."
        final = await self.assert_verbatim_handler_response(answer)
        # Original content is preserved in the formatted output.
        self.assertIn(answer, final.content)
        # Plain text must NOT get JSON-structured template sections.
        self.assertNotIn("### 📋 Summary", final.content)
        self.assertNotIn("### 🔍 Observed Evidence", final.content)
        self.assertNotIn("Raw response", final.content)
        # But it DOES get a contextual header and an Original Response block.
        self.assertIn("###", final.content)
        self.assertIn("Original Response", final.content)
        self.assertIn("<details>", final.content)

    async def test_long_response_has_no_frontend_truncation(self):
        answer = "# All observations\n" + "".join(
            f"- Observation {index}: read-only evidence; connectivity remains unverified.\n"
            for index in range(4000)
        ) + "\n## Final limitations\nEND-OF-COMPLETE-AGENT-RESPONSE\n"
        self.assertGreater(len(answer), 200_000)
        final = await self.assert_verbatim_handler_response(answer)
        # All 4000 observations appear in body AND again in the Original Response block = 8000 total.
        # Executive summary adds one more from the first paragraph = 8001 total.
        self.assertEqual(final.content.count("- Observation "), 8001)
        # The answer is fully preserved — the end marker appears in the formatted output.
        self.assertIn("END-OF-COMPLETE-AGENT-RESPONSE\n", final.content)

    async def test_existing_safety_exception_never_claims_unchanged_response(self):
        answer = 'Observed: Node Ready=True\npassword="synthetic-value"\nLimitations: connectivity untested.'
        result = self.app.WorkflowResult(answer)
        with patch.object(self.app, "orchestrate_request", AsyncMock(return_value=result)):
            await self.app.on_message(self.Message("Inspect deployment"))
        self.assertEqual(result.answer, answer)  # Backend source of truth stays intact.
        self.assertEqual(self.messages[-2].author, "Frontend notice")
        self.assertIn("not an exact source excerpt", self.messages[-2].content)
        self.assertNotIn("synthetic-value", self.messages[-1].content)
        self.assertIn("Limitations: connectivity untested.", self.messages[-1].content)

    async def test_allowed_filter_copy_cannot_rewrite_original_answer(self):
        answer = "Complete original evidence and limitations."
        decision = self.app.response_safety.DisplayResponse("Unexpected rewritten copy", "allowed", ())
        with patch.object(self.app.response_safety, "filter_response", return_value=decision):
            await self.assert_verbatim_handler_response(answer)

    async def test_cancellation_holds_lock_until_cleanup_completes(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def run(question):
            calls.append(question)
            if question == "first":
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return self.app.WorkflowResult("Complete.")

        with patch.object(self.app, "orchestrate_request", run):
            first = asyncio.create_task(self.app.on_message(self.Message("first")))
            await entered.wait()
            first.cancel()
            await cleaning.wait()
            second = asyncio.create_task(self.app.on_message(self.Message("second")))
            await asyncio.sleep(0)
            self.assertEqual(calls, ["first"])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await second
        self.assertFalse(self.app._REQUEST_LOCK.locked())

    async def test_local_only_launch_and_safe_configuration(self):
        self.settings.host = "0.0.0.0"
        with self.assertRaises(RuntimeError):
            self.app.validate_local_launch()
        self.settings.host = "127.0.0.1"
        for field in ("watch", "debug"):
            setattr(self.settings, field, True)
            with self.assertRaises(RuntimeError):
                self.app.validate_local_launch()
            setattr(self.settings, field, False)
        with (FRONTEND / ".chainlit/config.toml").open("rb") as file:
            config = tomllib.load(file)
        self.assertFalse(config["features"]["unsafe_allow_html"])
        for feature in ("spontaneous_file_upload", "audio", "mcp"):
            self.assertFalse(config["features"][feature]["enabled"])
        self.assertEqual(config["UI"]["cot"], "hidden")
        self.assertEqual(config["project"]["user_env"], [])
        self.assertNotIn("*", config["project"]["allow_origins"])


    async def test_execution_viewer_button_attached_to_response_messages(self):
        from src.observability import observed_request, emit, select_route
        self.app.cl.CustomElement = lambda **kwargs: SimpleNamespace(**kwargs)

        @observed_request(return_outcome=True)
        async def run(question):
            select_route("aks")
            emit("connections_start")
            emit("connections_ready")
            emit("model_call_started", model_call_sequence_number=1)
            await asyncio.sleep(0)
            emit("model_call_completed", model_call_sequence_number=1)
            emit("mcp_dispatch", dispatch_count=1, tool_name="kubectl_resources", operation="get")
            emit("mcp_result", dispatch_count=1, tool_name="kubectl_resources", operation="get")
            return self.app.WorkflowResult("Analysis complete.")

        with patch.object(self.app, "orchestrate_request", run):
            await self.app.on_message(self.Message("Check AKS cluster health"))

        # Check response message
        self.assertEqual(len(self.messages), 2)
        response_msg = self.messages[1]
        self.assertIn("Analysis complete.", response_msg.content)
        self.assertIsNotNone(response_msg.elements)

        # ExecutionViewerButton element is present
        viewer_btn = next((el for el in response_msg.elements if getattr(el, "name", None) == "ExecutionViewerButton"), None)
        self.assertIsNotNone(viewer_btn)
        props = viewer_btn.props
        self.assertIn("nodes", props)
        self.assertIn("timeline", props)
        self.assertIn("connections", props)
        self.assertIn("summary", props)
        self.assertIn("details", props)
        self.assertEqual(props["summary"]["category"], "AKS")
        self.assertEqual(props["connections"]["aks"]["status"], "ready")

    async def test_multiple_investigations_have_independent_execution_viewer_props(self):
        from src.observability import observed_request, emit, select_route
        self.app.cl.CustomElement = lambda **kwargs: SimpleNamespace(**kwargs)

        # Run 1: AKS
        @observed_request(return_outcome=True)
        async def run1(question):
            select_route("aks")
            emit("model_call_started", model_call_sequence_number=1)
            await asyncio.sleep(0)
            emit("model_call_completed", model_call_sequence_number=1)
            return self.app.WorkflowResult("AKS Answer")

        with patch.object(self.app, "orchestrate_request", run1):
            await self.app.on_message(self.Message("AKS question"))

        # Run 2: Terraform
        @observed_request(return_outcome=True)
        async def run2(question):
            select_route("terraform")
            emit("model_call_started", model_call_sequence_number=1)
            await asyncio.sleep(0)
            emit("model_call_completed", model_call_sequence_number=1)
            emit("mcp_dispatch", dispatch_count=1, tool_name="repo_file", operation="get_content")
            emit("mcp_result", dispatch_count=1, tool_name="repo_file", operation="get_content")
            return self.app.WorkflowResult("Terraform Answer")

        with patch.object(self.app, "orchestrate_request", run2):
            await self.app.on_message(self.Message("Terraform question"))

        # Both response messages are preserved in conversation history
        self.assertEqual(len(self.messages), 4)
        msg1 = self.messages[1]
        msg2 = self.messages[3]

        btn1 = next(el for el in msg1.elements if getattr(el, "name", None) == "ExecutionViewerButton")
        btn2 = next(el for el in msg2.elements if getattr(el, "name", None) == "ExecutionViewerButton")

        # Each button has its respective investigation's snapshot
        self.assertEqual(btn1.props["summary"]["category"], "AKS")
        self.assertEqual(btn2.props["summary"]["category"], "TERRAFORM")
        self.assertNotEqual(btn1.props["summary"]["category"], btn2.props["summary"]["category"])

    def test_execution_viewer_jsx_elements_exist_and_are_valid(self):
        btn_path = FRONTEND / "public" / "elements" / "ExecutionViewerButton.jsx"
        self.assertTrue(btn_path.exists())
        content = btn_path.read_text()
        self.assertIn("export default function ExecutionViewerButton", content)
        self.assertIn("ExecutionViewerModal", content)
        self.assertIn("View Execution Details", content)
        self.assertIn("Execution Flow Graph", content)
        self.assertIn("Step Details", content)
        self.assertIn("JSON", content)
        self.assertIn("Timeline & Logs", content)
        # Verify no bare imports that break in-browser Babel resolution
        self.assertNotIn('import ReactDOM from "react-dom"', content)
        self.assertNotIn("import React from 'react'", content)


if __name__ == "__main__":
    unittest.main()
