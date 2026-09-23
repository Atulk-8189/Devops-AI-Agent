"""Metadata-only diagnostics. Never serializes exception text or tool payloads."""
import json
import re
import unicodedata
from dataclasses import dataclass

CATEGORIES = frozenset({"validation", "policy", "authentication", "authorization", "configuration",
                        "mcp", "timeout", "model", "tool", "unexpected"})


def sensitive(value):
    if not isinstance(value, str):
        return True
    text = unicodedata.normalize("NFKC", value)
    return (any(unicodedata.category(c).startswith("C") for c in text)
            or bool(re.search(r"(?i)password|passwd|\bpwd\s*=|secret|token|credential|authorization|"
                r"bearer|\bbasic\s|api.?key|connection.?string|accountkey|sharedaccesssignature|"
                r"private.?key|\bstringdata\b|\bsig=|://|\b(?:sk-|ghp_|github_pat_|AKIA|ASIA)|"
                r"\beyJ\w*\.|[A-Za-z0-9+/=_-]{40,}", text)))


@dataclass(frozen=True)
class SafeError:
    category: str
    reason_code: str

    def user_message(self):
        return f"Request failed ({self.category}; {self.reason_code}). No sensitive diagnostic details are displayed."


def classify_error(error, *, boundary=None):
    # Text is inspected locally only; never returned, logged, or attached to a
    # new exception. Explicit type/status information takes precedence.
    name = type(error).__name__
    text = unicodedata.normalize("NFKC", str(error)).lower()
    status = getattr(error, "status_code", None)
    if name == "RequestBoundExceeded":
        return SafeError("timeout" if getattr(error, "kind", None) == "deadline" else "validation",
                         "operation_timeout" if getattr(error, "kind", None) == "deadline" else "aggregate_limit")
    if name == "ConfigurationError":
        category = "configuration"
    elif isinstance(error, PermissionError) and "read-only policy" in text:
        category = "policy"
    elif status == 401 or name == "AuthenticationError" or any(x in text for x in ("authentication", "unauthorized", "token expired", "401")):
        category = "authentication"
    elif isinstance(error, PermissionError) or status == 403 or any(x in text for x in ("authorization", "forbidden", "access denied", "permission", "security", "403")):
        category = "authorization"
    elif isinstance(error, TimeoutError) or "timeout" in name.lower() or "timed out" in text or "timeout" in text:
        category = "timeout"
    elif name == "MCPRuntimeError" or boundary == "mcp":
        category = "mcp"
    elif boundary == "model" or type(error).__module__.startswith("openai") or "azure openai" in text:
        category = "model"
    elif isinstance(error, (ValueError, TypeError)):
        category = "validation"
    else:
        category = "tool" if boundary == "tool" else "unexpected"
    return SafeError(category, {"mcp": "mcp_unavailable", "timeout": "operation_timeout",
                               "policy": "policy_blocked"}.get(category, category + "_failed"))


def safe_fields(**fields):
    """Reject unknown fields; omit non-scalar or unsafe values. No payload fields."""
    allowed = {"event", "request_id", "task_id", "route", "tool_name", "operation", "outcome",
               "error_category", "reason_code", "duration"}
    if set(fields) - allowed:
        raise ValueError("Unsupported diagnostic field")
    result = {}
    for key, value in fields.items():
        if key == "error_category" and isinstance(value, str) and value in CATEGORIES:
            result[key] = value
        elif key == "reason_code" and isinstance(value, str) and value in {category + "_failed" for category in CATEGORIES} | {"mcp_unavailable", "operation_timeout", "policy_blocked"}:
            result[key] = value
        elif key == "duration":
            if type(value) in (int, float) and 0 <= value < 1e9:
                result[key] = value
        elif isinstance(value, str) and len(value) <= 128 and not sensitive(value) and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            if key != "error_category" or value in CATEGORIES:
                result[key] = value
    return result


def diagnostic_line(**fields):
    return json.dumps(safe_fields(**fields), sort_keys=True)


def error_content_text(content):
    """Inspect only text, excluding random MCP block IDs from error classification."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "Tool returned an error"
    return "\n".join(block if isinstance(block, str) else block["text"] for block in content
        if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)))
