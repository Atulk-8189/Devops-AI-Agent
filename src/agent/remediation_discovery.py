"""Remediation discovery: correlate AKS workload diagnostic evidence with Azure Repos Terraform files."""

import json
import re
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from src.agent.aks_troubleshooting import TroubleshootingEvidence, AKSDiagnosis
from src.review.terraform_structure import extract_terraform_structure


class WorkloadEvidenceSummary(BaseModel):
    """Normalized evidence extracted from Kubernetes diagnostic state."""

    workload_name: str
    namespace: str
    container_images: list[str] = Field(default_factory=list)
    container_names: list[str] = Field(default_factory=list)
    unhealthy_containers: list[str] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class MatchedTerraformResource(BaseModel):
    """Specific Terraform resource or variable matching Kubernetes workload evidence."""

    file_path: str
    resource_type: str
    resource_name: str
    start_line: int
    end_line: int
    match_reasons: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class RemediationDiscoveryResult(BaseModel):
    """Structured result bridging AKS diagnostic evidence to Terraform source configuration."""

    status: Literal["matched", "ambiguous", "missing"] = Field(
        description="'matched' if a single authoritative resource was found, 'ambiguous' if multiple match, 'missing' if none."
    )
    confidence: Literal["exact", "ambiguous", "missing"] = Field(
        description="'exact', 'ambiguous', or 'missing'."
    )
    aks_diagnosis: dict[str, Any] = Field(
        description="Observed AKS diagnostic facts, root cause hypotheses, and recommendations."
    )
    workload_evidence: WorkloadEvidenceSummary = Field(
        description="Key facts extracted from the AKS deployment, pods, and container statuses."
    )
    repository_path: str | None = Field(
        default=None,
        description="Exact path to the matched Terraform file in Azure Repos, or None if missing/ambiguous."
    )
    matched_resource: MatchedTerraformResource | None = Field(
        default=None,
        description="Matched Terraform resource or variable metadata, or None if missing/ambiguous."
    )
    relevant_code: str | None = Field(
        default=None,
        description="Numbered source code snippet of the matched resource, or None if missing/ambiguous."
    )
    candidate_matches: list[MatchedTerraformResource] = Field(
        default_factory=list,
        description="List of candidate resources when matches are ambiguous."
    )
    mapping_uncertainties: list[str] = Field(
        default_factory=list,
        description="Explicit uncertainties, environment assumptions, or discovery coverage caveats."
    )

    model_config = ConfigDict(extra="forbid")


def is_aks_remediation_question(question: str) -> bool:
    """True for questions asking to correlate AKS issues with Terraform or find/fix resources in Terraform."""
    normalized = question.lower().replace("-", " ")
    if "terraform" not in normalized and ".tf" not in normalized:
        return False
    # Must have Kubernetes or workload context
    k8s_context = bool(re.search(
        r"\b(?:aks|kubernetes|k8s|pod|pods|deployment|deployments|task manager|workload|workloads|container|containers)\b",
        normalized,
    ))
    if not k8s_context:
        return False
    # Exclude standard Terraform code reviews like "Review the Terraform configuration for Task Manager AKS service connectivity."
    if normalized.strip().startswith("review") and not any(k in normalized for k in (
        "fix", "remediat", "correlat", "cause", "fail", "issue", "crash", "error", "broken", "why", "locate", "trace"
    )):
        return False
    # Must have remediation, diagnosis-to-code, correlation, or resource-finding intent
    remediation_intent = bool(re.search(
        r"\b(?:remediat\w*|fix\w*|correlat\w*|match\w*|locat\w*|trac\w*|find.*(?:file|resource|config|code)|root cause)\b",
        normalized,
    ))
    return remediation_intent


