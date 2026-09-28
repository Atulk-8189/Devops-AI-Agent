"""Frontend-only presentation of existing local lifecycle events."""
import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging

from frontend.activity import Activity, EVENTS

CAPABILITIES = (
    ("AKS Health", "Inspect cluster and workload health.", "Check AKS cluster health."),
    ("Pipeline Investigation", "Investigate Azure DevOps pipeline failures and logs.", "Investigate the latest pipeline failure."),
    ("Terraform Review", "Review Terraform code and infrastructure configuration.", "Review my Terraform configuration."),
    ("Troubleshooting", "Investigate infrastructure errors and identify possible causes.", "Help me troubleshoot this infrastructure issue."),
)
CONNECTIONS = "Azure authentication: Unknown — Status unavailable · Azure DevOps MCP: Unknown — Status unavailable · AKS / kubectl: Unknown — Status unavailable"
CATEGORIES = {"aks": "AKS", "aks_cluster_health": "AKS", "aks_remediation": "AKS", "azure_devops": "Azure DevOps", "terraform": "Terraform", "generic": "Other"}
_active = ContextVar("frontend_investigation", default=None)


class Investigation:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.activity = Activity()
        self.metadata = {"category": "Other", "access_mode": "Read-only", "started_at": datetime.now(timezone.utc).isoformat(), "coverage": "Not reported", "target": "Not reported"}

    def accept(self, event):
        name = event.get("event")
        if not isinstance(name, str) or name not in EVENTS:
            return
        if isinstance(event.get("route"), str) and event["route"] in CATEGORIES:
            self.metadata["category"] = CATEGORIES[event["route"]]
        if isinstance(event.get("evidence_status"), str) and event["evidence_status"] in {"partial", "incomplete", "unavailable"}:
            self.metadata["coverage"] = event["evidence_status"]
        if event.get("truncated") is True:
            self.metadata["collection_truncated"] = True
        if name in {"request_bounded", "execution_stopped"} or event.get("outcome") == "bounded":
            self.metadata["coverage"] = "Incomplete — collection limit reached"
        if self.activity.accept(event):
            self.queue.put_nowait(self.activity.snapshot())

    def finish(self, answer):
        self.metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        # Only explicit fields in an already display-approved answer. No raw tool data.
        try:
            data = json.loads(answer) if answer else None
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            keys = {"cluster_scope", "target", "namespace", "namespaces", "repository", "pipeline", "observed_evidence", "missing_evidence", "limitations", "evidence_coverage", "truncated"}
            self.metadata["agent_reported_details"] = {key: value for key, value in data.items() if key.lower().replace(" ", "_") in keys}
            details = self.metadata["agent_reported_details"]
            target = details.get("target")
            scope = details.get("cluster_scope")
            if target is None and isinstance(scope, dict):
                target = scope.get("cluster")
            if isinstance(target, str) and target:
                self.metadata["target"] = target
                self.metadata["target_source"] = "Agent-reported; not independently verified"
        return self.metadata


class EventHandler(logging.Handler):
    def emit(self, record):
        active = _active.get()
        if active is None:
            return
        try:
            if record.name == "src.observability":
                event = json.loads(record.getMessage())
                if isinstance(event, dict):
                    active.accept(event)
            elif record.name == "src.agent.orchestration":
                # Match application-owned templates, never render formatted log text.
                name = {
                    "agent startup: initializing Azure OpenAI and MCP runtime": "connections_start",
                    "tool discovery complete: %d allowed tools": "connections_ready",
                    "agent shutdown complete": "cleanup_completed",
                }.get(record.msg if isinstance(record.msg, str) else "")
                if name:
                    active.accept({"event": name})
        except (ValueError, TypeError, RecursionError):
            pass


async def display_events(investigation, status, panel=None, trace_panel=None):
    while True:
        snapshot = await investigation.queue.get()
        if snapshot is None:
            return
        status.content = snapshot["current"] if panel is None else ""
        if panel is not None:
            panel.props = snapshot
        if trace_panel is not None and "trace" in snapshot:
            trace_panel.props = snapshot["trace"]
        await status.update()
