import unittest
from unittest.mock import AsyncMock, patch

from src.agent.main import (
    DEFAULT_QUESTION,
    parse_cli_question,
    route_question,
    task_manager_evidence_only,
)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_task_manager_question_routes_to_aks(self):
        self.assertEqual(route_question("Why is my Task Manager application not working?"), "aks")

    def test_hyphenated_task_manager_question_routes_to_aks(self):
        self.assertEqual(route_question("Please troubleshoot task-manager"), "aks")

    def test_terraform_review_routes_to_terraform(self):
        self.assertEqual(
            route_question("Review the Terraform configuration in the terraform folder"), "terraform"
        )

    def test_terraform_review_with_why_does_not_route_to_aks(self):
        self.assertEqual(
            route_question("Why should we review the Terraform configuration?"), "terraform"
        )

    def test_other_question_routes_to_generic(self):
        self.assertEqual(route_question("List the available repositories"), "generic")

    def test_no_argument_cli_retains_documented_terraform_default(self):
        self.assertEqual(parse_cli_question([]), DEFAULT_QUESTION)

    async def test_evidence_only_api_uses_collector_without_an_llm_call(self):
        runtime = type("Runtime", (), {"tools": ["kubectl-tool"], "initialize": AsyncMock(), "close": AsyncMock()})()
        report = {"decision": {"stop_reason": None}}
        with patch("src.agent.main.MCPRuntime", return_value=runtime), \
                patch("src.agent.main.collect_task_manager_evidence_report", new=AsyncMock(return_value=report)) as collector:
            self.assertEqual(await task_manager_evidence_only(), report)
        collector.assert_awaited_once_with(runtime.tools)
        runtime.initialize.assert_awaited_once()
        runtime.close.assert_awaited_once()
