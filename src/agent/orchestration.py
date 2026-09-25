"""Coordinate one request; route workflows and safety rules remain elsewhere."""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from openai import AsyncOpenAI

from src.agent import workflows
from src.agent.context import TaskContext, context_expired
from src.agent.task_session import hint_text
from src.config import DEFAULT_REQUEST_DEADLINE_SECONDS, load_settings
from src.mcp.runtime import MCPRuntime
from src.observability import observed_request, select_route, emit
from src.policy.policy import ALLOWED_PROJECT
from src.request_budget import current_budget

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestServices:
    """Explicit per-invocation dependencies, including legacy entry-point seams."""
    load_environment: Callable = load_dotenv
    load_settings: Callable = load_settings
    client_factory: Callable = AsyncOpenAI
    runtime_factory: Callable = MCPRuntime
    router: Callable = workflows.route_question
    collect_task_manager_evidence: Callable = workflows.collect_task_manager_evidence
    diagnose_aks: Callable = workflows.diagnose_aks
    gather_terraform_evidence: Callable = workflows.gather_terraform_evidence


@observed_request(return_outcome=True)
async def orchestrate_request(question, *, task_context=None, context_enabled=False, services=None):
    """Return a workflow result or bounded outcome; hard failures propagate safely.

    The existing lifecycle decorator creates isolated request state, emits events,
    applies the outer deadline and classifies failures. It does not print outcomes
    here. The CLI owns presentation and exception-to-exit-code translation.
    """
    services = services or RequestServices()
    if task_context is not None:
        task_context = TaskContext.model_validate(task_context.model_dump())
        if context_expired(task_context, now=datetime.now(timezone.utc)) or task_context.scope.project != ALLOWED_PROJECT:
            task_context = None
    hints = hint_text(task_context)
    services.load_environment(Path(__file__).resolve().parents[2] / ".env")
    settings = services.load_settings()
    budget = current_budget()
    if budget:
        budget.configure(getattr(settings, "request_deadline_seconds", DEFAULT_REQUEST_DEADLINE_SECONDS))
    client = services.client_factory(api_key=settings.azure_openai_api_key,
        base_url=f"{settings.azure_openai_endpoint}/openai/v1/", timeout=60.0, max_retries=0)
    runtime = None
    failure = None
    try:
        LOGGER.info("agent startup: initializing Azure OpenAI and MCP runtime")
        runtime = services.runtime_factory(settings=settings)
        await runtime.initialize()
        LOGGER.info("tool discovery complete: %d allowed tools", len(runtime.tools))
        route = services.router(question)
        if route == "generic" and task_context is not None and task_context.topic in {"aks", "terraform"}:
            route = task_context.topic
        select_route(route)
        from src.agent.ado_pipeline_yaml import handle_pipeline_yaml
        handler = {"generic": workflows.handle_generic, "aks": workflows.handle_aks,
                   "aks_cluster_health": workflows.handle_aks_cluster_health,
                   "azure_devops": handle_pipeline_yaml,
                   "terraform": workflows.handle_terraform}[route]
        return await handler(client, runtime.tools, question, hints=hints,
                             task_context=task_context, context_enabled=context_enabled, services=services)
    except BaseException as error:
        # Include cancellation: cleanup must not replace the original failure.
        failure = error
        raise
    finally:
        cleanup_failure = None
        for resource in (client, runtime):
            if resource is None:
                continue
            try:
                await resource.close()
            except BaseException as error:
                if cleanup_failure is None:
                    cleanup_failure = error
                emit("cleanup_failed", outcome="failed", reason_code="cleanup_failed")
        if failure is None and cleanup_failure is not None:
            raise cleanup_failure
        if cleanup_failure is None:
            LOGGER.info("agent shutdown complete")
