"""Offline tests for presentation, lifecycle isolation and durable metadata."""
import asyncio
import json
import logging
from pathlib import Path
import tempfile
import unittest

from frontend.data_layer import SQLiteDataLayer
from frontend.investigation import (
    CAPABILITIES, CONNECTIONS, EventHandler, Investigation, _active,
)


class InvestigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cards_use_normal_chat_flow(self):
        self.assertEqual([card[2] for card in CAPABILITIES], [
            "Check AKS cluster health.", "Investigate the latest pipeline failure.",
            "Review my Terraform configuration.", "Help me troubleshoot this infrastructure issue.",
        ])
        source = (Path(__file__).parents[1] / "public/elements/Welcome.jsx").read_text()
        self.assertIn("sendUserMessage(prompt)", source)
        self.assertIn("props.cards.map", source)
        self.assertNotIn("callAction", source)

    async def test_connections_never_claim_verification(self):
        self.assertEqual(CONNECTIONS.count("Unknown — Status unavailable"), 3)
        self.assertNotIn("Connected", CONNECTIONS)
        self.assertNotIn("Disconnected", CONNECTIONS)

    async def test_events_only_report_observed_stages(self):
        investigation = Investigation()
        self.assertTrue(investigation.queue.empty())
        investigation.accept({"event": "route_selected", "route": "aks_cluster_health", "args": "secret"})
        self.assertEqual(investigation.metadata["category"], "AKS")
        investigation.accept({"event": "mcp_dispatch", "tool_output": "secret"})
        self.assertEqual(investigation.queue.qsize(), 2)
        investigation.accept({"event": "request_completed", "outcome": "bounded"})
        investigation.accept({"event": "evidence_result", "truncated": True})
        self.assertIn("Incomplete", investigation.metadata["coverage"])
        self.assertTrue(investigation.metadata["collection_truncated"])
        self.assertNotIn("secret", json.dumps(investigation.metadata))

    async def test_listener_context_isolation(self):
        handler = EventHandler()
        record = logging.LogRecord("src.observability", logging.INFO, "", 0,
                                   '{"event":"route_selected","route":"terraform"}', (), None)
        one, two = Investigation(), Investigation()
        async def receive(investigation):
            token = _active.set(investigation)
            try:
                await asyncio.sleep(0)
                handler.emit(record)
            finally:
                _active.reset(token)
        await asyncio.gather(receive(one), receive(two))
        handler.emit(record)
        self.assertEqual(one.queue.qsize(), 1)
        self.assertEqual(two.queue.qsize(), 1)

    async def test_explicit_evidence_and_original_preserved(self):
        original = ' {"cluster_scope":{"cluster":"sample","namespace":"default"}, "missing_evidence":["logs"], "unrelated":"kept in answer"}\n'
        investigation = Investigation()
        metadata = investigation.finish(original)
        self.assertEqual(metadata["agent_reported_details"]["missing_evidence"], ["logs"])
        self.assertEqual(metadata["agent_reported_details"]["cluster_scope"]["cluster"], "sample")
        self.assertEqual(metadata["coverage"], "Not reported")
        self.assertEqual(metadata["target"], "sample")
        self.assertTrue(original.endswith('\n'))
        self.assertEqual(Investigation().finish("plain answer")["target"], "Not reported")

    async def test_restart_restore_and_continue_without_cross_thread_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            layer = SQLiteDataLayer(path)
            metadata = {"investigation": Investigation().finish('{"limitations":["logs unavailable"]}')}
            await layer.create_step({"id": "status-one", "threadId": "one", "metadata": metadata, "output": "Read-only"})
            await layer.create_step({"id": "answer-one", "threadId": "one", "output": " exact answer\r\n"})
            await layer.create_step({"id": "status-two", "threadId": "two", "metadata": {}})
            layer._conn.close()
            restored = SQLiteDataLayer(path)
            first = await restored.get_thread("one")
            self.assertEqual(first["steps"][0]["metadata"], metadata)
            self.assertEqual(first["steps"][1]["output"], " exact answer\r\n")
            self.assertEqual((await restored.get_thread("two"))["steps"][0]["metadata"], {})
            await restored.create_step({"id": "continued", "threadId": "one", "output": "next request"})
            self.assertEqual(len((await restored.get_thread("one"))["steps"]), 3)
            restored._conn.close()
