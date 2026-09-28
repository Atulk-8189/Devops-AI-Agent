"""Local, single-process UI. Agent execution and security remain backend-owned."""
import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import sys

import chainlit as cl
from chainlit.config import config

# Chainlit is launched with frontend/ as its application root, not as a package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.orchestration import orchestrate_request
from src.agent.workflows import WorkflowResult
from src.request_budget import BoundedResult
from src.safe_diagnostics import classify_error
from frontend import response_safety
from frontend.investigation import (CAPABILITIES, CONNECTIONS, Investigation, EventHandler,
                                    _active, display_events)
try:
    from frontend.formatter import format_response
except ImportError:
    from formatter import format_response

try:
    from frontend.data_layer import SQLiteDataLayer, clean_thread_title
except ImportError:
    from data_layer import SQLiteDataLayer, clean_thread_title

if not os.environ.get("CHAINLIT_AUTH_SECRET"):
    os.environ["CHAINLIT_AUTH_SECRET"] = "devops-ai-agent-local-loopback-secret-2026"

_DATA_LAYER = SQLiteDataLayer()

if hasattr(cl, "header_auth_callback"):
    @cl.header_auth_callback
    def header_auth_callback(headers=None):
        user_cls = getattr(cl, "User", None)
        if user_cls:
            return user_cls(identifier="local-user", metadata={"role": "admin", "provider": "header"})
        return None

if hasattr(cl, "data_layer"):
    @cl.data_layer
    def get_data_layer():
        return _DATA_LAYER

if hasattr(cl, "on_chat_resume"):
    @cl.on_chat_resume
    async def on_chat_resume(thread):
        """Cleanly resume existing conversation without cross-conversation pollution."""
        # Chainlit restores persisted steps, their metadata and side elements.
        # No agent context or connection claims are restored into execution.
        return None


def validate_local_launch():
    if config.run.host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("This frontend only supports a loopback host.")
    if config.run.watch or config.run.debug:
        raise RuntimeError("Watch and debug modes are disabled for this frontend.")


validate_local_launch()
response_safety.log_loaded_filter()

# Shared across ALL chats in this process, not just messages in one session.
# Keep ownership until orchestration (including its finally/cleanup) finishes.
_REQUEST_LOCK = asyncio.Lock()



@dataclass(frozen=True, repr=False)
class Presentation:
    content: str | None = None
    notice: str | None = None
    language: str | None = None
    elements: list | None = None
    raw_response: str | None = None


def response_language(text):
    """Inspect JSON for highlighting only; never reserialize or replace the answer."""
    if text.lstrip().startswith(("{", "[")):
        try:
            if isinstance(json.loads(text), (dict, list)):
                return "json"
        except (ValueError, RecursionError):
            pass
    return None


def prepare_result(result):
    """Keep UI notices separate from the complete agent-authored response."""
    if isinstance(result, BoundedResult):
        # Never serialize retained tool evidence, usage internals or an arbitrary result.
        return Presentation(notice=(
            "Investigation stopped at a request safety limit. Evidence is incomplete; "
            "no further collection or diagnosis was performed."
        ))
    if not isinstance(result, WorkflowResult) or not isinstance(result.answer, str):
        return Presentation(notice="The agent did not return a usable final response. No diagnosis is established.")
    answer = result.answer
    if not answer.strip():
        return Presentation(content=answer, notice="The agent returned no final answer. No diagnosis is established.")
    decision = response_safety.filter_response(answer)
    if decision.outcome == "allowed":
        formatted = format_response(answer)
        return Presentation(content=formatted, raw_response=answer)
    if decision.outcome == "redacted" and decision.text.startswith(response_safety.NOTICE):
        # Existing security exception is visibly distinguished from an exact response.
        redacted = decision.text[len(response_safety.NOTICE):]
        formatted = format_response(redacted)
        return Presentation(content=formatted, notice=response_safety.NOTICE, raw_response=redacted)
    return Presentation(notice=response_safety.WITHHELD)


@cl.on_chat_start
async def on_chat_start():
    if hasattr(cl, "user_session"):
        cl.user_session.set("current_trace_panel", None)
    elements = []
    if hasattr(cl, "CustomElement"):
        elements = [cl.CustomElement(name="Welcome", props={
            "cards": CAPABILITIES, "connections": CONNECTIONS,
        })]
    await cl.Message(content="" if elements else "DevOps AI Agent · Read-only\n\n" + CONNECTIONS,
                     elements=elements, author="Frontend welcome").send()



