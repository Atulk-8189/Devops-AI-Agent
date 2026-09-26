"""Automated tests for Stage 2: structured Terraform fix proposal generation."""

import hashlib
import json
import unittest

from src.agent.proposals import Proposal, action_hash
from src.agent.remediation_discovery import (
    MatchedTerraformResource,
    RemediationDiscoveryResult,
    WorkloadEvidenceSummary,
)
from src.agent.remediation_proposal import (
    ProposeTerraformFixInput,
    ProposeTerraformFixTool,
    RemediationProposalResult,
    generate_remediation_proposal,
)
from src.policy.write_policy import WritePolicy


class TestRemediationProposalGeneration(unittest.TestCase):
    """Test generating validated Proposals from AKS remediation discovery evidence."""

    def setUp(self):
        self.original_code = (
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
        self.replacement_code = self.original_code.replace(":badtag", ":v2.0.0")
        self.commit_sha = "a" * 40
        self.repo_id = "repo-task-manager"
        self.file_path = "/terraform/main.tf"
        self.original_files = {self.file_path: self.original_code}
        self.policy = WritePolicy(repository_branches={self.repo_id: ["main"]})

        self.matched_resource = MatchedTerraformResource(
            file_path=self.file_path,
            resource_type="kubernetes_deployment",
            resource_name="task_manager",
            start_line=1,
            end_line=17,
            match_reasons=[
                "Resource type 'kubernetes_deployment' matches workload 'task-manager'",
                "Resource defines matching container image 'myregistry.azurecr.io/task-manager:badtag'",
            ],
        )

        self.discovery_result = RemediationDiscoveryResult(
            status="matched",
            confidence="exact",
            aks_diagnosis={
                "Summary": "Container task-manager is in ImagePullBackOff.",
                "Observed evidence": ["Failed to pull image :badtag"],
                "Likely root causes": ["Image tag :badtag does not exist in registry."],
                "Recommended next diagnostic step": "Update tag in Terraform.",
            },
            workload_evidence=WorkloadEvidenceSummary(
                workload_name="task-manager",
                namespace="default",
                container_images=["myregistry.azurecr.io/task-manager:badtag"],
                container_names=["task-manager"],
                unhealthy_containers=["task-manager"],
                failure_reasons=["ImagePullBackOff"],
            ),
            repository_path=self.file_path,
            matched_resource=self.matched_resource,
            relevant_code="1: resource \"kubernetes_deployment\" \"task_manager\" {\n...",
            candidate_matches=[],
            mapping_uncertainties=[
                "Assumes repository 'main' branch reflects the intended desired state for the cluster."
            ],
        )

    def test_valid_proposal_generation(self):
        result = generate_remediation_proposal(
            discovery_result=self.discovery_result,
            proposed_replacement_content=self.replacement_code,
            commit_sha=self.commit_sha,
            repository_id=self.repo_id,
            branch="main",
            policy=self.policy,
            original_files=self.original_files,
        )

        self.assertEqual(result.status, "proposed")
        self.assertIsNone(result.block_reason)
        self.assertIsNotNone(result.proposal)
        self.assertIsNotNone(result.action)

        # Precondition verification
        expected_sha = hashlib.sha256(self.original_code.encode("utf-8")).hexdigest()
        self.assertEqual(result.proposal.preconditions.expected_commit, self.commit_sha)
        self.assertEqual(result.proposal.preconditions.original_content_sha256, expected_sha)
        self.assertEqual(result.action.preconditions.expected_commit, self.commit_sha)
        self.assertEqual(result.action.preconditions.original_content_sha256, expected_sha)

        # Action and Proposal constraints
        self.assertEqual(result.proposal.approval_required, True)
        self.assertEqual(result.proposal.approval_status, "pending")
        self.assertEqual(result.proposal.max_write_operations, 1)
        self.assertEqual(result.proposal.action_sha256, action_hash(result.action))
        self.assertEqual(result.proposal.target.path, self.file_path)
        self.assertEqual(result.proposal.target.project, "My Project")
        self.assertEqual(result.proposal.target.branch, "main")
        self.assertEqual(result.proposal.payload.replacement_content, self.replacement_code)

        # Audit and supporting evidence fields
        self.assertEqual(result.repository_path, self.file_path)
        self.assertEqual(result.resource, self.matched_resource)
        self.assertEqual(result.original_content, self.original_code)
        self.assertEqual(result.proposed_replacement_content, self.replacement_code)
        self.assertTrue(len(result.supporting_evidence) > 0)
        self.assertTrue(any("ImagePullBackOff" in e for e in result.supporting_evidence))
        self.assertTrue(any("kubernetes_deployment.task_manager" in e for e in result.supporting_evidence))
        self.assertEqual(result.mapping_uncertainties, self.discovery_result.mapping_uncertainties)

    def test_missing_commit_sha_blocks_proposal(self):
        for bad_sha in (None, "", "HEAD", "12345", "z" * 40, "a" * 39, "a" * 41):
            with self.subTest(bad_sha=bad_sha):
                result = generate_remediation_proposal(
                    discovery_result=self.discovery_result,
                    proposed_replacement_content=self.replacement_code,
                    commit_sha=bad_sha,
                    repository_id=self.repo_id,
                    branch="main",
                    policy=self.policy,
                    original_files=self.original_files,
                )
                self.assertEqual(result.status, "blocked")
                self.assertIsNone(result.proposal)
                self.assertIsNone(result.action)
                self.assertIn("Reliable repository commit SHA metadata is unavailable", result.block_reason)

    def test_missing_repository_id_blocks_proposal(self):
        for bad_id in (None, "", "   "):
            with self.subTest(bad_id=bad_id):
                result = generate_remediation_proposal(
                    discovery_result=self.discovery_result,
                    proposed_replacement_content=self.replacement_code,
                    commit_sha=self.commit_sha,
                    repository_id=bad_id,
                    branch="main",
                    policy=self.policy,
                    original_files=self.original_files,
                )
                self.assertEqual(result.status, "blocked")
                self.assertIsNone(result.proposal)
                self.assertIn("Repository ID metadata is unavailable", result.block_reason)

    def test_missing_original_content_blocks_proposal(self):
        result = generate_remediation_proposal(
            discovery_result=self.discovery_result,
            proposed_replacement_content=self.replacement_code,
            commit_sha=self.commit_sha,
            repository_id=self.repo_id,
            branch="main",
            policy=self.policy,
            original_files={},  # file not present
        )
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.proposal)
        self.assertIn("Original file content for '/terraform/main.tf' is unavailable", result.block_reason)

    def test_ambiguous_mapping_blocks_proposal(self):
        ambiguous_discovery = self.discovery_result.model_copy(update={
            "status": "ambiguous",
            "confidence": "ambiguous",
            "repository_path": None,
            "matched_resource": None,
            "candidate_matches": [
                self.matched_resource,
                self.matched_resource.model_copy(update={"file_path": "/terraform/dev.tf"}),
            ],
            "mapping_uncertainties": ["Multiple matching resources found"],
        })

        result = generate_remediation_proposal(
            discovery_result=ambiguous_discovery,
            proposed_replacement_content=self.replacement_code,
            commit_sha=self.commit_sha,
            repository_id=self.repo_id,
            branch="main",
            policy=self.policy,
            original_files=self.original_files,
        )

        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.proposal)
        self.assertIsNone(result.action)
        self.assertIn("Terraform mapping is ambiguous", result.block_reason)

    def test_missing_mapping_blocks_proposal(self):
        missing_discovery = self.discovery_result.model_copy(update={
            "status": "missing",
            "confidence": "missing",
            "repository_path": None,
            "matched_resource": None,
            "relevant_code": None,
        })

        result = generate_remediation_proposal(
            discovery_result=missing_discovery,
            proposed_replacement_content=self.replacement_code,
            commit_sha=self.commit_sha,
            repository_id=self.repo_id,
            branch="main",
            policy=self.policy,
            original_files=self.original_files,
        )

        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.proposal)
        self.assertIn("No confirmed Terraform resource mapping was found", result.block_reason)

    def test_unsupported_resource_type_blocks_proposal(self):
        unsupported_discovery = self.discovery_result.model_copy(update={
            "matched_resource": self.matched_resource.model_copy(update={"resource_type": "text_match"}),
        })

        result = generate_remediation_proposal(
            discovery_result=unsupported_discovery,
            proposed_replacement_content=self.replacement_code,
            commit_sha=self.commit_sha,
            repository_id=self.repo_id,
            branch="main",
            policy=self.policy,
            original_files=self.original_files,
        )

        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.proposal)
        self.assertIn("not supported for remediation proposals", result.block_reason)

    def test_invalid_replacements_blocked(self):
        # Case 1: Identical to original content
        identical_result = generate_remediation_proposal(
            discovery_result=self.discovery_result,
            proposed_replacement_content=self.original_code,
            commit_sha=self.commit_sha,
            repository_id=self.repo_id,
            branch="main",
            policy=self.policy,
            original_files=self.original_files,
        )
        self.assertEqual(identical_result.status, "blocked")
        self.assertIn("identical to the original content", identical_result.block_reason)

        # Case 2: Empty replacement
        for empty_val in ("", "   ", "\n\t"):
            with self.subTest(empty_val=repr(empty_val)):
                empty_result = generate_remediation_proposal(
                    discovery_result=self.discovery_result,
                    proposed_replacement_content=empty_val,
                    commit_sha=self.commit_sha,
                    repository_id=self.repo_id,
                    branch="main",
                    policy=self.policy,
                    original_files=self.original_files,
                )
                self.assertEqual(empty_result.status, "blocked")
                self.assertIn("empty or whitespace-only", empty_result.block_reason)

    def test_policy_violations_rejected(self):
        # Case 1: Branch not allowlisted
        disallowed_branch_policy = WritePolicy(repository_branches={self.repo_id: ["staging"]})
        branch_result = generate_remediation_proposal(
            discovery_result=self.discovery_result,
            proposed_replacement_content=self.replacement_code,
            commit_sha=self.commit_sha,
            repository_id=self.repo_id,
            branch="main",  # not in staging
            policy=disallowed_branch_policy,
            original_files=self.original_files,
        )
        self.assertEqual(branch_result.status, "rejected")
        self.assertIsNone(branch_result.proposal)
        self.assertIn("Write policy violation", branch_result.block_reason)

        # Case 2: Repository not allowlisted
        disallowed_repo_policy = WritePolicy(repository_branches={"other-repo": ["main"]})
        repo_result = generate_remediation_proposal(
            discovery_result=self.discovery_result,
            proposed_replacement_content=self.replacement_code,
            commit_sha=self.commit_sha,
            repository_id=self.repo_id,
            branch="main",
            policy=disallowed_repo_policy,
            original_files=self.original_files,
        )
        self.assertEqual(repo_result.status, "rejected")
        self.assertIsNone(repo_result.proposal)
        self.assertIn("Write policy violation", repo_result.block_reason)


