"""Structured execution trace graph, timeline, and details for frontend visualization."""
from copy import deepcopy
from time import monotonic

from frontend.activity import TOOL_PURPOSES, OPERATIONS, ERRORS, safe_error, tool_label, EVENTS


class TraceGraph:
    def __init__(self, clock=monotonic, started=None):
        self.clock = clock
        self.started = started if started is not None else clock()
        self.nodes = []
        self.edges = []
        self.timeline = []
        self.pending_nodes = {}  # key -> node_id
        self.active_tool_ids = []  # list of currently active tool node_ids
        self.last_join_nodes = []  # list of node_ids that should connect to the next step
        self.last_node_id = None
        self.parallel_counter = 0
        self.current_parallel_group = None

        self.model_call_count = 0
        self.tool_call_count = 0
        self.terminal = False
        self.outcome = "active"
        self.category = "Other"
        self.elapsed = 0.0

        # Structured details tracking
        self.llm_calls = []
        self.tool_calls = []
        self.lifecycle_events = {
            "mcp_startup_duration_seconds": None,
            "mcp_cleanup_status": None,
            "safety_bounded": False,
        }
        self.connections = {
            "azure": {"name": "Azure", "status": "unknown", "label": "Unknown"},
            "azure_devops": {"name": "Azure DevOps", "status": "unknown", "label": "Unknown"},
            "aks": {"name": "AKS", "status": "unknown", "label": "Unknown"},
            "mcp": {"name": "MCP", "status": "unknown", "label": "Unknown"},
        }
        self.metadata = {}

    def _now(self):
        return max(0.0, self.clock() - self.started)

    def _add_node(self, stage, title, subtitle="", status="completed", category="system",
                  parallel=False, group_id=None, connect_from=None):
        self.elapsed = self._now()
        node_id = f"node-{len(self.nodes)}"
        node = {
            "id": node_id,
            "stage": stage,
            "title": title,
            "subtitle": subtitle,
            "status": status,
            "category": category,
            "started_seconds": round(self.elapsed, 3),
            "duration_seconds": None,
            "parallel": parallel,
            "group_id": group_id,
            "error": None,
        }
        self.nodes.append(node)

        # Connect edges
        sources = []
        if connect_from is not None:
            if isinstance(connect_from, list):
                sources = connect_from
            else:
                sources = [connect_from]
        elif self.last_join_nodes:
            sources = list(self.last_join_nodes)
        elif self.last_node_id:
            sources = [self.last_node_id]

        for src in sources:
            if src != node_id:
                edge_id = f"edge-{src}-{node_id}"
                self.edges.append({
                    "id": edge_id,
                    "source": src,
                    "target": node_id,
                })

        return node

    def _add_timeline(self, title, category="system", status="completed", duration_seconds=None, details=""):
        self.elapsed = self._now()
        item = {
            "id": len(self.timeline),
            "timestamp": f"+{self.elapsed:.2f}s",
            "relative_seconds": round(self.elapsed, 3),
            "title": title,
            "category": category,
            "status": status,
            "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else None,
            "details": details,
        }
        self.timeline.append(item)
        return item

    def _key(self, family, event):
        field = "dispatch_count" if family == "mcp" else "model_call_sequence_number"
        value = event.get(field)
        return (family, value if type(value) is int else None)

    def accept(self, event):
        name = event.get("event")
        if not isinstance(name, str) or name not in EVENTS or self.terminal:
            return False

        self.elapsed = self._now()

        if name == "request_started":
            node = self._add_node(
                stage="user_request",
                title="User Request",
                subtitle="Investigation initiated",
                status="completed",
                category="user",
            )
            self.last_node_id = node["id"]
            self.last_join_nodes = [node["id"]]
            self._add_timeline("User request received", category="user", status="completed")

        elif name == "route_selected":
            route = event.get("route", "generic")
            self.category = route.upper() if route in {"aks", "terraform"} else route.replace("_", " ").title()
            node = self._add_node(
                stage="init",
                title="Initialize Investigation",
                subtitle=f"Category: {self.category}",
                status="completed",
                category="orchestration",
            )
            self.last_node_id = node["id"]
            self.last_join_nodes = [node["id"]]
            self._add_timeline(f"Investigation initialized ({self.category})", category="orchestration", status="completed")

        elif name == "connections_start":
            self.connections["mcp"] = {"name": "MCP", "status": "connecting", "label": "Connecting"}
            node = self._add_node(
                stage="mcp_start",
                title="Start MCP Connections",
                subtitle="Initializing runtime & tool discovery",
                status="active",
                category="mcp",
            )
            self.pending_nodes[("connections", None)] = node["id"]
            self.last_node_id = node["id"]
            self.last_join_nodes = [node["id"]]
            self._add_timeline("Starting MCP connections & tool discovery", category="mcp", status="active")

        elif name == "connections_ready":
            self.connections["mcp"] = {"name": "MCP", "status": "ready", "label": "Ready"}
            node_id = self.pending_nodes.pop(("connections", None), None)
            duration = None
            if node_id:
                for n in self.nodes:
                    if n["id"] == node_id:
                        n["status"] = "completed"
                        duration = max(0.0, self.elapsed - n["started_seconds"])
                        n["duration_seconds"] = round(duration, 3)
                        n["subtitle"] = f"Runtime ready ({duration:.1f}s)"
                        self.lifecycle_events["mcp_startup_duration_seconds"] = round(duration, 3)
                        break
            self._add_timeline("MCP connections & tools ready", category="mcp", status="completed", duration_seconds=duration)

        elif name == "model_call_started":
            self.model_call_count += 1
            seq = self.model_call_count
            title = "LLM Request #1" if seq == 1 else f"Additional LLM Analysis #{seq}"
            subtitle = "Waiting for LLM response"
            node = self._add_node(
                stage="llm_request",
                title=title,
                subtitle=subtitle,
                status="active",
                category="llm",
            )
            key = ("model", event.get("model_call_sequence_number", seq))
            self.pending_nodes[key] = node["id"]
            self.last_node_id = node["id"]
            self.last_join_nodes = [node["id"]]
            self.active_tool_ids.clear()
            self.current_parallel_group = None

            self.llm_calls.append({
                "sequence": seq,
                "status": "active",
                "started_seconds": round(self.elapsed, 3),
                "duration_seconds": None,
                "error": None,
            })
            self._add_timeline(f"Sent request to LLM (iteration {seq})", category="llm", status="active")

        elif name in {"model_call_completed", "model_call_failed", "model_call_stopped"}:
            key = ("model", event.get("model_call_sequence_number", self.model_call_count))
            node_id = self.pending_nodes.pop(key, None)
            state = {"model_call_completed": "completed", "model_call_failed": "failed", "model_call_stopped": "stopped"}[name]
            duration = None
            err_msg = safe_error(event) if state == "failed" else None

            if node_id:
                for n in self.nodes:
                    if n["id"] == node_id:
                        n["status"] = state
                        duration = max(0.0, self.elapsed - n["started_seconds"])
                        n["duration_seconds"] = round(duration, 3)
                        if state == "completed":
                            n["title"] = f"LLM Response #{self.model_call_count}"
                            n["subtitle"] = f"Response received ({duration:.1f}s)"
                        elif state == "failed":
                            n["title"] = f"LLM Call #{self.model_call_count} Failed"
                            n["subtitle"] = err_msg or "Failed"
                            n["error"] = err_msg
                        else:
                            n["title"] = f"LLM Call #{self.model_call_count} Stopped"
                            n["subtitle"] = "Safety boundary reached"
                        break

            for c in self.llm_calls:
                if c["sequence"] == self.model_call_count and c["status"] == "active":
                    c["status"] = state
                    c["duration_seconds"] = round(duration, 3) if duration is not None else None
                    c["error"] = err_msg
                    break

            tl_title = "LLM response received" if state == "completed" else (f"LLM request failed — {err_msg}" if state == "failed" else "LLM stopped at limit")
            self._add_timeline(tl_title, category="llm", status=state, duration_seconds=duration)

        elif name == "mcp_dispatch":
            self.tool_call_count += 1
            raw_t_name = event.get("tool_name")
            t_name = raw_t_name if isinstance(raw_t_name, str) and raw_t_name in TOOL_PURPOSES else "unknown_tool"
            raw_op = event.get("operation")
            op = raw_op if isinstance(raw_op, str) and raw_op in OPERATIONS else "unknown_operation"
            t_label = tool_label(event)
            purpose = TOOL_PURPOSES.get(t_name, "tool name unavailable")
            if op in OPERATIONS:
                purpose = OPERATIONS[op]

            # Update connection indicators on dispatch
            if t_name in {"az_aks_operations", "kubectl_resources", "kubectl_cluster", "call_kubectl"}:
                if self.connections["aks"]["status"] != "ready":
                    self.connections["aks"] = {"name": "AKS", "status": "connecting", "label": "Connecting"}
            elif t_name in {"pipelines_definition", "repo_repository", "repo_file"}:
                if self.connections["azure_devops"]["status"] != "ready":
                    self.connections["azure_devops"] = {"name": "Azure DevOps", "status": "connecting", "label": "Connecting"}

            # Detect parallel tool execution
            is_parallel = False
            group_id = None
            if self.active_tool_ids:
                # Concurrent tool dispatch
                is_parallel = True
                if not self.current_parallel_group:
                    self.parallel_counter += 1
                    self.current_parallel_group = f"group-{self.parallel_counter}"
                group_id = self.current_parallel_group
                # Mark existing active tools as parallel
                for aid in self.active_tool_ids:
                    for n in self.nodes:
                        if n["id"] == aid:
                            n["parallel"] = True
                            n["group_id"] = group_id

            # Connect from last node (e.g. LLM response or previous sequential tool)
            connect_from = self.last_node_id if not is_parallel else (self.last_join_nodes[0] if self.last_join_nodes else self.last_node_id)
            node_title = f"Tool: {t_name}" if t_name != "unknown_tool" else "Tool: Unavailable"
            node = self._add_node(
                stage="tool_execution",
                title=node_title,
                subtitle=f"{purpose} (executing…)",
                status="active",
                category="tool",
                parallel=is_parallel,
                group_id=group_id,
                connect_from=connect_from,
            )
            key = self._key("mcp", event)
            self.pending_nodes[key] = node["id"]
            self.active_tool_ids.append(node["id"])

            if is_parallel:
                if node["id"] not in self.last_join_nodes:
                    self.last_join_nodes.append(node["id"])
            else:
                self.last_node_id = node["id"]
                self.last_join_nodes = [node["id"]]

            self.tool_calls.append({
                "dispatch_count": event.get("dispatch_count", self.tool_call_count),
                "tool_name": t_name,
                "operation": op,
                "purpose": purpose,
                "status": "active",
                "started_seconds": round(self.elapsed, 3),
                "duration_seconds": None,
                "error": None,
            })
            self._add_timeline(f"Executing tool: {t_label}", category="tool", status="active")

        elif name in {"mcp_result", "mcp_failure"}:
            key = self._key("mcp", event)
            node_id = self.pending_nodes.pop(key, None)
            failed = name == "mcp_failure"
            state = "failed" if failed else "completed"
            duration = None
            err_msg = safe_error(event) if failed else None
            t_label = tool_label(event)
            raw_t_name = event.get("tool_name")
            t_name = raw_t_name if isinstance(raw_t_name, str) and raw_t_name in TOOL_PURPOSES else "unknown_tool"

            # Update connection indicators on result/failure
            if not failed:
                if t_name == "az_aks_operations":
                    self.connections["aks"] = {"name": "AKS", "status": "ready", "label": "Connected"}
                    self.connections["azure"] = {"name": "Azure", "status": "ready", "label": "Connected"}
                elif t_name in {"kubectl_resources", "kubectl_cluster", "call_kubectl"}:
                    self.connections["aks"] = {"name": "AKS", "status": "ready", "label": "Connected"}
                elif t_name in {"pipelines_definition", "repo_repository", "repo_file"}:
                    self.connections["azure_devops"] = {"name": "Azure DevOps", "status": "ready", "label": "Connected"}
            else:
                category = event.get("error_category")
                if t_name in {"az_aks_operations", "kubectl_resources", "kubectl_cluster", "call_kubectl"}:
                    self.connections["aks"] = {"name": "AKS", "status": "failed", "label": "Failed"}
                    if category in {"authentication", "authorization"}:
                        self.connections["azure"] = {"name": "Azure", "status": "failed", "label": "Auth Failed" if category == "authentication" else "Denied"}
                elif t_name in {"pipelines_definition", "repo_repository", "repo_file"}:
                    self.connections["azure_devops"] = {"name": "Azure DevOps", "status": "failed", "label": "Failed"}

            if node_id:
                if node_id in self.active_tool_ids:
                    self.active_tool_ids.remove(node_id)
                for n in self.nodes:
                    if n["id"] == node_id:
                        n["status"] = state
                        duration = max(0.0, self.elapsed - n["started_seconds"])
                        n["duration_seconds"] = round(duration, 3)
                        purpose = n["subtitle"].replace(" (executing…)", "")
                        if failed:
                            n["subtitle"] = f"{purpose} — Failed: {err_msg}"
                            n["error"] = err_msg
                        else:
                            n["subtitle"] = f"{purpose} ({duration:.1f}s)"
                        break

            disp = event.get("dispatch_count")
            for tc in self.tool_calls:
                if tc["dispatch_count"] == disp and tc["status"] == "active":
                    tc["status"] = state
                    tc["duration_seconds"] = round(duration, 3) if duration is not None else None
                    tc["error"] = err_msg
                    break

            tl_title = f"Tool failed: {t_label} — {err_msg}" if failed else f"Tool completed: {t_label}"
            self._add_timeline(tl_title, category="tool", status=state, duration_seconds=duration)

        elif name == "evidence_result":
            outcome = event.get("outcome", "completed")
            state = "completed" if outcome != "failed" else "failed"
            subtitle = "Evidence collected and verified" if state == "completed" else "Evidence collection failed / unavailable"
            node = self._add_node(
                stage="evidence",
                title="Evidence Processing",
                subtitle=subtitle,
                status=state,
                category="orchestration",
            )
            self.last_node_id = node["id"]
            self.last_join_nodes = [node["id"]]
            self._add_timeline(f"Evidence processing {state}", category="orchestration", status=state)

        elif name == "cleanup_completed":
            self.lifecycle_events["mcp_cleanup_status"] = "completed"
            self.connections["mcp"] = {"name": "MCP", "status": "stopped", "label": "Stopped"}
            node = self._add_node(
                stage="mcp_cleanup",
                title="Stop MCP Connections",
                subtitle="MCP & model client cleanup completed",
                status="completed",
                category="mcp",
            )
            self.last_node_id = node["id"]
            self.last_join_nodes = [node["id"]]
            self._add_timeline("MCP & client cleanup completed", category="mcp", status="completed")

        elif name == "cleanup_failed":
            self.lifecycle_events["mcp_cleanup_status"] = "failed"
            self.connections["mcp"] = {"name": "MCP", "status": "failed", "label": "Failed"}
            node = self._add_node(
                stage="mcp_cleanup",
                title="Stop MCP Connections",
                subtitle="Connection cleanup encountered error",
                status="failed",
                category="mcp",
            )
            self.last_node_id = node["id"]
            self.last_join_nodes = [node["id"]]
            self._add_timeline("Connection cleanup failed", category="mcp", status="failed")

        elif name == "request_completed":
            outcome = event.get("outcome", "completed")
            if outcome == "completed":
                self.end("completed", "Investigation completed")
            elif outcome == "bounded":
                self.lifecycle_events["safety_bounded"] = True
                self.end("stopped", "Investigation stopped at a safety limit")
            else:
                self.end("unconfirmed", "Investigation ended; outcome unconfirmed")

        elif name == "request_failed":
            err_msg = safe_error(event)
            self.end("failed", f"Investigation failed — {err_msg}")

        elif name in {"request_bounded", "execution_stopped"}:
            self.lifecycle_events["safety_bounded"] = True
            self._add_timeline("Collection safety limit reached", category="orchestration", status="stopped")

        return True

    def end(self, outcome, title):
        if self.terminal:
            return
        self.terminal = True
        self.outcome = outcome
        self.elapsed = self._now()

        # Mark unconfirmed pending nodes
        for key, node_id in self.pending_nodes.items():
            for n in self.nodes:
                if n["id"] == node_id:
                    n["status"] = "unconfirmed"
        self.pending_nodes.clear()
        self.active_tool_ids.clear()

        # Terminal stage node
        node_status = "completed" if outcome == "completed" else outcome
        stage = "terminal"
        node = self._add_node(
            stage=stage,
            title="Investigation Completed" if outcome == "completed" else ("Investigation Stopped" if outcome == "stopped" else f"Investigation {outcome.title()}"),
            subtitle=title,
            status=node_status,
            category="terminal",
        )
        self.last_node_id = node["id"]
        self.last_join_nodes = [node["id"]]
        self._add_timeline(title, category="terminal", status=node_status)

    def snapshot(self):
        return {
            "nodes": deepcopy(self.nodes),
            "edges": deepcopy(self.edges),
            "timeline": deepcopy(self.timeline),
            "connections": deepcopy(self.connections),
            "metadata": deepcopy(self.metadata),
            "summary": {
                "category": self.category,
                "access_mode": "Read-only",
                "outcome": self.outcome,
                "elapsed_seconds": round(self.elapsed, 3),
                "total_nodes": len(self.nodes),
                "model_call_count": self.model_call_count,
                "tool_call_count": self.tool_call_count,
                "terminal": self.terminal,
            },
            "details": {
                "llm_calls": deepcopy(self.llm_calls),
                "tool_calls": deepcopy(self.tool_calls),
                "lifecycle": deepcopy(self.lifecycle_events),
            },
        }