@cl.on_message
async def on_message(message: cl.Message):
    if not isinstance(message.content, str) or not message.content.strip():
        await cl.Message(content="Please enter a DevOps question.").send()
        return
    if message.elements:
        await cl.Message(content="This frontend accepts text questions only.").send()
        return

    if hasattr(cl, "context") and hasattr(cl.context, "session"):
        session = getattr(cl.context, "session", None)
        thread_id = getattr(session, "thread_id", None) if session else None
        if thread_id and _DATA_LAYER:
            await _DATA_LAYER.ensure_thread_title(thread_id, message.content, user_id="local-user")

    if hasattr(cl, "user_session"):
        prev_trace = cl.user_session.get("current_trace_panel")
        if prev_trace is not None and hasattr(prev_trace, "remove"):
            try:
                await prev_trace.remove()
            except Exception:
                pass

    investigation = Investigation()
    panel = None
    trace_panel = None
    elements = []
    if hasattr(cl, "CustomElement"):
        panel = cl.CustomElement(name="Activity", props=investigation.activity.snapshot())
        elements.append(panel)
        trace_panel = cl.CustomElement(name="ExecutionTrace", props=investigation.activity.trace.snapshot(), display="side")
        elements.append(trace_panel)
        if hasattr(cl, "user_session"):
            cl.user_session.set("current_trace_panel", trace_panel)
    status = cl.Message(content="" if panel else "Waiting for the read-only agent…",
                        elements=elements, author="Execution status")
    await status.send()
    try:
        async with _REQUEST_LOCK:
            status.content = "Read-only agent execution started…"
            await status.update()
            # Do not forward chat history, browser settings, approvals or credentials.
            loggers = [logging.getLogger(name) for name in (
                "src.observability", "src.agent.orchestration",
            )]
            handler = EventHandler()
            old_levels = [logger.level for logger in loggers]
            token = _active.set(investigation)
            for logger in loggers:
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
            pump = asyncio.create_task(display_events(investigation, status, panel, trace_panel))
            try:
                result = await orchestrate_request(message.content)
                presentation = prepare_result(result)
                if isinstance(result, BoundedResult):
                    investigation.activity.end("stopped", "Investigation stopped at a safety limit; evidence is incomplete")
                elif isinstance(result, WorkflowResult):
                    investigation.activity.end("completed", "Investigation completed")
                else:
                    investigation.activity.end("unconfirmed", "Request ended without a usable result")
            finally:
                _active.reset(token)
                for logger, old_level in zip(loggers, old_levels):
                    logger.removeHandler(handler)
                    logger.setLevel(old_level)
                investigation.queue.put_nowait(None)
                try:
                    await pump
                except Exception:
                    # A display failure must not replace the original agent result.
                    pass
    except asyncio.CancelledError:
        # Direct awaiting lets cancellation reach the existing cleanup lifecycle.
        # No background agent task is left running after releasing the lock.
        investigation.activity.end("cancelled", "Investigation cancelled")
        status.metadata = {"activity": investigation.activity.snapshot(), "trace": investigation.activity.trace.snapshot()}
        status.content = "Investigation cancelled"
        if panel is not None:
            panel.props = investigation.activity.snapshot()
        if trace_panel is not None:
            trace_panel.props = investigation.activity.trace.snapshot()
        await status.update()
        raise
    except Exception as error:
        safe = classify_error(error)
        investigation.accept({"event": "request_failed", "error_category": safe.category})
        presentation = Presentation(notice=safe.user_message())
    metadata = investigation.finish(presentation.raw_response)
    investigation.activity.trace.metadata = metadata
    status.metadata = {
        "investigation": metadata,
        "activity": investigation.activity.snapshot(),
        "trace": investigation.activity.trace.snapshot(),
    }
    status_elements = []
    if panel is not None:
        panel.props = investigation.activity.snapshot()
        status_elements.append(panel)
    if trace_panel is not None:
        trace_panel.props = investigation.activity.trace.snapshot()
        status_elements.append(trace_panel)
    if not status_elements and hasattr(cl, "Text"):
        status_elements.append(cl.Text(name="Investigation details", content=json.dumps(metadata, indent=2, ensure_ascii=False), display="side"))
    status.elements = status_elements
    status.content = ("" if panel else investigation.activity.current + " · ") + metadata["category"] + " · Read-only · Investigation details"
    await status.update()
    if presentation.notice:
        await cl.Message(content=presentation.notice, author="Frontend notice").send()
    if presentation.content is not None:
        msg_kwargs = {"content": presentation.content}
        msg_elements = list(presentation.elements) if presentation.elements else []
        if hasattr(cl, "CustomElement"):
            msg_elements.append(cl.CustomElement(name="ExecutionViewerButton", props=investigation.activity.trace.snapshot()))
        if msg_elements:
            msg_kwargs["elements"] = msg_elements
        if presentation.language:
            msg_kwargs["language"] = presentation.language
        await cl.Message(**msg_kwargs).send()
