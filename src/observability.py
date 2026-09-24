"""Local metadata-only events; no handlers, persistence, or external exporters."""
import json
import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from time import monotonic
from uuid import uuid4

from src.safe_diagnostics import classify_error, safe_fields, safe_model_error_metadata, API_METADATA_FIELDS
from src.metrics import METRICS
from src.model_request_diagnostics import request_structure
from src.request_budget import (RequestBudget, RequestBoundExceeded, BoundedResult,
    current_budget, checkpoint, charge_input, accept_result,
    MAX_REQUEST_MODEL_CALLS, MAX_REQUEST_TOOL_ATTEMPTS)

LOGGER = logging.getLogger(__name__)
_request = ContextVar("observability_request", default=None)
_route = ContextVar("observability_route", default=None)
_FIELDS = {
    "event", "timestamp", "level", "request_id", "task_id", "route", "tool_call_id",
    "mcp_server", "tool_name", "operation", "purpose_code", "policy_decision",
    "reason_code", "outcome", "duration_ms", "attempt_count", "dispatch_count",
    "remaining_budget", "evidence_status", "truncated", "error_category",
    "budget_type", "limit", "current_usage", "model_call_sequence_number",
} | API_METADATA_FIELDS


def event_record(event, **fields):
    """Strict field allowlist using the existing diagnostic value protections."""
    if set(fields) - (_FIELDS - {"event", "timestamp"}):
        raise ValueError("Unsupported event field")
    result = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": "INFO"}
    for key, value in {"event": event, **fields}.items():
        if value is None:
            continue
        if key in API_METADATA_FIELDS:
            result.update(safe_fields(**{key: value}))
        elif key == "truncated":
            if type(value) is bool:
                result[key] = value
        elif key in {"duration_ms", "attempt_count", "dispatch_count", "remaining_budget", "limit", "current_usage", "model_call_sequence_number"}:
            safe = safe_fields(duration=value)
            if safe:
                result[key] = safe["duration"]
        else:
            field = key if key in {"reason_code", "error_category"} else "event"
            safe = safe_fields(**{field: value})
            if safe:
                result[key] = safe[field]
    return result


def emit(event, **fields):
    state = _request.get()
    if state is None:
        return
    # Observability must not alter application execution, including on sink failure.
    try:
        metadata = {"request_id": state["request_id"], "task_id": state["task_id"],
                    "route": _route.get(), **fields}
        if event in {"request_bounded", "execution_stopped"} or fields.get("evidence_status") in {"partial", "incomplete", "unavailable"} or fields.get("truncated") is True:
            state["incomplete"] = True
        if event in {"request_completed", "request_failed"} and state.get("incomplete"):
            metadata["evidence_status"] = "incomplete"
        record = event_record(event, **metadata)
        try:
            METRICS.record(record)
        except Exception:
            pass
        LOGGER.info(json.dumps(record, sort_keys=True))
    except Exception:
        pass


def observed_request(function=None, *, return_outcome=False):
    if function is None:
        return lambda wrapped: observed_request(wrapped, return_outcome=return_outcome)
    @wraps(function)
    async def wrapped(*args, **kwargs):
        context = kwargs.get("task_context")
        budget = RequestBudget()
        token = _request.set({"request_id": str(uuid4()),
                              "task_id": getattr(context, "task_id", None),
                              "attempts": 0, "dispatches": 0, "budget": budget})
        route_token = _route.set(None)
        started = monotonic()
        emit("request_started", outcome="started")
        try:
            try:
                async with asyncio.timeout(budget.remaining) as deadline:
                    budget.timeout = deadline
                    result = await function(*args, **kwargs)
                    budget.check()
            except TimeoutError:
                if not deadline.expired():
                    raise
                budget.stop("deadline", budget.deadline_seconds, budget.elapsed)
            emit("request_completed", outcome="completed", duration_ms=(monotonic()-started)*1000)
            return result
        except RequestBoundExceeded as error:
            emit("request_completed", outcome="bounded", evidence_status="incomplete",
                 reason_code="deadline_exceeded" if error.kind == "deadline" else "aggregate_limit")
            if return_outcome:
                return BoundedResult(error.kind, budget.usage, tuple(budget.evidence))
            print("Investigation stopped at the request safety limit (" + error.kind + "). "
                  "Evidence is incomplete; no additional collection or diagnosis was performed. "
                  + str(len(budget.evidence)) + " bounded tool results were retained for this request.")
            if kwargs.get("context_enabled"):
                return None
            return BoundedResult(error.kind, budget.usage, tuple(budget.evidence))
        except Exception as error:
            safe = classify_error(error)
            emit("request_failed", outcome="failed", error_category=safe.category,
                 reason_code=safe.reason_code, duration_ms=(monotonic()-started)*1000)
            raise
        finally:
            _route.reset(route_token)
            _request.reset(token)
    return wrapped


