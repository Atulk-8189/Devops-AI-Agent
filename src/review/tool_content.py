"""Create compact model inputs without changing the evidence-bearing history."""
import json

from langchain_core.messages import AIMessage, ToolMessage

from src.review.review_validation import retrieved_files


def compact_tool_messages(messages, diagnostics=False):
    calls = {}
    compacted = []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[call['id']] = message
        if isinstance(message, ToolMessage) and message.tool_call_id in calls:
            files = retrieved_files([calls[message.tool_call_id], message])
            if files:
                path, code = next(iter(files.items()))
                content = f'FILE: {path.lstrip("/")}\n{code}'
                if diagnostics:
                    raw = message.content
                    raw_length = len(raw) if isinstance(raw, str) else len(json.dumps(raw))
                    print(f'Content sizes: {path}: raw={raw_length}, compacted={len(content)} characters', flush=True)
                message = message.model_copy(update={'content': content})
        compacted.append(message)
    return compacted
