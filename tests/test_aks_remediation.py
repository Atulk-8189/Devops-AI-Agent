"""Automated tests for the AKS-to-Terraform remediation discovery workflow."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent.aks_troubleshooting import (
    AKSDiagnosis,
    EvidenceStep,
    TroubleshootingEvidence,
)
from src.agent.remediation_discovery import (
    MatchedTerraformResource,
    RemediationDiscoveryResult,
    WorkloadEvidenceSummary,
    correlate_aks_to_terraform,
    extract_workload_evidence,
    format_code_with_lines,
    is_aks_remediation_question,
)
from src.agent.remediation_proposal import RemediationProposalResult
from src.agent.workflows import handle_aks_remediation, route_question
from src.agent.orchestration import RequestServices, orchestrate_request


class TestAKSRemediationRouting(unittest.TestCase):
    """Test question classification and routing for the AKS-to-Terraform bridge."""

    def test_remediation_questions_route_to_aks_remediation(self):
        questions = [
            "Investigate the Task Manager failure in AKS and find the Terraform resource to fix it.",
            "Remediate the task-manager AKS issue in Terraform.",
            "Find the Terraform file for the failing Task Manager pod.",
            "Correlate Task Manager AKS errors with Terraform configuration.",
            "Diagnose the failing task-manager pod and locate its Terraform resource for remediation.",
            "Trace the AKS Task Manager ImagePullBackOff to the Terraform code.",
        ]
        for q in questions:
            with self.subTest(question=q):
                self.assertTrue(is_aks_remediation_question(q))
                self.assertEqual(route_question(q), "aks_remediation")

    def test_existing_routes_are_not_stolen(self):
        cases = [
            ("Review the Terraform configuration in the terraform folder", "terraform"),
            ("Review the Terraform configuration for Task Manager AKS service connectivity.", "terraform"),
            ("Why is my Task Manager application not working?", "aks"),
            ("Check the node status in my AKS cluster.", "aks_cluster_health"),
            ("Investigate the health of my AKS cluster.", "aks_cluster_health"),
            ("List the available repositories", "generic"),
            ("Inspect the Task Manager repository for Kubernetes deployment configuration.", "generic"),
        ]
        for q, expected in cases:
            with self.subTest(question=q):
                self.assertFalse(is_aks_remediation_question(q))
                self.assertEqual(route_question(q), expected)


class TestWorkloadEvidenceExtraction(unittest.TestCase):
    """Test parsing and summarizing Kubernetes diagnostic evidence."""

    def test_extract_workload_evidence_from_troubleshooting_evidence(self):
        evidence = TroubleshootingEvidence(
            deployment={
                "metadata": {"name": "task-manager", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "task-manager",
                                    "image": "myregistry.azurecr.io/task-manager:badtag",
                                }
                            ]
                        }
                    }
                },
                "status": {
                    "conditions": [
                        {
                            "type": "Progressing",
                            "status": "False",
                            "reason": "ProgressDeadlineExceeded",
                        }
                    ]
                },
            },
            pod_health={
                "pods": [
                    {
                        "metadata": {"name": "task-manager-789-xyz", "namespace": "default"},
                        "containers": [
                            {
                                "name": "task-manager",
                                "image": "myregistry.azurecr.io/task-manager:badtag",
                                "ready": False,
                                "restart_count": 0,
                                "waiting_reason": "ImagePullBackOff",
                                "waiting_message": "Back-off pulling image myregistry.azurecr.io/task-manager:badtag",
                            }
                        ],
                    }
                ]
            },
        )

        workload = extract_workload_evidence(evidence)
        self.assertEqual(workload.workload_name, "task-manager")
        self.assertEqual(workload.namespace, "default")
        self.assertIn("myregistry.azurecr.io/task-manager:badtag", workload.container_images)
        self.assertIn("task-manager", workload.container_names)
        self.assertIn("task-manager", workload.unhealthy_containers)
        self.assertIn("ImagePullBackOff", workload.failure_reasons)
        self.assertIn("ProgressDeadlineExceeded", workload.failure_reasons)


class TestRemediationCorrelation(unittest.TestCase):
    """Test correlation logic between AKS workload evidence and Terraform resources."""

    def setUp(self):
        self.aks_evidence = TroubleshootingEvidence(
            deployment={
                "metadata": {"name": "task-manager", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "task-manager",
                                    "image": "myregistry.azurecr.io/task-manager:badtag",
                                }
                            ]
                        }
                    }
                },
            },
            pod_health={
                "pods": [
                    {
                        "metadata": {"name": "task-manager-789-xyz", "namespace": "default"},
                        "containers": [
                            {
                                "name": "task-manager",
                                "image": "myregistry.azurecr.io/task-manager:badtag",
                                "ready": False,
                                "restart_count": 0,
                                "waiting_reason": "ImagePullBackOff",
                            }
                        ],
                    }
                ]
            },
        )
        self.diagnosis = AKSDiagnosis(
            summary="Pod task-manager-789-xyz is failing to pull image 'badtag'.",
            observed_evidence=["Container task-manager waiting reason: ImagePullBackOff"],
            likely_root_causes=["Image tag 'badtag' does not exist in registry."],
            recommended_next_diagnostic_step="Inspect Azure Repos Terraform files to find the image tag definition.",
        )

    def test_successful_discovery_exact_match(self):
        tf_files = {
            "/terraform/main.tf": (
                'resource "kubernetes_deployment" "task_manager" {\n'
                '  metadata {\n'
                '    name = "task-manager"\n'
                '  }\n'
                '  spec {\n'
                '    template {\n'
                '      spec {\n'
                '        container {\n'
                '          name  = "task-manager"\n'
                '          image = "myregistry.azurecr.io/task-manager:badtag"\n'
                '        }\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}\n'
            )
        }

        result = correlate_aks_to_terraform(self.aks_evidence, self.diagnosis, tf_files)

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.confidence, "exact")
        self.assertEqual(result.repository_path, "/terraform/main.tf")
        self.assertIsNotNone(result.matched_resource)
        self.assertEqual(result.matched_resource.resource_type, "kubernetes_deployment")
        self.assertEqual(result.matched_resource.resource_name, "task_manager")
        self.assertEqual(result.candidate_matches, [])
        self.assertIsNotNone(result.relevant_code)
        self.assertIn("myregistry.azurecr.io/task-manager:badtag", result.relevant_code)
        self.assertIn("1: resource \"kubernetes_deployment\" \"task_manager\"", result.relevant_code)
        self.assertTrue(any("out-of-band" in u.lower() for u in result.mapping_uncertainties))

    def test_missing_terraform_mappings(self):
        tf_files = {
            "/terraform/network.tf": (
                'resource "azurerm_virtual_network" "vnet" {\n'
                '  name                = "dev-vnet"\n'
                '  address_space       = ["10.0.0.0/16"]\n'
                '  location            = "eastus"\n'
                '  resource_group_name = "rg-dev"\n'
                '}\n'
            ),
            "/terraform/storage.tf": (
                'resource "azurerm_storage_account" "sa" {\n'
                '  name                     = "devstorage"\n'
                '  resource_group_name      = "rg-dev"\n'
                '  location                 = "eastus"\n'
                '  account_tier             = "Standard"\n'
                '  account_replication_type = "LRS"\n'
                '}\n'
            ),
        }

        result = correlate_aks_to_terraform(self.aks_evidence, self.diagnosis, tf_files)

        self.assertEqual(result.status, "missing")
        self.assertEqual(result.confidence, "missing")
        self.assertIsNone(result.repository_path)
        self.assertIsNone(result.matched_resource)
        self.assertIsNone(result.relevant_code)
        self.assertEqual(result.candidate_matches, [])
        self.assertTrue(any("No Terraform resources" in u for u in result.mapping_uncertainties))

    def test_ambiguous_matches(self):
        tf_files = {
            "/terraform/dev.tf": (
                'resource "kubernetes_deployment" "task_manager_dev" {\n'
                '  metadata {\n'
                '    name = "task-manager"\n'
                '  }\n'
                '  spec {\n'
                '    template {\n'
                '      spec {\n'
                '        container {\n'
                '          name  = "task-manager"\n'
                '          image = "myregistry.azurecr.io/task-manager:badtag"\n'
                '        }\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}\n'
            ),
            "/terraform/prod.tf": (
                'resource "kubernetes_deployment" "task_manager_prod" {\n'
                '  metadata {\n'
                '    name = "task-manager"\n'
                '  }\n'
                '  spec {\n'
                '    template {\n'
                '      spec {\n'
                '        container {\n'
                '          name  = "task-manager"\n'
                '          image = "myregistry.azurecr.io/task-manager:badtag"\n'
                '        }\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}\n'
            ),
        }

        result = correlate_aks_to_terraform(self.aks_evidence, self.diagnosis, tf_files)

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.confidence, "ambiguous")
        self.assertIsNone(result.repository_path)
        self.assertIsNone(result.matched_resource)
        self.assertIsNone(result.relevant_code)
        self.assertEqual(len(result.candidate_matches), 2)
        paths = {c.file_path for c in result.candidate_matches}
        self.assertEqual(paths, {"/terraform/dev.tf", "/terraform/prod.tf"})
        self.assertTrue(any("Multiple candidate" in u for u in result.mapping_uncertainties))

    def test_empty_terraform_files_returns_missing(self):
        result = correlate_aks_to_terraform(self.aks_evidence, self.diagnosis, {})
        self.assertEqual(result.status, "missing")
        self.assertEqual(result.confidence, "missing")
        self.assertIsNone(result.repository_path)
        self.assertTrue(any("No Terraform source files" in u for u in result.mapping_uncertainties))

    def test_incomplete_discovery_metadata_recorded_in_uncertainties(self):
        tf_files = {
            "/terraform/main.tf": (
                'resource "kubernetes_deployment" "task_manager" {\n'
                '  metadata {\n'
                '    name = "task-manager"\n'
                '  }\n'
                '  spec {\n'
                '    template {\n'
                '      spec {\n'
                '        container {\n'
                '          name  = "task-manager"\n'
                '          image = "myregistry.azurecr.io/task-manager:badtag"\n'
                '        }\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}\n'
            )
        }

        result = correlate_aks_to_terraform(
            self.aks_evidence,
            self.diagnosis,
            tf_files,
            discovery_metadata={"incomplete": True},
        )
        self.assertEqual(result.status, "matched")
        self.assertTrue(any("discovery was incomplete" in u for u in result.mapping_uncertainties))


class TestHandleAKSRemediationWorkflow(unittest.IsolatedAsyncioTestCase):
    """Test the orchestrated handle_aks_remediation workflow."""

    async def test_handle_aks_remediation_end_to_end(self):
        evidence = TroubleshootingEvidence(
            deployment={
                "metadata": {"name": "task-manager", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "task-manager",
                                    "image": "myregistry.azurecr.io/task-manager:badtag",
                                }
                            ]
                        }
                    }
                },
            },
            pod_health={
                "pods": [
                    {
                        "metadata": {"name": "task-manager-1", "namespace": "default"},
                        "containers": [
                            {
                                "name": "task-manager",
                                "image": "myregistry.azurecr.io/task-manager:badtag",
                                "ready": False,
                                "restart_count": 0,
                                "waiting_reason": "ImagePullBackOff",
                            }
                        ],
                    }
                ]
            },
        )
        diag = AKSDiagnosis(
            summary="ImagePullBackOff observed",
            observed_evidence=["Container waiting reason is ImagePullBackOff"],
            likely_root_causes=["Invalid image tag"],
            recommended_next_diagnostic_step="Check Terraform source",
        )

        tf_content = (
            'resource "kubernetes_deployment" "task_manager" {\n'
            '  metadata {\n'
            '    name = "task-manager"\n'
            '  }\n'
            '  spec {\n'
            '    template {\n'
            '      spec {\n'
            '        container {\n'
            '          name  = "task-manager"\n'
            '          image = "myregistry.azurecr.io/task-manager:badtag"\n'
            '        }\n'
            '      }\n'
            '    }\n'
            '  }\n'
            '}\n'
        )

        from src.review.terraform_evidence import TerraformEvidence
        from langchain_core.messages import ToolMessage, AIMessage

        fake_tf_evidence = TerraformEvidence()
        fake_tf_evidence.discovery.update(repository_id="repo-123", incomplete=False)
        fake_tf_evidence.extend([
            AIMessage(content="", tool_calls=[{"name": "repo_file", "id": "call-1", "args": {"path": "/terraform/main.tf", "action": "get_content"}}]),
            ToolMessage(content=[{"type": "text", "text": tf_content}], tool_call_id="call-1", name="repo_file", status="success"),
        ])

        services = RequestServices(
            collect_task_manager_evidence=AsyncMock(return_value=evidence),
            diagnose_aks=AsyncMock(return_value=diag),
            gather_terraform_evidence=AsyncMock(return_value=fake_tf_evidence),
        )

        res = await handle_aks_remediation(
            client=MagicMock(),
            tools=[],
            question="Find the Terraform file to remediate the Task Manager AKS failure.",
            services=services,
        )

        parsed = RemediationProposalResult.model_validate_json(res.answer)
        # Blocked because no commit_sha metadata or replacement content was specified
        self.assertEqual(parsed.status, "blocked")
        self.assertEqual(parsed.confidence, "exact")
        self.assertEqual(parsed.repository_path, "/terraform/main.tf")
        self.assertEqual(parsed.resource.resource_name, "task_manager")
        self.assertIn("myregistry.azurecr.io/task-manager:badtag", parsed.relevant_code)

    async def test_handle_aks_remediation_simulated_imagepullbackoff_proposal(self):
        evidence = TroubleshootingEvidence(
            deployment={
                "metadata": {"name": "task-manager", "namespace": "default"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "task-manager",
                                    "image": "myregistry.azurecr.io/task-manager:badtag",
                                }
                            ]
                        }
                    }
                },
            },
            pod_health={
                "pods": [
                    {
                        "metadata": {"name": "task-manager-pod", "namespace": "default"},
                        "containers": [
                            {
                                "name": "task-manager",
                                "image": "myregistry.azurecr.io/task-manager:badtag",
                                "ready": False,
                                "restart_count": 0,
                                "waiting_reason": "ImagePullBackOff",
                                "waiting_message": "Back-off pulling image myregistry.azurecr.io/task-manager:badtag",
                            }
                        ],
                    }
                ]
            },
        )
        diag = AKSDiagnosis(
            summary="Pod task-manager-pod is failing with ImagePullBackOff due to bad image tag :badtag.",
            observed_evidence=["Container waiting reason is ImagePullBackOff"],
            likely_root_causes=["Image tag :badtag does not exist in registry."],
            recommended_next_diagnostic_step="Update image tag in Terraform.",
        )

        tf_content = (
            'resource "kubernetes_deployment" "task_manager" {\n'
            '  metadata {\n'
            '    name = "task-manager"\n'
            '  }\n'
            '  spec {\n'
            '    template {\n'
            '      spec {\n'
            '        container {\n'
            '          name  = "task-manager"\n'
            '          image = "myregistry.azurecr.io/task-manager:badtag"\n'
            '        }\n'
            '      }\n'
            '    }\n'
            '  }\n'
            '}\n'
        )

        from src.review.terraform_evidence import TerraformEvidence
        from langchain_core.messages import ToolMessage, AIMessage

        fake_tf_evidence = TerraformEvidence()
        fake_tf_evidence.discovery.update(
            repository_id="repo-123",
            commit_sha="c" * 40,
            incomplete=False,
        )
        fake_tf_evidence.extend([
            AIMessage(content="", tool_calls=[{"name": "repo_file", "id": "call-1", "args": {"path": "/terraform/main.tf", "action": "get_content"}}]),
            ToolMessage(content=[{"type": "text", "text": tf_content}], tool_call_id="call-1", name="repo_file", status="success"),
        ])

        services = RequestServices(
            collect_task_manager_evidence=AsyncMock(return_value=evidence),
            diagnose_aks=AsyncMock(return_value=diag),
            gather_terraform_evidence=AsyncMock(return_value=fake_tf_evidence),
        )

        res = await handle_aks_remediation(
            client=MagicMock(),
            tools=[],
            question="Remediate Task Manager ImagePullBackOff by updating image to myregistry.azurecr.io/task-manager:v2.0.0 in Terraform.",
            services=services,
        )

        parsed = RemediationProposalResult.model_validate_json(res.answer)
        self.assertEqual(parsed.status, "proposed")
        self.assertEqual(parsed.confidence, "exact")
        self.assertEqual(parsed.repository_path, "/terraform/main.tf")
        self.assertIsNotNone(parsed.proposal)
        self.assertEqual(parsed.proposal.preconditions.expected_commit, "c" * 40)
        self.assertIn("myregistry.azurecr.io/task-manager:v2.0.0", parsed.proposed_replacement_content)
        self.assertNotIn("badtag", parsed.proposed_replacement_content)
        self.assertEqual(parsed.proposal.payload.replacement_content, parsed.proposed_replacement_content)
        self.assertEqual(parsed.workload_evidence.workload_name, "task-manager")
        self.assertIn("ImagePullBackOff", parsed.workload_evidence.failure_reasons)

    async def test_handle_aks_remediation_missing_metadata_blocks_proposal(self):
        evidence = TroubleshootingEvidence(
            deployment={"metadata": {"name": "task-manager", "namespace": "default"}},
            pod_health={"pods": [{"containers": [{"name": "c", "image": "img:bad", "ready": False, "waiting_reason": "ImagePullBackOff"}]}]},
        )
        diag = AKSDiagnosis(summary="err", observed_evidence=[], likely_root_causes=[], recommended_next_diagnostic_step="")
        tf_content = 'resource "kubernetes_deployment" "task_manager" { image = "img:bad" }\n'

        from src.review.terraform_evidence import TerraformEvidence
        from langchain_core.messages import ToolMessage, AIMessage

        fake_tf_evidence = TerraformEvidence()
        fake_tf_evidence.discovery.update(repository_id="repo-123", incomplete=False)
        # Note: commit_sha is intentionally omitted from discovery!
        fake_tf_evidence.extend([
            AIMessage(content="", tool_calls=[{"name": "repo_file", "id": "c1", "args": {"path": "/terraform/main.tf", "action": "get_content"}}]),
            ToolMessage(content=[{"type": "text", "text": tf_content}], tool_call_id="c1", name="repo_file", status="success"),
        ])

        services = RequestServices(
            collect_task_manager_evidence=AsyncMock(return_value=evidence),
            diagnose_aks=AsyncMock(return_value=diag),
            gather_terraform_evidence=AsyncMock(return_value=fake_tf_evidence),
        )

        res = await handle_aks_remediation(
            client=MagicMock(),
            tools=[],
            question="Remediate task-manager by updating image to img:v2.0 in Terraform.",
            services=services,
        )

        parsed = RemediationProposalResult.model_validate_json(res.answer)
        self.assertEqual(parsed.status, "blocked")
        self.assertIn("Reliable repository commit SHA metadata is unavailable", parsed.block_reason)
        self.assertIsNone(parsed.proposal)

    async def test_handle_aks_remediation_ambiguous_mapping_blocks_proposal(self):
        evidence = TroubleshootingEvidence(
            deployment={"metadata": {"name": "task-manager", "namespace": "default"}},
            pod_health={"pods": [{"containers": [{"name": "c", "image": "img:bad", "ready": False, "waiting_reason": "ImagePullBackOff"}]}]},
        )
        diag = AKSDiagnosis(summary="err", observed_evidence=[], likely_root_causes=[], recommended_next_diagnostic_step="")
        tf_dev = 'resource "kubernetes_deployment" "task_manager_dev" {\n  image = "img:bad"\n}\n'
        tf_prod = 'resource "kubernetes_deployment" "task_manager_prod" {\n  image = "img:bad"\n}\n'

        from src.review.terraform_evidence import TerraformEvidence
        from langchain_core.messages import ToolMessage, AIMessage

        fake_tf_evidence = TerraformEvidence()
        fake_tf_evidence.discovery.update(repository_id="repo-123", commit_sha="c" * 40, incomplete=False)
        fake_tf_evidence.extend([
            AIMessage(content="", tool_calls=[{"name": "repo_file", "id": "c1", "args": {"path": "/terraform/dev.tf", "action": "get_content"}}]),
            ToolMessage(content=[{"type": "text", "text": tf_dev}], tool_call_id="c1", name="repo_file", status="success"),
            AIMessage(content="", tool_calls=[{"name": "repo_file", "id": "c2", "args": {"path": "/terraform/prod.tf", "action": "get_content"}}]),
            ToolMessage(content=[{"type": "text", "text": tf_prod}], tool_call_id="c2", name="repo_file", status="success"),
        ])

        services = RequestServices(
            collect_task_manager_evidence=AsyncMock(return_value=evidence),
            diagnose_aks=AsyncMock(return_value=diag),
            gather_terraform_evidence=AsyncMock(return_value=fake_tf_evidence),
        )

        res = await handle_aks_remediation(
            client=MagicMock(),
            tools=[],
            question="Remediate task-manager by updating image to img:v2.0 in Terraform.",
            services=services,
        )

        parsed = RemediationProposalResult.model_validate_json(res.answer)
        self.assertEqual(parsed.status, "blocked")
        self.assertIn("Terraform mapping is ambiguous", parsed.block_reason)
        self.assertIsNone(parsed.proposal)
        self.assertEqual(len(parsed.candidate_matches), 2)

    async def test_handle_aks_remediation_unsupported_resource_blocks_proposal(self):
        evidence = TroubleshootingEvidence(
            deployment={"metadata": {"name": "task-manager", "namespace": "default"}},
            pod_health={"pods": [{"containers": [{"name": "c", "image": "img:bad", "ready": False, "waiting_reason": "ImagePullBackOff"}]}]},
        )
        diag = AKSDiagnosis(summary="err", observed_evidence=[], likely_root_causes=[], recommended_next_diagnostic_step="")
        # Only plain text string match, no HCL resource block (parsed as text_match)
        tf_content = '# Plain text comment mentioning img:bad\n'

        from src.review.terraform_evidence import TerraformEvidence
        from langchain_core.messages import ToolMessage, AIMessage

        fake_tf_evidence = TerraformEvidence()
        fake_tf_evidence.discovery.update(repository_id="repo-123", commit_sha="c" * 40, incomplete=False)
        fake_tf_evidence.extend([
            AIMessage(content="", tool_calls=[{"name": "repo_file", "id": "c1", "args": {"path": "/terraform/main.tf", "action": "get_content"}}]),
            ToolMessage(content=[{"type": "text", "text": tf_content}], tool_call_id="c1", name="repo_file", status="success"),
        ])

        services = RequestServices(
            collect_task_manager_evidence=AsyncMock(return_value=evidence),
            diagnose_aks=AsyncMock(return_value=diag),
            gather_terraform_evidence=AsyncMock(return_value=fake_tf_evidence),
        )

        res = await handle_aks_remediation(
            client=MagicMock(),
            tools=[],
            question="Remediate task-manager by updating image to img:v2.0 in Terraform.",
            services=services,
        )

        parsed = RemediationProposalResult.model_validate_json(res.answer)
        self.assertEqual(parsed.status, "blocked")
        self.assertIn("not supported for remediation proposals", parsed.block_reason)
        self.assertIsNone(parsed.proposal)

    async def test_orchestrate_request_routes_to_aks_remediation(self):
        question = "Investigate the Task Manager AKS failure and find the Terraform resource to fix it."
        mock_handler = AsyncMock()
        mock_handler.return_value = MagicMock(answer="remediation_result_json")

        fake_runtime = AsyncMock()
        fake_runtime.initialize = AsyncMock()
        fake_runtime.close = AsyncMock()
        fake_runtime.tools = []

        fake_client = AsyncMock()
        fake_client.close = AsyncMock()

        services = RequestServices(
            load_environment=MagicMock(),
            load_settings=MagicMock(return_value=MagicMock(
                azure_openai_api_key="k",
                azure_openai_endpoint="https://fake",
                request_deadline_seconds=30,
            )),
            runtime_factory=MagicMock(return_value=fake_runtime),
            client_factory=MagicMock(return_value=fake_client),
            handle_aks_remediation=mock_handler,
        )

        with patch("src.agent.orchestration.select_route") as mock_select_route:
            result = await orchestrate_request(question, services=services)
            mock_select_route.assert_called_with("aks_remediation")
            mock_handler.assert_called_once()
            self.assertEqual(result.answer, "remediation_result_json")
