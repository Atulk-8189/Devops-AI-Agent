import unittest

from src.mcp.runtime import select_allowed_tools
from src.policy.policy import ALLOWED_PROJECT, enforce_read_only_policy


def call(name, **args):
    return {"name": name, "args": args, "id": "test", "type": "tool_call"}


class ReadOnlyPolicyTests(unittest.TestCase):
    def test_azure_devops_operation_still_normalizes_project_and_branch(self):
        request = call("repo_file", action="get_content")
        enforce_read_only_policy(request)
        self.assertEqual(request["args"]["project"], ALLOWED_PROJECT)
        self.assertEqual(request["args"]["version"], "main")
        self.assertEqual(request["args"]["versionType"], "Branch")

    def test_azure_devops_mutation_is_rejected(self):
        with self.assertRaises(PermissionError):
            enforce_read_only_policy(call("repo_file", action="create"))

    def test_allowed_aks_operation(self):
        enforce_read_only_policy(call("az_aks_operations", operation="show", args="--name aks"))

    def test_unsupported_aks_operation_is_rejected(self):
        with self.assertRaises(PermissionError):
            enforce_read_only_policy(call("az_aks_operations", operation="delete", args="--name aks"))

    def test_kubectl_get_and_describe_are_allowed(self):
        enforce_read_only_policy(call("kubectl_resources", operation="get", resource="pods",
                                      args="-n default -l app=task-manager"))
        enforce_read_only_policy(call("kubectl_resources", operation="describe", resource="deployments",
                                      args="task-manager --namespace=default"))
        enforce_read_only_policy(call("kubectl_resources", operation="get", resource="nodes",
                                      args="--namespace default"))
        enforce_read_only_policy(call("kubectl_resources", operation="get", resource="networkpolicies",
                                      args="--namespace default -o json"))

    def test_non_default_or_cluster_wide_namespace_is_rejected(self):
        for args in ("-n production", "--all-namespaces", "-A", ""):
            with self.subTest(args=args), self.assertRaises(PermissionError):
                enforce_read_only_policy(call("kubectl_resources", operation="get", resource="pods", args=args))

    def test_sensitive_resources_and_command_syntax_are_rejected(self):
        forbidden = [
            ("secrets", "-n default"),
            ("serviceaccounts", "-n default"),
            ("networkpolicies", "-n default exec"),
            ("pods", "-n default; kubectl get secrets"),
            ("pods", "-n default | cat"),
            ("pods", "-n default $(whoami)"),
            ("pods", "-n default exec"),
        ]
        for resource, args in forbidden:
            with self.subTest(resource=resource, args=args), self.assertRaises(PermissionError):
                enforce_read_only_policy(call("kubectl_resources", operation="get", resource=resource, args=args))

    def test_kubectl_config_and_write_operations_are_rejected(self):
        with self.assertRaises(PermissionError):
            enforce_read_only_policy(call("kubectl_config", operation="config", resource="current-context", args=""))
        with self.assertRaises(PermissionError):
            enforce_read_only_policy(call("kubectl_resources", operation="delete", resource="pods", args="-n default"))

    def test_duplicate_tool_names_fail_closed(self):
        class Tool:
            def __init__(self, name):
                self.name = name

        with self.assertRaisesRegex(RuntimeError, "Duplicate MCP tool names"):
            select_allowed_tools([Tool("repo_file"), Tool("repo_file")])

    def test_call_kubectl_logs_allowed_combinations(self):
        allowed_commands = [
            "kubectl logs task-manager-123 -n default",
            "kubectl logs task-manager-123 --namespace=default",
            "kubectl logs task-manager-123 -n default -c app",
            "kubectl logs task-manager-123 -n default --container=app",
            "kubectl logs task-manager-123 -n default --previous",
            "kubectl logs task-manager-123 -n default --tail 50",
            "kubectl logs task-manager-123 -n default --tail=1000",
            "kubectl logs task-manager-123 -n default -c app --previous --tail 100",
            "kubectl logs task-manager.frontend_v1 -n default",
        ]
        for cmd in allowed_commands:
            with self.subTest(command=cmd):
                enforce_read_only_policy(call("call_kubectl", command=cmd))

    def test_call_kubectl_namespace_restrictions(self):
        forbidden_namespace_commands = [
            "kubectl logs task-manager-123 -n kube-system",
            "kubectl logs task-manager-123 -n prod",
            "kubectl logs task-manager-123 --namespace=ingress-nginx",
            "kubectl logs task-manager-123",
            "kubectl logs task-manager-123 --all-namespaces",
            "kubectl logs task-manager-123 -A",
            "kubectl logs task-manager-123 -n default -n kube-system",
        ]
        for cmd in forbidden_namespace_commands:
            with self.subTest(command=cmd), self.assertRaises(PermissionError):
                enforce_read_only_policy(call("call_kubectl", command=cmd))

    def test_call_kubectl_invalid_inputs_and_bounds(self):
        invalid_commands = [
            "kubectl logs task-manager-123 -n default --tail 0",
            "kubectl logs task-manager-123 -n default --tail=1001",
            "kubectl logs task-manager-123 -n default --tail -10",
            "kubectl logs task-manager-123 -n default --tail abc",
            "kubectl logs ../secrets -n default",
            "kubectl logs task-manager -n default -c ../sidecar",
            "kubectl logs -n default",
            "kubectl logs pod1 pod2 -n default",
        ]
        for cmd in invalid_commands:
            with self.subTest(command=cmd), self.assertRaises(PermissionError):
                enforce_read_only_policy(call("call_kubectl", command=cmd))

    def test_call_kubectl_shell_injection_and_unauthorized_commands(self):
        attack_commands = [
            "kubectl logs task-manager -n default; rm -rf /",
            "kubectl logs task-manager -n default | grep secret",
            "kubectl logs task-manager -n default $(whoami)",
            "kubectl logs task-manager -n default & background",
            "kubectl logs task-manager -n default\nkubectl get secrets",
            "kubectl exec task-manager -n default -- sh",
            "kubectl get pods -n default",
            "kubectl delete pod task-manager -n default",
            "kubectl logs task-manager -n default -f",
            "kubectl logs task-manager -n default --follow",
        ]
        for cmd in attack_commands:
            with self.subTest(command=cmd), self.assertRaises(PermissionError):
                enforce_read_only_policy(call("call_kubectl", command=cmd))
