"""Stage 4: Approval-gated, branch-isolated Terraform remediation.

Applies validated Terraform proposals strictly to dedicated feature branches
following operator approval, while keeping main protected and verifying
preconditions (commit SHA and original content hash) immediately prior to write.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.agent.approval import ApprovalRecord, ApprovalSession, AuditLog, digest
from src.agent.mock_executor import MockExecutor, MockRepository, content_hash
from src.agent.proposals import (
    Action,
    Payload,
    Preconditions,
    Proposal,
    Target,
    build_proposal,
)
from src.policy.policy import READ_ONLY_POLICY
from src.policy.write_policy import WritePolicy

# Protected branches that must NEVER receive direct automated writes
PROTECTED_BRANCHES = frozenset({
    "main",
    "master",
    "release",
    "production",
    "prod",
    "staging",
    "develop",
    "dev",
})

FEATURE_BRANCH_PREFIX = "remediation/"
SAFE_BRANCH_PATTERN = re.compile(r"^remediation/[a-z0-9][a-z0-9._/-]*$", re.IGNORECASE)


class UnauthorizedBranchError(PermissionError):
    """Raised when an operation targets a protected or unauthorized branch."""


class StaleProposalError(ValueError):
    """Raised when proposal preconditions or timestamps fail freshness checks."""


class ApprovalRequiredError(PermissionError):
    """Raised when write execution is attempted without required human approval."""


class BranchRemediationResult(BaseModel):
    """Result of Stage 4 branch-isolated remediation execution."""

    status: Literal[
        "approved_and_applied",
        "approval_required",
        "approval_denied",
        "stale_proposal",
        "unauthorized_branch",
        "tools_unavailable",
        "failed",
    ] = Field(description="Outcome status of the branch-isolated remediation.")
    proposal_id: str = Field(description="Unique proposal identifier.")
    action_sha256: str = Field(description="Cryptographic hash of the canonical action.")
    target_branch: str = Field(description="Target branch for the change.")
    base_branch: str = Field(default="main", description="Source base branch (read-only reference).")
    repository_id: str = Field(description="Azure Repos repository identifier.")
    repository_path: str = Field(description="Target repository file path.")
    approval_status: str = Field(description="Status of operator approval session.")
    preconditions_verified: bool = Field(description="Whether repository commit and content hashes matched.")
    main_protected: bool = Field(default=True, description="Safety invariant: main branch was protected from direct writes.")
    auto_merge_prevented: bool = Field(default=True, description="Safety invariant: automatic merge was not executed.")
    auto_apply_prevented: bool = Field(default=True, description="Safety invariant: terraform apply was not executed.")
    gaps_reported: list[str] = Field(default_factory=list, description="Missing tools or policy constraints reported.")
    execution_summary: dict[str, Any] | None = Field(default=None, description="Details of execution dispatch if executed.")
    error_message: str | None = Field(default=None, description="Detailed explanation if execution failed or was blocked.")

    model_config = ConfigDict(extra="forbid")


def is_protected_branch(branch: str) -> bool:
    """Return True if the given branch name is a protected branch (e.g. main, master)."""
    normalized = branch.strip().removeprefix("refs/heads/").lower()
    return normalized in PROTECTED_BRANCHES


def validate_target_branch(branch: str) -> None:
    """Validate that the target branch is an isolated remediation feature branch and not protected.

    Raises UnauthorizedBranchError if the branch is invalid or protected.
    """
    normalized = branch.strip().removeprefix("refs/heads/")
    if is_protected_branch(normalized):
        raise UnauthorizedBranchError(
            f"Target branch '{branch}' is protected. Direct writes to protected branches ('main', 'master') are forbidden."
        )
    if not normalized.startswith(FEATURE_BRANCH_PREFIX):
        raise UnauthorizedBranchError(
            f"Target branch '{branch}' is not authorized. Remediation writes must target a dedicated "
            f"feature branch starting with '{FEATURE_BRANCH_PREFIX}'."
        )
    if not SAFE_BRANCH_PATTERN.match(normalized):
        raise UnauthorizedBranchError(
            f"Target branch '{branch}' contains invalid characters. Must conform to '{SAFE_BRANCH_PATTERN.pattern}'."
        )


def generate_feature_branch_name(resource_name: str, proposal_id: str) -> str:
    """Generate a deterministic, safe feature branch name for an isolated remediation proposal."""
    clean_name = re.sub(r"[^a-z0-9]+", "-", resource_name.lower()).strip("-")
    if not clean_name:
        clean_name = "workload"
    short_id = proposal_id.replace("-", "")[:8]
    return f"{FEATURE_BRANCH_PREFIX}terraform-{clean_name}-{short_id}"


def create_branch_isolated_proposal(
    base_proposal: Any,
    *,
    feature_branch: str | None = None,
    resource_name: str | None = None,
    requested_by: str = "operator",
) -> tuple[Proposal, WritePolicy]:
    """Derive a branch-isolated Proposal and matching WritePolicy from a base proposal.

    The base proposal (typically discovered against 'main') is rewritten to target
    a dedicated feature branch. 'main' is excluded from the WritePolicy's repository_branches,
    guaranteeing branch isolation.
    """
    proposal_obj: Proposal
    res_name = resource_name

    # Handle RemediationProposalResult wrapper from Stage 2/3
    if hasattr(base_proposal, "proposal"):
        if getattr(base_proposal, "status", None) != "proposed" or base_proposal.proposal is None:
            block_reason = getattr(base_proposal, "block_reason", "Proposal is not in proposed state")
            raise ValueError(f"Cannot isolate non-proposed result: {block_reason}")
        proposal_obj = base_proposal.proposal
        if not res_name:
            matched = getattr(base_proposal, "matched_resource", None) or getattr(base_proposal, "resource", None)
            if matched and hasattr(matched, "resource_name"):
                res_name = matched.resource_name
    elif isinstance(base_proposal, Proposal):
        proposal_obj = base_proposal
    else:
        raise TypeError(f"Expected Proposal or RemediationProposalResult, got {type(base_proposal)}")

    if feature_branch is None:
        feature_branch = generate_feature_branch_name(res_name or "remediation", proposal_obj.proposal_id)

    validate_target_branch(feature_branch)

    # Build isolated target pointing to feature branch
    isolated_target = Target(
        project=proposal_obj.target.project,
        repository_id=proposal_obj.target.repository_id,
        branch=feature_branch,
        path=proposal_obj.target.path,
    )

    # Build action with identical payload and preconditions
    isolated_action = Action(
        target=isolated_target,
        operation="update_existing_file",
        payload=proposal_obj.payload,
        preconditions=proposal_obj.preconditions,
    )

    # WritePolicy allows only the dedicated feature branch; 'main' is never allowlisted
    isolated_policy = WritePolicy(
        repository_branches={proposal_obj.target.repository_id: [feature_branch]}
    )

    isolated_proposal = build_proposal(
        isolated_action,
        requested_by=requested_by,
        reason=proposal_obj.reason,
        expected_impact=f"[Branch-Isolated] {proposal_obj.expected_impact}",
        risk_level=proposal_obj.risk_level,
        policy=isolated_policy,
    )

    return isolated_proposal, isolated_policy


def format_proposal_for_review(
    proposal: Proposal,
    *,
    original_content: str | None = None,
    base_branch: str = "main",
    diagnosis: dict[str, Any] | None = None,
    evidence: list[str] | None = None,
    uncertainties: list[str] | None = None,
) -> str:
    """Render a comprehensive, human-readable review representation of the remediation proposal.

    Displays file targets, branch isolation safeguards, preconditions, diagnostic evidence,
    and a unified diff before any approval or write action.
    """
    lines = [
        "================================================================================",
        "             TERRAFORM REMEDIATION PROPOSAL - HUMAN REVIEW REQUIRED             ",
        "================================================================================",
        f"Proposal ID:      {proposal.proposal_id}",
        f"Action SHA-256:   {proposal.action_sha256}",
        f"Risk Level:       {proposal.risk_level.upper()}",
        f"Expires At:       {proposal.expires_at.isoformat()}",
        "",
        "--- TARGET REPOSITORY & BRANCH ISOLATION ---",
        f"Project:          {proposal.target.project}",
        f"Repository ID:    {proposal.target.repository_id}",
        f"File Path:        {proposal.target.path}",
        f"Base Branch:      {base_branch} (READ-ONLY, PROTECTED)",
        f"Target Branch:    {proposal.target.branch} (DEDICATED FEATURE BRANCH)",
        "Branch Policy:    ISOLATED - 'main' branch is protected and cannot receive direct writes.",
        "Auto-Merge:       DISABLED - Changes require manual Pull Request review after push.",
        "Auto-Apply:       DISABLED - Terraform apply must be executed separately by operator.",
        "",
        "--- PRECONDITIONS (VERIFIED BEFORE WRITE) ---",
        f"Expected Base Commit: {proposal.preconditions.expected_commit}",
        f"Original Content SHA: {proposal.preconditions.original_content_sha256}",
        "",
        "--- RATIONALE & EVIDENCE ---",
        f"Rationale: {proposal.reason}",
        f"Expected Impact: {proposal.expected_impact}",
    ]

    if diagnosis:
        primary_issue = diagnosis.get("primary_issue") or diagnosis.get("summary")
        if primary_issue:
            lines.append(f"AKS Diagnostic Issue: {primary_issue}")

    if evidence:
        lines.append("Observed Supporting Evidence:")
        for item in evidence:
            lines.append(f"  * {item}")

    if uncertainties:
        lines.append("Mapping Uncertainties / Assumptions:")
        for item in uncertainties:
            lines.append(f"  * {item}")

    lines.append("")
    lines.append("--- PROPOSED CHANGES ---")
    if original_content is not None:
        diff_lines = list(
            difflib.unified_diff(
                original_content.splitlines(keepends=True),
                proposal.payload.replacement_content.splitlines(keepends=True),
                fromfile=f"a{proposal.target.path} ({base_branch})",
                tofile=f"b{proposal.target.path} ({proposal.target.branch})",
                n=3,
            )
        )
        if diff_lines:
            lines.extend("".join(diff_lines).splitlines())
        else:
            lines.append("[No diff: proposed replacement content matches original content]")
    else:
        lines.append("Proposed Replacement Content:")
        for idx, line in enumerate(proposal.payload.replacement_content.splitlines(), start=1):
            lines.append(f"{idx:4d} | {line}")

    lines.extend([
        "",
        "================================================================================",
        "OPERATOR CONFIRMATION INSTRUCTIONS:",
        f"To record explicit approval in terminal, enter:",
        f"  APPROVE {proposal.proposal_id} {proposal.action_sha256}",
        "================================================================================",
    ])

    return "\n".join(lines)


def inspect_ado_write_capabilities(live_tools: dict[str, Any] | None = None) -> list[str]:
    """Inspect the live environment and policies to report exact missing write capabilities."""
    gaps: list[str] = []

    # 1. Inspect live MCP runtime tools
    available_tools = set(live_tools.keys()) if live_tools else set()
    write_tools = {"create_branch", "repo_file.edit", "git_push", "create_pull_request"}
    missing_tools = write_tools - available_tools
    if missing_tools:
        gaps.append(
            f"Missing Azure DevOps MCP write tools: {', '.join(sorted(missing_tools))} are not provided by the MCP server."
        )

    # 2. Check read-only security policy
    ado_policy = READ_ONLY_POLICY.get("repo_file", set())
    if "edit" not in ado_policy and "create" not in ado_policy:
        gaps.append(
            "Read-only security policy restriction: READ_ONLY_POLICY permits only read-only tools "
            f"('pipelines_definition:list', 'repo_repository:list', 'repo_file:list_directory, get_content'). "
            "Write operations are strictly blocked by policy."
        )

    # 3. Check WritePolicy disabled state
    gaps.append(
        "WritePolicy enforcement: WritePolicy.writes_enabled is hardcoded to False. "
        "Live write dispatch is disabled in this environment."
    )

    # 4. Merging and Apply policies
    gaps.append(
        "Workflow restriction: Automatic branch merging and 'terraform apply' execution are prohibited."
    )

    return gaps


def verify_proposal_preconditions(
    proposal: Proposal,
    *,
    current_commit: str | None = None,
    current_content: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Verify that proposal preconditions and freshness hold immediately prior to write.

    Checks:
    1. Proposal expiration timestamp.
    2. Target branch is not protected.
    3. Current repository commit matches expected_commit.
    4. Current file content SHA-256 matches original_content_sha256.
    """
    check_time = now or datetime.now(timezone.utc)
    if check_time >= proposal.expires_at:
        return False, f"Proposal has expired (expiry: {proposal.expires_at.isoformat()})."

    if is_protected_branch(proposal.target.branch):
        return False, f"Target branch '{proposal.target.branch}' is a protected branch."

    if current_commit is not None and current_commit != proposal.preconditions.expected_commit:
        return (
            False,
            f"Repository commit SHA drifted: expected {proposal.preconditions.expected_commit}, found {current_commit}.",
        )

    if current_content is not None:
        current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
        if current_hash != proposal.preconditions.original_content_sha256:
            return (
                False,
                f"Original file content SHA-256 drifted: expected {proposal.preconditions.original_content_sha256}, "
                f"found {current_hash}.",
            )

    return True, None


