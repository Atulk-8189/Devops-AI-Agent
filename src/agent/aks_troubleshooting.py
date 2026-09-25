"""Evidence collection for read-only AKS application troubleshooting."""

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from src.policy.policy import enforce_read_only_policy, CLUSTER_SCOPED_RESOURCES
from src.config import ConfigurationError
from src.mcp.runtime import MCPRuntimeError
from src.agent.aks_network import network_evidence
from src.safe_diagnostics import diagnostic_line, classify_error
from src.agent.aks_events import event_evidence


NAMESPACE = "default"
DEPLOYMENT = "task-manager"
SERVICE = "task-manager"
# Healthy path: eight reads. Four additional calls allow focused pod describes.
MAX_AKS_COLLECTION_CALLS = 12
# Entire serialized evidence document, including truncation metadata (characters).
MAX_AKS_EVIDENCE_CHARS = 24000
_UNTRUSTED_ENVELOPE = re.compile(r"<<([^>]+)>>[^\n]*\n([\s\S]*)\n<</\1>>")
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|credential|password|secret|token)\b\s*[:=]\s*[^\s,;]+"
)


class AKSDiagnosis(BaseModel):
    """Evidence-bound response for a Kubernetes investigation."""

    summary: str = Field(alias="Summary")
    observed_evidence: list[str] = Field(alias="Observed evidence")
    likely_root_causes: list[str] = Field(alias="Likely root causes")
    recommended_next_diagnostic_step: str = Field(alias="Recommended next diagnostic step")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    _is_fallback: bool = PrivateAttr(default=False)


@dataclass
class EvidenceStep:
    name: str
    operation: str
    resource: str
    args: str
    outcome: str
    payload: Any = None
    raw: str = ""
    note: str = ""
    parser_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TroubleshootingEvidence:
    """Collected observations; this class intentionally makes no diagnosis."""

    steps: list[EvidenceStep] = field(default_factory=list)
    deployment: dict[str, Any] = field(default_factory=dict)
    selector: str | None = None
    deployment_health: dict[str, Any] = field(default_factory=dict)
    pod_health: dict[str, Any] = field(default_factory=dict)
    service_health: dict[str, Any] = field(default_factory=dict)
    load_balancer_ingress: dict[str, Any] = field(
        default_factory=lambda: {"state": "not_collected"}
    )
    network_policy_evidence: dict[str, Any] = field(
        default_factory=lambda: {"state": "not_collected"}
    )
    stop_reason: str | None = None
    collection_calls: int = 0
    budget_exhausted: bool = False

    def model_input(self) -> str:
        """Serialize the focused report, never raw tool transport or full pod objects."""
        try:
            return bounded_evidence(task_manager_evidence_report(self))
        except (TypeError, ValueError, RecursionError):
            return json.dumps({"state": "unknown", "reason": "evidence_serialization_failed",
                               "evidence_truncated": True, "incomplete": True})


