import unittest
from unittest.mock import AsyncMock, patch

from src.agent.main import (
    DEFAULT_QUESTION,
    parse_cli_question,
    route_question,
    task_manager_evidence_only,
)
from src.agent.aks_troubleshooting import (
    is_aks_cluster_health_question,
    is_task_manager_troubleshooting_question,
)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_task_manager_question_routes_to_aks(self):
        self.assertEqual(route_question("Why is my Task Manager application not working?"), "aks")

    def test_hyphenated_task_manager_question_routes_to_aks(self):
        self.assertEqual(route_question("Please troubleshoot task-manager"), "aks")

    def test_live_task_manager_health_request_routes_to_aks(self):
        self.assertEqual(route_question(
            "Check the Task Manager application in the AKS cluster and tell me whether "
            "the deployment and pods are healthy. If there are problems, explain the observed evidence."
        ), "aks")

    def test_task_manager_pod_health_routes_to_aks(self):
        self.assertEqual(route_question("Are the Task Manager pods healthy?"), "aks")

    def test_live_availability_investigation_routes_to_aks(self):
        question = (
            "My Task Manager application is deployed on AKS.\n\n"
            "Investigate whether there is any evidence of a current availability or connectivity "
            "problem with the Task Manager deployment and service.\n\n"
            "Use only read-only Kubernetes/Azure evidence. Separate:\n\n"
            "- observed facts\n- possible hypotheses\n- evidence that is still missing\n\n"
            "Do not make any changes or remediation."
        )
        self.assertEqual(route_question(question), "aks")

    def test_task_manager_investigation_language(self):
        for question in (
            "Investigate the Task Manager workload on Kubernetes.",
            "Is there a current connectivity problem with the Task Manager service?",
            "Show read-only evidence of Task Manager deployment availability.",
            "Investigate current problems with task-manager pods.",
        ):
            with self.subTest(question=question):
                self.assertEqual(route_question(question), "aks")

    def test_source_investigations_and_other_workloads_do_not_route_to_aks(self):
        for question, expected in (
            ("Inspect the Task Manager repository for Kubernetes deployment configuration.", "generic"),
            ("Why is the Task Manager AKS deployment pipeline not working?", "generic"),
            ("Review the Terraform configuration for Task Manager AKS service connectivity.", "terraform"),
            ("Investigate availability of the billing deployment on AKS.", "aks_cluster_health"),
            ("Show read-only Kubernetes pod evidence.", "generic"),
            ("List Task Manager pipelines.", "generic"),
        ):
            with self.subTest(question=question):
                self.assertEqual(route_question(question), expected)

    # --- General AKS cluster-health route ---

    def test_general_aks_cluster_health_routes_to_aks_cluster_health(self):
        self.assertEqual(
            route_question(
                "Investigate the health of my AKS cluster aks-azure-learning-dev. "
                "Check node and pod status, identify any unhealthy workloads, "
                "and explain any issues using the available evidence."
            ),
            "aks_cluster_health",
        )

    def test_cluster_node_status_request_routes_to_aks_cluster_health(self):
        self.assertEqual(route_question("Check the node status in my AKS cluster."), "aks_cluster_health")

    def test_cluster_pod_status_request_routes_to_aks_cluster_health(self):
        self.assertEqual(route_question("Are the pods in my AKS cluster healthy?"), "aks_cluster_health")

    def test_cluster_health_investigation_routes_to_aks_cluster_health(self):
        self.assertEqual(route_question("Investigate the health of my Kubernetes cluster."), "aks_cluster_health")

    def test_cluster_issues_request_routes_to_aks_cluster_health(self):
        self.assertEqual(route_question("Identify any issues in my AKS cluster."), "aks_cluster_health")

    def test_cluster_health_does_not_steal_task_manager_route(self):
        # Task Manager troubleshooting predicate fires first; cluster-health should not interfere.
        self.assertEqual(route_question("Why is my Task Manager application not working on AKS?"), "aks")

    def test_cluster_health_does_not_steal_terraform_route(self):
        self.assertEqual(
            route_question("Review the Terraform configuration for my AKS cluster."),
            "terraform",
        )

    def test_cluster_health_does_not_steal_ado_pipeline_route(self):
        from src.agent.ado_pipeline_yaml import pipeline_yaml_request
        question = "Summarize the AKS bootstrap pipeline YAML."
        if pipeline_yaml_request(question):
            self.assertEqual(route_question(question), "azure_devops")
        else:
            # If ADO predicate doesn't match, cluster-health should not match either
            # because "pipeline" is excluded.
            self.assertNotEqual(route_question(question), "aks_cluster_health")

    # --- is_aks_cluster_health_question predicate unit tests ---

    def test_predicate_true_for_aks_node_check(self):
        self.assertTrue(is_aks_cluster_health_question("Check the node status in my AKS cluster."))

    def test_predicate_true_for_kubernetes_health(self):
        self.assertTrue(is_aks_cluster_health_question("Investigate the health of my Kubernetes cluster."))

    def test_predicate_true_for_aks_pod_status(self):
        self.assertTrue(is_aks_cluster_health_question("Are the pods in my AKS cluster healthy?"))

    def test_predicate_true_for_unhealthy_workloads(self):
        self.assertTrue(is_aks_cluster_health_question(
            "Identify any unhealthy workloads in the cluster."
        ))

    def test_predicate_false_without_cluster_context(self):
        # No AKS/cluster/kubernetes keyword → False
        self.assertFalse(is_aks_cluster_health_question("Check if the pods are healthy."))

    def test_predicate_false_without_health_intent(self):
        # No check/investigate/health keyword → False
        self.assertFalse(is_aks_cluster_health_question("My AKS cluster name."))

    def test_predicate_false_for_pipeline_mention(self):
        self.assertFalse(is_aks_cluster_health_question("Check the AKS bootstrap pipeline."))

    def test_predicate_false_for_terraform_mention(self):
        self.assertFalse(is_aks_cluster_health_question("Review Terraform for the AKS cluster."))

    def test_predicate_false_for_repo_mention(self):
        self.assertFalse(is_aks_cluster_health_question("Inspect the repo for AKS cluster config."))

    def test_health_detection_stays_workload_and_kubernetes_specific(self):
        for question in ("Check Task Manager's pipeline status",):
            with self.subTest(question=question):
                self.assertEqual(route_question(question), "generic")

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
