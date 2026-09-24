"""Offline SDK-error fixtures: no client/network initialization."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai import BadRequestError, AuthenticationError, PermissionDeniedError

import test_openai_flow as fixtures
from src.agent.main import cli
from src.observability import event_record, model_call, observed_request
from src.safe_diagnostics import safe_model_error_metadata


def api_error(status=400, body=None):
    response = SimpleNamespace(status_code=status, headers={"Authorization": "Bearer hidden"},
                               request=SimpleNamespace(content=b"private prompt and YAML"))
    cls = {400: BadRequestError, 401: AuthenticationError, 403: PermissionDeniedError}[status]
    return cls("private YAML /private/server password=hidden", response=response, body=body)


class ModelErrorMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def record_failure(self, error):
        client = fixtures.FakeOpenAIClient([error])

        @observed_request
        async def request():
            await model_call(client, model="gpt-5-mini", messages=[{"role": "user", "content": "private prompt"}])

        with self.assertLogs("src.observability", level="INFO") as logs:
            with self.assertRaises(type(error)) as raised:
                await request()
        self.assertIs(raised.exception, error)
        client.create.assert_awaited_once()
        records = [json.loads(r.getMessage()) for r in logs.records]
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertEqual(records[-1]["event"], "request_failed")
        for value in ("hidden", "private", "Authorization", "Bearer", "password"):
            self.assertNotIn(value, json.dumps(records))
        return next(r for r in records if r["event"] == "model_call_failed")

    async def test_400_preserves_only_safe_structured_metadata(self):
        fields = {"type": "invalid_request_error", "code": "invalid_value", "param": "messages[9].tool_call_id",
                  "message": "private YAML", "innererror": {"content": "private prompt"}}
        for body in (fields, {"error": fields}):
            with self.subTest(wrapped="error" in body):
                record = await self.record_failure(api_error(body=body))
                self.assertEqual(record["http_status"], 400)
                self.assertEqual(record["api_error_type"], "invalid_request_error")
                self.assertEqual(record["api_error_code"], "invalid_value")
                self.assertEqual(record["api_error_param"], "messages[9].tool_call_id")
                self.assertEqual((record["error_category"], record["reason_code"]), ("model", "model_failed"))

    async def test_authentication_and_authorization_keep_existing_categories(self):
        for status, category in ((401, "authentication"), (403, "authorization")):
            with self.subTest(status=status):
                record = await self.record_failure(api_error(status, {"type": "invalid_request_error"}))
                self.assertEqual(record["http_status"], status)
                self.assertEqual(record["error_category"], category)

    async def test_absent_or_malformed_metadata_is_not_parsed_from_text(self):
        for error in (RuntimeError("private YAML password=hidden"), api_error(body=None),
                      api_error(body="private YAML"), api_error(body={"type": [], "code": {}, "param": 9})):
            with self.subTest(error_type=type(error).__name__):
                record = await self.record_failure(error)
                self.assertFalse(any(k.startswith("api_error_") for k in record))

    async def test_sensitive_unknown_and_obfuscated_values_are_omitted(self):
        values = ["Bearer hidden", "api_key=hidden", "password=hidden", "Authorization",
                  "ConnectionString=hidden", "ｐａｓｓｗｏｒｄ=hidden", "pass\u200bword=hidden",
                  "/private/file.yml", "a" * 200, "innocent_looking_unrecognized_value"]
        for value in values:
            with self.subTest(value=value):
                error = api_error(body={"type": value, "code": value, "param": value})
                self.assertEqual(safe_model_error_metadata(error), {"http_status": 400})
                record = await self.record_failure(error)
                self.assertFalse(any(k.startswith("api_error_") for k in record))

    def test_event_boundary_revalidates_metadata(self):
        record = event_record("model_call_failed", http_status=True, api_error_type="unrecognized",
                              api_error_code="password=hidden", api_error_param="messages[1].private_file")
        self.assertEqual(set(record), {"event", "timestamp", "level"})
        record = event_record("model_call_failed", http_status=400, api_error_code="content_filter",
                              api_error_param="messages.9.content")
        self.assertEqual(record["api_error_code"], "content_filter")
        self.assertEqual(record["api_error_param"], "messages.9.content")


class ModelErrorPublicMessageTests(unittest.TestCase):
    def test_public_cli_message_is_unchanged(self):
        with patch("src.agent.main.main", new=AsyncMock(side_effect=api_error(body={"code": "invalid_value"}))), \
                patch("src.agent.main.parse_cli_question", return_value="question"), \
                patch("builtins.print") as output, self.assertLogs("src.agent.main", level="ERROR") as logs, \
                self.assertRaises(SystemExit) as stopped:
            cli()
        self.assertEqual(stopped.exception.code, 1)
        output.assert_called_once_with(
            "Request failed (model; model_failed). No sensitive diagnostic details are displayed.", flush=True)
        self.assertNotIn("hidden", str(logs.output))
