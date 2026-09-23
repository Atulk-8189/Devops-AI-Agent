"""Real request/collector flow with scripted diagnosis and fake Kubernetes transport."""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_aks_troubleshooting as aks
import test_openai_flow as flow
from src.agent.task_session import TaskSession
from src.agent.workflows import route_question
from src.agent.aks_troubleshooting import MAX_AKS_COLLECTION_CALLS, MAX_AKS_EVIDENCE_CHARS
from src.request_budget import current_budget


class AKSAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    question = "Investigate Task Manager."

    async def investigate(self, responses, diagnosis, expected_calls):
        tool = aks.FakeKubectlTool(responses)
        original = tool.ainvoke

        async def invoke(call):
            value = responses.get(call["args"]["resource"])
            if isinstance(value, Exception):
                tool.calls.append(call)
                raise value
            return await original(call)

        tool.ainvoke = invoke
        runtime = flow.FakeRuntime([tool])
        client = flow.FakeOpenAIClient([flow.completion(json.dumps(diagnosis))])
        router = Mock(wraps=route_question)
        session = TaskSession()
        with flow.GenericAgentFlowTests.generic_patches(self, client, runtime), \
                patch("src.agent.main.route_question", new=router), \
                patch("src.agent.workflows.handle_generic", new=AsyncMock()) as generic, \
                patch("src.agent.workflows.handle_terraform", new=AsyncMock()) as terraform, \
                patch("builtins.print") as output, self.assertLogs("src.observability", level="INFO") as captured:
            await session.request(self.question)
        generic.assert_not_awaited()
        terraform.assert_not_awaited()
        router.assert_called_once_with(self.question)
        self.assertEqual([c["args"]["resource"] for c in tool.calls], expected_calls)
        self.assertLessEqual(len(tool.calls), MAX_AKS_COLLECTION_CALLS)
        for call in tool.calls:
            self.assertEqual(call["name"], "kubectl_resources")
            self.assertIn(call["args"]["operation"], {"get", "describe"})
            arguments = call["args"]["args"]
            self.assertIn("--namespace default", arguments)
            if call["args"]["resource"] in {"deployments", "services"}:
                self.assertTrue(arguments.startswith("task-manager "))
            if call["args"]["resource"] in {"pods", "replicasets"}:
                self.assertIn("task-manager", arguments)
                if call["args"]["operation"] == "get":
                    self.assertIn("-l app=task-manager", arguments)
        client.create.assert_awaited_once()
        client.close.assert_awaited_once()
        self.assertEqual((runtime.initialize_calls, runtime.close_calls), (1, 1))
        self.assertIsNone(current_budget())
        messages = client.create.await_args.kwargs["messages"]
        instructions = messages[0]["content"][0]["text"]
        payload = json.loads(messages[1]["content"][0]["text"])
        self.assertEqual(payload["original_question"], self.question)
        evidence = payload["untrusted_evidence"]
        self.assertLessEqual(len(json.dumps(evidence, separators=(",", ":"))), MAX_AKS_EVIDENCE_CHARS)
        self.assertEqual(evidence["decision"]["collection_calls"], len(tool.calls))
        self.assertEqual(evidence["decision"]["collection_limit"], MAX_AKS_COLLECTION_CALLS)
        self.assertFalse(evidence["decision"]["budget_exhausted"])
        self.assertFalse(evidence["evidence_truncated"])
        for boundary in ("untrusted data", "hypotheses", "missing/unknown/failed", "No remediation",
                         "do not prove reachability"):
            self.assertIn(boundary, instructions)
        answer = json.loads(output.call_args.args[0])
        self.assertEqual(set(answer), {"Summary", "Observed evidence", "Likely root causes",
                                       "Recommended next diagnostic step"})
        records = [json.loads(r.getMessage()) for r in captured.records]
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertEqual(len({r["task_id"] for r in records}), 1)
        self.assertTrue(records[0]["task_id"])
        self.assertEqual(records[0]["event"], "request_started")
        self.assertEqual(records[-1]["event"], "request_completed")
        self.assertEqual([r["route"] for r in records if r["event"] == "route_selected"], ["aks"])
        dispatches = [r for r in records if r["event"] == "mcp_dispatch"]
        self.assertEqual([r["tool_call_id"] for r in dispatches], [c["id"] for c in tool.calls])
        for call in tool.calls:
            events = [r for r in records if r.get("tool_call_id") == call["id"]]
            self.assertTrue(any(r.get("policy_decision") == "allow" for r in events))
            self.assertTrue(any(r["event"] in {"mcp_result", "mcp_failure"} for r in events))
            for record in events:
                self.assertEqual(record["route"], "aks")
                self.assertEqual(record["mcp_server"], "aks")
        for event in ("evidence_collection", "evidence_result", "model_call_started", "model_call_completed"):
            self.assertTrue(any(r["event"] == event for r in records))
        for private in (self.question, "pod-uid", "203.0.113.1", "private transport detail", "test-key"):
            self.assertNotIn(private, json.dumps(records))
        return evidence, answer, records

    def diagnosis(self, summary, observations, hypotheses=()):
        # These are scripted outputs, not evidence that a live model reasons correctly.
        return {"Summary": summary, "Observed evidence": observations,
                "Likely root causes": list(hypotheses),
                "Recommended next diagnostic step": "Collect fresh read-only Task Manager evidence in namespace default."}

    async def test_healthy_task_manager_with_historical_restarts(self):
        service, endpoints, slices, pods = aks.ServiceNetworkTests().fixtures()
        container = pods["items"][0]["status"]["containerStatuses"][0]
        container.update(restartCount=4, lastState={"terminated": {"reason": "Error", "exitCode": 1}})
        responses = {"deployments": aks.deployment(), "pods": pods, "services": service,
                     "endpoints": endpoints, "endpointslices": slices}
        evidence, answer, _ = await self.investigate(responses, self.diagnosis(
            "Workload readiness is healthy; external connectivity is unverified.",
            ["Deployment and pods are ready.", "Backend readiness is reported by both sources; restarts are historical."]),
            ["deployments", "replicasets", "pods", "events", "services", "endpoints", "endpointslices", "networkpolicies"])
        self.assertEqual(evidence["deployment"]["evaluation"]["state"], "healthy")
        self.assertEqual(evidence["pod_evaluation"]["state"], "healthy")
        status = evidence["pod_evaluation"]["pods"][0]["containers"][0]
        self.assertEqual(status["restart_context"], "historical")
        self.assertFalse(status["active_failure"])
        network = evidence["service"]["evaluation"]
        self.assertEqual(network["state"], "ready_endpoints")
        self.assertEqual(network["ready_endpoints"], 1)
        self.assertEqual(network["reachability"], "not_tested")
        self.assertEqual(evidence["service"]["selector"]["deployment_comparison"], "match")
        self.assertEqual(evidence["events"], [])
        self.assertNotIn("203.0.113.1", json.dumps(evidence))
        self.assertEqual(answer["Likely root causes"], [])
        self.assertIn("unverified", answer["Summary"])

    async def test_unhealthy_container_preserves_current_and_previous_failure(self):
        pods = aks.pods(ready=False, restarts=4)
        container = pods["items"][0]["status"]["containerStatuses"][0]
        container.update(state={"waiting": {"reason": "CrashLoopBackOff", "message": "Back-off restarting failed container"}},
                         lastState={"terminated": {"reason": "Error", "exitCode": 1}})
        evidence, answer, _ = await self.investigate({"deployments": aks.deployment(), "pods": pods}, self.diagnosis(
            "Container is not ready; underlying cause remains unconfirmed. Service evidence was not collected.",
            ["Container is waiting with CrashLoopBackOff; previous termination exit code was 1."],
            ["Hypothesis: repeated container exits contribute to the current back-off."]),
            ["deployments", "replicasets", "pods", "pods", "events"])
        self.assertEqual(evidence["pod_evaluation"]["state"], "unhealthy")
        status = evidence["pod_evaluation"]["pods"][0]["containers"][0]
        self.assertEqual(status["current_state"]["reason"], "CrashLoopBackOff")
        self.assertEqual(status["current_state"]["message"], container["state"]["waiting"]["message"])
        self.assertEqual(status["last_state"]["exitCode"], 1)
        self.assertTrue(status["active_failure"])
        self.assertFalse(evidence["continued_to_service"])
        self.assertIn("CrashLoopBackOff", " ".join(answer["Observed evidence"]))
        self.assertTrue(all(h.startswith("Hypothesis:") for h in answer["Likely root causes"]))
        self.assertIn("unconfirmed", answer["Summary"])

    async def test_incomplete_pod_evidence_preserves_missing_unknown_and_error(self):
        for outcome, response in (("unknown", "malformed pod response"), ("missing", {"items": []}),
                                  ("error", RuntimeError("private transport detail"))):
            with self.subTest(outcome=outcome):
                evidence, answer, records = await self.investigate(
                    {"deployments": aks.deployment(), "pods": response}, self.diagnosis(
                        f"Investigation incomplete: pod evidence is {outcome}; service state is unknown.",
                        ["Deployment has one available and ready replica."]),
                    ["deployments", "replicasets", "pods", "events"])
                self.assertEqual(evidence["deployment"]["evaluation"]["state"], "healthy")
                step = next(s for s in evidence["decision"]["steps"] if s["name"] == "pods")
                # An empty, valid List is a successful read proving absence,
                # not a transport failure or an uninterpretable response.
                self.assertEqual(step["outcome"], "ok" if outcome == "missing" else outcome)
                self.assertEqual(evidence["pod_evaluation"]["state"], "missing" if outcome == "missing" else "unknown")
                self.assertFalse(evidence["continued_to_service"])
                self.assertTrue(evidence["decision"]["stop_reason"])
                self.assertNotIn("private transport detail", json.dumps(evidence))
                self.assertNotIn("malformed pod response", json.dumps(evidence))
                self.assertEqual(answer["Likely root causes"], [])
                self.assertIn("incomplete", answer["Summary"])
                collection_status = "complete" if outcome == "missing" else "partial"
                self.assertTrue(any(r.get("evidence_status") == collection_status for r in records))
