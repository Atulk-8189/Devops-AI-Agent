"""Offline structural diagnostics; no SDK client or MCP startup."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.model_request_diagnostics import request_structure
from src.observability import model_call, observed_request


def paired():
    return [
        {"role": "user", "content": "PRIVATE_QUESTION"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "PRIVATE_ID", "type": "function", "function": {
                "name": "repo_file", "arguments": '{"path":"PRIVATE_PATH"}'}}]},
        {"role": "tool", "tool_call_id": "PRIVATE_ID", "content": [
            {"type": "text", "text": "PRIVATE_YAML"}]}]


class StructureTests(unittest.TestCase):
    def test_valid_pairing_null_assistant_and_text_parts(self):
        messages = paired()
        before = copy.deepcopy(messages)
        result = request_structure(messages)
        self.assertTrue(result["message_structure_valid"])
        self.assertTrue(result["tool_pairing_valid"])
        self.assertEqual(result["message_structure"][1]["content_type"], "null")
        self.assertEqual(result["message_structure"][1]["tool_result_count"], 1)
        self.assertTrue(result["message_structure"][2]["tool_call_id_matches_previous_assistant"])
        self.assertEqual(result["total_content_chars"], len("PRIVATE_QUESTIONPRIVATE_YAML"))
        self.assertEqual(result["serialized_messages_chars"], len(json.dumps(messages, ensure_ascii=False)))
        self.assertEqual(messages, before)

    def test_orphan_mismatch_missing_duplicate_and_interrupted_pairs(self):
        orphan = [paired()[-1]]
        mismatch = paired()
        mismatch[-1]["tool_call_id"] = "different"
        interrupted = paired()
        interrupted.insert(2, {"role": "user", "content": "interruption"})
        for messages in (orphan, mismatch, paired()[:-1], paired() + [paired()[-1]], interrupted):
            with self.subTest(case=len(messages)):
                result = request_structure(messages)
                self.assertFalse(result["tool_pairing_valid"])
                self.assertFalse(result["message_structure_valid"])

    def test_multiple_calls_require_exactly_one_result_each(self):
        messages = paired()
        call = copy.deepcopy(messages[1]["tool_calls"][0])
        call["id"] = "another"
        messages[1]["tool_calls"].append(call)
        self.assertFalse(request_structure(messages)["tool_pairing_valid"])
        messages.append({"role": "tool", "tool_call_id": "another", "content": "result"})
        self.assertTrue(request_structure(messages)["tool_pairing_valid"])

    def test_content_types_and_malformed_structure(self):
        for content, valid, kind in (("text", True, "string"),
                                     ([{"type": "text", "text": "text"}], True, "list"),
                                     (None, False, "null"), ({}, False, "invalid"),
                                     ([{"type": "image_url", "text": "text"}], False, "list")):
            with self.subTest(kind=kind, valid=valid):
                result = request_structure([{"role": "user", "content": content}])
                self.assertEqual(result["message_structure_valid"], valid)
                self.assertEqual(result["message_structure"][0]["content_type"], kind)
        self.assertFalse(request_structure([None])["message_structure_valid"])

    def test_sensitive_values_and_unknown_names_never_emitted(self):
        secret = "PRIVATE_PASSWORD=Bearer PRIVATE_TOKEN"
        messages = paired()
        messages[1]["tool_calls"][0]["function"].update(name=secret, arguments=secret)
        messages[0]["extra"] = secret
        messages.append({"role": secret, "content": secret})
        tools = [{"type": "function", "function": {
            "name": name, "description": secret, "parameters": {"path": secret}}}
            for name in (secret, "repo_file")]
        result = request_structure(messages, tools)
        self.assertEqual(result["tool_names"], ["repo_file"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("arguments", json.dumps(result))

    def test_snapshot_is_bounded(self):
        result = request_structure([{"role": "user", "content": "x"}] * 129)
        self.assertEqual(result["message_count"], 129)
        self.assertEqual(len(result["message_structure"]), 128)
        self.assertFalse(result["snapshot_complete"])
        self.assertFalse(result["message_structure_valid"])


class InstrumentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequence_correlation_failure_and_unchanged_request(self):
        create = AsyncMock(side_effect=[SimpleNamespace(choices=[]), RuntimeError("PRIVATE_EXCEPTION")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        messages = paired()
        before = copy.deepcopy(messages)

        @observed_request
        async def request():
            await model_call(client, messages=messages)
            with self.assertRaises(RuntimeError):
                await model_call(client, messages=messages)

        with self.assertLogs("src.observability", level="INFO") as logs:
            await request()
        records = [json.loads(record.getMessage()) for record in logs.records]
        snapshots = [r for r in records if r["event"] == "model_request_structure"]
        self.assertEqual([r["model_call_sequence_number"] for r in snapshots], [1, 2])
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        failed = next(r for r in records if r["event"] == "model_call_failed")
        self.assertEqual(failed["model_call_sequence_number"], 2)
        self.assertNotIn("PRIVATE", json.dumps(records))
        self.assertEqual(messages, before)
        self.assertIs(create.call_args.kwargs["messages"], messages)

    async def test_diagnostic_failure_does_not_block_call_and_sequence_resets(self):
        create = AsyncMock(return_value=SimpleNamespace(choices=[]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        @observed_request
        async def request():
            await model_call(client, messages=paired())

        with patch("src.observability.request_structure", side_effect=RuntimeError("PRIVATE")):
            await request()
        with self.assertLogs("src.observability", level="INFO") as logs:
            await request()
            await request()
        snapshots = [json.loads(r.getMessage()) for r in logs.records
                     if json.loads(r.getMessage())["event"] == "model_request_structure"]
        self.assertEqual([r["model_call_sequence_number"] for r in snapshots], [1, 1])
        self.assertNotEqual(snapshots[0]["request_id"], snapshots[1]["request_id"])
        self.assertEqual(create.await_count, 3)
