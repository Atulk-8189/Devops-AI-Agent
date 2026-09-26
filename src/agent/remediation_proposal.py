"""Stage 2 & 3: Structured Terraform fix proposal generation from AKS remediation evidence.

Converts confirmed AKS-to-Terraform discovery evidence into a strictly validated
Action and Proposal without executing any write or modifying remote state.
"""

import hashlib
import json
import posixpath
import re
from typing import Any, Literal
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from src.agent.proposals import (
    Action,
    Payload,
    Preconditions,
    Proposal,
    Target,
    build_proposal,
    action_hash,
)
from src.agent.remediation_discovery import (
    MatchedTerraformResource,
    RemediationDiscoveryResult,
    WorkloadEvidenceSummary,
)
from src.policy.write_policy import WritePolicy


ALLOWED_REMEDIATION_RESOURCE_TYPES = frozenset({
    "kubernetes_deployment",
    "kubernetes_deployment_v1",
    "kubernetes_pod",
    "kubernetes_pod_v1",
    "kubernetes_stateful_set",
    "kubernetes_daemonset",
    "helm_release",
    "variable",
})

COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class RemediationProposalResult(BaseModel):
    """Unified result of AKS diagnosis, Terraform discovery, and structured Proposal generation."""

    status: Literal["proposed", "blocked", "rejected"] = Field(
        description="'proposed' if valid Proposal was generated, 'blocked' if metadata or mapping prevented proposal, 'rejected' on policy failure."
    )
    confidence: Literal["exact", "ambiguous", "missing"] | None = Field(
        default=None,
        description="Confidence from Terraform AST discovery."
    )
    aks_diagnosis: dict[str, Any] | None = Field(
        default=None,
        description="Structured AKS diagnosis facts, root causes, and recommendations."
    )
    workload_evidence: WorkloadEvidenceSummary | None = Field(
        default=None,
        description="Observed facts from AKS deployment and pods."
    )
    repository_path: str | None = Field(
        default=None,
        description="Exact repository path (e.g. /terraform/main.tf)."
    )
    resource: MatchedTerraformResource | None = Field(
        default=None,
        description="The matched Terraform resource."
    )
    matched_resource: MatchedTerraformResource | None = Field(
        default=None,
        description="Alias for resource to maintain compatibility with discovery results."
    )
    relevant_code: str | None = Field(
        default=None,
        description="Numbered excerpt of the relevant Terraform code."
    )
    candidate_matches: list[MatchedTerraformResource] = Field(
        default_factory=list,
        description="Candidate Terraform resources when mapping is ambiguous."
    )
    original_content: str | None = Field(
        default=None,
        description="Original content of the file from repository."
    )
    proposed_replacement_content: str | None = Field(
        default=None,
        description="Proposed replacement content."
    )
    proposal: Proposal | None = Field(
        default=None,
        description="The validated Proposal object, or None if blocked/rejected."
    )
    action: Action | None = Field(
        default=None,
        description="The underlying Action object, or None if blocked/rejected."
    )
    rationale: str = Field(
        description="Explanation of why this replacement fixes the AKS workload issue."
    )
    supporting_evidence: list[str] = Field(
        default_factory=list,
        description="Observed facts from AKS and Terraform."
    )
    mapping_uncertainties: list[str] = Field(
        default_factory=list,
        description="Uncertainties or unverified assumptions."
    )
    block_reason: str | None = Field(
        default=None,
        description="Reason why proposal generation was blocked or rejected."
    )
    branch_remediation: Any | None = Field(
        default=None,
        description="Stage 4 branch-isolated remediation result if executed."
    )

    model_config = ConfigDict(extra="forbid")


def _collect_supporting_evidence(discovery_result: RemediationDiscoveryResult) -> list[str]:
    """Compile facts from AKS diagnosis and workload evidence into an audit trail."""
    evidence: list[str] = []
    wl = discovery_result.workload_evidence
    evidence.append(f"Workload: {wl.workload_name} in namespace '{wl.namespace}'")
    if wl.unhealthy_containers:
        evidence.append(f"Unhealthy containers: {', '.join(wl.unhealthy_containers)}")
    if wl.container_images:
        evidence.append(f"Observed container images: {', '.join(wl.container_images)}")
    if wl.failure_reasons:
        evidence.append(f"Observed failure reasons: {', '.join(wl.failure_reasons)}")
    summary = discovery_result.aks_diagnosis.get("Summary")
    if summary:
        evidence.append(f"AKS diagnosis summary: {summary}")
    for obs in discovery_result.aks_diagnosis.get("Observed evidence", []):
        evidence.append(f"AKS observed fact: {obs}")
    for cause in discovery_result.aks_diagnosis.get("Likely root causes", []):
        evidence.append(f"AKS root cause hypothesis: {cause}")
    if discovery_result.matched_resource and discovery_result.repository_path:
        res = discovery_result.matched_resource
        evidence.append(
            f"Matched Terraform resource: {res.resource_type}.{res.resource_name} "
            f"in {discovery_result.repository_path} (lines {res.start_line}-{res.end_line})"
        )
        for reason in res.match_reasons:
            evidence.append(f"Resource match basis: {reason}")
    return evidence


