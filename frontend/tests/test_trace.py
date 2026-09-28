"""Unit tests for the Execution Trace graph, timeline, and structured details."""
import json
import unittest

from frontend.trace import TraceGraph


class TraceGraphTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.trace = TraceGraph(clock=lambda: self.now)

    def emit(self, name, **fields):
        return self.trace.accept({"event": name, **fields})

    def test_full_sequential_lifecycle_stages(self):
        self.emit("request_started")
        self.emit("route_selected", route="aks")
        self.emit("connections_start")
        self.now = 0.5
        self.emit("connections_ready")
        self.emit("model_call_started", model_call_sequence_number=1)
        self.now = 1.8
        self.emit("model_call_completed", model_call_sequence_number=1)
        self.emit("mcp_dispatch", dispatch_count=1, tool_name="kubectl_resources", operation="get")
        self.now = 2.4
        self.emit("mcp_result", dispatch_count=1, tool_name="kubectl_resources", operation="get")
        self.emit("model_call_started", model_call_sequence_number=2)
        self.now = 3.2
        self.emit("model_call_completed", model_call_sequence_number=2)
        self.emit("cleanup_completed")
        self.emit("request_completed", outcome="completed")

        snap = self.trace.snapshot()
        self.assertEqual(snap["summary"]["outcome"], "completed")
        self.assertEqual(snap["summary"]["category"], "AKS")
        self.assertEqual(snap["summary"]["model_call_count"], 2)
        self.assertEqual(snap["summary"]["tool_call_count"], 1)

        stages = [n["stage"] for n in snap["nodes"]]
        self.assertIn("user_request", stages)
        self.assertIn("init", stages)
        self.assertIn("mcp_start", stages)
        self.assertIn("llm_request", stages)
        self.assertIn("tool_execution", stages)
        self.assertIn("mcp_cleanup", stages)
        self.assertIn("terminal", stages)

        # Check edge continuity
        edges = snap["edges"]
        self.assertTrue(len(edges) >= len(snap["nodes"]) - 1)

        # Check timeline
        self.assertTrue(len(snap["timeline"]) >= 7)
        self.assertEqual(snap["timeline"][-1]["status"], "completed")

        # Check details
        self.assertEqual(len(snap["details"]["llm_calls"]), 2)
        self.assertEqual(len(snap["details"]["tool_calls"]), 1)
        self.assertEqual(snap["details"]["tool_calls"][0]["tool_name"], "kubectl_resources")
        self.assertEqual(snap["details"]["lifecycle"]["mcp_startup_duration_seconds"], 0.5)
        self.assertEqual(snap["details"]["lifecycle"]["mcp_cleanup_status"], "completed")

    def test_parallel_tool_dispatch_detection(self):
        self.emit("request_started")
        self.emit("model_call_started", model_call_sequence_number=1)
        self.emit("model_call_completed", model_call_sequence_number=1)

        # Dispatch two tools in parallel before either returns
        self.emit("mcp_dispatch", dispatch_count=1, tool_name="kubectl_resources", operation="get")
        self.emit("mcp_dispatch", dispatch_count=2, tool_name="repo_file", operation="get_content")

        snap_mid = self.trace.snapshot()
        tool_nodes = [n for n in snap_mid["nodes"] if n["stage"] == "tool_execution"]
        self.assertEqual(len(tool_nodes), 2)
        self.assertTrue(tool_nodes[0]["parallel"])
        self.assertTrue(tool_nodes[1]["parallel"])
        self.assertEqual(tool_nodes[0]["group_id"], tool_nodes[1]["group_id"])

        self.emit("mcp_result", dispatch_count=1, tool_name="kubectl_resources")
        self.emit("mcp_result", dispatch_count=2, tool_name="repo_file")
        self.emit("cleanup_completed")
        self.emit("request_completed", outcome="completed")

        snap_final = self.trace.snapshot()
        self.assertEqual(snap_final["summary"]["tool_call_count"], 2)

    def test_tool_failure_and_safe_error_reporting(self):
        self.emit("request_started")
        self.emit("mcp_dispatch", dispatch_count=1, tool_name="repo_repository")
        self.emit("mcp_failure", dispatch_count=1, tool_name="repo_repository",
                  error_category="authorization", error="secret_token=abc")
        self.emit("request_failed", error_category="authorization")

        snap = self.trace.snapshot()
        self.assertEqual(snap["summary"]["outcome"], "failed")

        tool_node = [n for n in snap["nodes"] if n["stage"] == "tool_execution"][0]
        self.assertEqual(tool_node["status"], "failed")
        self.assertIn("Access was denied", tool_node["error"])
        self.assertNotIn("secret_token", json.dumps(snap))
        self.assertNotIn("abc", json.dumps(snap))

    def test_bounded_and_cancellation_states(self):
        self.emit("request_started")
        self.emit("model_call_started", model_call_sequence_number=1)
        self.emit("request_bounded")
        self.emit("request_completed", outcome="bounded")

        snap = self.trace.snapshot()
        self.assertEqual(snap["summary"]["outcome"], "stopped")
        self.assertTrue(snap["details"]["lifecycle"]["safety_bounded"])
        self.assertEqual(snap["nodes"][-1]["status"], "stopped")

        # Cancellation test
        trace_c = TraceGraph()
        trace_c.accept({"event": "request_started"})
        trace_c.accept({"event": "model_call_started", "model_call_sequence_number": 1})
        trace_c.end("cancelled", "Investigation cancelled")
        snap_c = trace_c.snapshot()
        self.assertEqual(snap_c["summary"]["outcome"], "cancelled")
        self.assertEqual(snap_c["nodes"][1]["status"], "unconfirmed")

    def test_connection_status_mapping_and_unknown_defaults(self):
        # 1. Defaults must all be unknown / unavailable
        snap_init = self.trace.snapshot()
        conns = snap_init["connections"]
        self.assertEqual(conns["azure"]["status"], "unknown")
        self.assertEqual(conns["azure_devops"]["status"], "unknown")
        self.assertEqual(conns["aks"]["status"], "unknown")
        self.assertEqual(conns["mcp"]["status"], "unknown")

        # 2. MCP connects and becomes ready
        self.emit("connections_start")
        self.assertEqual(self.trace.snapshot()["connections"]["mcp"]["status"], "connecting")
        self.emit("connections_ready")
        self.assertEqual(self.trace.snapshot()["connections"]["mcp"]["status"], "ready")

        # Azure and AKS remain unknown until tool execution verifies them
        self.assertEqual(self.trace.snapshot()["connections"]["azure"]["status"], "unknown")
        self.assertEqual(self.trace.snapshot()["connections"]["aks"]["status"], "unknown")

        # 3. kubectl Tool execution verifies AKS (Azure remains unknown)
        self.emit("mcp_dispatch", dispatch_count=1, tool_name="kubectl_resources", operation="get")
        self.assertEqual(self.trace.snapshot()["connections"]["aks"]["status"], "connecting")
        self.emit("mcp_result", dispatch_count=1, tool_name="kubectl_resources", operation="get")
        self.assertEqual(self.trace.snapshot()["connections"]["aks"]["status"], "ready")
        self.assertEqual(self.trace.snapshot()["connections"]["azure"]["status"], "unknown")

        # 4. Azure ARM / AKS operations verify Azure connection
        self.emit("mcp_dispatch", dispatch_count=2, tool_name="az_aks_operations", operation="show")
        self.emit("mcp_result", dispatch_count=2, tool_name="az_aks_operations", operation="show")
        self.assertEqual(self.trace.snapshot()["connections"]["azure"]["status"], "ready")

        # Azure DevOps remains unknown
        self.assertEqual(self.trace.snapshot()["connections"]["azure_devops"]["status"], "unknown")

        # 5. Azure DevOps tool execution verifies ADO
        self.emit("mcp_dispatch", dispatch_count=3, tool_name="repo_file", operation="get_content")
        self.assertEqual(self.trace.snapshot()["connections"]["azure_devops"]["status"], "connecting")
        self.emit("mcp_result", dispatch_count=3, tool_name="repo_file", operation="get_content")
        self.assertEqual(self.trace.snapshot()["connections"]["azure_devops"]["status"], "ready")

        # 6. MCP Cleanup marks MCP stopped (clean shutdown)
        self.emit("cleanup_completed")
        self.assertEqual(self.trace.snapshot()["connections"]["mcp"]["status"], "stopped")
        self.assertEqual(self.trace.snapshot()["connections"]["mcp"]["label"], "Stopped")

    def test_connection_status_failure_transitions(self):
        self.emit("connections_start")
        self.emit("connections_ready")

        # Tool failure with auth error marks AKS and Azure failed
        self.emit("mcp_dispatch", dispatch_count=1, tool_name="az_aks_operations", operation="show")
        self.emit("mcp_failure", dispatch_count=1, tool_name="az_aks_operations",
                  error_category="authentication", error="auth failed")
        conns = self.trace.snapshot()["connections"]
        self.assertEqual(conns["aks"]["status"], "failed")
        self.assertEqual(conns["azure"]["status"], "failed")
        self.assertIn("Auth Failed", conns["azure"]["label"])

        # ADO failure
        self.emit("mcp_dispatch", dispatch_count=2, tool_name="pipelines_definition")
        self.emit("mcp_failure", dispatch_count=2, tool_name="pipelines_definition", error_category="mcp")
        self.assertEqual(self.trace.snapshot()["connections"]["azure_devops"]["status"], "failed")

        # Cleanup failure marks MCP failed
        self.emit("cleanup_failed")
        self.assertEqual(self.trace.snapshot()["connections"]["mcp"]["status"], "failed")

    def test_no_inference_from_unrelated_tools(self):
        # Inspecting repo or Terraform files must never mark Azure or AKS as Connected
        self.emit("request_started")
        self.emit("route_selected", route="terraform")
        self.emit("connections_start")
        self.emit("connections_ready")
        self.emit("mcp_dispatch", dispatch_count=1, tool_name="repo_file", operation="get_content")
        self.emit("mcp_result", dispatch_count=1, tool_name="repo_file", operation="get_content")

        snap = self.trace.snapshot()
        self.assertEqual(snap["connections"]["azure_devops"]["status"], "ready")
        self.assertEqual(snap["connections"]["azure"]["status"], "unknown")
        self.assertEqual(snap["connections"]["aks"]["status"], "unknown")
        self.assertEqual(snap["connections"]["mcp"]["status"], "ready")

        self.emit("cleanup_completed")
        snap_end = self.trace.snapshot()
        self.assertEqual(snap_end["connections"]["mcp"]["status"], "stopped")
        self.assertEqual(snap_end["connections"]["mcp"]["label"], "Stopped")
