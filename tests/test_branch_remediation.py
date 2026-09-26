"""Tests for Stage 4: Approval-gated, branch-isolated Terraform remediation."""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.agent.approval import ApprovalRecord, ApprovalSession, AuditLog, digest
from src.agent.branch_remediation import (
    ApprovalRequiredError,
    BranchRemediationResult,
    PROTECTED_BRANCHES,
    StaleProposalError,
    UnauthorizedBranchError,
    create_branch_isolated_proposal,
    execute_branch_isolated_remediation,
    format_proposal_for_review,
    generate_feature_branch_name,
    inspect_ado_write_capabilities,
    is_protected_branch,
    validate_target_branch,
    verify_proposal_preconditions,
)
from src.agent.mock_executor import MockExecutor, MockRepository, content_hash
from src.agent.proposals import (
    Action,
    Payload,
    Preconditions,
    Proposal,
    Target,
    build_proposal,
)
from src.agent.remediation_discovery import (
    MatchedTerraformResource,
    RemediationDiscoveryResult,
    WorkloadEvidenceSummary,
)
from src.agent.remediation_proposal import (
    RemediationProposalResult,
    generate_remediation_proposal,
)
from src.policy.write_policy import WritePolicy


class TestBranchRemediationSafety(unittest.TestCase):
    """Test branch isolation, validation rules, and protected branch enforcement."""

    def test_protected_branches_identified(self):
        for branch in ["main", "refs/heads/main", "master", "refs/heads/master", "release", "production", "prod"]:
            with self.subTest(branch=branch):
                self.assertTrue(is_protected_branch(branch))
                with self.assertRaises(UnauthorizedBranchError):
                    validate_target_branch(branch)

    def test_valid_remediation_branches_accepted(self):
        valid_branches = [
            "remediation/terraform-task-manager-12345678",
            "remediation/fix-image-tag",
            "remediation/k8s-pod-fix-abcd1234",
            "refs/heads/remediation/feature-1",
        ]
        for branch in valid_branches:
            with self.subTest(branch=branch):
                self.assertFalse(is_protected_branch(branch))
                # Should not raise
                validate_target_branch(branch)

    def test_invalid_remediation_branches_rejected(self):
        invalid_branches = [
            "feature/new-button",
            "bugfix/issue-1",
            "remediation",  # missing slash / name
            "remediation/invalid branch name with spaces",
            "remediation/;rm -rf",
        ]
        for branch in invalid_branches:
            with self.subTest(branch=branch):
                with self.assertRaises(UnauthorizedBranchError):
                    validate_target_branch(branch)

    def test_generate_feature_branch_name(self):
        branch = generate_feature_branch_name("task_manager", "abcdef12-3456-7890-abcd-ef1234567890")
        self.assertEqual(branch, "remediation/terraform-task-manager-abcdef12")
        validate_target_branch(branch)

        # Handles messy or empty names safely
        branch2 = generate_feature_branch_name("---!@#$---", "12345678-0000")
        self.assertTrue(branch2.startswith("remediation/terraform-"))
        validate_target_branch(branch2)


