"""Tests for terminal logging configuration, progress filtering, and verbose mode."""
import io
import json
import logging
import unittest
from unittest.mock import patch

from src.agent.main import (
    DEFAULT_QUESTION,
    TerminalFormatter,
    TerminalProgressFilter,
    build_cli_parser,
    cli,
    configure_logging,
    parse_cli_args,
    parse_cli_question,
)
from src.config import ConfigurationError
from src.observability import emit, observed_request


class TerminalLoggingFilterTests(unittest.TestCase):
    def setUp(self):
        self.progress_record = logging.LogRecord(
            name="src.agent.progress",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Connecting to services and discovering tools...",
            args=(),
            exc_info=None,
        )
        self.observability_record = logging.LogRecord(
            name="src.observability",
            level=logging.INFO,
            pathname=__file__,
            lineno=20,
            msg=json.dumps({"event": "request_started", "request_id": "req-1"}),
            args=(),
            exc_info=None,
        )
        self.httpx_record = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=30,
            msg="HTTP Request: POST https://example.com/openai",
            args=(),
            exc_info=None,
        )
        self.warning_record = logging.LogRecord(
            name="src.agent.workflows",
            level=logging.WARNING,
            pathname=__file__,
            lineno=40,
            msg="Recoverable tool issue encountered",
            args=(),
            exc_info=None,
        )
        self.error_record = logging.LogRecord(
            name="src.agent.main",
            level=logging.ERROR,
            pathname=__file__,
            lineno=50,
            msg="Request failed due to configuration error",
            args=(),
            exc_info=None,
        )

    def test_normal_mode_filter_allows_progress_and_warnings_only(self):
        filt = TerminalProgressFilter(verbose=False)
        self.assertTrue(filt.filter(self.progress_record))
        self.assertTrue(filt.filter(self.warning_record))
        self.assertTrue(filt.filter(self.error_record))
        self.assertFalse(filt.filter(self.observability_record))
        self.assertFalse(filt.filter(self.httpx_record))

    def test_verbose_mode_filter_allows_all_records(self):
        filt = TerminalProgressFilter(verbose=True)
        self.assertTrue(filt.filter(self.progress_record))
        self.assertTrue(filt.filter(self.observability_record))
        self.assertTrue(filt.filter(self.httpx_record))
        self.assertTrue(filt.filter(self.warning_record))
        self.assertTrue(filt.filter(self.error_record))

    def test_filter_handles_mcp_process_group_warnings(self):
        pg_warning = logging.LogRecord(
            name="mcp.os.posix.utilities",
            level=logging.WARNING,
            pathname=__file__,
            lineno=60,
            msg="Process group termination failed for PID 1234: [Errno 3] No such process, falling back to simple terminate",
            args=(),
            exc_info=None,
        )
        filt_normal = TerminalProgressFilter(verbose=False)
        self.assertFalse(filt_normal.filter(pg_warning))

        filt_verbose = TerminalProgressFilter(verbose=True)
        self.assertTrue(filt_verbose.filter(pg_warning))

    def test_filter_handles_mcp_subprocess_logs(self):
        info_subproc = logging.LogRecord(
            name="src.mcp.subprocess",
            level=logging.INFO,
            pathname=__file__,
            lineno=70,
            msg='[npx] {"level":"info","message":"Azure DevOps MCP server running..."}',
            args=(),
            exc_info=None,
        )
        err_subproc = logging.LogRecord(
            name="src.mcp.subprocess",
            level=logging.ERROR,
            pathname=__file__,
            lineno=80,
            msg='[aks-mcp] [ERROR] connection failed',
            args=(),
            exc_info=None,
        )
        filt_normal = TerminalProgressFilter(verbose=False)
        self.assertFalse(filt_normal.filter(info_subproc))
        self.assertTrue(filt_normal.filter(err_subproc))

        filt_verbose = TerminalProgressFilter(verbose=True)
        self.assertTrue(filt_verbose.filter(info_subproc))
        self.assertTrue(filt_verbose.filter(err_subproc))

    def test_filter_handles_diagnostics_collection_allowed(self):
        diag_record = logging.LogRecord(
            name="src.diagnostics",
            level=logging.INFO,
            pathname=__file__,
            lineno=90,
            msg='{"event":"collection_allowed","tool_name":"kubectl_resources","operation":"get","outcome":"allowed"}',
            args=(),
            exc_info=None,
        )
        filt_normal = TerminalProgressFilter(verbose=False)
        self.assertFalse(filt_normal.filter(diag_record))

        filt_verbose = TerminalProgressFilter(verbose=True)
        self.assertTrue(filt_verbose.filter(diag_record))


