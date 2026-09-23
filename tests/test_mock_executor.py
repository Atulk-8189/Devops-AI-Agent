from datetime import timedelta
from unittest.mock import patch
import unittest

import test_approval as approval_helpers
from src.agent.approval import ApprovalSession
from src.agent.mock_executor import MockExecutor, MockRepository, content_hash, MockExecutionResult
from src.agent.proposals import action_hash, build_proposal, Action
from src.policy.write_policy import WritePolicy


class MockExecutorTests(unittest.TestCase):
    approve = approval_helpers.ApprovalTests.approve
    events = approval_helpers.ApprovalTests.events

    def setUp(self):
        approval_helpers.ApprovalTests.setUp(self)
        action = Action.model_validate({**self.proposal.model_dump(include=set(Action.model_fields)),
            "preconditions": {"expected_commit": "a" * 40, "original_content_sha256": content_hash("original")}})
        self.proposal = build_proposal(action, requested_by="operator", reason="Update the example file",
                                       expected_impact="One mock file", risk_level="high", policy=self.policy)
        self.session = ApprovalSession(self.proposal, self.policy, self.audit)
        self.repository = MockRepository()
        self.repository.seed(self.proposal.target, commit="a" * 40, content="original")
        self.executor = MockExecutor(self.repository, self.audit)

    def run_mock(self, proposal=None, policy=None):
        return self.executor.execute(self.proposal if proposal is None else proposal, self.session,
                                     self.policy if policy is None else policy)

    def test_end_to_end_approved_mock_without_external_calls(self):
        self.approve()
        with patch("subprocess.Popen", side_effect=AssertionError("No subprocess")), \
             patch("socket.socket", side_effect=AssertionError("No network")), \
             patch("src.mcp.runtime.MCPRuntime.initialize", side_effect=AssertionError("No MCP")), \
             patch("openai.AsyncOpenAI", side_effect=AssertionError("No API client")):
            result = self.run_mock()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.verification_status, "verified")
        self.assertTrue(result.changed)
        self.assertEqual(self.session.record.use_count, 1)
        self.assertEqual(self.repository.dispatches, 1)
        self.assertEqual(MockExecutionResult.model_validate_json(result.model_dump_json()), result)
        self.assertNotIn("NEVER_LOG_ME", result.model_dump_json())
        self.assertNotIn("NEVER_LOG_ME", self.path.read_text())
        events = [row["event"]["event_type"] for row in self.events()]
        self.assertEqual(events[-5:], ["approval_approved", "execution_started", "approval_consumed", "execution_succeeded", "verification_succeeded"])

    def test_no_approval(self):
        self.assertEqual(self.run_mock().status, "rejected")
        self.assertEqual(self.executor.execute(self.proposal, None, self.policy).status, "rejected")
        self.assertEqual(self.repository.dispatches, 0)
        self.assertEqual(self.session.record.use_count, 0)

    def test_changed_model_arguments_and_bindings(self):
        for field, value in (("proposal_id", "other"), ("action_sha256", "0" * 64),
                             ("payload", self.proposal.payload.model_copy(update={"replacement_content": "Y"})),
                             ("target", self.proposal.target.model_copy(update={"path": "/terraform/B.tf"})),
                             ("max_write_operations", 2), ("operation", "delete_file")):
            with self.subTest(field=field):
                self.session = ApprovalSession(self.proposal, self.policy, self.audit)
                self.approve()
                changed = self.proposal.model_copy(update={field: value})
                if field in {"payload", "target"}:
                    changed = changed.model_copy(update={"action_sha256": action_hash(changed)})
                self.assertEqual(self.run_mock(changed).status, "rejected")
                self.assertEqual(self.session.record.use_count, 0)
                self.assertEqual(self.repository.dispatches, 0)

    def test_policy_changes_rejected(self):
        for policy in (WritePolicy(), WritePolicy(repository_branches={"repo": ["other"]}),
                       self.policy.model_copy(update={"policy_version": "2"})):
            self.session = ApprovalSession(self.proposal, self.policy, self.audit)
            self.approve()
            self.assertEqual(self.run_mock(policy=policy).status, "unauthorized")
            self.assertEqual(self.repository.dispatches, 0)
            self.assertEqual(self.session.record.use_count, 0)

    def test_expired_approval(self):
        self.approve()
        future = self.proposal.expires_at + timedelta(seconds=1)
        with patch("src.agent.mock_executor.datetime") as clock, patch("src.agent.approval.datetime") as approval_clock:
            clock.now.return_value = approval_clock.now.return_value = future
            self.assertEqual(self.run_mock().status, "expired")
        self.assertEqual(self.repository.dispatches, 0)
        self.assertEqual(self.session.record.use_count, 0)

    def test_invalidated_and_consumed_rejected(self):
        self.approve()
        self.session.invalidate()
        self.assertEqual(self.run_mock().status, "rejected")
        self.session = ApprovalSession(self.proposal, self.policy, self.audit)
        self.approve()
        self.session.consume(self.proposal, self.policy)
        self.assertEqual(self.run_mock().status, "rejected")
        self.assertEqual(self.repository.dispatches, 0)

    def test_preconditions_and_missing_target(self):
        self.approve()
        for commit, content in (("c" * 40, "original"), ("a" * 40, "changed")):
            self.repository.seed(self.proposal.target, commit=commit, content=content)
            self.assertEqual(self.run_mock().status, "precondition_failed")
        self.repository.files.clear()
        self.assertEqual(self.run_mock().status, "precondition_failed")
        self.assertEqual(self.session.record.use_count, 0)
        self.assertEqual(self.repository.dispatches, 0)

    def test_dispatch_failure_does_not_consume(self):
        self.approve()
        self.repository.fail_dispatch = True
        self.assertEqual(self.run_mock().error, "mock_dispatch_failed")
        self.assertEqual(self.session.record.use_count, 0)
        self.assertEqual(self.repository.dispatches, 0)

    def test_verification_failure_does_not_rollback_or_rewrite(self):
        for mode, status in (("mismatch", "failed"), ("unavailable", "unverified")):
            self.session = ApprovalSession(self.proposal, self.policy, self.audit)
            self.repository.seed(self.proposal.target, commit="a" * 40, content="original")
            self.approve()
            self.repository.verification_mode = mode
            before = self.repository.dispatches
            result = self.run_mock()
            self.assertEqual(result.error, "verification_failed")
            self.assertEqual(result.verification_status, status)
            self.assertEqual(self.session.record.use_count, 1)
            self.assertEqual(self.run_mock().status, "rejected")
            self.assertEqual(self.repository.dispatches, before + 1)

    def test_consumption_occurs_only_after_dispatch(self):
        self.approve()
        append = self.audit.append
        def inspect(record):
            if record.event_type == "execution_started":
                self.assertEqual(self.repository.dispatches, 0)
                self.assertEqual(self.session.record.use_count, 0)
            if record.event_type == "approval_consumed":
                self.assertEqual(self.repository.dispatches, 1)
            append(record)
        with patch.object(self.audit, "append", side_effect=inspect):
            self.assertEqual(self.run_mock().status, "success")
        self.assertEqual(self.run_mock().status, "rejected")
        self.assertEqual(self.repository.dispatches, 1)

    def test_audit_failure_before_and_after_dispatch(self):
        for failure_event, expected_dispatches in (("execution_started", 0), ("approval_consumed", 1),
                                                   ("verification_succeeded", 1)):
            self.session = ApprovalSession(self.proposal, self.policy, self.audit)
            self.repository.seed(self.proposal.target, commit="a" * 40, content="original")
            self.approve()
            before = self.repository.dispatches
            append = self.audit.append
            def fail(record):
                if record.event_type == failure_event:
                    raise OSError("credential=do-not-leak")
                append(record)
            with patch.object(self.audit, "append", side_effect=fail):
                result = self.run_mock()
            self.assertEqual(result.error, "audit_failed")
            self.assertEqual(self.repository.dispatches, before + expected_dispatches)
            self.assertEqual(self.run_mock().status, "rejected")
            self.assertEqual(self.repository.dispatches, before + expected_dispatches)

    def test_no_external_backend_allowed(self):
        with self.assertRaises(TypeError):
            MockExecutor(object(), self.audit)
