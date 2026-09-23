"""Outer request limits. No retained raw responses, persistence, or permissions."""
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic

from src.agent.context import RequestUsage
from src.config import DEFAULT_REQUEST_DEADLINE_SECONDS

MAX_REQUEST_MODEL_CALLS = 12
MAX_REQUEST_TOOL_ATTEMPTS = 64
MAX_REQUEST_RESULT_CHARS = 512_000
MAX_REQUEST_INPUT_CHARS = 1_000_000


class RequestBoundExceeded(Exception):
    def __init__(self, kind):
        self.kind = kind
        super().__init__("Request deadline exceeded" if kind == "deadline" else "Request aggregate bound exceeded")


@dataclass(repr=False)
class BoundedResult:
    reason_code: str
    usage: RequestUsage
    # Already-normalized untrusted envelopes, not raw MCP responses. Never printed,
    # logged, sent for diagnosis, or inserted into TaskContext automatically.
    evidence: tuple = field(default=(), repr=False)
    outcome: str = "bounded"
    evidence_status: str = "incomplete"


@dataclass(repr=False)
class RequestBudget:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started: float = field(default_factory=monotonic)
    deadline_seconds: float = DEFAULT_REQUEST_DEADLINE_SECONDS
    usage: RequestUsage = field(default_factory=RequestUsage)
    evidence: list = field(default_factory=list, repr=False)
    stopped: str | None = None
    timeout: object = None

    @property
    def elapsed(self):
        return max(0, monotonic() - self.started)

    @property
    def remaining(self):
        return max(0, self.deadline_seconds - self.elapsed)

    def stop(self, kind, limit, usage):
        from src.observability import emit
        self.stopped = kind
        emit("request_bounded", outcome="bounded", evidence_status="incomplete",
             budget_type=kind, limit=limit, current_usage=usage,
             remaining_budget=max(0, limit-usage), reason_code="deadline_exceeded" if kind == "deadline" else "aggregate_limit")
        raise RequestBoundExceeded(kind)

    def check(self):
        if self.stopped:
            raise RequestBoundExceeded(self.stopped)
        if self.remaining <= 0:
            self.stop("deadline", self.deadline_seconds, self.elapsed)

    def charge(self, kind, amount, limit):
        self.check()
        current = getattr(self.usage, kind)
        if current + amount > limit:
            self.stop(kind, limit, current + amount)
        self.usage = self.usage.model_copy(update={kind: current + amount})

    def configure(self, seconds):
        self.deadline_seconds = seconds
        self.check()
        if self.timeout is not None:
            self.timeout.reschedule(asyncio.get_running_loop().time() + self.remaining)


def current_budget():
    # Reuse the existing request owner and correlation lifetime, not another session.
    from src.observability import _request
    state = _request.get()
    return state.get("budget") if state else None


def checkpoint():
    budget = current_budget()
    if budget:
        budget.check()


def measured_size(value, cap):
    """Measure JSON-compatible data incrementally, stopping at cap+1 characters.

    Do not stringify unknown objects or allocate a complete serialized payload.
    SDK/tool envelopes are selected by callers before measurement.
    """
    total = 0
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 64:
            return cap + 1
        if isinstance(item, str):
            total += len(item)
        elif isinstance(item, dict):
            total += 2 + len(item)
            if len(item) > cap-total:
                return cap + 1
            for key, child in item.items():
                stack.extend(((key, depth+1), (child, depth+1)))
        elif isinstance(item, (list, tuple)):
            total += 2 + len(item)
            if len(item) > cap-total:
                return cap + 1
            stack.extend((child, depth+1) for child in item)
        elif item is None or type(item) in (int, float, bool):
            total += len(json.dumps(item))
        else:
            return cap + 1  # Unknown representation is not safely measurable.
        if total > cap:
            return cap + 1
    return total


def charge_input(value):
    budget = current_budget()
    if budget:
        budget.charge("input_chars", measured_size(value, MAX_REQUEST_INPUT_CHARS-budget.usage.input_chars), MAX_REQUEST_INPUT_CHARS)


def accept_result(result, boundary):
    budget = current_budget()
    if not budget:
        return
    budget.check()
    if boundary == "model":
        choices = getattr(result, "choices", ())
        values = []
        for choice in choices:
            message = choice.message
            values.append(getattr(message, "content", None))
            for call in getattr(message, "tool_calls", ()) or ():
                values.extend((call.id, call.function.name, call.function.arguments))
    else:
        messages = result.get("messages", ()) if isinstance(result, dict) else [result]
        values = [getattr(message, "content", None) for message in messages]
    budget.charge("result_chars", measured_size(values, MAX_REQUEST_RESULT_CHARS-budget.usage.result_chars), MAX_REQUEST_RESULT_CHARS)
    if boundary == "mcp":
        from langchain_core.messages import ToolMessage
        from src.tool_results import normalize_successful_tool_result
        for message in messages:
            if isinstance(message, ToolMessage) and message.status == "success":
                budget.check()
                try:
                    preview = normalize_successful_tool_result(message)
                except (TypeError, ValueError, RecursionError):
                    # Preview creation must not replace dedicated malformed-evidence
                    # handling or turn it into an unexpected transport failure.
                    preview = {"status": "unknown", "truncated": True, "untrusted_data": True}
                budget.evidence.append(preview)
    budget.check()