class TerminalFormatterTests(unittest.TestCase):
    def test_normal_mode_formatter_formats_cleanly(self):
        formatter = TerminalFormatter(verbose=False)
        prog_record = logging.LogRecord(
            "src.agent.progress", logging.INFO, __file__, 10,
            "Connecting to cluster...", (), None,
        )
        warn_record = logging.LogRecord(
            "src.agent.workflows", logging.WARNING, __file__, 20,
            "Retry scheduled", (), None,
        )
        err_record = logging.LogRecord(
            "src.agent.workflows", logging.ERROR, __file__, 30,
            "Fatal failure", (), None,
        )

        self.assertEqual(formatter.format(prog_record), "Connecting to cluster...")
        self.assertEqual(formatter.format(warn_record), "WARNING: Retry scheduled")
        self.assertEqual(formatter.format(err_record), "ERROR: Fatal failure")

    def test_verbose_mode_formatter_includes_logger_name_and_level(self):
        formatter = TerminalFormatter(verbose=True)
        record = logging.LogRecord(
            "src.observability", logging.INFO, __file__, 10,
            '{"event":"request_started"}', (), None,
        )
        self.assertEqual(
            formatter.format(record),
            'INFO src.observability: {"event":"request_started"}',
        )


class CliArgParsingTests(unittest.TestCase):
    def test_default_question_when_no_arguments_given(self):
        q, verbose = parse_cli_args([])
        self.assertEqual(q, DEFAULT_QUESTION)
        self.assertFalse(verbose)
        self.assertEqual(parse_cli_question([]), DEFAULT_QUESTION)

    def test_verbose_flags(self):
        q1, v1 = parse_cli_args(["-v"])
        self.assertEqual(q1, DEFAULT_QUESTION)
        self.assertTrue(v1)

        q2, v2 = parse_cli_args(["--verbose"])
        self.assertEqual(q2, DEFAULT_QUESTION)
        self.assertTrue(v2)

    def test_custom_question_with_verbose_flag(self):
        q, verbose = parse_cli_args(["-v", "Check", "AKS", "cluster", "health"])
        self.assertEqual(q, "Check AKS cluster health")
        self.assertTrue(verbose)

    def test_custom_question_without_verbose_flag(self):
        q, verbose = parse_cli_args(["Why", "is", "the", "pod", "failing?"])
        self.assertEqual(q, "Why is the pod failing?")
        self.assertFalse(verbose)