class TestBranchIsolationProposalCreation(unittest.TestCase):
    """Test creating branch-isolated proposals from base proposals."""

    def setUp(self):
        self.base_commit = "a" * 40
        self.original_content = 'image = "task-manager:v1.0.0"\n'
        self.replacement_content = 'image = "task-manager:v1.0.1"\n'
        self.orig_sha = hashlib.sha256(self.original_content.encode("utf-8")).hexdigest()

        self.base_target = Target(
            project="My Project",
            repository_id="repo-1",
            branch="main",
            path="/terraform/main.tf",
        )
        self.base_action = Action(
            target=self.base_target,
            operation="update_existing_file",
            payload=Payload(replacement_content=self.replacement_content),
            preconditions=Preconditions(
                expected_commit=self.base_commit,
                original_content_sha256=self.orig_sha,
            ),
        )
        self.base_policy = WritePolicy(repository_branches={"repo-1": ["main"]})
        self.base_proposal = build_proposal(
            self.base_action,
            requested_by="operator",
            reason="Fix container image",
            expected_impact="Update task-manager image tag",
            risk_level="medium",
            policy=self.base_policy,
        )

    def test_create_branch_isolated_proposal_derives_feature_branch(self):
        isolated_proposal, isolated_policy = create_branch_isolated_proposal(
            self.base_proposal,
            resource_name="task_manager",
        )

        self.assertNotEqual(isolated_proposal.proposal_id, self.base_proposal.proposal_id)
        self.assertTrue(isolated_proposal.target.branch.startswith("remediation/terraform-task-manager-"))
        self.assertNotEqual(isolated_proposal.target.branch, "main")
        self.assertEqual(isolated_proposal.target.path, "/terraform/main.tf")
        self.assertEqual(isolated_proposal.target.repository_id, "repo-1")
        self.assertEqual(isolated_proposal.payload.replacement_content, self.replacement_content)
        self.assertEqual(isolated_proposal.preconditions.expected_commit, self.base_commit)

        # Isolated policy must allow only the feature branch, NEVER main
        self.assertNotIn("main", isolated_policy.repository_branches.get("repo-1", []))
        self.assertIn(isolated_proposal.target.branch, isolated_policy.repository_branches["repo-1"])
        self.assertFalse(isolated_policy.writes_enabled)

    def test_create_branch_isolated_proposal_from_remediation_result(self):
        result = RemediationProposalResult(
            status="proposed",
            confidence="exact",
            repository_path="/terraform/main.tf",
            resource=MatchedTerraformResource(
                resource_type="kubernetes_deployment",
                resource_name="task_manager",
                file_path="/terraform/main.tf",
                start_line=1,
                end_line=10,
                match_reasons=["Matched container image"],
            ),
            matched_resource=MatchedTerraformResource(
                resource_type="kubernetes_deployment",
                resource_name="task_manager",
                file_path="/terraform/main.tf",
                start_line=1,
                end_line=10,
                match_reasons=["Matched container image"],
            ),
            original_content=self.original_content,
            proposed_replacement_content=self.replacement_content,
            proposal=self.base_proposal,
            action=self.base_action,
            rationale="Fix image",
            supporting_evidence=["Image tag mismatch"],
        )

        isolated_proposal, isolated_policy = create_branch_isolated_proposal(result)
        self.assertTrue(isolated_proposal.target.branch.startswith("remediation/terraform-task-manager-"))
        self.assertNotIn("main", isolated_policy.repository_branches["repo-1"])

    def test_blocked_remediation_result_cannot_be_isolated(self):
        blocked_result = RemediationProposalResult(
            status="blocked",
            rationale="Ambiguous match",
            block_reason="Ambiguous candidate matches",
        )
        with self.assertRaises(ValueError):
            create_branch_isolated_proposal(blocked_result)


class TestHumanReviewPresentation(unittest.TestCase):
    """Test proposal review formatting for human review prior to approval."""

    def test_format_proposal_for_review_contains_all_critical_elements(self):
        commit = "b" * 40
        original = 'image = "nginx:1.14"\nport = 80\n'
        replacement = 'image = "nginx:1.15"\nport = 80\n'
        orig_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

        target = Target(
            project="My Project",
            repository_id="web-repo",
            branch="remediation/terraform-web-app-12345678",
            path="/terraform/app.tf",
        )
        action = Action(
            target=target,
            operation="update_existing_file",
            payload=Payload(replacement_content=replacement),
            preconditions=Preconditions(expected_commit=commit, original_content_sha256=orig_sha),
        )
        policy = WritePolicy(repository_branches={"web-repo": [target.branch]})
        proposal = build_proposal(
            action,
            requested_by="operator",
            reason="Upgrade nginx container version to 1.15",
            expected_impact="Update container image attribute",
            risk_level="low",
            policy=policy,
        )

        review = format_proposal_for_review(
            proposal,
            original_content=original,
            base_branch="main",
            diagnosis={"primary_issue": "ImagePullBackOff for nginx:1.14"},
            evidence=["Pod in CrashLoopBackOff", "Deployment specifies nginx:1.14"],
            uncertainties=["Assumes nginx:1.15 exists in registry"],
        )

        self.assertIn("TERRAFORM REMEDIATION PROPOSAL - HUMAN REVIEW REQUIRED", review)
        self.assertIn(proposal.proposal_id, review)
        self.assertIn(proposal.action_sha256, review)
        self.assertIn("remediation/terraform-web-app-12345678", review)
        self.assertIn("main (READ-ONLY, PROTECTED)", review)
        self.assertIn("ISOLATED - 'main' branch is protected and cannot receive direct writes.", review)
        self.assertIn(commit, review)
        self.assertIn(orig_sha, review)
        self.assertIn("ImagePullBackOff for nginx:1.14", review)
        self.assertIn("Pod in CrashLoopBackOff", review)
        self.assertIn("Assumes nginx:1.15 exists in registry", review)
        self.assertIn("-image = \"nginx:1.14\"", review)
        self.assertIn("+image = \"nginx:1.15\"", review)
        self.assertIn(f"APPROVE {proposal.proposal_id} {proposal.action_sha256}", review)