def determine_proposed_replacement(
    discovery_result: RemediationDiscoveryResult,
    original_content: str,
    question: str,
    hints: str = "",
) -> tuple[str | None, str]:
    """Determine proposed replacement content and rationale from user request and discovery evidence."""
    combined_text = f"{question} {hints}".strip()
    workload = discovery_result.workload_evidence
    matched = discovery_result.matched_resource

    # 1. Look for explicit full image (e.g., myregistry.azurecr.io/task-manager:v2.0.0 or image:tag)
    image_match = re.search(
        r'\b(?:to|with|image|use)\s+([a-zA-Z0-9._/-]+:[a-zA-Z0-9._-]+)\b',
        combined_text,
        re.IGNORECASE,
    )
    target_image = image_match.group(1) if image_match else None

    # 2. Look for tag if only tag is specified (e.g., "to v2.0.0", "tag v2.0.0")
    if not target_image:
        tag_match = re.search(
            r'\b(?:tag|version|to|with)\s+(v[0-9]+(?:\.[0-9]+)*|[a-zA-Z0-9._-]+)\b',
            combined_text,
            re.IGNORECASE,
        )
        if tag_match:
            candidate_tag = tag_match.group(1)
            if candidate_tag.lower() not in {"terraform", "main", "the", "image", "tag", "fix", "a", "an"}:
                for img in workload.container_images:
                    if ":" in img:
                        base = img.rsplit(":", 1)[0]
                        target_image = f"{base}:{candidate_tag}"
                        break

    if not target_image:
        return None, "No target container image or replacement specification was found in the request."

    # Perform replacement in original_content
    replaced = False
    replacement_content = original_content
    failing_found = None
    for failing_img in workload.container_images:
        if failing_img in original_content:
            replacement_content = original_content.replace(failing_img, target_image)
            failing_found = failing_img
            replaced = True
            break

    if not replaced and matched:
        lines = original_content.splitlines(keepends=True)
        if 1 <= matched.start_line <= len(lines):
            end_line = min(matched.end_line, len(lines))
            new_lines = []
            for idx, line in enumerate(lines, 1):
                if matched.start_line <= idx <= end_line and re.search(r'\bimage\s*=', line):
                    new_line = re.sub(r'image\s*=\s*"[^"]+"', f'image = "{target_image}"', line)
                    new_lines.append(new_line)
                    replaced = True
                else:
                    new_lines.append(line)
            if replaced:
                replacement_content = "".join(new_lines)

    if not replaced or replacement_content == original_content:
        return None, f"Could not locate image to replace with '{target_image}' in matched resource."

    res_desc = f"{matched.resource_type}.{matched.resource_name}" if matched else "Terraform configuration"
    rationale = (
        f"Update container image from '{failing_found or 'failing image'}' to '{target_image}' "
        f"in {res_desc} ({discovery_result.repository_path}) "
        f"to resolve {', '.join(workload.failure_reasons) or 'container failure'}."
    )
    return replacement_content, rationale


