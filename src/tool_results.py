import json
import logging
import os
from typing import Any

from langchain_core.messages import ToolMessage
from src.config import DEFAULT_KUBECTL_LOGS_MAX_CHARS

LOGGER = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 8000
MAX_TOOL_CONTENT_DEPTH = 64


def _json_safe_tool_content(value: Any, depth=0):
    """Represent tool output as inert JSON-compatible data without interpreting it."""
    if depth > MAX_TOOL_CONTENT_DEPTH:
        raise ValueError("Tool content exceeds supported nesting")
    if isinstance(value, dict):
        return {str(key): _json_safe_tool_content(item, depth+1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_tool_content(item, depth+1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return _json_safe_tool_content(value.model_dump(), depth+1)
    return str(value)


def _tool_content_type(content):
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content, "text"
        if isinstance(parsed, (dict, list)):
            return _json_safe_tool_content(parsed), "json"
        return content, "text"
    normalized = _json_safe_tool_content(content)
    if isinstance(normalized, list) and all(isinstance(item, dict) and "type" in item for item in normalized):
        return normalized, "content_blocks"
    if isinstance(normalized, (dict, list)):
        return normalized, "json"
    return normalized, "text"


def normalize_successful_tool_result(message: ToolMessage, max_chars: int = MAX_TOOL_RESULT_CHARS):
    """Return a bounded, explicit envelope for untrusted successful tool output."""
    try:
        content, content_type = _tool_content_type(message.content)
        serialized = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    except (ValueError, TypeError, RecursionError):
        return {"tool_name": message.name or "unknown", "status": "unknown",
                "untrusted_data": True, "content_type": "unknown", "content": None,
                "truncated": True, "truncation_notice": "Tool content could not be safely normalized; evidence is incomplete."}
    truncated = len(serialized) > max_chars
    if truncated:
        if isinstance(content, str):
            if message.name == "call_kubectl":
                bounded_content = f"...[truncated {len(content) - max_chars} chars]...\n" + content[-(max_chars):]
            else:
                bounded_content = content[:max_chars] + f"\n...[truncated {len(content) - max_chars} chars]..."
        else:
            bounded_content = {
                "original_content_type": content_type,
                "truncated_preview": serialized[:max_chars] + "... [TRUNCATED]",
            }
    else:
        bounded_content = content
    envelope = {
        "tool_name": message.name or "unknown",
        "status": "success",
        "untrusted_data": True,
        "content_type": content_type,
        "content": bounded_content,
        "truncated": truncated,
    }
    truncation_info = getattr(message, "additional_kwargs", {}).get("truncation") if hasattr(message, "additional_kwargs") else None
    if truncation_info:
        envelope["truncated"] = True
        envelope["truncation_metadata"] = truncation_info
    if truncated:
        envelope["truncation_notice"] = (
            f"Result content was limited to {max_chars} characters."
        )
    return envelope


def get_kubectl_logs_max_chars() -> int:
    """Return the configured per-tool-result character limit for call_kubectl."""
    val = os.getenv("KUBECTL_LOGS_MAX_CHARS") or os.getenv("CALL_KUBECTL_MAX_CHARS")
    if val:
        try:
            parsed = int(val)
            if 1000 <= parsed <= 4_000_000:
                return parsed
        except (ValueError, TypeError):
            pass
    return DEFAULT_KUBECTL_LOGS_MAX_CHARS


def _bound_log_text(text: str, max_chars: int) -> tuple[str, bool, int, int]:
    """Preserve the most recent log output (tail) with a clear truncation marker."""
    original_chars = len(text)
    if original_chars <= max_chars:
        return text, False, original_chars, original_chars

    retained_tail = text[-max_chars:]
    omitted_chars = original_chars - max_chars
    marker = (
        f"[TRUNCATED: original {original_chars} characters, retained {max_chars} characters from log tail]\n"
        f"...[truncated {omitted_chars} characters]...\n"
    )
    bounded = marker + retained_tail
    retained_chars = len(retained_tail)
    LOGGER.info(
        "call_kubectl log bounded: original_chars=%d, retained_chars=%d, omitted_chars=%d, limit=%d",
        original_chars, retained_chars, omitted_chars, max_chars,
    )
    return bounded, True, original_chars, retained_chars


def _bound_message_content(message: ToolMessage, max_chars: int) -> None:
    content = message.content
    if isinstance(content, str):
        bounded, truncated, orig_len, ret_len = _bound_log_text(content, max_chars)
        if truncated:
            message.content = bounded
            kwargs = getattr(message, "additional_kwargs", None)
            if kwargs is None:
                message.additional_kwargs = {}
            message.additional_kwargs["truncation"] = {
                "original_chars": orig_len,
                "retained_chars": ret_len,
                "truncated": True,
            }
    elif isinstance(content, list):
        new_blocks = []
        truncated_any = False
        total_orig = 0
        total_ret = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                bounded, truncated, orig_len, ret_len = _bound_log_text(block["text"], max_chars)
                new_block = dict(block)
                new_block["text"] = bounded
                new_blocks.append(new_block)
                if truncated:
                    truncated_any = True
                    total_orig += orig_len
                    total_ret += ret_len
            elif isinstance(block, str):
                bounded, truncated, orig_len, ret_len = _bound_log_text(block, max_chars)
                new_blocks.append(bounded)
                if truncated:
                    truncated_any = True
                    total_orig += orig_len
                    total_ret += ret_len
            else:
                new_blocks.append(block)
        if truncated_any:
            message.content = new_blocks
            kwargs = getattr(message, "additional_kwargs", None)
            if kwargs is None:
                message.additional_kwargs = {}
            message.additional_kwargs["truncation"] = {
                "original_chars": total_orig,
                "retained_chars": total_ret,
                "truncated": True,
            }


def bound_kubectl_log_result(result: Any, max_chars: int | None = None, tool_name: str | None = None) -> Any:
    """Pre-budget bounding for call_kubectl output to protect the aggregate request budget.

    Preserves the tail of the log with a clear truncation marker and records counts
    without logging raw output.
    """
    if tool_name is not None and tool_name != "call_kubectl":
        return result

    if max_chars is None:
        max_chars = get_kubectl_logs_max_chars()

    if isinstance(result, ToolMessage):
        if getattr(result, "name", None) in (None, "call_kubectl"):
            _bound_message_content(result, max_chars)
    elif isinstance(result, dict) and "messages" in result:
        for msg in result["messages"]:
            if isinstance(msg, ToolMessage) and getattr(msg, "name", None) in (None, "call_kubectl"):
                _bound_message_content(msg, max_chars)
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, ToolMessage) and getattr(item, "name", None) in (None, "call_kubectl"):
                _bound_message_content(item, max_chars)
    elif isinstance(result, str):
        bounded, truncated, orig_len, ret_len = _bound_log_text(result, max_chars)
        return bounded

    return result