def extract_workload_evidence(aks_evidence: TroubleshootingEvidence) -> WorkloadEvidenceSummary:
    """Extract workload identity, container images, unhealthy states, and failure reasons from AKS evidence."""
    deployment = aks_evidence.deployment if isinstance(aks_evidence.deployment, dict) else {}
    meta = deployment.get("metadata", {}) if isinstance(deployment.get("metadata"), dict) else {}
    workload_name = meta.get("name") or "task-manager"
    namespace = meta.get("namespace") or "default"

    container_images: list[str] = []
    container_names: list[str] = []
    unhealthy_containers: list[str] = []
    failure_reasons: list[str] = []

    # 1. Spec containers from deployment
    spec = deployment.get("spec", {}) if isinstance(deployment.get("spec"), dict) else {}
    template = spec.get("template", {}) if isinstance(spec.get("template"), dict) else {}
    pod_spec = template.get("spec", {}) if isinstance(template.get("spec"), dict) else {}

    containers_spec = pod_spec.get("containers", []) if isinstance(pod_spec.get("containers"), list) else []
    init_containers_spec = pod_spec.get("initContainers", []) if isinstance(pod_spec.get("initContainers"), list) else []

    for c in containers_spec + init_containers_spec:
        if isinstance(c, dict):
            name = c.get("name")
            image = c.get("image")
            if name and name not in container_names:
                container_names.append(name)
            if image and image not in container_images:
                container_images.append(image)

    # 2. Pod statuses and evaluations
    pod_health = aks_evidence.pod_health if isinstance(aks_evidence.pod_health, dict) else {}
    pods = pod_health.get("pods", []) if isinstance(pod_health.get("pods"), list) else []

    for pod in pods:
        if not isinstance(pod, dict):
            continue
        containers = pod.get("containers", []) if isinstance(pod.get("containers"), list) else []
        init_containers = pod.get("init_containers", []) if isinstance(pod.get("init_containers"), list) else []

        for c in containers + init_containers:
            if not isinstance(c, dict):
                continue
            c_name = c.get("name")
            c_image = c.get("image")
            if c_name and c_name not in container_names:
                container_names.append(c_name)
            if c_image and c_image not in container_images:
                container_images.append(c_image)

            # Check health
            is_ready = c.get("ready")
            restarts = c.get("restart_count", 0) or 0
            waiting_reason = c.get("waiting_reason")
            waiting_message = c.get("waiting_message")

            if is_ready is False or restarts > 0 or waiting_reason:
                if c_name and c_name not in unhealthy_containers:
                    unhealthy_containers.append(c_name)

            if waiting_reason and waiting_reason not in failure_reasons:
                failure_reasons.append(waiting_reason)
            if waiting_message and waiting_message not in failure_reasons:
                failure_reasons.append(waiting_message)

    # 3. Check deployment conditions
    dep_status = deployment.get("status", {}) if isinstance(deployment.get("status"), dict) else {}
    for cond in dep_status.get("conditions", []) if isinstance(dep_status.get("conditions"), list) else []:
        if isinstance(cond, dict) and cond.get("type") == "Progressing" and cond.get("status") == "False":
            reason = cond.get("reason")
            if reason and reason not in failure_reasons:
                failure_reasons.append(reason)

    return WorkloadEvidenceSummary(
        workload_name=workload_name,
        namespace=namespace,
        container_images=container_images,
        container_names=container_names,
        unhealthy_containers=unhealthy_containers,
        failure_reasons=failure_reasons,
    )


def format_code_with_lines(file_content: str, start_line: int, end_line: int) -> str:
    """Format lines from start_line to end_line (1-indexed inclusive) with line numbers."""
    lines = file_content.splitlines()
    snippet_lines = []
    for line_num in range(max(1, start_line), min(end_line + 1, len(lines) + 1)):
        snippet_lines.append(f"{line_num}: {lines[line_num - 1]}")
    return "\n".join(snippet_lines)