def generate_remediation_proposal(
    discovery_result: RemediationDiscoveryResult,
    proposed_replacement_content: str,
    *,
    commit_sha: str | None = None,
    repository_id: str | None = None,
    branch: str = "main",
    requested_by: str = "devops-agent",
    reason: str | None = None,
    expected_impact: str | None = None,
    risk_level: Literal["low", "medium", "high"] = "medium",
    policy: WritePolicy | None = None,
    original_files: dict[str, str] | None = None,
) -> RemediationProposalResult:
    """Generate a validated Action and Proposal from AKS remediation discovery evidence.

    Captures current repository commit SHA and original file SHA-256 as preconditions.
    Fails closed if mapping is ambiguous, metadata is missing, or policy is violated.
    """
    supporting_evidence = _collect_supporting_evidence(discovery_result)
    mapping_uncertainties = list(discovery_result.mapping_uncertainties)

    base_context = {
        "confidence": discovery_result.confidence,
        "aks_diagnosis": discovery_result.aks_diagnosis,
        "workload_evidence": discovery_result.workload_evidence,
        "candidate_matches": list(discovery_result.candidate_matches),
        "relevant_code": discovery_result.relevant_code,
    }

    # 1. Reject ambiguous mappings
    if discovery_result.status == "ambiguous":
        return RemediationProposalResult(
            status="blocked",
            rationale=reason or "Terraform mapping is ambiguous.",
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason="Terraform mapping is ambiguous: multiple candidate resources match the workload. "
                         "A proposal cannot be generated for an ambiguous target.",
            **base_context,
        )

    # 2. Reject missing mappings or missing matched resource
    if discovery_result.status == "missing" or not discovery_result.matched_resource or not discovery_result.repository_path:
        return RemediationProposalResult(
            status="blocked",
            rationale=reason or "Terraform mapping is missing.",
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason="No confirmed Terraform resource mapping was found for the AKS workload. "
                         "Cannot generate a remediation proposal without a confirmed resource.",
            **base_context,
        )

    matched_res = discovery_result.matched_resource
    repo_path = discovery_result.repository_path

    # 3. Validate supported resource type
    if matched_res.resource_type not in ALLOWED_REMEDIATION_RESOURCE_TYPES:
        return RemediationProposalResult(
            status="blocked",
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            rationale=reason or f"Resource type '{matched_res.resource_type}' is unsupported.",
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason=f"Resource type '{matched_res.resource_type}' is not supported for remediation proposals. "
                         f"Supported types: {sorted(ALLOWED_REMEDIATION_RESOURCE_TYPES)}.",
            **base_context,
        )

    # 4. Check original content availability
    original_content: str | None = None
    if original_files and repo_path in original_files:
        original_content = original_files[repo_path]

    if original_content is None:
        return RemediationProposalResult(
            status="blocked",
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            rationale=reason or "Original file content unavailable.",
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason=f"Original file content for '{repo_path}' is unavailable; "
                         "original_content_sha256 precondition cannot be computed.",
            **base_context,
        )

    # 5. Validate replacement content
    if not proposed_replacement_content or not proposed_replacement_content.strip():
        return RemediationProposalResult(
            status="blocked",
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            original_content=original_content,
            proposed_replacement_content=proposed_replacement_content,
            rationale=reason or "Invalid replacement content.",
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason="Proposed replacement content is empty or whitespace-only.",
            **base_context,
        )

    if proposed_replacement_content == original_content:
        return RemediationProposalResult(
            status="blocked",
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            original_content=original_content,
            proposed_replacement_content=proposed_replacement_content,
            rationale=reason or "No change in replacement content.",
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason="Proposed replacement content is identical to the original content; no change would be made.",
            **base_context,
        )

    # 6. Capture repository commit SHA precondition (Requirement 3 & 4)
    if not commit_sha or not isinstance(commit_sha, str) or not COMMIT_SHA_PATTERN.match(commit_sha):
        return RemediationProposalResult(
            status="blocked",
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            original_content=original_content,
            proposed_replacement_content=proposed_replacement_content,
            rationale=reason or "Missing reliable commit SHA.",
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason="Reliable repository commit SHA metadata is unavailable (must be a valid 40-character hex SHA). "
                         "Proposal cannot be constructed without verified commit precondition.",
            **base_context,
        )

    # 7. Capture repository ID (Requirement 3)
    if not repository_id or not isinstance(repository_id, str) or not repository_id.strip():
        return RemediationProposalResult(
            status="blocked",
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            original_content=original_content,
            proposed_replacement_content=proposed_replacement_content,
            rationale=reason or "Missing repository ID.",
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason="Repository ID metadata is unavailable; cannot construct proposal target without verified repository ID.",
            **base_context,
        )

    # 8. Compute original file SHA-256 precondition
    original_sha256 = hashlib.sha256(original_content.encode("utf-8")).hexdigest()

    # 9. Format rationale and impact
    effective_reason = reason or (
        f"Remediate {discovery_result.workload_evidence.workload_name} failure in {repo_path} "
        f"({matched_res.resource_type}.{matched_res.resource_name})"
    )
    effective_impact = expected_impact or (
        f"Update {matched_res.resource_type}.{matched_res.resource_name} in {repo_path} to address "
        f"{', '.join(discovery_result.workload_evidence.failure_reasons) or 'container failures'}."
    )

    # 10. Construct Target, Payload, Preconditions, Action
    try:
        target = Target(
            project="My Project",
            repository_id=repository_id,
            branch=branch,
            path=repo_path,
        )
        payload = Payload(replacement_content=proposed_replacement_content)
        preconditions = Preconditions(
            expected_commit=commit_sha,
            original_content_sha256=original_sha256,
        )
        action = Action(
            target=target,
            operation="update_existing_file",
            payload=payload,
            preconditions=preconditions,
        )
    except ValueError as err:
        return RemediationProposalResult(
            status="blocked",
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            original_content=original_content,
            proposed_replacement_content=proposed_replacement_content,
            rationale=effective_reason,
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason=f"Action validation failed: {err}",
            **base_context,
        )

    # 11. Validate against WritePolicy and build Proposal
    effective_policy = policy or WritePolicy(repository_branches={repository_id: [branch]})

    try:
        proposal = build_proposal(
            action,
            requested_by=requested_by,
            reason=effective_reason,
            expected_impact=effective_impact,
            risk_level=risk_level,
            policy=effective_policy,
        )
    except PermissionError as err:
        return RemediationProposalResult(
            status="rejected",
            action=action,
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            original_content=original_content,
            proposed_replacement_content=proposed_replacement_content,
            rationale=effective_reason,
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason=f"Write policy violation: {err}",
            **base_context,
        )
    except (ValueError, Exception) as err:
        return RemediationProposalResult(
            status="blocked",
            action=action,
            repository_path=repo_path,
            resource=matched_res,
            matched_resource=matched_res,
            original_content=original_content,
            proposed_replacement_content=proposed_replacement_content,
            rationale=effective_reason,
            supporting_evidence=supporting_evidence,
            mapping_uncertainties=mapping_uncertainties,
            block_reason=f"Proposal construction failed: {err}",
            **base_context,
        )

    return RemediationProposalResult(
        status="proposed",
        proposal=proposal,
        action=action,
        repository_path=repo_path,
        resource=matched_res,
        matched_resource=matched_res,
        original_content=original_content,
        proposed_replacement_content=proposed_replacement_content,
        rationale=effective_reason,
        supporting_evidence=supporting_evidence,
        mapping_uncertainties=mapping_uncertainties,
        block_reason=None,
        **base_context,
    )


