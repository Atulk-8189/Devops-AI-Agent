import hashlib
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from src.agent.proposals import Action, Proposal, action_hash, build_proposal, display_proposal
from src.agent.proposal_only import ProposalRequest, propose_only
from src.policy.write_policy import WritePolicy
from src.review.terraform_evidence import TerraformEvidence


def action_data():
    return {"target": {"project": "My Project", "repository_id": "repo-1", "branch": "main", "path": "/terraform/main.tf"},
            "operation": "update_existing_file", "payload": {"replacement_content": "# replacement\n"},
            "preconditions": {"expected_commit": "a" * 40,
                              "original_content_sha256": hashlib.sha256(b"# original\n").hexdigest()}}


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.action = Action.model_validate(action_data())
        self.policy = WritePolicy(repository_branches={"repo-1": ["main"]})

    def proposal(self):
        return build_proposal(self.action, requested_by="operator", reason="Improve readability",
                              expected_impact="One source file changes", risk_level="high", policy=self.policy)

    def test_valid_and_strict_fields(self):
        proposal = self.proposal()
        self.assertEqual(proposal.approval_status, "pending")
        self.assertEqual(Proposal.model_validate_json(proposal.model_dump_json()), proposal)
        for field in Proposal.model_fields:
            data = proposal.model_dump(mode="json")
            del data[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Proposal.model_validate_json(json.dumps(data))

    def test_invalid_proposal_values(self):
        for field, value in (("unexpected", 1), ("reason", " "), ("requested_by", ""),
                             ("risk_level", "safe"), ("approval_status", "approved"),
                             ("approval_required", 1), ("max_write_operations", 2),
                             ("max_write_operations", True), ("action_sha256", "x" * 64),
                             ("action_sha256", "0" * 64), ("expires_at", "bad"),
                             ("expires_at", "2000-01-01T00:00:00Z"),
                             ("expires_at", "2099-01-01T00:00:00")):
            data = self.proposal().model_dump(mode="json")
            data[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                Proposal.model_validate_json(json.dumps(data))

    def test_nested_schema_and_forbidden_operations(self):
        for operation in ("delete_file", "kubectl", "terraform_apply", "run_command"):
            data = action_data()
            data["operation"] = operation
            with self.subTest(operation=operation), self.assertRaises(ValidationError):
                Action.model_validate(data)
        for section, key, value in (("target", "project", "Other"), ("target", "branch", ""),
                                    ("target", "path", "/terraform/../secret"),
                                    ("payload", "command", "sh"), ("payload", "replacement_content", ""),
                                    ("preconditions", "expected_commit", "")):
            data = action_data()
            data[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValidationError):
                Action.model_validate(data)
        data = action_data()
        data["preconditions"] = {}
        with self.assertRaises(ValidationError):
            Action.model_validate(data)

    def test_policy_allowlist_and_writes_disabled(self):
        self.assertFalse(self.policy.writes_enabled)
        with self.assertRaises(ValidationError):
            WritePolicy(writes_enabled=True)
        for policy in (WritePolicy(), WritePolicy(repository_branches={"other": ["main"]}),
                       WritePolicy(repository_branches={"repo-1": ["sandbox"]})):
            with self.subTest(policy=policy), self.assertRaises(PermissionError):
                policy.validate_proposal(self.proposal())

    def test_hash_deterministic_and_bound_to_every_action_field(self):
        data = action_data()
        reordered = json.loads(json.dumps(data, sort_keys=True))
        self.assertEqual(action_hash(self.action), action_hash(Action.model_validate(reordered)))
        for section, key, value in (("target", "repository_id", "repo-2"), ("target", "branch", "sandbox"),
                                    ("target", "path", "/terraform/other.tf"),
                                    ("payload", "replacement_content", "# replacement\r\n"),
                                    ("preconditions", "expected_commit", "b" * 40),
                                    ("preconditions", "original_content_sha256", "b" * 64)):
            data = action_data()
            data[section][key] = value
            with self.subTest(key=key):
                self.assertNotEqual(action_hash(self.action), action_hash(Action.model_validate(data)))

    def test_safe_display(self):
        proposal = self.proposal()
        display = display_proposal(proposal)
        for text in ("WRITES DISABLED", "repo-1", "update_existing_file", "Improve readability",
                     "One source file changes", "high", "expected_commit", "pending", "expires_at", proposal.action_sha256):
            self.assertIn(text, display)
        self.assertNotIn("# replacement", display)
        dirty = proposal.model_copy(update={"reason": "password=hidden token=secretvalue\x1b"})
        display = display_proposal(dirty)
        self.assertNotIn("hidden", display)
        self.assertNotIn("secretvalue", display)
        self.assertNotIn("\x1b", display)


class ProposalPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_path_only_collects_reads_and_stops(self):
        calls = []

        class Tool:
            def __init__(self, name):
                self.name = name

            async def ainvoke(self, call):
                calls.append(call)
                args = call["args"]
                if self.name == "repo_repository":
                    content = json.dumps([{"id": "repo-1", "name": "My Project"}])
                elif args["action"] == "list_directory":
                    content = json.dumps({"items": [{"path": "/terraform/main.tf", "isFolder": False}]})
                elif args["action"] == "get_content":
                    content = "# original\n"
                else:
                    raise AssertionError("Write attempted")
                return ToolMessage(content=content, name=self.name, tool_call_id=call["id"])

        runtime = AsyncMock()
        runtime.tools = [Tool("repo_repository"), Tool("repo_file")]
        request = ProposalRequest(action=Action.model_validate(action_data()), question="Improve comments", expected_impact="Comments change")
        result = await propose_only(request, WritePolicy(repository_branches={"repo-1": ["main"]}), runtime)
        self.assertIn("STOP", result)
        self.assertIn("NOT verified", result)
        self.assertEqual([c["args"]["action"] for c in calls], ["list", "list_directory", "get_content"])
        runtime.close.assert_awaited_once()

        audit = Mock()
        with patch("sys.stdin.isatty", return_value=False), patch("builtins.print"):
            result = await propose_only(request, WritePolicy(repository_branches={"repo-1": ["main"]}),
                                        runtime, approval_audit=audit)
        self.assertIn("Approval status: denied", result)
        self.assertEqual([c["args"]["action"] for c in calls], ["list", "list_directory", "get_content"] * 2)
        self.assertEqual([call.args[0].event_type for call in audit.append.call_args_list],
                         ["proposal_created", "approval_requested", "approval_denied"])

    async def test_no_allowlist_blocks_before_collection(self):
        runtime = AsyncMock()
        request = ProposalRequest(action=Action.model_validate(action_data()), question="Improve comments", expected_impact="Comments change")
        with self.assertRaises(PermissionError):
            await propose_only(request, WritePolicy(), runtime)
        runtime.initialize.assert_not_awaited()

    async def test_evidence_mismatch_or_failure_never_produces_proposal(self):
        request = ProposalRequest(action=Action.model_validate(action_data()), question="Improve comments", expected_impact="Comments change")
        policy = WritePolicy(repository_branches={"repo-1": ["main"]})
        for repository, content in (("wrong", "# original\n"), ("repo-1", "different"), ("repo-1", None)):
            messages = TerraformEvidence()
            messages.discovery["repository_id"] = repository
            if content is not None:
                messages.extend([
                    AIMessage(content="", tool_calls=[{"name": "repo_file", "id": "read", "args": {"action": "get_content", "path": "/terraform/main.tf"}}]),
                    ToolMessage(content=content, tool_call_id="read"),
                ])
            runtime = AsyncMock()
            with self.subTest(repository=repository, content=content), \
                 patch("src.agent.proposal_only.gather_terraform_evidence", new=AsyncMock(return_value=messages)), \
                 self.assertRaises((PermissionError, ValueError)):
                await propose_only(request, policy, runtime)
            runtime.close.assert_awaited_once()
        runtime = AsyncMock()
        with patch("src.agent.proposal_only.gather_terraform_evidence", new=AsyncMock(side_effect=PermissionError("denied"))), \
             self.assertRaises(PermissionError):
            await propose_only(request, policy, runtime)
        runtime.close.assert_awaited_once()

    async def test_other_branch_is_not_silently_read_from_main(self):
        data = action_data()
        data["target"]["branch"] = "sandbox"
        request = ProposalRequest(action=Action.model_validate(data), question="Improve comments", expected_impact="Comments change")
        runtime = AsyncMock()
        with self.assertRaises(PermissionError):
            await propose_only(request, WritePolicy(repository_branches={"repo-1": ["sandbox"]}), runtime)
        runtime.initialize.assert_not_awaited()