def select_route(route):
    _route.set(route)
    emit("route_selected", outcome="selected")


def tool_metadata(call):
    from src.policy.policy import TOOL_SERVERS, READ_ONLY_POLICY, AKS_READ_ONLY_POLICY
    name = call.get("name")
    args = call.get("args", {})
    operation = args.get("action", args.get("operation")) if isinstance(args, dict) else None
    allowed = READ_ONLY_POLICY.get(name, AKS_READ_ONLY_POLICY.get(name, set()))
    if not isinstance(operation, str) or operation not in allowed:
        operation = None
    # Unknown model-generated tool names are not diagnostic metadata.
    return {"tool_call_id": call.get("id"),
            "tool_name": name if name in TOOL_SERVERS else None,
            "mcp_server": TOOL_SERVERS.get(name),
            "operation": operation}


def observed_policy(function):
    @wraps(function)
    def wrapped(call):
        budget = current_budget()
        if budget:
            budget.charge("tool_attempts", 1, MAX_REQUEST_TOOL_ATTEMPTS)
        state = _request.get()
        if state is not None:
            state["attempts"] += 1
        metadata = tool_metadata(call)
        emit("tool_attempt", outcome="attempted", attempt_count=state["attempts"] if state else None, **metadata)
        try:
            result = function(call)
        except Exception as error:
            safe = classify_error(error)
            reason = safe.reason_code
            detail = getattr(error, "diagnostic_reason", None)
            if (safe.category == "policy" and call.get("name") == "pipelines_definition"
                    and type(detail) is str and detail in {"action_not_allowed", "project_mismatch"}):
                reason = detail
            emit("policy_decision", policy_decision="reject", outcome="rejected",
                 error_category=safe.category, reason_code=reason, **metadata)
            raise
        emit("policy_decision", policy_decision="allow", outcome="allowed", **metadata)
        return result
    return wrapped


async def model_call(client, **kwargs):
    budget = current_budget()
    try:
        if budget:
            budget.check()
            charge_input(kwargs)
            budget.charge("model_calls", 1, MAX_REQUEST_MODEL_CALLS)
    except RequestBoundExceeded as error:
        emit("model_call_stopped", outcome="bounded", reason_code="deadline_exceeded" if error.kind == "deadline" else "aggregate_limit")
        raise
    state = _request.get()
    sequence = None
    if state is not None:
        sequence = state.get("model_calls", 0) + 1
        state["model_calls"] = sequence
        try:
            record = event_record("model_request_structure", request_id=state["request_id"],
                                  task_id=state["task_id"], route=_route.get(),
                                  model_call_sequence_number=sequence)
            # Only this closed, content-free projector may add nested metadata.
            record.update(request_structure(kwargs.get("messages"), kwargs.get("tools")))
            LOGGER.info(json.dumps(record, sort_keys=True))
        except Exception:
            pass  # Diagnostics must never alter model execution or its errors.
    return await _call("model", lambda: client.chat.completions.create(**kwargs),
                       model_call_sequence_number=sequence)


async def evidence_call(invoke):
    try:
        return await invoke()
    except Exception:
        emit("evidence_result", outcome="failed", evidence_status="unavailable")
        raise


async def mcp_call(call, invoke, *, remaining_budget=None):
    checkpoint()
    charge_input(call.get("args", {}))
    budget = current_budget()
    if budget:
        budget.charge("mcp_dispatches", 1, MAX_REQUEST_TOOL_ATTEMPTS)
    state = _request.get()
    if state is not None:
        state["dispatches"] += 1
    return await _call("mcp", invoke, **tool_metadata(call), remaining_budget=remaining_budget,
                       dispatch_count=state["dispatches"] if state else None)


async def _call(boundary, invoke, **metadata):
    started = monotonic()
    emit("mcp_dispatch" if boundary == "mcp" else "model_call_started", outcome="started", **metadata)
    try:
        result = await invoke()
    except asyncio.CancelledError:
        budget = current_budget()
        if boundary == "model" and budget and budget.remaining <= 0:
            emit("model_call_stopped", outcome="bounded", reason_code="deadline_exceeded")
        raise
    except Exception as error:
        safe = classify_error(error, boundary=boundary)
        emit("mcp_failure" if boundary == "mcp" else "model_call_failed", outcome="failed",
             duration_ms=(monotonic()-started)*1000, error_category=safe.category,
             reason_code=safe.reason_code, **metadata,
             **(safe_model_error_metadata(error) if boundary == "model" else {}))
        raise
    messages = result.get("messages", []) if isinstance(result, dict) else [result]
    failed = any(getattr(message, "status", None) == "error" for message in messages)
    emit("mcp_failure" if failed else ("mcp_result" if boundary == "mcp" else "model_call_completed"),
         outcome="failed" if failed else "completed", duration_ms=(monotonic()-started)*1000, **metadata)
    accept_result(result, boundary)
    return result
