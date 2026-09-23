"""Bounded process-local counters derived only from structured events."""
from collections import Counter
from threading import Lock

from src.safe_diagnostics import CATEGORIES

GROUPS = {
    "requests": ("started", "completed", "failed", "bounded_incomplete"),
    "model": ("calls", "failures", "stopped"),
    "tools": ("attempts", "dispatches", "successes", "failures", "policy_rejected"),
    "mcp": ("dispatches", "successes", "failures"),
    "safety": ("schema_validation_failures", "duplicate_stops", "budget_exhaustion", "deadline_exhaustion", "classified_errors"),
    "evidence": ("attempts", "successes", "failures", "truncated", "bounded"),
}


class Metrics:
    """Thread-safe totals and independent, fixed-vocabulary dimension breakdowns.

    No event retention and no request-ID tracking. Unknown labels are omitted.
    Snapshots are detached JSON-compatible copies. Reset is explicit, never per request.
    """
    def __init__(self):
        self._lock = Lock()
        self._counts = Counter()

    def record(self, event):
        from src.policy.policy import ALLOWED_TOOL_NAMES
        allowed = {"route": {"generic", "aks", "terraform", "azure_devops"},
                   "tool_name": ALLOWED_TOOL_NAMES, "mcp_server": {"aks", "azure-devops"},
                   "error_category": CATEGORIES}
        labels = [(key, event[key]) for key, values in allowed.items()
                  if type(event.get(key)) is str and event[key] in values]
        increments = []
        name = event.get("event")
        mapping = {
            "request_started": (("requests", "started"),),
            "request_completed": (("requests", "completed"),),
            "request_failed": (("requests", "failed"),),
            "model_call_started": (("model", "calls"),),
            "model_call_failed": (("model", "failures"),),
            "model_call_stopped": (("model", "stopped"),),
            "tool_attempt": (("tools", "attempts"),),
            "mcp_dispatch": (("tools", "dispatches"), ("mcp", "dispatches")),
            "mcp_result": (("tools", "successes"), ("mcp", "successes")),
            "mcp_failure": (("tools", "failures"), ("mcp", "failures")),
            "schema_validation_failed": (("safety", "schema_validation_failures"),),
            "evidence_collection": (("evidence", "attempts"),),
        }
        if type(name) is not str:
            return
        increments.extend(mapping.get(name, ()))
        if name in {"request_completed", "request_failed"} and (
                event.get("outcome") == "bounded" or event.get("evidence_status") in {"partial", "incomplete"}):
            increments.append(("requests", "bounded_incomplete"))
        if name == "policy_decision" and event.get("policy_decision") == "reject":
            increments.append(("tools", "policy_rejected"))
        if name in {"request_bounded", "execution_stopped"}:
            reason = event.get("reason_code")
            metric = {"deadline_exceeded": "deadline_exhaustion", "aggregate_limit": "budget_exhaustion",
                      "budget_exhausted": "budget_exhaustion", "duplicate_call": "duplicate_stops"}.get(reason) if type(reason) is str else None
            if metric:
                increments.append(("safety", metric))
        if name == "evidence_result":
            if event.get("outcome") == "collected":
                increments.append(("evidence", "successes"))
            elif event.get("outcome") == "failed":
                increments.append(("evidence", "failures"))
            if event.get("truncated") is True:
                increments.append(("evidence", "truncated"))
            if event.get("evidence_status") in {"partial", "incomplete"} or event.get("truncated") is True:
                increments.append(("evidence", "bounded"))
        if name == "request_bounded" and event.get("budget_type") == "result_chars":
            increments.append(("evidence", "bounded"))
        if any(key == "error_category" for key, _ in labels):
            increments.append(("safety", "classified_errors"))
        with self._lock:
            for group, metric in increments:
                self._counts[(group, metric, "", "")] += 1
                for key, value in labels:
                    self._counts[(group, metric, key, value)] += 1

    def snapshot(self):
        with self._lock:
            counts = self._counts.copy()
        result = {group: {metric: {"total": counts[(group, metric, "", "")], "dimensions": {}}
                          for metric in metrics} for group, metrics in GROUPS.items()}
        for (group, metric, key, value), count in sorted(counts.items()):
            if key:
                result[group][metric]["dimensions"].setdefault(key, {})[value] = count
        return result

    def reset(self):
        with self._lock:
            self._counts.clear()


METRICS = Metrics()


def snapshot():
    return METRICS.snapshot()


def reset():
    METRICS.reset()
