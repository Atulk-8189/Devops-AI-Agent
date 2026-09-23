"""In-memory simulation only. No clients, MCP tools, shells, or real executor."""
import hashlib
import threading
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from src.agent.approval import ApprovalRecord, ApprovalSession, AuditRecord, digest
from src.agent.proposals import Hash, Proposal, StrictModel, Text
from src.policy.write_policy import WritePolicy


def content_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class MockFile(StrictModel):
    commit: Text
    content: str


class MockExecutionResult(StrictModel):
    execution_id: Text
    proposal_id: Text
    action_sha256: Hash
    status: Literal["success", "rejected", "failed", "precondition_failed", "expired", "unauthorized"]
    operation: Literal["update_existing_file"]
    target: dict[str, str]  # Fingerprints only; never file content or sensitive paths.
    changed: bool
    error: Literal["validation_failed", "approval_unavailable", "policy_rejected", "precondition_failed",
                   "mock_dispatch_failed", "verification_failed", "audit_failed", "expired"] | None
    verification_status: Literal["verified", "unverified", "failed"]


class MockRepository:
    """Owned in-memory fixture. Entries are keyed by the complete target."""
    def __init__(self):
        self._lock = threading.RLock()
        self.files = {}
        self.dispatches = 0
        self.fail_dispatch = False
        self.verification_mode = "normal"  # normal, mismatch, unavailable (test simulation)

    def seed(self, target, *, commit, content):
        with self._lock:
            self.files[digest(target.model_dump())] = MockFile(commit=commit, content=content)


class MockExecutor:
    def __init__(self, repository, audit):
        if type(repository) is not MockRepository:
            raise TypeError("Only the in-memory mock repository is supported")
        self.repository = repository
        self.audit = audit

    def execute(self, proposal, session, policy):
        """Candidate is checked, but only the session's stored snapshot is applied.

        Approval and fixture locks cover validation, dispatch and consumption.
        The returned consumed state is never interpreted as permission to replay.
        """
        execution_id = str(uuid4())
        trusted = session._proposal if type(session) is ApprovalSession else None
        proposal_id = trusted.proposal_id if trusted else "unavailable"
        action_sha256 = trusted.action_sha256 if trusted else "0" * 64
        target = {key + "_sha256": digest(value) for key, value in trusted.target.model_dump().items()} if trusted else {}
        changed = False

        def result(status, error=None, verification="unverified"):
            return MockExecutionResult(execution_id=execution_id, proposal_id=proposal_id,
                action_sha256=action_sha256, status=status, operation="update_existing_file", target=target,
                changed=changed, error=error, verification_status=verification)

        def event(kind, status):
            try:
                self.audit.append(AuditRecord(event_id=str(uuid4()), proposal_id=proposal_id,
                    action_sha256=action_sha256, event_type=kind, actor=session._actor if trusted else "application",
                    timestamp=datetime.now(timezone.utc), target_summary=target, operation="update_existing_file",
                    status=status, reason="mock_only", policy_version=session._policy_version if trusted else "unknown",
                    execution_id=execution_id))
            except Exception:
                if trusted:
                    session._failed = True
                raise RuntimeError("Mock audit unavailable") from None

        def reject(status, error):
            event("execution_rejected", status)
            return result(status, error)

        try:
            if trusted is None:
                return reject("rejected", "approval_unavailable")
            with session._lock, self.repository._lock:
                if session._failed or session._audit is not self.audit:
                    return reject("rejected", "approval_unavailable")
                try:
                    record = ApprovalRecord.model_validate(session.record.model_dump())
                except (ValueError, AttributeError):
                    return reject("rejected", "approval_unavailable")
                if record.approval_status != "approved" or record.use_count != 0:
                    return reject("expired" if record.approval_status == "expired" else "rejected", "approval_unavailable")
                if datetime.now(timezone.utc) >= record.expires_at:
                    session._check(trusted, policy)
                    return reject("expired", "expired")
                try:
                    policy = WritePolicy.model_validate(policy.model_dump())
                    policy.validate_proposal(trusted)
                except (ValueError, PermissionError, AttributeError):
                    session.invalidate()
                    return reject("unauthorized", "policy_rejected")
                if digest(policy.model_dump(mode="json")) != session._policy:
                    session.invalidate()
                    return reject("unauthorized", "policy_rejected")
                try:
                    candidate = Proposal.model_validate(proposal.model_dump())
                    bound = (record.proposal_id == trusted.proposal_id and record.action_sha256 == trusted.action_sha256
                             and record.expires_at == trusted.expires_at
                             and digest(candidate.model_dump(mode="json")) == session._fingerprint)
                except (ValueError, AttributeError):
                    bound = False
                if not bound or not session._check(trusted, policy):
                    session.invalidate()
                    return reject("rejected", "validation_failed")
                key = digest(trusted.target.model_dump())
                before = self.repository.files.get(key)
                if (before is None or before.commit != trusted.preconditions.expected_commit
                        or content_hash(before.content) != trusted.preconditions.original_content_sha256):
                    event("precondition_failed", "precondition_failed")
                    return result("precondition_failed", "precondition_failed")
                event("execution_started", "started")
                # Check expiry again after the durable pre-dispatch audit.
                if not session._check(trusted, policy):
                    return reject("expired", "expired")
                if self.repository.fail_dispatch:
                    event("execution_failed", "failed")
                    return result("failed", "mock_dispatch_failed")
                self.repository.files[key] = MockFile(commit=digest({"previous": before.commit, "action": action_sha256}),
                                                       content=trusted.payload.replacement_content)
                self.repository.dispatches += 1
                changed = before.content != trusted.payload.replacement_content
                # Dispatch has succeeded. Do not re-test expiry here: expiry
                # during dispatch cannot leave a dispatched approval reusable.
                session._transition("consumed", "approval_consumed", "state_transition_only", use_count=1)
                event("execution_succeeded", "success")
                after = self.repository.files.get(key)
                verified = (after is not None and after.content == trusted.payload.replacement_content
                            and self.repository.verification_mode == "normal")
                if not verified:
                    event("verification_failed", "unverified" if self.repository.verification_mode == "unavailable" else "failed")
                    return result("failed", "verification_failed",
                                  "unverified" if self.repository.verification_mode == "unavailable" else "failed")
                event("verification_succeeded", "verified")
                return result("success", verification="verified")
        except RuntimeError:
            # A post-dispatch audit failure poisons the approval session. It
            # cannot be reused, even if the consumed event could not be saved.
            return result("failed", "audit_failed")