class ProposeTerraformFixInput(BaseModel):
    """Input arguments for the propose_terraform_fix agent tool."""

    repository_path: str = Field(description="Exact repository path to the Terraform file, e.g. /terraform/main.tf")
    replacement_content: str = Field(description="The complete new content of the Terraform file")
    commit_sha: str = Field(description="The 40-character hex commit SHA of the target branch HEAD")
    repository_id: str = Field(description="The Azure Repos repository ID")
    branch: str = Field(default="main", description="Target branch, default 'main'")
    rationale: str = Field(description="Why this change fixes the AKS failure")
    expected_impact: str = Field(default="Update Terraform configuration to fix AKS workload failure")
    risk_level: Literal["low", "medium", "high"] = Field(default="medium")

    model_config = ConfigDict(extra="forbid")


class ProposeTerraformFixTool:
    """Controlled agent tool that constructs a validated Proposal from confirmed Terraform remediation inputs.

    Never executes writes; outputs an immutable, strictly validated Proposal and Action.
    """

    name: str = "propose_terraform_fix"
    description: str = (
        "Generate a structured, policy-checked Action and Proposal to update a Terraform file. "
        "Does NOT execute writes; generates an immutable Proposal for operator approval."
    )
    args_schema: type[BaseModel] = ProposeTerraformFixInput

    def __init__(
        self,
        policy: WritePolicy | None = None,
        original_files: dict[str, str] | None = None,
        discovery_result: RemediationDiscoveryResult | None = None,
    ):
        self.policy = policy
        self.original_files = original_files or {}
        self.discovery_result = discovery_result

    async def ainvoke(self, call: dict[str, Any]) -> ToolMessage:
        call_id = call.get("id", "call-proposal")
        raw_args = call.get("args", {})

        try:
            args = ProposeTerraformFixInput.model_validate(raw_args)
        except Exception as err:
            return ToolMessage(
                content=json.dumps({"status": "blocked", "block_reason": f"Invalid tool arguments: {err}"}),
                tool_call_id=call_id,
                name=self.name,
                status="error",
            )

        if self.discovery_result:
            disc = self.discovery_result
        else:
            disc = RemediationDiscoveryResult(
                status="matched",
                confidence="exact",
                aks_diagnosis={"Summary": args.rationale},
                workload_evidence=WorkloadEvidenceSummary(workload_name="task-manager", namespace="default"),
                repository_path=args.repository_path,
                matched_resource=MatchedTerraformResource(
                    file_path=args.repository_path,
                    resource_type="kubernetes_deployment",
                    resource_name="task_manager",
                    start_line=1,
                    end_line=1,
                    match_reasons=["Direct tool invocation"],
                ),
            )

        result = generate_remediation_proposal(
            discovery_result=disc,
            proposed_replacement_content=args.replacement_content,
            commit_sha=args.commit_sha,
            repository_id=args.repository_id,
            branch=args.branch,
            reason=args.rationale,
            expected_impact=args.expected_impact,
            risk_level=args.risk_level,
            policy=self.policy,
            original_files=self.original_files,
        )

        is_success = result.status == "proposed"
        return ToolMessage(
            content=json.dumps(result.model_dump(mode="json"), indent=2),
            tool_call_id=call_id,
            name=self.name,
            status="success" if is_success else "error",
        )
