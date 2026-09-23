"""CLI presentation and backwards-compatible one-shot/task-session entry point."""
import argparse
import asyncio
import logging

from dotenv import load_dotenv
from openai import AsyncOpenAI
from src.config import ConfigurationError, load_settings
from src.mcp.runtime import MCPRuntime
from src.safe_diagnostics import classify_error, diagnostic_line
from src.agent.aks_troubleshooting import collect_task_manager_evidence, collect_task_manager_evidence_report
from src.review.terraform_evidence import gather_terraform_evidence
from src.request_budget import BoundedResult
from src.tool_results import (
    normalize_successful_tool_result, MAX_TOOL_RESULT_CHARS, MAX_TOOL_CONTENT_DEPTH,
)
from src.agent.orchestration import RequestServices, orchestrate_request
# Preserve existing imports and patch seams while workflow code lives elsewhere.
from src.agent.workflows import (
    collect_evidence_report,
    State,
    ToolArgumentValidationError,
    openai_tools,
    text_content,
    openai_messages,
    parse_openai_tool_calls,
    validate_tool_arguments,
    validation_error_message,
    is_hard_tool_error,
    recoverable_tool_error_message,
    route_question,
    structured_json,
    diagnose_aks,
    MODEL,
    MAX_TOOL_EXECUTIONS,
    MAX_MULTIPLE_TOOL_CALL_ATTEMPTS,
    MAX_REPOSITORY_RECURSION_DEPTH,
    TOOL_BUDGET_EXHAUSTED_RESPONSE,
    DUPLICATE_TOOL_CALL_RESPONSE,
    EMPTY_FINAL_RESPONSE,
    MULTIPLE_TOOL_CALL_RESPONSE,
    MULTIPLE_TOOL_CALL_LIMIT_RESPONSE,
    DEFAULT_QUESTION,
)

LOGGER = logging.getLogger(__name__)


def parse_cli_question(argv=None):
    parser = argparse.ArgumentParser(description="Run one DevOps AI Agent request.")
    parser.add_argument("question", nargs="*", help="Question to send to the agent.")
    parsed = parser.parse_args(argv)
    return " ".join(parsed.question) if parsed.question else DEFAULT_QUESTION


async def task_manager_evidence_only():
    """Collect the sanitized AKS report without calling Azure OpenAI."""
    return await collect_evidence_report(runtime_factory=MCPRuntime, collector=collect_task_manager_evidence_report)


async def main(question=DEFAULT_QUESTION, *, task_context=None, context_enabled=False):
    services = RequestServices(
        load_environment=load_dotenv, load_settings=load_settings,
        client_factory=AsyncOpenAI, runtime_factory=MCPRuntime, router=route_question,
        collect_task_manager_evidence=collect_task_manager_evidence, diagnose_aks=diagnose_aks,
        gather_terraform_evidence=gather_terraform_evidence,
    )
    result = await orchestrate_request(question, task_context=task_context,
                                       context_enabled=context_enabled, services=services)
    if isinstance(result, BoundedResult):
        print("Investigation stopped at the request safety limit (" + result.reason_code + "). "
              "Evidence is incomplete; no additional collection or diagnosis was performed. "
              + str(len(result.evidence)) + " bounded tool results were retained for this request.")
        return None if context_enabled else result
    print(result.answer)
    return result.progress


def cli():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main(question=parse_cli_question()))
    except ConfigurationError as exc:
        safe = classify_error(exc)
        LOGGER.error(diagnostic_line(event="request_failed", error_category=safe.category, reason_code=safe.reason_code))
        print(safe.user_message(), flush=True)
        raise SystemExit(2)
    except Exception as exc:
        safe = classify_error(exc)
        LOGGER.error(diagnostic_line(event="request_failed", error_category=safe.category, reason_code=safe.reason_code))
        print(safe.user_message(), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
