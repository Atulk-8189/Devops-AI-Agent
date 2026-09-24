"""Temporary content-free Chat Completions diagnostics; never returns input values."""
import json

MAX_DIAGNOSTIC_MESSAGES = 128


def request_structure(messages, tools=None):
    # Import lazily: policy uses observability, which uses this helper.
    from src.policy.policy import ALLOWED_TOOL_NAMES

    def safe_name(value):
        return value if type(value) is str and value in ALLOWED_TOOL_NAMES else None

    def kind(value):
        return {str: "string", list: "list", type(None): "null"}.get(type(value), "invalid")

    rows, pending, seen = [], {}, set()
    valid = type(messages) is list and bool(messages)
    pairing = True
    complete = type(messages) is list and len(messages) <= MAX_DIAGNOSTIC_MESSAGES
    total = 0
    for index, message in enumerate(messages[:MAX_DIAGNOSTIC_MESSAGES] if type(messages) is list else []):
        if type(message) is not dict:
            valid = False
            rows.append({"index": index, "role": "invalid"})
            continue
        role = message.get("role")
        role = role if type(role) is str and role in {"system", "developer", "user", "assistant", "tool"} else "invalid"
        content = message.get("content")
        content_valid = type(content) is str
        length = len(content) if type(content) is str else 0
        if type(content) is list:
            content_valid = bool(content)
            for block in content:
                ok = type(block) is dict and block.get("type") == "text" and type(block.get("text")) is str
                content_valid &= ok
                if ok:
                    length += len(block["text"])
        calls = message.get("tool_calls")
        count = len(calls) if type(calls) is list else 0
        row = {"index": index, "role": role, "content_type": kind(content),
               "content_length": length, "has_tool_calls": bool(count), "tool_call_count": count}
        total += length
        if pending and role != "tool":
            pairing = False
        if calls is not None:
            valid &= role == "assistant" and type(calls) is list and bool(calls)
        if role == "assistant" and count:
            content_valid |= content is None
            row["tool_names"] = []
            row["tool_call_ids_present"] = []
            for call in calls:
                call = call if type(call) is dict else {}
                identifier = call.get("id")
                present = type(identifier) is str and bool(identifier)
                row["tool_call_ids_present"].append(present)
                function = call.get("function")
                function = function if type(function) is dict else {}
                name = safe_name(function.get("name"))
                if name:
                    row["tool_names"].append(name)
                valid &= present and call.get("type") == "function" and name is not None and type(function.get("arguments")) is str
                if not present or identifier in seen:
                    pairing = False
                else:
                    seen.add(identifier)
                    pending[identifier] = row
            row["tool_result_count"] = 0
        if role == "tool":
            identifier = message.get("tool_call_id")
            present = type(identifier) is str and bool(identifier)
            matched = present and identifier in pending
            row.update(tool_call_id_present=present, tool_call_id_matches_previous_assistant=matched)
            valid &= present
            pairing &= matched
            if matched:
                pending.pop(identifier)["tool_result_count"] += 1
        valid &= role != "invalid" and content_valid
        rows.append(row)
    pairing = bool(pairing and not pending and complete)
    serialized = None
    if complete:
        try:
            # Compute length only; never return or emit serialized content.
            serialized = len(json.dumps(messages, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            valid = False
    names = []
    if type(tools) is list:
        for tool in tools:
            function = tool.get("function") if type(tool) is dict else None
            name = safe_name(function.get("name")) if type(function) is dict else None
            if name:
                names.append(name)
    return {"message_count": len(messages) if type(messages) is list else 0,
            "message_structure": rows, "message_structure_valid": bool(valid and pairing and complete),
            "tool_pairing_valid": pairing, "snapshot_complete": complete,
            "total_content_chars": total, "serialized_messages_chars": serialized,
            "tools_count": len(tools) if type(tools) is list else 0, "tool_names": names}