def correlate_aks_to_terraform(
    aks_evidence: TroubleshootingEvidence,
    diagnosis: AKSDiagnosis | dict[str, Any],
    terraform_files: dict[str, str],
    discovery_metadata: dict[str, Any] | None = None,
) -> RemediationDiscoveryResult:
    """Deterministically correlate AKS workload evidence with Terraform files from Azure Repos."""
    workload = extract_workload_evidence(aks_evidence)
    diag_dict = (
        diagnosis.model_dump(by_alias=True)
        if isinstance(diagnosis, AKSDiagnosis)
        else dict(diagnosis)
    )

    mapping_uncertainties: list[str] = []

    if discovery_metadata and discovery_metadata.get("incomplete"):
        mapping_uncertainties.append(
            "Terraform repository discovery was incomplete; some repository files or directories "
            "were not inspected due to discovery traversal limits."
        )

    if not terraform_files:
        mapping_uncertainties.append(
            f"No Terraform source files were retrieved from Azure Repos under /terraform for workload '{workload.workload_name}'."
        )
        return RemediationDiscoveryResult(
            status="missing",
            confidence="missing",
            aks_diagnosis=diag_dict,
            workload_evidence=workload,
            repository_path=None,
            matched_resource=None,
            relevant_code=None,
            candidate_matches=[],
            mapping_uncertainties=mapping_uncertainties,
        )

    # 1. Structural extraction
    structure = extract_terraform_structure(terraform_files)
    for file_summary in structure.get("files", []):
        if file_summary.get("parse_status") != "complete":
            mapping_uncertainties.append(
                f"Terraform file '{file_summary.get('file')}' had {file_summary.get('parse_status')} "
                f"structural parsing ({file_summary.get('reason', 'unknown')}); line boundaries may be partial."
            )

    candidates: list[tuple[int, MatchedTerraformResource]] = []

    # 2. Inspect parsed resources
    for res in structure.get("resources", []):
        file_path = res["file"]
        res_type = res["type"]
        res_name = res["name"]
        start_line = res["start_line"]
        end_line = res["end_line"]

        file_text = terraform_files.get(file_path, "")
        block_lines = file_text.splitlines()[start_line - 1 : end_line]
        block_content = "\n".join(block_lines)

        match_reasons = []
        score = 0

        # Match exact container image
        matched_exact_image = False
        for img in workload.container_images:
            if img in block_content:
                match_reasons.append(f"Resource defines matching container image '{img}'")
                score += 3
                matched_exact_image = True
                break

        # Match container image base name
        if not matched_exact_image:
            for img in workload.container_images:
                # e.g., "myregistry.azurecr.io/task-manager:badtag" -> "task-manager"
                base_name = img.split("/")[-1].split(":")[0]
                if base_name and base_name in block_content and re.search(r'\bimage\s*=', block_content):
                    match_reasons.append(f"Resource references container image name '{base_name}'")
                    score += 2
                    break

        # Match Kubernetes workload type and workload name
        k8s_types = {
            "kubernetes_deployment", "kubernetes_deployment_v1",
            "kubernetes_pod", "kubernetes_pod_v1",
            "kubernetes_stateful_set", "kubernetes_daemonset",
            "helm_release",
        }
        if res_type in k8s_types:
            norm_res_name = res_name.replace("_", "-")
            norm_workload = workload.workload_name.replace("_", "-")
            if norm_res_name == norm_workload or workload.workload_name in block_content:
                match_reasons.append(
                    f"Resource type '{res_type}' matches Kubernetes workload '{workload.workload_name}'"
                )
                score += 2

        if score > 0:
            candidates.append((score, MatchedTerraformResource(
                file_path=file_path,
                resource_type=res_type,
                resource_name=res_name,
                start_line=start_line,
                end_line=end_line,
                match_reasons=match_reasons,
            )))

    # 3. Inspect parsed variables if container image is parameterized
    for var in structure.get("variables", []):
        file_path = var["file"]
        var_name = var["name"]
        start_line = var["start_line"]
        end_line = var["end_line"]

        file_text = terraform_files.get(file_path, "")
        block_lines = file_text.splitlines()[start_line - 1 : end_line]
        block_content = "\n".join(block_lines)

        match_reasons = []
        score = 0
        for img in workload.container_images:
            if img in block_content:
                match_reasons.append(f"Variable '{var_name}' defines default for container image '{img}'")
                score += 2
                break

        if score > 0:
            candidates.append((score, MatchedTerraformResource(
                file_path=file_path,
                resource_type="variable",
                resource_name=var_name,
                start_line=start_line,
                end_line=end_line,
                match_reasons=match_reasons,
            )))

    # 4. Fallback search if structural parsing found zero candidates
    if not candidates:
        for file_path, file_text in sorted(terraform_files.items()):
            for img in workload.container_images:
                if img in file_text:
                    # Find approximate line
                    lines = file_text.splitlines()
                    for idx, line in enumerate(lines, 1):
                        if img in line:
                            start = max(1, idx - 5)
                            end = min(len(lines), idx + 10)
                            candidates.append((1, MatchedTerraformResource(
                                file_path=file_path,
                                resource_type="text_match",
                                resource_name=img.split("/")[-1].split(":")[0],
                                start_line=start,
                                end_line=end,
                                match_reasons=[f"File contains container image string '{img}' at line {idx}"],
                            )))
                            break

    # 5. Evaluate candidates
    if not candidates:
        mapping_uncertainties.append(
            f"No Terraform resources, modules, or variables in the repository match the workload "
            f"'{workload.workload_name}' or observed container images: {workload.container_images or 'none'}."
        )
        mapping_uncertainties.append(
            "The workload may have been deployed outside of Terraform (e.g., manual kubectl apply or Helm) "
            "or resides in a separate repository or branch."
        )
        return RemediationDiscoveryResult(
            status="missing",
            confidence="missing",
            aks_diagnosis=diag_dict,
            workload_evidence=workload,
            repository_path=None,
            matched_resource=None,
            relevant_code=None,
            candidate_matches=[],
            mapping_uncertainties=mapping_uncertainties,
        )

    # Sort candidates by score descending
    candidates.sort(key=lambda c: c[0], reverse=True)
    max_score = candidates[0][0]
    top_candidates = [c[1] for c in candidates if c[0] == max_score]

    # If multiple top candidates exist, it is ambiguous
    if len(top_candidates) > 1:
        mapping_uncertainties.append(
            f"Multiple candidate Terraform resources ({len(top_candidates)}) match the workload evidence: "
            + ", ".join(f"{c.file_path} ({c.resource_type}.{c.resource_name})" for c in top_candidates) + "."
        )
        mapping_uncertainties.append(
            "Cannot determine the authoritative resource without additional environment or cluster mapping metadata."
        )
        return RemediationDiscoveryResult(
            status="ambiguous",
            confidence="ambiguous",
            aks_diagnosis=diag_dict,
            workload_evidence=workload,
            repository_path=None,
            matched_resource=None,
            relevant_code=None,
            candidate_matches=top_candidates,
            mapping_uncertainties=mapping_uncertainties,
        )

    # Exactly one top candidate
    best_candidate = top_candidates[0]
    relevant_code = format_code_with_lines(
        terraform_files[best_candidate.file_path],
        best_candidate.start_line,
        best_candidate.end_line,
    )

    mapping_uncertainties.append(
        "Assumes repository 'main' branch reflects the intended desired state for the AKS cluster."
    )
    mapping_uncertainties.append(
        "Cluster state may deviate if resources were modified out-of-band with kubectl or independent CI/CD pipelines."
    )

    return RemediationDiscoveryResult(
        status="matched",
        confidence="exact",
        aks_diagnosis=diag_dict,
        workload_evidence=workload,
        repository_path=best_candidate.file_path,
        matched_resource=best_candidate,
        relevant_code=relevant_code,
        candidate_matches=[],
        mapping_uncertainties=mapping_uncertainties,
    )