class TestProposeTerraformFixTool(unittest.IsolatedAsyncioTestCase):
    """Test the ProposeTerraformFixTool agent tool wrapper."""

    def setUp(self):
        self.original_code = 'resource "kubernetes_deployment" "task_manager" { image = "badtag" }\n'
        self.replacement_code = 'resource "kubernetes_deployment" "task_manager" { image = "v2.0.0" }\n'
        self.commit_sha = "f" * 40
        self.repo_id = "repo-123"
        self.file_path = "/terraform/main.tf"
        self.policy = WritePolicy(repository_branches={self.repo_id: ["main"]})
        self.tool = ProposeTerraformFixTool(
            policy=self.policy,
            original_files={self.file_path: self.original_code},
        )

    async def test_tool_attributes(self):
        self.assertEqual(self.tool.name, "propose_terraform_fix")
        self.assertEqual(self.tool.args_schema, ProposeTerraformFixInput)

    async def test_tool_ainvoke_success(self):
        call = {
            "id": "call-1",
            "name": "propose_terraform_fix",
            "args": {
                "repository_path": self.file_path,
                "replacement_content": self.replacement_code,
                "commit_sha": self.commit_sha,
                "repository_id": self.repo_id,
                "branch": "main",
                "rationale": "Update image tag to v2.0.0",
                "expected_impact": "Resolve ImagePullBackOff",
                "risk_level": "medium",
            },
        }

        msg = await self.tool.ainvoke(call)
        self.assertEqual(msg.status, "success")
        self.assertEqual(msg.name, "propose_terraform_fix")
        payload = json.loads(msg.content)
        self.assertEqual(payload["status"], "proposed")
        self.assertIsNotNone(payload["proposal"])
        self.assertEqual(payload["proposal"]["preconditions"]["expected_commit"], self.commit_sha)

    async def test_tool_ainvoke_blocked_on_missing_sha(self):
        call = {
            "id": "call-2",
            "name": "propose_terraform_fix",
            "args": {
                "repository_path": self.file_path,
                "replacement_content": self.replacement_code,
                "commit_sha": "invalid-sha",
                "repository_id": self.repo_id,
                "branch": "main",
                "rationale": "Update image tag to v2.0.0",
            },
        }

        msg = await self.tool.ainvoke(call)
        self.assertEqual(msg.status, "error")
        payload = json.loads(msg.content)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("Reliable repository commit SHA metadata is unavailable", payload["block_reason"])
