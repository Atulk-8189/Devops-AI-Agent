"""Terminal-owned approval state; consumption is bookkeeping, never execution.

Sessions are intentionally not reloadable. Audit files are evidence, not approval
tokens. A process restart requires a new proposal and a new human decision.
"""
import hashlib
import json
import os
import pwd
import sys
import threading
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from src.agent.proposals import Hash, StrictModel, Text, display_proposal

Status = Literal["pending", "approved", "denied", "expired", "consumed", "invalidated"]
Event = Literal["proposal_created", "approval_requested", "approval_approved", "approval_denied",
                "approval_expired", "proposal_invalidated", "approval_consumed", "execution_started",
                "execution_succeeded", "execution_failed", "precondition_failed", "verification_succeeded",
                "verification_failed", "execution_rejected"]


def serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


class ApprovalRecord(StrictModel):
    proposal_id: Text
    action_sha256: Hash
    approved_by: Text | None
    approved_at: AwareDatetime | None
    expires_at: AwareDatetime
    approval_status: Status
    use_count: Annotated[int, Field(strict=True, ge=0, le=1)]

    @model_validator(mode="after")
    def consistent(self):
        if (self.approved_by is None) != (self.approved_at is None):
            raise ValueError("Approval identity and time must be paired")
        if self.approval_status in {"approved", "consumed"} and self.approved_at is None:
            raise ValueError("Human approval is required")
        if self.approved_at is not None and self.approved_at >= self.expires_at:
            raise ValueError("Approval must precede expiry")
        if (self.use_count == 1) != (self.approval_status == "consumed"):
            raise ValueError("Consumed approval must have exactly one use")
        return self


class AuditRecord(StrictModel):
    event_id: Text
    proposal_id: Text
    action_sha256: Hash
    event_type: Event
    actor: Text
    timestamp: AwareDatetime
    target_summary: dict[str, str]
    operation: Literal["update_existing_file"]
    status: Status | Literal["started", "success", "rejected", "failed", "precondition_failed", "unauthorized", "verified", "unverified"]
    reason: Literal["created", "terminal_confirmation", "invalid_confirmation", "expired",
                    "binding_or_policy_changed", "explicit_invalidation", "state_transition_only", "mock_only"]
    policy_version: Text
    execution_id: Text | None = None


class AuditLog:
    """Exclusive new file, owner-only access, append-only API, hash-chained JSONL.

    Does not import prior records. The owner can still tamper with local storage;
    this is not a remote tamper-proof audit service.
    """
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._previous = "0" * 64

    @serialized
    def append(self, record):
        event = record.model_dump(mode="json")
        envelope = {"event": event, "previous_sha256": self._previous}
        hashed = digest(envelope)
        data = (json.dumps({**envelope, "record_sha256": hashed}, sort_keys=True) + "\n").encode()
        with os.fdopen(os.dup(self._fd), "ab", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self._previous = hashed

    def close(self):
        os.close(self._fd)


class ApprovalSession:
    def __init__(self, proposal, policy, audit):
        self._lock = threading.RLock()
        policy.validate_proposal(proposal)
        if str(UUID(proposal.proposal_id)) != proposal.proposal_id:
            raise ValueError("Approval requires an application-generated proposal ID")
        self._proposal = proposal.model_copy(deep=True)
        self._fingerprint = digest(proposal.model_dump(mode="json"))
        self._policy = digest(policy.model_dump(mode="json"))
        self._policy_version = policy.policy_version
        self._audit = audit
        self._failed = False
        self._actor = pwd.getpwuid(os.getuid()).pw_name
        self._record = ApprovalRecord(proposal_id=proposal.proposal_id, action_sha256=proposal.action_sha256,
                                      approved_by=None, approved_at=None, expires_at=proposal.expires_at,
                                      approval_status="pending", use_count=0)
        self._event("proposal_created", "created", self._record)

    @property
    def record(self):
        return self._record

    def _event(self, event, reason, record):
        target = self._proposal.target
        # Never persist free-form proposal prose or contents. Fingerprint target
        # fields so even credentials placed in a path cannot reach the audit log.
        summary = {key + "_sha256": digest(value) for key, value in target.model_dump().items()}
        try:
            self._audit.append(AuditRecord(event_id=str(uuid4()),
                proposal_id=self._proposal.proposal_id, action_sha256=self._proposal.action_sha256,
                event_type=event, actor=self._actor, timestamp=datetime.now(timezone.utc),
                target_summary=summary, operation="update_existing_file", status=record.approval_status,
                reason=reason, policy_version=self._policy_version))
        except Exception:
            self._failed = True
            raise RuntimeError("Approval unavailable: audit recording failed") from None

    def _transition(self, status, event, reason, **updates):
        record = ApprovalRecord.model_validate({**self._record.model_dump(), "approval_status": status, **updates})
        self._event(event, reason, record)  # Durable audit before granting state.
        self._record = record
        return record

    def _check(self, proposal, policy):
        if self._failed:
            raise RuntimeError("Approval unavailable: audit recording failed")
        if self.record.approval_status not in {"pending", "approved"}:
            return False
        if datetime.now(timezone.utc) >= self.record.expires_at:
            self._transition("expired", "approval_expired", "expired")
            return False
        try:
            policy.validate_proposal(proposal)
            valid = (digest(proposal.model_dump(mode="json")) == self._fingerprint
                     and digest(policy.model_dump(mode="json")) == self._policy)
        except (ValueError, PermissionError):
            valid = False
        if not valid:
            self._transition("invalidated", "proposal_invalidated", "binding_or_policy_changed")
        return valid

    @serialized
    def request_from_terminal(self, proposal, policy):
        if not self._check(proposal, policy) or self.record.approval_status != "pending":
            return self.record
        self._event("approval_requested", "terminal_confirmation", self.record)
        print(display_proposal(proposal))
        print("Approval records intent only. Contents are omitted; inspect the exact operator-owned request first.")
        print("Commit precondition is unverified. No execution is available.")
        command = ""
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                command = input("Type APPROVE <proposal-id> <action-hash> to record approval: ")
            except (EOFError, KeyboardInterrupt):
                pass
        if not self._check(proposal, policy):
            return self.record
        expected = f"APPROVE {proposal.proposal_id} {proposal.action_sha256}"
        if command != expected:
            return self._transition("denied", "approval_denied", "invalid_confirmation")
        return self._transition("approved", "approval_approved", "terminal_confirmation",
                                approved_by=self._actor, approved_at=datetime.now(timezone.utc))

    @serialized
    def consume(self, proposal, policy):
        """Single-use bookkeeping only. No tool calls or execution capability."""
        if self._check(proposal, policy) and self.record.approval_status == "approved":
            return self._transition("consumed", "approval_consumed", "state_transition_only", use_count=1)
        return self.record

    @serialized
    def invalidate(self):
        if self._failed:
            raise RuntimeError("Approval unavailable: audit recording failed")
        if self.record.approval_status in {"pending", "approved"}:
            return self._transition("invalidated", "proposal_invalidated", "explicit_invalidation")
        return self.record
