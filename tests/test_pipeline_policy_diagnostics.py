"""Policy diagnostics only; no MCP or model clients are constructed."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from src.agent.main import cli
from src.observability import observed_request
from src.policy.policy import enforce_read_only_policy, READ_ONLY_POLICY
from src.safe_diagnostics import classify_error


class PipelinePolicyDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, arguments, expected_reason=None, name="pipelines_definition"):
        call = {"name": name, "args": dict(arguments), "id": "policy-test"}
        dispatch = AsyncMock()

        @observed_request
        async def request():
            enforce_read_only_policy(call)
            await dispatch()

        with self.assertLogs("src.observability", level="INFO") as logs:
            if expected_reason:
                with self.assertRaises(PermissionError) as caught:
                    await request()
                self.assertEqual(str(caught.exception), "Tool call blocked by read-only policy")
                self.assertEqual(classify_error(caught.exception).user_message(),
                    "Request failed (policy; policy_blocked). No sensitive diagnostic details are displayed.")
                dispatch.assert_not_awaited()
            else:
                await request()
                dispatch.assert_awaited_once()
        records = [json.loads(r.getMessage()) for r in logs.records]
        decision = next(r for r in records if r["event"] == "policy_decision")
        self.assertEqual(decision["policy_decision"], "reject" if expected_reason else "allow")
        self.assertEqual(decision["tool_call_id"], "policy-test")
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        if expected_reason:
            self.assertEqual(decision["error_category"], "policy")
            self.assertEqual(decision["reason_code"], expected_reason)
            self.assertEqual(records[-1]["reason_code"], "policy_blocked")
        for value in ("private-value", "password", "My Project", "arguments", "prompt"):
            self.assertNotIn(value, json.dumps(records))
        return call, decision

    async def test_missing_and_invalid_actions(self):
        for args in ({}, {"action": None}, {"action": "get"}, {"action": "LIST"},
                     {"action": "password=private-value"}, {"action": 7}):
            with self.subTest(args=args):
                call, decision = await self.check(args, "action_not_allowed")
                self.assertNotIn("project", call["args"])  # Action check still runs first.
                self.assertNotIn("operation", decision)

    async def test_list_revisions_stays_forbidden(self):
        self.assertEqual(READ_ONLY_POLICY["pipelines_definition"], {"list"})
        await self.check({"action": "list_revisions", "project": "My Project"}, "action_not_allowed")

    async def test_wrong_project_is_not_normalized(self):
        for project in ("Other", "My Project ", None, "password=private-value"):
            with self.subTest(project=project):
                call, decision = await self.check({"action": "list", "project": project}, "project_mismatch")
                self.assertEqual(call["args"]["project"], project)
                self.assertEqual(decision["operation"], "list")

    async def test_correct_action_and_project_are_allowed(self):
        call, decision = await self.check({"action": "list", "project": "My Project"})
        self.assertEqual(call["args"], {"action": "list", "project": "My Project"})
        self.assertNotIn("reason_code", decision)

    async def test_missing_project_is_injected(self):
        call, _ = await self.check({"action": "list"})
        self.assertEqual(call["args"]["project"], "My Project")

    async def test_action_precedence_and_other_tools_unchanged(self):
        await self.check({"action": "list_revisions", "project": "Other"}, "action_not_allowed")
        await self.check({"action": "list", "project": "Other"}, "policy_blocked", name="repo_repository")


class PipelinePolicyPublicErrorTests(unittest.TestCase):
    def test_cli_message_remains_generic(self):
        for args in ({"action": "list_revisions"}, {"action": "list", "project": "Other"}):
            with self.subTest(args=args):
                try:
                    enforce_read_only_policy({"name": "pipelines_definition", "args": args})
                except PermissionError as error:
                    with patch("src.agent.main.main", new=AsyncMock(side_effect=error)), \
                            patch("src.agent.main.parse_cli_question", return_value="question"), \
                            patch("builtins.print") as output, self.assertRaises(SystemExit) as stopped:
                        cli()
                self.assertEqual(stopped.exception.code, 1)
                output.assert_called_once_with(
                    "Request failed (policy; policy_blocked). No sensitive diagnostic details are displayed.", flush=True)


class PipelineActionDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def check_action(self, arguments, expected):
        call = {"name": "pipelines_definition", "args": dict(arguments), "id": "action-test"}

        @observed_request
        async def request():
            try:
                enforce_read_only_policy(call)
            except PermissionError:
                pass

        with self.assertLogs("src.observability", level="INFO") as logs:
            await request()
        records = [json.loads(record.getMessage()) for record in logs.records]
        decision = next(record for record in records if record["event"] == "policy_decision")
        self.assertEqual(
            {key: decision[key] for key in ("action_present", "action_category", "action_allowed")},
            expected,
        )
        self.assertNotIn("action", decision)
        return decision

    async def test_allowed_action_classification(self):
        await self.check_action(
            {"action": "list", "project": "My Project"},
            {"action_present": True, "action_category": "allowed", "action_allowed": True},
        )

    async def test_unsupported_action_classification(self):
        await self.check_action(
            {"action": "list_revisions", "project": "My Project"},
            {"action_present": True, "action_category": "unsupported", "action_allowed": False},
        )

    async def test_missing_action_classification(self):
        await self.check_action(
            {"project": "My Project"},
            {"action_present": False, "action_category": "missing", "action_allowed": False},
        )

    async def test_non_string_action_classification(self):
        await self.check_action(
            {"action": 7, "project": "My Project"},
            {"action_present": True, "action_category": "non_string", "action_allowed": False},
        )