class TestApprovalGatedExecution(unittest.TestCase):
    """Test approval states: required, denied, stale, unauthorized branches, and successful isolated writes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.audit_path = Path(self.temp_dir.name) / "audit.jsonl"
        self.audit = AuditLog(self.audit_path)
        self.addCleanup(self.audit.close)

        self.commit = "c" * 40
        self.original_content = 'resource "kubernetes_deployment" "tm" {\n  image = "tm:bad"\n}\n'
        self.replacement_content = 'resource "kubernetes_deployment" "tm" {\n  image = "tm:good"\n}\n'
        self.orig_sha = content_hash(self.original_content)

        self.repo_id = "repo-task-mgr"
        self.file_path = "/terraform/deploy.tf"
        self.feature_branch = "remediation/terraform-task-manager-1234abcd"

        # Isolated proposal targeting feature branch
        self.target = Target(
            project="My Project",
            repository_id=self.repo_id,
            branch=self.feature_branch,
            path=self.file_path,
        )
        self.action = Action(
            target=self.target,
            operation="update_existing_file",
            payload=Payload(replacement_content=self.replacement_content),
            preconditions=Preconditions(
                expected_commit=self.commit,
                original_content_sha256=self.orig_sha,
            ),
        )
        self.policy = WritePolicy(repository_branches={self.repo_id: [self.feature_branch]})
        self.proposal = build_proposal(
            self.action,
            requested_by="operator",
            reason="Fix Task Manager bad container image",
            expected_impact="Update container image tag to good",
            risk_level="medium",
            policy=self.policy,
        )

        self.session = ApprovalSession(self.proposal, self.policy, self.audit)

        # Mock repository with main branch seeded
        self.repository = MockRepository()
        self.base_target = Target(
            project="My Project",
            repository_id=self.repo_id,
            branch="main",
            path=self.file_path,
        )
        self.repository.seed(self.base_target, commit=self.commit, content=self.original_content)

    def approve_session(self):
        cmd = f"APPROVE {self.proposal.proposal_id} {self.proposal.action_sha256}"
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", return_value=cmd), patch("builtins.print"):
            self.session.request_from_terminal(self.proposal, self.policy)

    def deny_session(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", return_value="DENY"), patch("builtins.print"):
            self.session.request_from_terminal(self.proposal, self.policy)

    def test_approval_required_blocks_execution(self):
        """1. When approval is pending, execution must be blocked with approval_required."""
        self.assertEqual(self.session.record.approval_status, "pending")
        result = execute_branch_isolated_remediation(
            self.proposal,
            self.session,
            self.policy,
            repository=self.repository,
        )

        self.assertEqual(result.status, "approval_required")
        self.assertEqual(result.approval_status, "pending")
        self.assertFalse(result.preconditions_verified)
        self.assertTrue(result.main_protected)

        # Verify repository was NOT modified
        key_base = digest(self.base_target.model_dump())
        self.assertEqual(self.repository.files[key_base].content, self.original_content)
        key_feature = digest(self.target.model_dump())
        self.assertNotIn(key_feature, self.repository.files)

    def test_approval_denied_blocks_execution(self):
        """2. When approval is denied, execution must be blocked with approval_denied."""
        self.deny_session()
        self.assertEqual(self.session.record.approval_status, "denied")

        result = execute_branch_isolated_remediation(
            self.proposal,
            self.session,
            self.policy,
            repository=self.repository,
        )

        self.assertEqual(result.status, "approval_denied")
        self.assertEqual(result.approval_status, "denied")
        self.assertTrue(result.main_protected)

        # Verify repository was NOT modified
        key_base = digest(self.base_target.model_dump())
        self.assertEqual(self.repository.files[key_base].content, self.original_content)

    def test_stale_proposal_expired_timestamp_rejected(self):
        """3a. When proposal has expired, execution must reject with stale_proposal."""
        self.approve_session()
        # Mock time forward past expiration
        future_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        with patch("src.agent.branch_remediation.datetime") as mock_dt:
            mock_dt.now.return_value = future_time
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            result = execute_branch_isolated_remediation(
                self.proposal,
                self.session,
                self.policy,
                repository=self.repository,
            )

        self.assertEqual(result.status, "stale_proposal")
        self.assertIn("expired", result.error_message.lower())

    def test_stale_proposal_commit_drift_rejected(self):
        """3b. When repository commit SHA has drifted, execution must reject with stale_proposal."""
        self.approve_session()

        # Update repository commit on main
        new_commit = "d" * 40
        self.repository.seed(self.base_target, commit=new_commit, content=self.original_content)

        result = execute_branch_isolated_remediation(
            self.proposal,
            self.session,
            self.policy,
            repository=self.repository,
        )

        self.assertEqual(result.status, "stale_proposal")
        self.assertFalse(result.preconditions_verified)
        self.assertIn("commit drifted", result.error_message.lower())

    def test_stale_proposal_content_drift_rejected(self):
        """3c. When original file content has changed, execution must reject with stale_proposal."""
        self.approve_session()

        # Update repository content on main
        modified_content = 'resource "kubernetes_deployment" "tm" {\n  image = "tm:modified"\n}\n'
        self.repository.seed(self.base_target, commit=self.commit, content=modified_content)

        result = execute_branch_isolated_remediation(
            self.proposal,
            self.session,
            self.policy,
            repository=self.repository,
        )

        self.assertEqual(result.status, "stale_proposal")
        self.assertFalse(result.preconditions_verified)
        self.assertIn("content drifted", result.error_message.lower())

    def test_unauthorized_branch_targeting_main_rejected(self):
        """4a. Directly targeting 'main' branch for write must be strictly rejected."""
        # Create a proposal explicitly targeting main
        main_target = Target(
            project="My Project",
            repository_id=self.repo_id,
            branch="main",
            path=self.file_path,
        )
        main_action = Action(
            target=main_target,
            operation="update_existing_file",
            payload=Payload(replacement_content=self.replacement_content),
            preconditions=Preconditions(expected_commit=self.commit, original_content_sha256=self.orig_sha),
        )
        main_policy = WritePolicy(repository_branches={self.repo_id: ["main"]})
        main_proposal = build_proposal(
            main_action,
            requested_by="operator",
            reason="Fix",
            expected_impact="Update",
            risk_level="high",
            policy=main_policy,
        )
        session = ApprovalSession(main_proposal, main_policy, self.audit)

        result = execute_branch_isolated_remediation(
            main_proposal,
            session,
            main_policy,
            repository=self.repository,
        )

        self.assertEqual(result.status, "unauthorized_branch")
        self.assertTrue(result.main_protected)
        self.assertIn("protected branch", result.error_message.lower())

    def test_unauthorized_branch_naming_format_rejected(self):
        """4b. Non-remediation branch name format is rejected."""
        custom_target = Target(
            project="My Project",
            repository_id=self.repo_id,
            branch="feature/custom-branch",
            path=self.file_path,
        )
        custom_action = Action(
            target=custom_target,
            operation="update_existing_file",
            payload=Payload(replacement_content=self.replacement_content),
            preconditions=Preconditions(expected_commit=self.commit, original_content_sha256=self.orig_sha),
        )
        custom_policy = WritePolicy(repository_branches={self.repo_id: ["feature/custom-branch"]})
        custom_proposal = build_proposal(
            custom_action,
            requested_by="operator",
            reason="Fix",
            expected_impact="Update",
            risk_level="high",
            policy=custom_policy,
        )
        session = ApprovalSession(custom_proposal, custom_policy, self.audit)

        result = execute_branch_isolated_remediation(
            custom_proposal,
            session,
            custom_policy,
            repository=self.repository,
        )

        self.assertEqual(result.status, "unauthorized_branch")
        self.assertIn("not an authorized feature branch", result.error_message)

    def test_successful_isolated_change_using_mock_repository(self):
        """5. Successful isolated change:
        - feature branch created in mock repository from main
        - proposal applied ONLY to feature branch
        - main branch verified untouched
        - approval consumed (single-use)
        - auto-merge and auto-apply verified prevented
        """
        self.approve_session()
        self.assertEqual(self.session.record.approval_status, "approved")

        result = execute_branch_isolated_remediation(
            self.proposal,
            self.session,
            self.policy,
            repository=self.repository,
            base_branch="main",
        )

        self.assertEqual(result.status, "approved_and_applied")
        self.assertTrue(result.preconditions_verified)
        self.assertTrue(result.main_protected)
        self.assertTrue(result.auto_merge_prevented)
        self.assertTrue(result.auto_apply_prevented)
        self.assertEqual(result.target_branch, self.feature_branch)

        # Invariant 1: main branch content is UNTOUCHED
        key_base = digest(self.base_target.model_dump())
        self.assertEqual(self.repository.files[key_base].content, self.original_content)
        self.assertEqual(self.repository.files[key_base].commit, self.commit)

        # Invariant 2: feature branch HAS the replacement content
        key_feature = digest(self.target.model_dump())
        self.assertIn(key_feature, self.repository.files)
        self.assertEqual(self.repository.files[key_feature].content, self.replacement_content)
        self.assertNotEqual(self.repository.files[key_feature].commit, self.commit)

        # Invariant 3: Approval session is consumed (single-use)
        self.assertEqual(self.session.record.approval_status, "consumed")
        self.assertEqual(self.session.record.use_count, 1)

        # Replay attempt must fail
        replay_result = execute_branch_isolated_remediation(
            self.proposal,
            self.session,
            self.policy,
            repository=self.repository,
        )
        self.assertEqual(replay_result.status, "failed")
        self.assertIn("already been consumed", replay_result.error_message)

    def test_missing_tools_gap_reporting_when_live_writes_requested(self):
        """6. When live write tools are requested, reports exact tool & policy gaps without writes."""
        self.approve_session()

        live_tools_mock = {
            "pipelines_definition:list": MagicMock(),
            "repo_repository:list": MagicMock(),
            "repo_file:get_content": MagicMock(),
        }

        result = execute_branch_isolated_remediation(
            self.proposal,
            self.session,
            self.policy,
            live_tools=live_tools_mock,
        )

        self.assertEqual(result.status, "tools_unavailable")
        self.assertTrue(result.main_protected)
        self.assertTrue(result.auto_merge_prevented)
        self.assertTrue(result.auto_apply_prevented)
        self.assertGreater(len(result.gaps_reported), 0)

        # Check gap descriptions
        gaps_text = "\n".join(result.gaps_reported)
        self.assertIn("create_branch", gaps_text)
        self.assertIn("READ_ONLY_POLICY", gaps_text)
        self.assertIn("WritePolicy.writes_enabled", gaps_text)


class TestFullEndToEndRemediationPipeline(unittest.TestCase):
    """Test full integration from AKS correlation to branch-isolated remediation."""

    def test_end_to_end_aks_discovery_to_branch_isolated_execution(self):
        temp_dir = tempfile.TemporaryDirectory()
        audit_path = Path(temp_dir.name) / "audit.jsonl"
        audit = AuditLog(audit_path)

        commit = "e" * 40
        orig_content = (
            'resource "kubernetes_deployment" "task_manager" {\n'
            '  metadata {\n'
            '    name = "task-manager"\n'
            '  }\n'
            '  spec {\n'
            '    template {\n'
            '      spec {\n'
            '        container {\n'
            '          name  = "task-manager"\n'
            '          image = "myacr.azurecr.io/task-manager:bad-tag"\n'
            '        }\n'
            '      }\n'
            '    }\n'
            '  }\n'
            '}\n'
        )
        replacement_content = orig_content.replace(":bad-tag", ":v1.0.0")

        # 1. Simulate discovery result from Stage 1
        discovery_result = RemediationDiscoveryResult(
            status="matched",
            confidence="exact",
            workload_evidence=WorkloadEvidenceSummary(
                workload_name="task-manager",
                namespace="default",
                container_names=["task-manager"],
                container_images=["myacr.azurecr.io/task-manager:bad-tag"],
                unhealthy_containers=["task-manager"],
                failure_reasons=["Failed to pull image myacr.azurecr.io/task-manager:bad-tag"],
            ),
            aks_diagnosis={"primary_issue": "ImagePullBackOff"},
            repository_path="/terraform/task_manager.tf",
            matched_resource=MatchedTerraformResource(
                resource_type="kubernetes_deployment",
                resource_name="task_manager",
                file_path="/terraform/task_manager.tf",
                start_line=1,
                end_line=17,
                match_reasons=["Image match"],
            ),
            relevant_code="1 | ...",
        )

        # 2. Generate Proposal (Stage 2/3)
        proposal_result = generate_remediation_proposal(
            discovery_result=discovery_result,
            proposed_replacement_content=replacement_content,
            commit_sha=commit,
            repository_id="repo-aks-tf",
            reason="Fix image tag mismatch in task_manager deployment",
            original_files={"/terraform/task_manager.tf": orig_content},
        )
        self.assertEqual(proposal_result.status, "proposed")
        self.assertIsNotNone(proposal_result.proposal)

        # 3. Create Branch-Isolated Proposal (Stage 4)
        isolated_proposal, isolated_policy = create_branch_isolated_proposal(
            proposal_result,
            resource_name="task_manager",
        )
        self.assertTrue(isolated_proposal.target.branch.startswith("remediation/terraform-task-manager-"))
        self.assertNotIn("main", isolated_policy.repository_branches["repo-aks-tf"])

        # 4. Format for human review
        review_doc = format_proposal_for_review(
            isolated_proposal,
            original_content=orig_content,
            base_branch="main",
            diagnosis=discovery_result.aks_diagnosis,
            evidence=discovery_result.workload_evidence.failure_reasons,
        )
        self.assertIn("TERRAFORM REMEDIATION PROPOSAL - HUMAN REVIEW REQUIRED", review_doc)
        self.assertIn("myacr.azurecr.io/task-manager:bad-tag", review_doc)
        self.assertIn("myacr.azurecr.io/task-manager:v1.0.0", review_doc)

        # 5. Initialize approval session and grant approval
        session = ApprovalSession(isolated_proposal, isolated_policy, audit)
        cmd = f"APPROVE {isolated_proposal.proposal_id} {isolated_proposal.action_sha256}"
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", return_value=cmd), patch("builtins.print"):
            session.request_from_terminal(isolated_proposal, isolated_policy)
        self.assertEqual(session.record.approval_status, "approved")

        # 6. Seed mock repository on main
        repo = MockRepository()
        base_target = Target(
            project="My Project",
            repository_id="repo-aks-tf",
            branch="main",
            path="/terraform/task_manager.tf",
        )
        repo.seed(base_target, commit=commit, content=orig_content)

        # 7. Execute branch-isolated remediation
        remediation_result = execute_branch_isolated_remediation(
            isolated_proposal,
            session,
            isolated_policy,
            repository=repo,
            base_branch="main",
        )

        self.assertEqual(remediation_result.status, "approved_and_applied")
        self.assertTrue(remediation_result.preconditions_verified)
        self.assertTrue(remediation_result.main_protected)
        self.assertTrue(remediation_result.auto_merge_prevented)
        self.assertTrue(remediation_result.auto_apply_prevented)

        # Verify main remains intact
        key_main = digest(base_target.model_dump())
        self.assertEqual(repo.files[key_main].content, orig_content)

        # Verify feature branch has the fix
        key_feature = digest(isolated_proposal.target.model_dump())
        self.assertEqual(repo.files[key_feature].content, replacement_content)

        # Cleanup
        audit.close()
        temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
