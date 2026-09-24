"""Shared read-only policy, normalization, and graph state."""
import re
import shlex
import logging
from src.observability import observed_policy

LOGGER = logging.getLogger(__name__)


class PipelinePolicyError(PermissionError):
    """Preserve the public policy error while attaching an application-owned reason."""

    def __init__(self, diagnostic_reason):
        super().__init__("Tool call blocked by read-only policy")
        self.diagnostic_reason = diagnostic_reason


READ_ONLY_POLICY = {
    "pipelines_definition": {"list"},
    "repo_repository": {"list"},
    "repo_file": {"list_directory", "get_content"},
}
AKS_READ_ONLY_POLICY = {
    "az_aks_operations": {
        "show", "nodepool-list", "nodepool-show", "account-list", "get-versions",
        "check-network",
    },
    "kubectl_resources": {"get", "describe"},
    "kubectl_cluster": {"cluster-info", "api-resources", "api-versions", "explain"},
}
TOOL_SERVERS = {
    **{name: "azure-devops" for name in READ_ONLY_POLICY},
    **{name: "aks" for name in AKS_READ_ONLY_POLICY},
}
ALLOWED_TOOL_NAMES = frozenset(TOOL_SERVERS)
ALLOWED_PROJECT = "My Project"
ALLOWED_BRANCH = "main"
ALLOWED_NAMESPACE = "default"

KUBERNETES_RESOURCES = frozenset({
    "pods", "deployments", "replicasets", "services", "endpoints", "endpointslices",
    "events", "networkpolicies", "configmaps", "jobs", "cronjobs", "nodes", "horizontalpodautoscalers",
    "persistentvolumeclaims",
})
SHELL_METACHARACTERS = frozenset({";", "|", "&", "<", ">", "`", "$", "(", ")", "\\", "\n", "\r"})
SAFE_KUBERNETES_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
SAFE_SELECTOR = re.compile(r"^[A-Za-z0-9._/!=(),-]+$")
FORBIDDEN_KUBECTL_TOKENS = frozenset({
    "kubectl", "exec", "cp", "port-forward", "apply", "create", "delete", "patch",
    "replace", "rollout", "restart", "scale", "sh", "bash", "zsh",
})


def _parse_arguments(value):
    """Parse a tool argument string without ever passing it to a shell."""
    if not isinstance(value, str) or any(char in value for char in SHELL_METACHARACTERS):
        raise PermissionError("Tool call blocked by read-only policy")
    try:
        return shlex.split(value, posix=True)
    except ValueError as exc:
        raise PermissionError("Tool call blocked by read-only policy") from exc


def _validate_kubectl_arguments(args):
    """Allow a deliberately small, namespace-bound kubectl resource grammar."""
    tokens = _parse_arguments(args)
    namespace = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in FORBIDDEN_KUBECTL_TOKENS:
            raise PermissionError("Tool call blocked by read-only policy")
        if token in {"-n", "--namespace"}:
            index += 1
            if index == len(tokens) or namespace is not None:
                raise PermissionError("Tool call blocked by read-only policy")
            namespace = tokens[index]
        elif token.startswith("--namespace="):
            if namespace is not None:
                raise PermissionError("Tool call blocked by read-only policy")
            namespace = token.removeprefix("--namespace=")
        elif token in {"--all-namespaces", "-A"}:
            raise PermissionError("Tool call blocked by read-only policy")
        elif token in {"-l", "--selector", "--field-selector"}:
            index += 1
            if index == len(tokens) or not SAFE_SELECTOR.fullmatch(tokens[index]):
                raise PermissionError("Tool call blocked by read-only policy")
        elif token.startswith("--selector=") or token.startswith("--field-selector="):
            selector = token.split("=", 1)[1]
            if not SAFE_SELECTOR.fullmatch(selector):
                raise PermissionError("Tool call blocked by read-only policy")
        elif token in {"-o", "--output"}:
            index += 1
            if index == len(tokens) or tokens[index] not in {"json", "yaml", "wide", "name"}:
                raise PermissionError("Tool call blocked by read-only policy")
        elif token.startswith("--output="):
            if token.split("=", 1)[1] not in {"json", "yaml", "wide", "name"}:
                raise PermissionError("Tool call blocked by read-only policy")
        elif token in {"--show-labels", "--no-headers", "--ignore-not-found"}:
            pass
        elif token.startswith("-") or not SAFE_KUBERNETES_NAME.fullmatch(token):
            raise PermissionError("Tool call blocked by read-only policy")
        index += 1
    if namespace != ALLOWED_NAMESPACE:
        raise PermissionError("Tool call blocked by read-only policy")


def _validate_aks_arguments(args):
    """The MCP operation is allowlisted; reject shell syntax in its argument field."""
    tokens = _parse_arguments(args)
    if any(token in {"az", "kubectl", "sh", "bash", "zsh"} for token in tokens):
        raise PermissionError("Tool call blocked by read-only policy")


@observed_policy
def enforce_read_only_policy(call):
    name = call["name"]
    args = call["args"]
    server = TOOL_SERVERS.get(name)
    if server is None:
        raise PermissionError("Tool call blocked by read-only policy")
    if server == "aks":
        if args.get("operation") not in AKS_READ_ONLY_POLICY[name]:
            raise PermissionError("Tool call blocked by read-only policy")
        if name == "kubectl_resources":
            if args.get("resource", "").lower() not in KUBERNETES_RESOURCES:
                raise PermissionError("Tool call blocked by read-only policy")
            _validate_kubectl_arguments(args.get("args"))
        elif name == "az_aks_operations":
            _validate_aks_arguments(args.get("args"))
        else:
            # Cluster metadata tools have no namespace argument and accept no shell syntax.
            _parse_arguments(args.get("args"))
        return

    allowed_actions = READ_ONLY_POLICY[name]
    if args.get("action") not in allowed_actions:
        if name == "pipelines_definition":
            raise PipelinePolicyError("action_not_allowed")
        raise PermissionError("Tool call blocked by read-only policy")
    if "project" not in args:
        call["args"]["project"] = ALLOWED_PROJECT
        LOGGER.info("policy normalization: injected authorized project")
    elif args["project"] != ALLOWED_PROJECT:
        if name == "pipelines_definition":
            raise PipelinePolicyError("project_mismatch")
        raise PermissionError("Tool call blocked by read-only policy")
    if name == "repo_file":
        if (
            call["args"].get("version") != ALLOWED_BRANCH
            or call["args"].get("versionType") != "Branch"
        ):
            call["args"]["version"] = ALLOWED_BRANCH
            call["args"]["versionType"] = "Branch"
            LOGGER.info("policy normalization: applied authorized branch")
