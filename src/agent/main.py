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

import sys

LOGGER = logging.getLogger(__name__)
PROGRESS_LOGGER = logging.getLogger("src.agent.progress")


class TerminalProgressFilter(logging.Filter):
    """Filter records for terminal stderr output.

    In normal mode:
      - Allows WARNING, ERROR, CRITICAL records.
      - Allows concise progress messages from 'src.agent.progress'.
      - Suppresses low-level INFO logs from observability JSON, httpx, and internal modules.
    In verbose mode:
      - Allows all records (INFO and above).
    """

    def __init__(self, verbose: bool = False):
        super().__init__()
        self.verbose = verbose

    def filter(self, record: logging.LogRecord) -> bool:
        if self.verbose:
            return True
        if record.name == "mcp.os.posix.utilities" and "Process group termination failed" in record.getMessage():
            return False
        if record.levelno >= logging.WARNING:
            return True
        if record.name == "src.agent.progress":
            return True
        return False


class TerminalFormatter(logging.Formatter):
    """Formatter tailored for terminal presentation.

    In normal mode:
      - Progress messages are printed cleanly without logger name prefixes.
      - Warnings and errors display their severity level prefix clearly.
    In verbose mode:
      - Standard diagnostic format: '%(levelname)s %(name)s: %(message)s'.
    """

    def __init__(self, verbose: bool = False):
        super().__init__()
        self.verbose = verbose
        self._verbose_fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
        self._progress_fmt = logging.Formatter("%(message)s")
        self._warning_fmt = logging.Formatter("%(levelname)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        if self.verbose:
            return self._verbose_fmt.format(record)
        if record.name == "src.agent.progress":
            return self._progress_fmt.format(record)
        if record.levelno >= logging.WARNING:
            return self._warning_fmt.format(record)
        return self._verbose_fmt.format(record)


def configure_logging(verbose: bool = False, stream=None) -> logging.Handler:
    """Configure terminal logging for normal or verbose mode without globally disabling loggers."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(logging.INFO)
    handler.addFilter(TerminalProgressFilter(verbose=verbose))
    handler.setFormatter(TerminalFormatter(verbose=verbose))
    root.addHandler(handler)

    noisy_loggers = ("httpx", "httpcore", "openai")
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.INFO if verbose else logging.WARNING)

    logging.getLogger("src.observability").setLevel(logging.INFO)
    return handler


def build_cli_parser():
    parser = argparse.ArgumentParser(description="Run one DevOps AI Agent request.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Display detailed diagnostic logs.")
    parser.add_argument("question", nargs="*", help="Question to send to the agent.")
    return parser


def parse_cli_args(argv=None):
    parser = build_cli_parser()
    parsed = parser.parse_args(argv)
    question = " ".join(parsed.question) if parsed.question else DEFAULT_QUESTION
    return question, parsed.verbose


def parse_cli_question(argv=None):
    question, _ = parse_cli_args(argv)
    return question


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


def cli(argv=None):
    args = sys.argv[1:] if argv is None else argv
    verbose = any(arg in args for arg in ("-v", "--verbose"))
    raw_question = parse_cli_question(argv)
    if isinstance(raw_question, tuple):
        question, parsed_verbose = raw_question
        verbose = verbose or parsed_verbose
    else:
        question = raw_question

    configure_logging(verbose=verbose)
    try:
        asyncio.run(main(question=question))
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