def execute_branch_isolated_remediation(
    proposal: Proposal,
    session: ApprovalSession | None,
    policy: WritePolicy,
    *,
    repository: MockRepository | None = None,
    executor: MockExecutor | None = None,
    live_tools: dict[str, Any] | None = None,
    current_commit: str | None = None,
    current_content: str | None = None,
    base_branch: str = "main",
) -> BranchRemediationResult:
    """Execute approval-gated, branch-isolated remediation.

    Safety workflow:
    1. Verifies target branch is not 'main' or any protected branch.
    2. Verifies proposal has not expired.
    3. Checks ApprovalSession: requires explicit operator approval ('approved').
    4. Verifies preconditions immediately prior to write (commit SHA and original content hash).
    5. If live tools are specified, reports exact gaps and refuses live writes without bypassing policy.
    6. If a mock executor is provided, seeds the isolated feature branch from base 'main' state,
       applies the validated proposal only to the feature branch, confirms 'main' remains unchanged,
       and consumes the single-use approval.
    """
    target = proposal.target

    # 1. Target Branch Protection Check (Keep main protected)
    if is_protected_branch(target.branch):
        return BranchRemediationResult(
            status="unauthorized_branch",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=session.record.approval_status if session else "none",
            preconditions_verified=False,
            main_protected=True,
            error_message=f"Target branch '{target.branch}' is a protected branch. Direct writes to 'main' are strictly forbidden.",
        )

    if not target.branch.startswith(FEATURE_BRANCH_PREFIX) or not SAFE_BRANCH_PATTERN.match(target.branch):
        return BranchRemediationResult(
            status="unauthorized_branch",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=session.record.approval_status if session else "none",
            preconditions_verified=False,
            main_protected=True,
            error_message=f"Target branch '{target.branch}' is not an authorized feature branch. Must start with '{FEATURE_BRANCH_PREFIX}'.",
        )

    # 2. Expiration Check
    now = datetime.now(timezone.utc)
    if now >= proposal.expires_at:
        return BranchRemediationResult(
            status="stale_proposal",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=session.record.approval_status if session else "none",
            preconditions_verified=False,
            main_protected=True,
            error_message="Remediation proposal has expired.",
        )

    # 3. Approval Gating Check
    if session is None:
        return BranchRemediationResult(
            status="approval_required",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status="none",
            preconditions_verified=False,
            main_protected=True,
            error_message="Approval session not initialized. Explicit operator approval is required.",
        )

    approval_status = session.record.approval_status
    if approval_status == "pending":
        return BranchRemediationResult(
            status="approval_required",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status="pending",
            preconditions_verified=False,
            main_protected=True,
            error_message="Human approval is pending. Explicit operator confirmation is required before any write operation.",
        )

    if approval_status == "denied":
        return BranchRemediationResult(
            status="approval_denied",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status="denied",
            preconditions_verified=False,
            main_protected=True,
            error_message="Proposal approval was denied by operator.",
        )

    if approval_status in {"expired", "invalidated"}:
        return BranchRemediationResult(
            status="stale_proposal",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=approval_status,
            preconditions_verified=False,
            main_protected=True,
            error_message=f"Approval session is {approval_status}.",
        )

    if approval_status == "consumed" or session.record.use_count > 0:
        return BranchRemediationResult(
            status="failed",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status="consumed",
            preconditions_verified=False,
            main_protected=True,
            error_message="Approval token has already been consumed. Replay is rejected.",
        )

    if approval_status != "approved":
        return BranchRemediationResult(
            status="approval_required",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=approval_status,
            preconditions_verified=False,
            main_protected=True,
            error_message=f"Approval status is '{approval_status}'. Explicit approval is required.",
        )

    # 4. Immediate Precondition Verification (Commit SHA & Original Content Hash)
    valid_precond, fail_reason = verify_proposal_preconditions(
        proposal,
        current_commit=current_commit,
        current_content=current_content,
        now=now,
    )
    if not valid_precond:
        return BranchRemediationResult(
            status="stale_proposal",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=approval_status,
            preconditions_verified=False,
            main_protected=True,
            error_message=fail_reason,
        )

    # 5. Live Tool Gap Reporting (If no mock executor is provided)
    if executor is None and repository is None:
        gaps = inspect_ado_write_capabilities(live_tools)
        return BranchRemediationResult(
            status="tools_unavailable",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=approval_status,
            preconditions_verified=True,
            main_protected=True,
            auto_merge_prevented=True,
            auto_apply_prevented=True,
            gaps_reported=gaps,
            error_message="Live Azure DevOps write tools and write permissions are unavailable. "
                          "Operations are limited to safe proposal generation and mock testing.",
        )

    # 6. Mock Execution Path
    if executor is None and repository is not None:
        executor = MockExecutor(repository, session._audit)
    assert executor is not None

    repo = executor.repository
    base_target = Target(
        project=target.project,
        repository_id=target.repository_id,
        branch=base_branch,
        path=target.path,
    )
    key_base = digest(base_target.model_dump())
    key_feature = digest(target.model_dump())

    with repo._lock:
        base_file = repo.files.get(key_base)
        feature_file = repo.files.get(key_feature)

        # If feature branch not yet created in repository, branch it from base_target (main)
        if feature_file is None:
            if base_file is None:
                return BranchRemediationResult(
                    status="stale_proposal",
                    proposal_id=proposal.proposal_id,
                    action_sha256=proposal.action_sha256,
                    target_branch=target.branch,
                    base_branch=base_branch,
                    repository_id=target.repository_id,
                    repository_path=target.path,
                    approval_status=approval_status,
                    preconditions_verified=False,
                    main_protected=True,
                    error_message=f"Original file '{target.path}' not found on base branch '{base_branch}'.",
                )

            # Freshness verification against base repository file
            if base_file.commit != proposal.preconditions.expected_commit:
                return BranchRemediationResult(
                    status="stale_proposal",
                    proposal_id=proposal.proposal_id,
                    action_sha256=proposal.action_sha256,
                    target_branch=target.branch,
                    base_branch=base_branch,
                    repository_id=target.repository_id,
                    repository_path=target.path,
                    approval_status=approval_status,
                    preconditions_verified=False,
                    main_protected=True,
                    error_message=f"Repository base commit drifted: expected {proposal.preconditions.expected_commit}, found {base_file.commit}.",
                )

            if content_hash(base_file.content) != proposal.preconditions.original_content_sha256:
                return BranchRemediationResult(
                    status="stale_proposal",
                    proposal_id=proposal.proposal_id,
                    action_sha256=proposal.action_sha256,
                    target_branch=target.branch,
                    base_branch=base_branch,
                    repository_id=target.repository_id,
                    repository_path=target.path,
                    approval_status=approval_status,
                    preconditions_verified=False,
                    main_protected=True,
                    error_message=f"Original file content drifted: expected {proposal.preconditions.original_content_sha256}, found {content_hash(base_file.content)}.",
                )

            # Create the dedicated feature branch from main
            repo.seed(target, commit=base_file.commit, content=base_file.content)
        else:
            # Feature branch exists; check preconditions on it
            if (
                feature_file.commit != proposal.preconditions.expected_commit
                or content_hash(feature_file.content) != proposal.preconditions.original_content_sha256
            ):
                return BranchRemediationResult(
                    status="stale_proposal",
                    proposal_id=proposal.proposal_id,
                    action_sha256=proposal.action_sha256,
                    target_branch=target.branch,
                    base_branch=base_branch,
                    repository_id=target.repository_id,
                    repository_path=target.path,
                    approval_status=approval_status,
                    preconditions_verified=False,
                    main_protected=True,
                    error_message="Feature branch preconditions failed freshness verification.",
                )

    # Dispatch write strictly to the isolated feature branch via MockExecutor
    exec_result = executor.execute(proposal, session, policy)

    if exec_result.status == "success":
        # Post-execution Invariant Checks:
        with repo._lock:
            # 1. Main branch was NEVER modified!
            if key_base in repo.files and base_file is not None:
                main_after = repo.files[key_base]
                if main_after.content != base_file.content or main_after.commit != base_file.commit:
                    return BranchRemediationResult(
                        status="failed",
                        proposal_id=proposal.proposal_id,
                        action_sha256=proposal.action_sha256,
                        target_branch=target.branch,
                        base_branch=base_branch,
                        repository_id=target.repository_id,
                        repository_path=target.path,
                        approval_status="consumed",
                        preconditions_verified=True,
                        main_protected=False,
                        error_message="CRITICAL: Main branch was modified during isolated execution!",
                    )

            # 2. Feature branch received the replacement content!
            feature_after = repo.files.get(key_feature)
            if feature_after is None or feature_after.content != proposal.payload.replacement_content:
                return BranchRemediationResult(
                    status="failed",
                    proposal_id=proposal.proposal_id,
                    action_sha256=proposal.action_sha256,
                    target_branch=target.branch,
                    base_branch=base_branch,
                    repository_id=target.repository_id,
                    repository_path=target.path,
                    approval_status="consumed",
                    preconditions_verified=True,
                    main_protected=True,
                    error_message="Feature branch did not receive replacement content.",
                )

        return BranchRemediationResult(
            status="approved_and_applied",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=session.record.approval_status,
            preconditions_verified=True,
            main_protected=True,
            auto_merge_prevented=True,
            auto_apply_prevented=True,
            execution_summary=exec_result.model_dump(mode="json"),
        )

    if exec_result.status in {"precondition_failed", "expired"}:
        return BranchRemediationResult(
            status="stale_proposal",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=session.record.approval_status,
            preconditions_verified=False,
            main_protected=True,
            execution_summary=exec_result.model_dump(mode="json"),
            error_message=f"Preconditions failed during execution: {exec_result.error}",
        )

    if exec_result.status == "unauthorized":
        return BranchRemediationResult(
            status="unauthorized_branch",
            proposal_id=proposal.proposal_id,
            action_sha256=proposal.action_sha256,
            target_branch=target.branch,
            base_branch=base_branch,
            repository_id=target.repository_id,
            repository_path=target.path,
            approval_status=session.record.approval_status,
            preconditions_verified=False,
            main_protected=True,
            execution_summary=exec_result.model_dump(mode="json"),
            error_message=f"Unauthorized by policy: {exec_result.error}",
        )

    return BranchRemediationResult(
        status="failed",
        proposal_id=proposal.proposal_id,
        action_sha256=proposal.action_sha256,
        target_branch=target.branch,
        base_branch=base_branch,
        repository_id=target.repository_id,
        repository_path=target.path,
        approval_status=session.record.approval_status,
        preconditions_verified=False,
        main_protected=True,
        execution_summary=exec_result.model_dump(mode="json"),
        error_message=f"Mock execution failed: {exec_result.error}",
    )
