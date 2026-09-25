"""Shared deterministic normalization of untrusted successful tool results."""
import json
from typing import Any

from langchain_core.messages import ToolMessage

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
            bounded_content = content[:max_chars]
        else:
            bounded_content = {
                "original_content_type": content_type,
                "truncated_preview": serialized[:max_chars],
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
    if truncated:
        envelope["truncation_notice"] = (
            f"Result content was limited to {max_chars} characters."
        )
    return envelope


