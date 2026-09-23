import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from src.agent.approval import ApprovalRecord, ApprovalSession, AuditLog, digest
from src.agent.proposals import Action, action_hash, build_proposal
from src.policy.write_policy import WritePolicy


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "audit.jsonl"
        self.audit = AuditLog(self.path)
        self.addCleanup(self.audit.close)
        self.policy = WritePolicy(repository_branches={"repo": ["main"]})
        action = Action.model_validate({"target": {"project": "My Project", "repository_id": "repo",
                    "branch": "main", "path": "/terraform/main.tf"}, "operation": "update_existing_file",
                    "payload": {"replacement_content": "password=NEVER_LOG_ME"},
                    "preconditions": {"expected_commit": "a" * 40, "original_content_sha256": "b" * 64}})
        self.proposal = build_proposal(action, requested_by="operator", reason="token=NEVER_LOG_ME",
            expected_impact="secret=NEVER_LOG_ME", risk_level="high", policy=self.policy)
        self.session = ApprovalSession(self.proposal, self.policy, self.audit)

    def approve(self, command=None, tty=True):
        expected = f"APPROVE {self.proposal.proposal_id} {self.proposal.action_sha256}"
        with patch("sys.stdin.isatty", return_value=tty), patch("sys.stdout.isatty", return_value=tty), \
             patch("builtins.input", return_value=expected if command is None else command) as read, patch("builtins.print"):
            result = self.session.request_from_terminal(self.proposal, self.policy)
            if not tty:
                read.assert_not_called()
            return result

    def events(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_valid_approval_and_single_use(self):
        record = self.approve()
        self.assertEqual(record.approval_status, "approved")
        self.assertEqual(record.expires_at, self.proposal.expires_at)
        self.assertIsNotNone(record.approved_by)
        record = self.session.consume(self.proposal, self.policy)
        self.assertEqual((record.approval_status, record.use_count), ("consumed", 1))
        count = len(self.events())
        self.assertEqual(self.session.consume(self.proposal, self.policy), record)
        self.assertEqual(self.approve(), record)
        self.assertEqual(len(self.events()), count)
        self.assertFalse(self.policy.writes_enabled)

    def test_concurrent_consumption_records_only_one_use(self):
        self.approve()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.session.consume(self.proposal, self.policy), range(2)))
        self.assertEqual(self.session.record.use_count, 1)
        self.assertEqual(sum(row["event"]["event_type"] == "approval_consumed" for row in self.events()), 1)

    def test_wrong_id_hash_empty_malformed_and_denied(self):
        for command in ("", "DENY", "approve", "APPROVE wrong " + self.proposal.action_sha256,
                        "APPROVE " + self.proposal.proposal_id + " " + "0" * 64,
                        f"APPROVE {self.proposal.proposal_id} {self.proposal.action_sha256} extra"):
            with self.subTest(command=command):
                self.session = ApprovalSession(self.proposal, self.policy, self.audit)
                self.assertEqual(self.approve(command).approval_status, "denied")
                self.assertEqual(self.approve().approval_status, "denied")

    def test_eof_is_denied(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
             patch("builtins.input", side_effect=EOFError), patch("builtins.print"):
            self.assertEqual(self.session.request_from_terminal(self.proposal, self.policy).approval_status, "denied")

    def test_model_or_tool_text_and_piped_input_cannot_approve(self):
        self.assertEqual(self.approve(tty=False).approval_status, "denied")
        with self.assertRaises(TypeError):
            self.session.request_from_terminal(self.proposal, self.policy, command="APPROVE")

    def test_expiry_before_and_during_prompt(self):
        future = self.proposal.expires_at + timedelta(seconds=1)
        with patch("src.agent.approval.datetime") as clock:
            clock.now.return_value = future
            self.assertEqual(self.session.consume(self.proposal, self.policy).approval_status, "expired")
        self.session = ApprovalSession(self.proposal, self.policy, self.audit)
        with patch("src.agent.approval.datetime") as clock, patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdout.isatty", return_value=True), patch("builtins.input", return_value="APPROVE"), patch("builtins.print"):
            clock.now.side_effect = [datetime.now(timezone.utc), datetime.now(timezone.utc), future, future]
            self.assertEqual(self.session.request_from_terminal(self.proposal, self.policy).approval_status, "expired")

    def test_changed_proposals_are_invalidated(self):
        for field in ("payload", "target", "preconditions", "proposal_id", "action_sha256", "reason", "expires_at"):
            with self.subTest(field=field):
                self.session = ApprovalSession(self.proposal, self.policy, self.audit)
                self.approve()
                changes = {"payload": self.proposal.payload.model_copy(update={"replacement_content": "changed"}),
                    "target": self.proposal.target.model_copy(update={"path": "/terraform/other.tf"}),
                    "preconditions": self.proposal.preconditions.model_copy(update={"expected_commit": "c" * 40}),
                    "proposal_id": "different", "action_sha256": "d" * 64, "reason": "changed",
                    "expires_at": self.proposal.expires_at + timedelta(minutes=1)}
                changed = self.proposal.model_copy(update={field: changes[field]})
                if field in {"payload", "target", "preconditions"}:
                    changed = changed.model_copy(update={"action_sha256": action_hash(changed)})
                self.assertEqual(self.session.consume(changed, self.policy).approval_status, "invalidated")

    def test_policy_version_and_allowlist_changes_invalidate(self):
        for policy in (self.policy.model_copy(update={"policy_version": "2"}), WritePolicy()):
            self.session = ApprovalSession(self.proposal, self.policy, self.audit)
            self.approve()
            self.assertEqual(self.session.consume(self.proposal, policy).approval_status, "invalidated")
        with self.assertRaises(PermissionError):
            ApprovalSession(self.proposal, WritePolicy(), self.audit)

    def test_explicit_invalidation(self):
        self.approve()
        self.assertEqual(self.session.invalidate().approval_status, "invalidated")
        self.assertEqual(self.session.consume(self.proposal, self.policy).use_count, 0)

    def test_audit_events_integrity_and_secret_omission(self):
        self.approve()
        self.session.consume(self.proposal, self.policy)
        rows = self.events()
        self.assertEqual([row["event"]["event_type"] for row in rows],
                         ["proposal_created", "approval_requested", "approval_approved", "approval_consumed"])
        previous = "0" * 64
        for row in rows:
            self.assertEqual(row["previous_sha256"], previous)
            self.assertEqual(row["record_sha256"], digest({"event": row["event"], "previous_sha256": previous}))
            previous = row["record_sha256"]
        self.assertNotIn("NEVER_LOG_ME", self.path.read_text())
        self.assertNotIn("replacement_content", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            AuditLog(self.path)

    def test_failed_audit_never_grants_approval(self):
        with patch.object(self.audit, "append", side_effect=OSError("secret")):
            with self.assertRaisesRegex(RuntimeError, "audit recording failed"):
                self.approve()
        with self.assertRaises(RuntimeError):
            self.session.consume(self.proposal, self.policy)
        self.assertEqual(self.session.record.approval_status, "pending")

    def test_strict_record(self):
        data = self.session.record.model_dump()
        for changes in ({"unexpected": "x"}, {"use_count": 2}, {"use_count": True},
                        {"approval_status": "yes"}, {"action_sha256": "bad"},
                        {"approval_status": "approved"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ApprovalRecord.model_validate({**data, **changes})
