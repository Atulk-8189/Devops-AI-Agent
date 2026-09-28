"""Closed projection of real events into an ordered, payload-free activity log."""
from copy import deepcopy
from time import monotonic

TOOL_PURPOSES = {
    "pipelines_definition": "Pipeline definitions",
    "repo_repository": "Repository discovery",
    "repo_file": "Repository files",
    "az_aks_operations": "AKS information",
    "kubectl_resources": "Kubernetes resources",
    "kubectl_cluster": "Kubernetes cluster information",
    "call_kubectl": "Kubernetes logs",
}
OPERATIONS = {
    "list": "list", "list_directory": "list directory", "get_content": "get content",
    "get": "get", "describe": "describe", "show": "show",
    "nodepool-list": "list node pools", "nodepool-show": "show node pool",
    "account-list": "list accounts", "get-versions": "get versions",
    "check-network": "check network", "cluster-info": "cluster information",
    "api-resources": "API resources", "api-versions": "API versions", "explain": "explain",
}
ERRORS = {
    "authentication": "Authentication failed.",
    "authorization": "Access was denied.",
    "configuration": "Service configuration is unavailable or invalid.",
    "mcp": "The MCP operation failed.",
    "model": "The language model request failed.",
    "timeout": "The operation timed out.",
    "policy": "The read-only policy blocked the operation.",
    "validation": "Request validation failed.",
    "tool": "The tool operation failed.",
}
EVENTS = frozenset({
    "request_started", "route_selected", "model_call_started", "model_call_completed",
    "model_call_failed", "model_call_stopped", "mcp_dispatch", "mcp_result",
    "mcp_failure", "request_completed", "request_failed", "request_bounded",
    "execution_stopped", "cleanup_failed", "connections_start", "connections_ready",
    "cleanup_completed", "evidence_result",
})


def safe_error(event):
    category = event.get("error_category")
    return ERRORS.get(category, "The operation failed; sensitive diagnostic details are not displayed.") if isinstance(category, str) else ERRORS["tool"]


def tool_label(event):
    name = event.get("tool_name")
    if not isinstance(name, str) or name not in TOOL_PURPOSES:
        return "tool name unavailable"
    purpose = TOOL_PURPOSES[name]
    operation = event.get("operation")
    if isinstance(operation, str) and operation in OPERATIONS:
        purpose += ": " + OPERATIONS[operation]
    return f"{name} — {purpose}"


class Activity:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.started = clock()
        self.entries = []
        self.pending = {}
        self.model_calls = 0
        self.terminal = False
        self.current = "Waiting for the read-only agent"
        self.outcome = "waiting"
        self.elapsed = 0.0
        from frontend.trace import TraceGraph
        self.trace = TraceGraph(clock=self.clock, started=self.started)

    def _add(self, title, state="completed"):
        self.elapsed = max(0.0, self.clock() - self.started)
        row = {"id": len(self.entries), "title": title, "state": state,
               "started_seconds": self.elapsed}
        self.entries.append(row)
        self.current = title
        return row

    def _key(self, family, event):
        field = "dispatch_count" if family == "mcp" else "model_call_sequence_number"
        value = event.get(field)
        return (family, value if type(value) is int else None)

    def _start(self, family, event, title):
        key = self._key(family, event)
        # Duplicate or missing identifiers must never complete an earlier call.
        if key in self.pending:
            self.pending.pop(key)["state"] = "unconfirmed"
        self.pending[key] = self._add(title, "active")

    def _end(self, family, event, title, state):
        row = self.pending.pop(self._key(family, event), None)
        if row is not None:
            row["state"] = state
            row["duration_seconds"] = max(0.0, self.clock() - self.started - row["started_seconds"])
        self._add(title, state)

    def end(self, outcome, title):
        if self.terminal:
            return
        # A terminal request event is not evidence of individual call completion.
        for row in self.pending.values():
            row["state"] = "unconfirmed"
        self.pending.clear()
        self.outcome = outcome
        self._add(title, "completed" if outcome == "completed" else outcome)
        self.terminal = True
        self.trace.end(outcome, title)

    def accept(self, event):
        name = event.get("event")
        if not isinstance(name, str) or name not in EVENTS or self.terminal:
            return False
        self.outcome = "active"
        self.trace.accept(event)
        if name == "request_started":
            self._add("Investigation initialized")
        elif name == "connections_start":
            self._start("connections", {}, "Starting MCP connections and tool discovery")
        elif name == "connections_ready":
            self._end("connections", {}, "MCP initialization and tool discovery returned", "completed")
        elif name == "route_selected":
            self._add("Investigation category selected")
        elif name == "model_call_started":
            self.model_calls += 1
            title = "Sending request to LLM" if self.model_calls == 1 else "Requesting additional analysis from LLM"
            self._start("model", event, title + " — waiting for LLM response")
        elif name in {"model_call_completed", "model_call_failed", "model_call_stopped"}:
            state = {"model_call_completed": "completed", "model_call_failed": "failed", "model_call_stopped": "stopped"}[name]
            title = {"completed": "LLM response received", "failed": "LLM request failed — " + safe_error(event), "stopped": "LLM request stopped at a safety limit"}[state]
            self._end("model", event, title, state)
        elif name == "mcp_dispatch":
            self._start("mcp", event, "Executing MCP tool: " + tool_label(event))
        elif name in {"mcp_result", "mcp_failure"}:
            failed = name == "mcp_failure"
            title = ("MCP tool failed: " if failed else "Receiving MCP response: ") + tool_label(event)
            if failed:
                title += " — " + safe_error(event)
            self._end("mcp", event, title, "failed" if failed else "completed")
        elif name == "cleanup_failed":
            self._add("Connection cleanup failed", "failed")
        elif name == "cleanup_completed":
            self._add("MCP and model client cleanup completed")
        elif name == "request_completed":
            if event.get("outcome") == "completed":
                self.end("completed", "Investigation completed")
            elif event.get("outcome") == "bounded":
                self.end("stopped", "Investigation stopped at a safety limit; evidence is incomplete")
            else:
                self.end("unconfirmed", "Request ended; success not confirmed")
        elif name == "request_failed":
            self.end("failed", "Investigation failed — " + safe_error(event))
        elif name in {"request_bounded", "execution_stopped"}:
            self._add("Collection safety limit reached; awaiting request cleanup", "stopped")
        elif name == "evidence_result":
            if event.get("outcome") != "failed":
                return False
            self._add("Evidence collection failed; evidence unavailable", "failed")
        return True

    def snapshot(self):
        # Event-time durations remain accurate in saved/resumed conversations.
        return {
            "entries": deepcopy(self.entries),
            "current": self.current,
            "outcome": self.outcome,
            "elapsed_seconds": self.elapsed,
            "trace": self.trace.snapshot(),
        }