class LoggingConfigurationAndObservabilityTests(unittest.TestCase):
    def tearDown(self):
        # Reset logging handlers to prevent side effects across test cases
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

    def test_normal_mode_hides_observability_json_from_terminal_stream(self):
        stream = io.StringIO()
        configure_logging(verbose=False, stream=stream)

        progress_logger = logging.getLogger("src.agent.progress")
        obs_logger = logging.getLogger("src.observability")
        warn_logger = logging.getLogger("src.agent.orchestration")

        progress_logger.info("Connecting to services and discovering tools...")
        obs_logger.info(json.dumps({"event": "request_started", "request_id": "test-req-123"}))
        warn_logger.warning("Potential rate limit warning")

        output = stream.getvalue()
        self.assertIn("Connecting to services and discovering tools...", output)
        self.assertIn("WARNING: Potential rate limit warning", output)
        self.assertNotIn("test-req-123", output)
        self.assertNotIn("request_started", output)

    def test_verbose_mode_shows_observability_json_in_terminal_stream(self):
        stream = io.StringIO()
        configure_logging(verbose=True, stream=stream)

        obs_logger = logging.getLogger("src.observability")
        obs_logger.info(json.dumps({"event": "request_started", "request_id": "test-req-verbose"}))

        output = stream.getvalue()
        self.assertIn("test-req-verbose", output)
        self.assertIn("src.observability", output)

    def test_observability_still_captures_all_events_in_normal_mode(self):
        stream = io.StringIO()
        configure_logging(verbose=False, stream=stream)

        @observed_request
        async def sample_action():
            emit("evidence_collection", outcome="started")
            return "done"

        with self.assertLogs("src.observability", level="INFO") as captured:
            import asyncio
            asyncio.run(sample_action())

        records = [json.loads(r.getMessage()) for r in captured.records]
        self.assertTrue(any(r["event"] == "request_started" for r in records))
        self.assertTrue(any(r["event"] == "evidence_collection" for r in records))
        self.assertTrue(any(r["event"] == "request_completed" for r in records))

        # But terminal stream stayed clean
        terminal_output = stream.getvalue()
        self.assertNotIn("request_started", terminal_output)
        self.assertNotIn("evidence_collection", terminal_output)


class CliExecutionOutputTests(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

    def test_cli_handles_configuration_error_cleanly(self):
        with patch("src.agent.main.main", side_effect=ConfigurationError("Missing required key")), \
                patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as ctx:
            cli(["--verbose"])
        self.assertEqual(ctx.exception.code, 2)
        mock_print.assert_called_once()
        self.assertIn("configuration_failed", mock_print.call_args[0][0])


class MCPProcessLoggingAndCleanupTests(unittest.TestCase):
    def test_drain_stderr_worker_classifies_logs(self):
        import os
        from src.mcp.runtime import _drain_stderr_worker

        r_fd, w_fd = os.pipe()
        with os.fdopen(w_fd, "w", encoding="utf-8") as writer:
            writer.write('{"level":"info","message":"server ready"}\n')
            writer.write('{"level":"error","message":"fatal auth error"}\n')
            writer.write('[INFO] routine component loaded\n')
            writer.write('[ERROR] failed to connect to cluster\n')

        with self.assertLogs("src.mcp.subprocess", level="INFO") as captured:
            _drain_stderr_worker(r_fd, "test-server")

        records = captured.records
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0].levelno, logging.INFO)
        self.assertIn("server ready", records[0].getMessage())
        self.assertEqual(records[1].levelno, logging.ERROR)
        self.assertIn("fatal auth error", records[1].getMessage())
        self.assertEqual(records[2].levelno, logging.INFO)
        self.assertIn("routine component loaded", records[2].getMessage())
        self.assertEqual(records[3].levelno, logging.ERROR)
        self.assertIn("failed to connect to cluster", records[3].getMessage())

    def test_collection_allowed_does_not_print_to_stdout(self):
        from src.review.terraform_evidence import gather_terraform_evidence
        from unittest.mock import AsyncMock, patch

        mock_tool = AsyncMock()
        mock_tool.name = "repo_repository"
        mock_tool.args_schema = {"type": "object", "properties": {}}

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
                self.assertLogs("src.diagnostics", level="INFO") as captured, \
                patch("src.observability.mcp_call", side_effect=TypeError("stop")):
            import asyncio
            try:
                asyncio.run(gather_terraform_evidence([mock_tool]))
            except TypeError:
                pass

        # Nothing printed to stdout
        self.assertEqual(mock_stdout.getvalue(), "")
        # Logged to src.diagnostics instead
        self.assertTrue(any("collection_allowed" in r.getMessage() for r in captured.records))


if __name__ == "__main__":
    unittest.main()