def bounded_evidence(report):
    """Preserve structured summaries while deterministically reducing large lists/text."""
    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)

    report = {**report, "evidence_truncated": False}
    serialized = encode(report)
    if len(serialized) <= MAX_AKS_EVIDENCE_CHARS:
        return serialized

    def reduce(value, count, width, depth=0):
        if depth > 12:
            return {"state": "unknown", "reason": "evidence_depth_limit"}
        if isinstance(value, dict):
            return {k: reduce(v, count, width, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [reduce(v, count, width, depth + 1) for v in value[:count]]
        if isinstance(value, str):
            return value[:width]
        return value

    for count, width in ((8, 500), (4, 256), (2, 128), (1, 64)):
        candidate = reduce(report, count, width)
        candidate.update(evidence_truncated=True,
                         truncation_notice="Evidence lists/text were shortened; omitted evidence is unavailable for diagnosis.")
        # Keep the complete collection decision and failure provenance when possible.
        candidate["decision"] = report.get("decision", {})
        serialized = encode(candidate)
        if len(serialized) <= MAX_AKS_EVIDENCE_CHARS:
            return serialized
    return encode({"evidence_truncated": True, "state": "unknown",
                   "truncation_notice": "Evidence exceeded the report limit; diagnosis evidence is incomplete."})


def unwrap_tool_content(content: Any) -> str:
    """Extract text blocks while preserving MCP output as untrusted evidence."""
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return str(content)
    texts = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = str(block.get("text", ""))
        match = _UNTRUSTED_ENVELOPE.fullmatch(text)
        texts.append(match.group(2) if match else text)
    return "\n".join(texts)


def _safe_keys(payload: dict[str, Any]) -> list[str]:
    sensitive = re.compile(r"(?i)(api[_-]?key|authorization|credential|password|secret|token)")
    return ["[REDACTED_KEY]" if sensitive.search(str(key)) else str(key)
            for key in sorted(payload)[:30]]


def normalize_kubernetes_response(content: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Accept only the direct JSON-object contract used by the existing collector."""
    transport_kind = "text" if isinstance(content, str) else "text_blocks" if isinstance(content, list) else type(content).__name__
    raw = unwrap_tool_content(content)
    metadata = {
        "transport_content_kind": transport_kind,
        "json_parse_state": "not_attempted",
        "top_level_type": None,
        "top_level_keys": [],
        "item_count": None,
    }
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        metadata.update({"state": "unknown", "reason": "invalid_json", "json_parse_state": "invalid_json"})
        return None, metadata
    metadata["json_parse_state"] = "parsed"
    if isinstance(payload, dict):
        metadata["top_level_type"] = "object"
        metadata["top_level_keys"] = _safe_keys(payload)
        if isinstance(payload.get("items"), list):
            metadata["item_count"] = len(payload["items"])
        metadata["state"] = "parsed"
        return payload, metadata
    metadata["top_level_type"] = "array" if isinstance(payload, list) else type(payload).__name__
    metadata.update({"state": "unknown", "reason": "unsupported_response_format"})
    return None, metadata


def resource_missing(payload: Any) -> bool:
    """Return true only for an explicit, parsed Kubernetes not-found response."""
    if isinstance(payload, dict):
        return payload.get("reason") == "NotFound" or payload.get("code") == 404
    return False


def _unknown_state(reason: str, parser_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = dict(parser_metadata or {})
    metadata.pop("state", None)
    return {"state": "unknown", "reason": reason, **metadata}


def deployment_selector(deployment: Any) -> str | None:
    """Return an exact kubectl label selector when matchLabels is available."""
    if not isinstance(deployment, dict):
        return None
    spec = deployment.get("spec")
    selector = spec.get("selector") if isinstance(spec, dict) else None
    labels = selector.get("matchLabels") if isinstance(selector, dict) else None
    if not isinstance(labels, dict) or not labels:
        return None
    label = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?"
    prefix = r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/"
    if not all(isinstance(key, str) and re.fullmatch(f"(?:{prefix})?{label}", key)
               and isinstance(value, str) and (value == "" or re.fullmatch(label, value))
               for key, value in labels.items()):
        return None
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def _invalid_resource(payload: Any, resource: str) -> str | None:
    """Reject unusable shapes before evaluators or report builders consume them."""
    if not isinstance(payload, dict):
        return "unsupported_response_format"
    if resource_missing(payload):
        return None
    listing = resource in {"pods", "replicasets", "events", "endpointslices", "networkpolicies", "nodes"}
    items = payload.get("items") if listing else [payload]
    if not isinstance(items, list):
        return "unsupported_response_format"
    if resource == "events":
        # Event-specific validation preserves valid entries in mixed responses.
        return None
    for item in items:
        if not isinstance(item, dict):
            return "invalid_resource_object"
        metadata = item.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"]:
            return "missing_resource_metadata"
        if resource not in CLUSTER_SCOPED_RESOURCES:
            if metadata.get("namespace") != NAMESPACE:
                return "namespace_mismatch" if "namespace" in metadata else "namespace_unverified"
        if resource == "deployments" and metadata["name"] != DEPLOYMENT:
            return "workload_identity_mismatch"
        if resource in {"deployments", "replicasets", "pods"}:
            status = item.get("status")
            if not isinstance(status, dict) or not status:
                return "missing_workload_status"
            if resource != "pods" and not isinstance(item.get("spec"), dict):
                return "missing_workload_spec"
            counts = [status.get(key, 0) for key in ("replicas", "readyReplicas", "availableReplicas")]
            if resource != "pods":
                counts.append(item["spec"].get("replicas", 1))
            if any(type(count) is not int or count < 0 for count in counts):
                return "invalid_replica_counts"
            conditions = status.get("conditions", [])
            if not isinstance(conditions, list) or any(
                not isinstance(c, dict) or not isinstance(c.get("type"), str)
                or c.get("status") not in ("True", "False", "Unknown") for c in conditions
            ):
                return "invalid_conditions"
            if resource == "pods":
                if "phase" in status and not isinstance(status["phase"], str):
                    return "invalid_pod_phase"
                if not any(c["type"] == "Ready" for c in conditions):
                    return "insufficient_pod_evidence"
                containers = status.get("containerStatuses", [])
                if not isinstance(containers, list):
                    return "invalid_container_statuses"
                init_containers = status.get("initContainerStatuses", [])
                if not isinstance(init_containers, list):
                    return "invalid_container_statuses"
                for container in containers + init_containers:
                    if not isinstance(container, dict) or type(container.get("restartCount", 0)) is not int or container.get("restartCount", 0) < 0:
                        return "invalid_container_statuses"
                    state = container.get("state", {})
                    if not isinstance(state, dict) or any(
                        key in state and not isinstance(state[key], dict) for key in ("waiting", "terminated", "running")
                    ):
                        return "invalid_container_state"
                    if any("reason" in state.get(key, {}) and not isinstance(state[key]["reason"], str)
                           for key in ("waiting", "terminated")):
                        return "invalid_container_state"
        # Shape guards only: preserve existing downstream health interpretation.
        if any(key in item and not isinstance(item[key], dict) for key in ("spec", "status", "involvedObject")):
            return "invalid_nested_fields"
        if resource == "endpoints":
            subsets = item.get("subsets", [])
            if not isinstance(subsets, list) or any(
                not isinstance(s, dict) or not isinstance(s.get("addresses", []), list)
                or any(not isinstance(a, dict) for a in s.get("addresses", [])) for s in subsets
            ):
                return "invalid_nested_fields"
        if resource == "endpointslices":
            endpoints = item.get("endpoints", [])
            if not isinstance(endpoints, list) or any(
                not isinstance(e, dict) or not isinstance(e.get("conditions", {}), dict)
                or not isinstance(e.get("addresses", []), list) for e in endpoints
            ):
                return "invalid_nested_fields"
    return None


def evaluate_deployment(deployment: Any) -> dict[str, Any]:
    if resource_missing(deployment):
        return {"state": "missing", "reason": "Deployment was not found."}
    if not isinstance(deployment, dict):
        return {"state": "unknown", "reason": "Deployment output was not structured JSON."}
    invalid = _invalid_resource(deployment, "deployments")
    if invalid:
        return _unknown_state(invalid)
    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    desired = spec.get("replicas", 1)
    available = status.get("availableReplicas", 0)
    ready = status.get("readyReplicas", 0)
    conditions = status.get("conditions", [])
    progressing_failure = any(
        item.get("type") == "Progressing" and item.get("status") == "False"
        for item in conditions if isinstance(item, dict)
    )
    if not all(isinstance(value, int) for value in (desired, available, ready)):
        return {"state": "unknown", "reason": "Replica counts were unavailable."}
    if desired == 0:
        return {"state": "scaled_to_zero", "desired": desired, "available": available, "ready": ready}
    if available < desired or ready < desired or progressing_failure:
        return {"state": "unhealthy", "desired": desired, "available": available, "ready": ready}
    return {"state": "healthy", "desired": desired, "available": available, "ready": ready}


def _container_evidence(container: Any, *, init=False) -> dict[str, Any]:
    """Whitelist current and historical state; never include env, spec, or IDs."""
    result = {"kind": "init" if init else "application", "readiness": "Unknown",
              "evaluation": "unknown", "active_failure": None}
    if not isinstance(container, dict):
        return result
    if isinstance(container.get("name"), str):
        result["name"] = _redact_text(container["name"])
    ready = container.get("ready")
    result["readiness"] = "Ready" if ready is True else "Not Ready" if ready is False else "Unknown"
    restarts = container.get("restartCount")
    result["restart_count"] = restarts if type(restarts) is int and restarts >= 0 else None

    def state_summary(value):
        if not isinstance(value, dict) or len(value) != 1:
            return {"state": "unknown"}
        state, details = next(iter(value.items()))
        if state not in {"waiting", "running", "terminated"} or not isinstance(details, dict):
            return {"state": "unknown"}
        summary = {"state": state}
        for key in ("reason", "message", "startedAt", "finishedAt"):
            if key in details:
                if not isinstance(details[key], str):
                    return {"state": "unknown"}
                summary[key] = _redact_text(details[key])
        for key in ("exitCode", "signal"):
            if key in details:
                if type(details[key]) is not int:
                    return {"state": "unknown"}
                summary[key] = details[key]
        if state == "terminated" and "exitCode" not in summary:
            return {"state": "unknown"}
        return summary

    current = state_summary(container.get("state"))
    result["current_state"] = current
    result["last_state"] = state_summary(container.get("lastState", {}))
    if not isinstance(container.get("name"), str) or type(ready) is not bool or result["restart_count"] is None:
        return result
    if current["state"] == "waiting":
        result.update(evaluation="unhealthy", active_failure=True)
    elif current["state"] == "terminated":
        failed = current["exitCode"] != 0 or current.get("signal", 0) != 0
        result.update(evaluation="unhealthy" if failed else "completed", active_failure=failed)
    elif current["state"] == "running":
        result.update(evaluation="healthy" if ready else "unhealthy", active_failure=not ready)
    result["restart_context"] = (
        "historical" if restarts > 0 and result["evaluation"] in {"healthy", "completed"} else
        "current_failure_with_restarts" if restarts > 0 and result["evaluation"] == "unhealthy" else
        "none" if restarts == 0 else "unknown"
    )
    return result


def evaluate_pods(payload: Any, parser_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if parser_metadata and parser_metadata.get("state") == "unknown":
        return _unknown_state(parser_metadata.get("reason", "unsupported_response_format"), parser_metadata)
    if not isinstance(payload, dict) or "items" not in payload or not isinstance(payload["items"], list):
        return _unknown_state("unsupported_response_format", parser_metadata)
    invalid = _invalid_resource(payload, "pods")
    if invalid:
        return _unknown_state(invalid, parser_metadata)
    items = payload["items"]
    if not items:
        # A successfully parsed Kubernetes List with no items is affirmative absence.
        return {"state": "missing", "pods": [], "unhealthy_pods": []}
    pods, unhealthy, unknown = [], [], []
    for pod in items:
        metadata = pod.get("metadata") if isinstance(pod, dict) else None
        status = pod.get("status") if isinstance(pod, dict) else None
        if (not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str)
                or not isinstance(status, dict)
                or not isinstance(status.get("conditions", []), list)
                or not isinstance(status.get("containerStatuses", []), list)):
            return _unknown_state("insufficient_pod_evidence", parser_metadata)
        name = metadata["name"]
        ready_condition = next((c["status"] for c in status["conditions"] if c["type"] == "Ready"), "Unknown")
        relevant = [c for c in status["conditions"] if c["type"] in {"Ready", "Initialized", "PodScheduled"}]
        uncertain_conditions = any(c["status"] == "Unknown" for c in relevant) or len({c["type"] for c in relevant}) != len(relevant)
        ready = True if ready_condition == "True" else False if ready_condition == "False" else None
        containers = [_container_evidence(c) for c in status.get("containerStatuses", [])]
        init_containers = [_container_evidence(c, init=True) for c in status.get("initContainerStatuses", [])]
        incomplete_status = False
        for spec_key, observed in (("containers", containers), ("initContainers", init_containers)):
            declared = pod.get("spec", {}).get(spec_key)
            if declared is not None:
                if not isinstance(declared, list) or any(
                    not isinstance(c, dict) or not isinstance(c.get("name"), str) for c in declared
                ):
                    incomplete_status = True
                else:
                    incomplete_status |= {c["name"] for c in declared} != {c.get("name") for c in observed}
            incomplete_status |= len({c.get("name") for c in observed}) != len(observed)
        restarts = sum(item.get("restartCount", 0) for item in status.get("containerStatuses", [])
                       if isinstance(item, dict) and isinstance(item.get("restartCount", 0), int))
        waiting = [item.get("state", {}).get("waiting", {}).get("reason")
                   for item in status.get("containerStatuses", []) if isinstance(item, dict)]
        item = {"name": name, "phase": status.get("phase"), "ready": ready,
                "restarts": restarts, "waiting_reasons": [_redact_text(reason) for reason in waiting if reason],
                "containers": containers, "init_containers": init_containers,
                "conditions": _conditions(status.get("conditions", []))}
        pods.append(item)
        if ready is None or uncertain_conditions or incomplete_status or not containers or any(c["evaluation"] == "unknown" for c in containers + init_containers):
            item["state"] = "unknown"
            unknown.append(item)
        elif any(c["status"] == "False" for c in relevant) or any(c["evaluation"] == "unhealthy" for c in containers + init_containers):
            item["state"] = "unhealthy"
            unhealthy.append(item)
        elif any(c["evaluation"] == "completed" for c in containers):
            item["state"] = "unknown"  # Completed does not establish workload readiness.
            unknown.append(item)
        else:
            item["state"] = "healthy"
    return {"state": "unknown" if unknown else "unhealthy" if unhealthy else "healthy", "pods": pods,
            "unhealthy_pods": unhealthy, "unknown_pods": unknown}


def evaluate_service_endpoints(
    service: Any,
    endpoints: Any,
    endpoint_slices: Any,
    service_metadata: dict[str, Any] | None = None,
    endpoints_metadata: dict[str, Any] | None = None,
    endpoint_slices_metadata: dict[str, Any] | None = None,
    deployment: Any = None,
    pods: Any = None,
) -> dict[str, Any]:
    return network_evidence(service, endpoints, endpoint_slices, service_metadata,
                            endpoints_metadata, endpoint_slices_metadata, deployment, pods)


def evaluate_load_balancer_ingress(
    service: Any, parser_metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Extract only assignment status from a parsed Service LoadBalancer status."""
    if parser_metadata and parser_metadata.get("state") == "unknown":
        return _unknown_state(parser_metadata.get("reason", "unsupported_response_format"), parser_metadata)
    if resource_missing(service):
        return {"state": "missing", "external_ingress_assigned": False,
                "ip_assigned": False, "hostname_assigned": False, "ingress_count": 0}
    if not isinstance(service, dict) or not isinstance(service.get("spec"), dict):
        return _unknown_state("insufficient_service_evidence", parser_metadata)
    if service["spec"].get("type") != "LoadBalancer":
        return {"state": "not_load_balancer", "external_ingress_assigned": False,
                "ip_assigned": False, "hostname_assigned": False, "ingress_count": 0}
    status = service.get("status", {})
    load_balancer = status.get("loadBalancer", {}) if isinstance(status, dict) else None
    ingress = load_balancer.get("ingress", []) if isinstance(load_balancer, dict) else None
    if not isinstance(ingress, list):
        return _unknown_state("insufficient_load_balancer_evidence", parser_metadata)
    ip_assigned = hostname_assigned = False
    for entry in ingress:
        if not isinstance(entry, dict):
            return _unknown_state("insufficient_load_balancer_evidence", parser_metadata)
        ip, hostname = entry.get("ip"), entry.get("hostname")
        if ip is not None and not isinstance(ip, str):
            return _unknown_state("insufficient_load_balancer_evidence", parser_metadata)
        if hostname is not None and not isinstance(hostname, str):
            return _unknown_state("insufficient_load_balancer_evidence", parser_metadata)
        ip_assigned = ip_assigned or bool(ip)
        hostname_assigned = hostname_assigned or bool(hostname)
    assigned = ip_assigned or hostname_assigned
    return {"state": "assigned" if assigned else "unassigned",
            "external_ingress_assigned": assigned, "ip_assigned": ip_assigned,
            "hostname_assigned": hostname_assigned, "ingress_count": len(ingress)}


def evaluate_network_policies(
    payload: Any, parser_metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a focused, rule-content-free NetworkPolicy summary."""
    if parser_metadata and parser_metadata.get("state") == "unknown":
        return _unknown_state(parser_metadata.get("reason", "unsupported_response_format"), parser_metadata)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return _unknown_state("unsupported_response_format", parser_metadata)
    items = payload["items"]
    if not items:
        return {"state": "none", "policy_count": 0, "policies": []}
    policies = []
    for policy in items:
        metadata = policy.get("metadata") if isinstance(policy, dict) else None
        spec = policy.get("spec") if isinstance(policy, dict) else None
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not isinstance(spec, dict):
            return _unknown_state("insufficient_network_policy_evidence", parser_metadata)
        selector = spec.get("podSelector", {})
        ingress, egress = spec.get("ingress", []), spec.get("egress", [])
        policy_types = spec.get("policyTypes")
        match_labels = selector.get("matchLabels", {}) if isinstance(selector, dict) else None
        if (not isinstance(selector, dict) or not isinstance(match_labels, dict)
                or not all(isinstance(key, str) and isinstance(value, str)
                           for key, value in match_labels.items())
                or not isinstance(ingress, list) or not isinstance(egress, list)
                or (policy_types is not None and (
                    not isinstance(policy_types, list) or not all(
                        isinstance(policy_type, str) and policy_type in {"Ingress", "Egress"}
                        for policy_type in policy_types
                    )))):
            return _unknown_state("insufficient_network_policy_evidence", parser_metadata)
        effective_types = policy_types if policy_types is not None else (
            ["Ingress", "Egress"] if "egress" in spec else ["Ingress"]
        )
        policies.append({
            "name": metadata["name"],
            "pod_selector": {"match_labels": dict(sorted(match_labels.items()))},
            "policy_types": effective_types,
            "ingress_rule_count": len(ingress),
            "egress_rule_count": len(egress),
        })
    return {"state": "present", "policy_count": len(policies), "policies": policies}


class _CollectionBudgetReached(Exception):
    pass


async def collect_task_manager_evidence(tools, log=print) -> TroubleshootingEvidence:
    evidence = TroubleshootingEvidence()
    try:
        return await _collect_task_manager_evidence(tools, evidence, log)
    except _CollectionBudgetReached:
        from src.observability import emit
        emit("execution_stopped", outcome="stopped", reason_code="budget_exhausted", remaining_budget=0)
        evidence.budget_exhausted = True
        evidence.stop_reason = "AKS collection budget exhausted; remaining evidence was not collected. This is not a Kubernetes health failure."
        return evidence


async def _collect_task_manager_evidence(tools, evidence, log) -> TroubleshootingEvidence:
    """Collect the minimum evidence path and stop after a clear upstream failure."""
    by_name = {tool.name: tool for tool in tools}
    if "kubectl_resources" not in by_name:
        raise RuntimeError("AKS MCP did not expose the approved kubectl_resources tool")

    def hard_failure(error):
        from src.request_budget import RequestBoundExceeded
        if isinstance(error, RequestBoundExceeded):
            return True
        return isinstance(error, (PermissionError, ConfigurationError, MCPRuntimeError)) or any(
            marker in f"{type(error).__name__} {error}".lower() for marker in (
                "authentication", "authorization", "unauthorized", "forbidden", "credential",
                "permission", "security", "access denied", "api key", "api_key", "bearer token",
                "token expired", "401", "403",
            )
        )

    async def invoke(name: str, operation: str, resource: str, args: str, step_name: str):
        call = {"name": name, "args": {"operation": operation, "resource": resource, "args": args},
                "id": str(uuid4()), "type": "tool_call"}
        enforce_read_only_policy(call)
        if evidence.collection_calls >= MAX_AKS_COLLECTION_CALLS:
            evidence.steps.append(EvidenceStep(step_name, operation, resource, args, "not_collected",
                                               parser_metadata={"state": "unknown", "reason": "collection_budget_exhausted"}))
            raise _CollectionBudgetReached
        log(diagnostic_line(event="collection_allowed", tool_name=name, operation=operation, outcome="allowed"))
        evidence.collection_calls += 1
        try:
            from src.observability import mcp_call
            result = await mcp_call(call, lambda: by_name[name].ainvoke(call),
                                    remaining_budget=MAX_AKS_COLLECTION_CALLS-evidence.collection_calls)
        except Exception as error:
            if hard_failure(error):
                raise
            evidence.steps.append(EvidenceStep(
                step_name, operation, resource, args, "error",
                parser_metadata={"state": "unknown", "reason": "tool_transport_failure"},
            ))
            return None
        if not isinstance(result, ToolMessage):
            evidence.steps.append(EvidenceStep(
                step_name, operation, resource, args, "unknown",
                parser_metadata={"state": "unknown", "reason": "invalid_tool_response"},
            ))
            return None
        # Raw transport is needed only to produce a whitelisted describe summary.  It
        # is not retained for ordinary get responses or diagnostic parser metadata.
        raw = unwrap_tool_content(result.content) if operation == "describe" else ""
        payload, parser_metadata = normalize_kubernetes_response(result.content)
        if result.status == "error" and hard_failure(RuntimeError(unwrap_tool_content(result.content))):
            safe = classify_error(RuntimeError(unwrap_tool_content(result.content)), boundary="tool")
            raise PermissionError(safe.user_message())
        if result.status == "error" and not resource_missing(payload):
            payload, raw = None, ""
            parser_metadata = {"state": "unknown", "reason": "tool_failure"}
        elif payload is not None:
            invalid = _invalid_resource(payload, resource)
            if invalid:
                payload, raw = None, ""
                parser_metadata.update(state="unknown", reason=invalid)
        if operation == "describe" and raw:
            namespaces = re.findall(r"^\s*Namespace:\s*(\S+)", raw, re.MULTILINE)
            if any(namespace != NAMESPACE for namespace in namespaces):
                raw, payload = "", None
                parser_metadata.update(state="unknown", reason="namespace_mismatch")
        outcome = "missing" if resource_missing(payload) else "error" if result.status == "error" else (
            "unknown" if parser_metadata["state"] == "unknown" else
            "missing" if resource_missing(payload) else "ok"
        )
        step = EvidenceStep(step_name, operation, resource, args, outcome, payload, raw,
                            parser_metadata=parser_metadata)
        evidence.steps.append(step)
        return payload

    deployment = await invoke("kubectl_resources", "get", "deployments",
                              f"{DEPLOYMENT} --namespace {NAMESPACE} -o json", "deployment")
    evidence.deployment = deployment if isinstance(deployment, dict) else {}
    deployment_step = _step(evidence, "deployment")
    if deployment_step and deployment_step.parser_metadata.get("state") == "unknown":
        evidence.deployment_health = {
            "state": "unknown", "reason": deployment_step.parser_metadata["reason"],
            **deployment_step.parser_metadata,
        }
        evidence.stop_reason = "Deployment evidence could not be interpreted; downstream checks were not performed."
        return evidence
    evidence.deployment_health = evaluate_deployment(deployment)
    if evidence.deployment_health["state"] == "missing":
        await invoke("kubectl_resources", "get", "events", f"--namespace {NAMESPACE} -o json", "events")
        evidence.stop_reason = "Deployment is missing; workload, Service, and endpoint checks cannot establish an application path."
        return evidence

    evidence.selector = deployment_selector(deployment)
    if not evidence.selector:
        evidence.pod_health = _unknown_state("workload_correlation_unavailable")
        evidence.stop_reason = "Deployment has no usable matchLabels selector; workload correlation could not be established. Downstream checks were not performed."
        return evidence
    selector_args = (
        f"--namespace {NAMESPACE}"
        + (f" -l {evidence.selector}" if evidence.selector else "")
        + " -o json"
    )
    await invoke("kubectl_resources", "get", "replicasets", selector_args, "replicasets")
    replica_step = _step(evidence, "replicasets")
    if replica_step and replica_step.outcome in {"unknown", "error", "missing"}:
        evidence.pod_health = _unknown_state("replicaset_evidence_unavailable")
        evidence.stop_reason = "ReplicaSet evidence was unavailable or invalid; downstream checks were not performed."
        return evidence
    pods = await invoke("kubectl_resources", "get", "pods", selector_args, "pods")
    pod_step = _step(evidence, "pods")
    evidence.pod_health = evaluate_pods(pods, pod_step.parser_metadata if pod_step else None)
    if evidence.pod_health["state"] == "unhealthy":
        for pod in evidence.pod_health["unhealthy_pods"]:
            await invoke("kubectl_resources", "describe", "pods",
                         f"{pod['name']} --namespace {NAMESPACE}", f"pod:{pod['name']}")
    await invoke("kubectl_resources", "get", "events", f"--namespace {NAMESPACE} -o json", "events")

    if evidence.pod_health["state"] == "unknown":
        evidence.stop_reason = "Pod evidence could not be interpreted; downstream checks were not performed."
        return evidence
    if evidence.deployment_health["state"] in {"unhealthy", "scaled_to_zero"} or evidence.pod_health["state"] in {"unhealthy", "missing"}:
        evidence.stop_reason = (
            "Deployment is scaled to zero; downstream Service checks were not performed."
            if evidence.deployment_health["state"] == "scaled_to_zero" else
            "Workload evidence is unhealthy; collected pod details and Events before downstream Service checks."
        )
        return evidence

    service = await invoke("kubectl_resources", "get", "services",
                           f"{SERVICE} --namespace {NAMESPACE} -o json", "service")
    service_step = _step(evidence, "service")
    if service_step and service_step.parser_metadata.get("state") == "unknown":
        evidence.load_balancer_ingress = evaluate_load_balancer_ingress(
            service, service_step.parser_metadata
        )
        evidence.service_health = evaluate_service_endpoints(
            service, None, None, service_metadata=service_step.parser_metadata
        )
        evidence.stop_reason = "Service evidence could not be interpreted; endpoint checks were not performed."
        return evidence
    if resource_missing(service):
        evidence.load_balancer_ingress = evaluate_load_balancer_ingress(service)
        evidence.service_health = evaluate_service_endpoints(service, None, None)
        evidence.stop_reason = "Service is missing; endpoint checks would not add application-path evidence."
        return evidence
    evidence.load_balancer_ingress = evaluate_load_balancer_ingress(service)
    endpoints = await invoke("kubectl_resources", "get", "endpoints",
                             f"{SERVICE} --namespace {NAMESPACE} -o json", "endpoints")
    endpoint_step = _step(evidence, "endpoints")
    endpoint_slices = await invoke("kubectl_resources", "get", "endpointslices",
                                   f"--namespace {NAMESPACE} -l kubernetes.io/service-name={SERVICE} -o json",
                                   "endpoint_slices")
    slice_step = _step(evidence, "endpoint_slices")
    evidence.service_health = evaluate_service_endpoints(
        service, endpoints, endpoint_slices,
        endpoints_metadata=endpoint_step.parser_metadata if endpoint_step else None,
        endpoint_slices_metadata=slice_step.parser_metadata if slice_step else None,
        deployment=deployment, pods=pods,
    )
    if evidence.service_health["state"] == "unknown":
        evidence.stop_reason = "Network readiness could not be established from the collected endpoint evidence."
        return evidence
    network_policies = await invoke("kubectl_resources", "get", "networkpolicies",
                                    f"--namespace {NAMESPACE} -o json", "network_policies")
    network_policy_step = _step(evidence, "network_policies")
    evidence.network_policy_evidence = evaluate_network_policies(
        network_policies, network_policy_step.parser_metadata if network_policy_step else None
    )
    if evidence.network_policy_evidence["state"] == "unknown":
        evidence.stop_reason = "NetworkPolicy evidence could not be interpreted."
    return evidence


def is_task_manager_troubleshooting_question(question: str) -> bool:
    normalized = question.lower().replace("-", " ")
    if "task manager" not in normalized:
        return False
    # Source/configuration investigations stay with their existing routes, even
    # when they mention the Kubernetes workload they deploy. Mixed intent is
    # deliberately left to the generic route rather than guessed here.
    if re.search(r"\b(?:repositories|repository|repo|pipelines?|terraform)\b", normalized):
        return False
    existing_intent = any(phrase in normalized for phrase in (
        "not working", "why", "troubleshoot", "troubleshooting", "diagnose", "diagnosis",
    ))
    # Health checks must name both this workload and a Kubernetes context.
    health_check = (
        re.search(r"\b(?:aks|cluster|kubernetes|deployments?|pods?|services?|workloads?)\b", normalized)
        and re.search(r"\b(?:check|inspect|health|healthy|unhealthy|readiness|status|"
                      r"investigate|investigation|availability|connectivity|problems?|evidence)\b", normalized)
    )
    return existing_intent or bool(health_check)


def is_aks_cluster_health_question(question: str) -> bool:
    """True for general, cluster-wide AKS health requests (no specific workload name required).

    Does not fire when the question already matches the Task Manager troubleshooting
    predicate, or when it mentions ADO pipelines / repositories / Terraform (those
    routes own those intents).  Requires both a cluster-context keyword and an
    explicit health/investigation intent to avoid routing casual mentions.
    """
    normalized = question.lower().replace("-", " ")
    # Source / config / pipeline / terraform intents stay on their own routes.
    if re.search(r"\b(?:repositories|repository|repo|pipelines?|terraform)\b", normalized):
        return False
    # Must reference the cluster/infrastructure context.
    cluster_context = re.search(r"\b(?:aks|cluster|kubernetes)\b", normalized)
    # Must express a health or investigation intent.
    health_intent = re.search(
        r"\b(?:health|healthy|unhealthy|node.?status|pod.?status|workloads?|"
        r"investigate|check|inspect|status|problems?|issues?|diagnose|troubleshoot)\b",
        normalized,
    )
    return bool(cluster_context and health_intent)


CLUSTER_HEALTH_SCOPE = {
    "node_health_scope": "cluster-wide",
    "pod_health_scope": "namespace: default",
    "scope_limitations": (
        "Node health is checked cluster-wide across all cluster nodes. "
        "Pod health is checked only in the authorized 'default' namespace. "
        "System namespaces (including kube-system) and other namespaces were not inspected by security policy."
    ),
}


@dataclass
class ClusterHealthEvidence:
    """Lightweight cluster-wide health observations; intentionally makes no diagnosis."""

    steps: list[EvidenceStep] = field(default_factory=list)
    cluster_state: dict[str, Any] = field(default_factory=dict)
    node_health: dict[str, Any] = field(default_factory=dict)
    pod_health: dict[str, Any] = field(default_factory=dict)
    inspection_scope: dict[str, Any] = field(default_factory=lambda: dict(CLUSTER_HEALTH_SCOPE))
    stop_reason: str | None = None
    collection_calls: int = 0
    budget_exhausted: bool = False

    def model_input(self) -> str:
        """Serialize the focused cluster-health report."""
        report = {
            "inspection_scope": self.inspection_scope,
            "cluster_state": self.cluster_state,
            "node_health": self.node_health,
            "pod_health": self.pod_health,
            "decision": {
                "stop_reason": self.stop_reason,
                "collection_calls": self.collection_calls,
                "budget_exhausted": self.budget_exhausted,
            },
        }
        return bounded_evidence(report)


MAX_CLUSTER_HEALTH_CALLS = 4


def _evaluate_nodes(payload: Any, parser_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summarise node readiness from a kubectl get nodes -o json response."""
    if parser_metadata and parser_metadata.get("state") == "unknown":
        return _unknown_state(parser_metadata.get("reason", "unsupported_response_format"), parser_metadata)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return _unknown_state("unsupported_response_format", parser_metadata)
    items = payload["items"]
    if not items:
        return {"state": "missing", "nodes": [], "unhealthy_nodes": []}
    nodes, unhealthy = [], []
    for node in items:
        if not isinstance(node, dict):
            return _unknown_state("invalid_node_object", parser_metadata)
        metadata = node.get("metadata", {})
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not name:
            return _unknown_state("missing_node_metadata", parser_metadata)
        status = node.get("status", {}) if isinstance(node, dict) else {}
        conditions = status.get("conditions", []) if isinstance(status, dict) else []
        ready_condition = next(
            (c.get("status") for c in conditions if isinstance(c, dict) and c.get("type") == "Ready"),
            None,
        )
        ready = ready_condition == "True"
        item = {
            "name": _redact_text(name),
            "ready": ready,
            "ready_condition": ready_condition,
            "conditions": _conditions(conditions),
        }
        nodes.append(item)
        if not ready:
            unhealthy.append(item)
    state = "healthy" if not unhealthy else "unhealthy"
    return {"state": state, "node_count": len(nodes), "nodes": nodes, "unhealthy_nodes": unhealthy}


async def collect_aks_cluster_health_evidence(tools, log=print) -> ClusterHealthEvidence:
    """Deterministic read-only AKS cluster-health collector.

    Tool call sequence (max MAX_CLUSTER_HEALTH_CALLS):
      1. az_aks_operations / show  — cluster provisioning state
      2. kubectl_resources / get nodes  — node readiness
      3. kubectl_resources / get pods (namespace default)  — sampled pod health

    All calls go through enforce_read_only_policy.  No MCP writes are performed.
    """
    evidence = ClusterHealthEvidence()
    by_name = {tool.name: tool for tool in tools}
    if "kubectl_resources" not in by_name:
        raise RuntimeError("AKS MCP did not expose the approved kubectl_resources tool")

    def hard_failure(error):
        from src.request_budget import RequestBoundExceeded
        if isinstance(error, RequestBoundExceeded):
            return True
        return isinstance(error, (PermissionError, ConfigurationError, MCPRuntimeError)) or any(
            marker in f"{type(error).__name__} {error}".lower() for marker in (
                "authentication", "authorization", "unauthorized", "forbidden", "credential",
                "permission", "security", "access denied", "api key", "api_key", "bearer token",
                "token expired", "401", "403",
            )
        )

    async def invoke(name: str, operation: str, resource: str, args: str, step_name: str):
        call = {"name": name, "args": {"operation": operation, "resource": resource, "args": args},
                "id": str(uuid4()), "type": "tool_call"}
        enforce_read_only_policy(call)
        if evidence.collection_calls >= MAX_CLUSTER_HEALTH_CALLS:
            evidence.steps.append(EvidenceStep(step_name, operation, resource, args, "not_collected",
                                               parser_metadata={"state": "unknown", "reason": "collection_budget_exhausted"}))
            evidence.budget_exhausted = True
            return None
        log(diagnostic_line(event="collection_allowed", tool_name=name, operation=operation, outcome="allowed"))
        evidence.collection_calls += 1
        try:
            from src.observability import mcp_call
            result = await mcp_call(call, lambda: by_name[name].ainvoke(call),
                                    remaining_budget=MAX_CLUSTER_HEALTH_CALLS - evidence.collection_calls)
        except Exception as error:
            if hard_failure(error):
                raise
            evidence.steps.append(EvidenceStep(
                step_name, operation, resource, args, "error",
                parser_metadata={"state": "unknown", "reason": "tool_transport_failure"},
            ))
            return None
        if not isinstance(result, ToolMessage):
            evidence.steps.append(EvidenceStep(
                step_name, operation, resource, args, "unknown",
                parser_metadata={"state": "unknown", "reason": "invalid_tool_response"},
            ))
            return None
        payload, parser_metadata = normalize_kubernetes_response(result.content)
        if result.status == "error" and hard_failure(RuntimeError(unwrap_tool_content(result.content))):
            safe = classify_error(RuntimeError(unwrap_tool_content(result.content)), boundary="tool")
            raise PermissionError(safe.user_message())
        if result.status == "error":
            payload = None
            parser_metadata = {"state": "unknown", "reason": "tool_failure"}
        elif payload is not None:
            invalid = _invalid_resource(payload, resource)
            if invalid:
                payload = None
                parser_metadata.update(state="unknown", reason=invalid)
        outcome = (
            "error" if result.status == "error" else
            "unknown" if parser_metadata.get("state") == "unknown" else "ok"
        )
        step = EvidenceStep(step_name, operation, resource, args, outcome, payload,
                            parser_metadata=parser_metadata)
        evidence.steps.append(step)
        return payload

    # --- Step 1: cluster provisioning state (az_aks_operations/show) ---
    if "az_aks_operations" in by_name:
        call = {"name": "az_aks_operations",
                "args": {"operation": "show", "resource": "", "args": ""},
                "id": str(uuid4()), "type": "tool_call"}
        try:
            enforce_read_only_policy(call)
            evidence.collection_calls += 1
            from src.observability import mcp_call
            result = await mcp_call(call, lambda: by_name["az_aks_operations"].ainvoke(call),
                                    remaining_budget=MAX_CLUSTER_HEALTH_CALLS - evidence.collection_calls)
            if isinstance(result, ToolMessage) and result.status != "error":
                raw = unwrap_tool_content(result.content)
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        evidence.cluster_state = {
                            "name": parsed.get("name"),
                            "provisioningState": parsed.get("provisioningState"),
                            "powerState": parsed.get("powerState", {}).get("code") if isinstance(parsed.get("powerState"), dict) else None,
                            "kubernetesVersion": parsed.get("kubernetesVersion"),
                            "location": parsed.get("location"),
                        }
                except (TypeError, ValueError):
                    evidence.cluster_state = {"state": "unknown", "reason": "unparseable_cluster_response"}
        except Exception as error:
            if hard_failure(error):
                raise
            evidence.cluster_state = {"state": "unknown", "reason": "cluster_show_failed"}
    else:
        evidence.cluster_state = {"state": "unknown", "reason": "az_aks_operations_unavailable"}

    # --- Step 2: node status ---
    nodes = await invoke("kubectl_resources", "get", "nodes", "-o json", "nodes")
    node_step = _step_from(evidence.steps, "nodes")
    evidence.node_health = _evaluate_nodes(nodes, node_step.parser_metadata if node_step else None)

    # --- Step 3: pod status in default namespace ---
    pods = await invoke("kubectl_resources", "get", "pods", f"--namespace {NAMESPACE} -o json", "pods")
    pod_step = _step_from(evidence.steps, "pods")
    evidence.pod_health = evaluate_pods(pods, pod_step.parser_metadata if pod_step else None)

    if evidence.budget_exhausted:
        evidence.stop_reason = "AKS cluster-health collection budget exhausted; some evidence was not collected."
    return evidence


def _step_from(steps: list[EvidenceStep], name: str) -> EvidenceStep | None:
    return next((s for s in steps if s.name == name), None)


def _redact_text(value: Any, limit=500) -> str:
    """Keep diagnostic messages useful without returning credential-looking values."""
    return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}=[REDACTED]", str(value))[:limit]



def _metadata(item: Any) -> dict[str, Any]:
    metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
    return {"name": metadata.get("name"), "namespace": metadata.get("namespace")}


def _conditions(items: Any) -> list[dict[str, Any]]:
    return [
        {key: _redact_text(item[key]) if key == "message" else item.get(key)
         for key in ("type", "status", "reason", "message") if item.get(key) is not None}
        for item in items if isinstance(item, dict)
    ] if isinstance(items, list) else []


def _pod_summary(item: Any) -> dict[str, Any]:
    status = item.get("status", {}) if isinstance(item, dict) else {}
    containers = [_container_evidence(c) for c in status.get("containerStatuses", [])] if isinstance(status, dict) else []
    return {
        **_metadata(item), "phase": status.get("phase") if isinstance(status, dict) else None,
        "conditions": _conditions(status.get("conditions", []) if isinstance(status, dict) else []),
        "containers": containers,
        "init_containers": [_container_evidence(c, init=True) for c in status.get("initContainerStatuses", [])] if isinstance(status, dict) else [],
    }


def _items(payload: Any) -> list[Any]:
    return payload.get("items", []) if isinstance(payload, dict) and isinstance(payload.get("items"), list) else []


def _step(evidence: TroubleshootingEvidence, name: str) -> EvidenceStep | None:
    return next((step for step in evidence.steps if step.name == name), None)


def _focused_pod_description(raw: str) -> list[str]:
    """Whitelist status/event lines; never return environment or arbitrary describe output."""
    allowed = re.compile(r"^\s*(Name:|Namespace:|Status:|State:|Reason:|Ready:|Restart Count:|Type:|Message:|Events:)")
    return [_redact_text(line, limit=500) for line in raw.splitlines() if allowed.match(line)][:80]


def task_manager_evidence_report(evidence: TroubleshootingEvidence) -> dict[str, Any]:
    try:
        return _task_manager_evidence_report(evidence)
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, RecursionError):
        return {"state": "unknown", "reason": "malformed_evidence_report",
                "decision": {"stop_reason": "Collected evidence could not be safely interpreted.",
                             "collection_calls": evidence.collection_calls,
                             "budget_exhausted": evidence.budget_exhausted}}


def _task_manager_evidence_report(evidence: TroubleshootingEvidence) -> dict[str, Any]:
    """Return a focused, JSON-safe diagnostic report without raw MCP dumps."""
    deployment = evidence.deployment
    deployment_status = deployment.get("status", {}) if isinstance(deployment, dict) else {}
    deployment_spec = deployment.get("spec", {}) if isinstance(deployment, dict) else {}
    replica_sets = []
    for item in _items((_step(evidence, "replicasets") or EvidenceStep("", "", "", "", "")).payload):
        status = item.get("status", {}) if isinstance(item, dict) else {}
        spec = item.get("spec", {}) if isinstance(item, dict) else {}
        replica_sets.append({
            **_metadata(item), "desired_replicas": spec.get("replicas"),
            "ready_replicas": status.get("readyReplicas"), "available_replicas": status.get("availableReplicas"),
        })
    pod_step = _step(evidence, "pods")
    event_step = _step(evidence, "events")
    service_step = _step(evidence, "service")
    endpoint_step = _step(evidence, "endpoints")
    slice_step = _step(evidence, "endpoint_slices")
    pod_descriptions = {
        step.name.removeprefix("pod:"): _focused_pod_description(step.raw)
        for step in evidence.steps if step.name.startswith("pod:")
    }
    objects = {("Deployment", DEPLOYMENT): None, ("Service", SERVICE): None}
    for step in evidence.steps:
        if step.outcome != "ok":
            continue
        kind = {"deployment": "Deployment", "service": "Service", "replicasets": "ReplicaSet", "pods": "Pod"}.get(step.name)
        if kind:
            for item in _items(step.payload) if step.name in {"replicasets", "pods"} else [step.payload]:
                meta = item.get("metadata", {})
                if meta.get("namespace") == NAMESPACE and isinstance(meta.get("name"), str):
                    objects[(kind, meta["name"])] = meta.get("uid")
    events = event_evidence(event_step, objects, _redact_text)
    service = service_step.payload if service_step and isinstance(service_step.payload, dict) else {}
    service_spec = service.get("spec", {}) if isinstance(service, dict) else {}
    return {
        "deployment": {
            **_metadata(deployment), "selector": evidence.selector,
            "desired_replicas": deployment_spec.get("replicas") if isinstance(deployment_spec, dict) else None,
            "ready_replicas": deployment_status.get("readyReplicas") if isinstance(deployment_status, dict) else None,
            "available_replicas": deployment_status.get("availableReplicas") if isinstance(deployment_status, dict) else None,
            "conditions": _conditions(deployment_status.get("conditions", []) if isinstance(deployment_status, dict) else []),
            "evaluation": evidence.deployment_health,
        },
        "replica_sets": replica_sets,
        "pods": [_pod_summary(item) for item in _items(pod_step.payload if pod_step else None)],
        "pod_evaluation": evidence.pod_health,
        "pod_descriptions": pod_descriptions,
        "events": events["events"],
        "event_collection": {key: value for key, value in events.items() if key != "events"},
        "continued_to_service": service_step is not None,
        "service": {
            **_metadata(service), "type": service_spec.get("type") if isinstance(service_spec, dict) else None,
            "evaluation": evidence.service_health,
            "selector": evidence.service_health.get("service", {}).get("selector", {"state": "unknown"}),
            "ports": evidence.service_health.get("service", {}).get("ports", {"state": "unknown"}),
            "existence": evidence.service_health.get("service", {}).get("existence", "not_collected"),
        },
        "load_balancer_ingress": evidence.load_balancer_ingress,
        "endpoints": evidence.service_health.get("sources", {}).get("endpoints", {"state": "not_collected"}),
        "endpoint_slices": evidence.service_health.get("sources", {}).get("endpoint_slices", {"state": "not_collected"}),
        "network_policies": evidence.network_policy_evidence,
        "decision": {"stop_reason": evidence.stop_reason,
                     "collection_calls": evidence.collection_calls,
                     "collection_limit": MAX_AKS_COLLECTION_CALLS,
                     "budget_exhausted": evidence.budget_exhausted, "steps": [
            {"name": step.name, "operation": step.operation, "resource": step.resource,
             "outcome": step.outcome, "parser": step.parser_metadata} for step in evidence.steps
        ]},
    }


async def collect_task_manager_evidence_report(tools, log=print) -> dict[str, Any]:
    """Run the existing deterministic collector and return its focused evidence-only report."""
    return task_manager_evidence_report(await collect_task_manager_evidence(tools, log=log))
