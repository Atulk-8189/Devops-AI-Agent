"""Immutable application-owned proposals. There is intentionally no executor."""
import hashlib
import json
import posixpath
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, AwareDatetime, model_validator, field_validator

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Target(StrictModel):
    project: Literal["My Project"]
    repository_id: Text
    branch: Text
    path: Text

    @model_validator(mode="after")
    def safe_path(self):
        if (not self.path.startswith("/") or self.path.startswith("//")
                or posixpath.normpath(self.path) != self.path or self.path == "/"
                or "\\" in self.path or any(ord(c) < 32 for c in self.path)):
            raise ValueError("An exact absolute repository file path is required")
        return self


class Payload(StrictModel):
    replacement_content: Text


class Preconditions(StrictModel):
    expected_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    original_content_sha256: Hash


class Action(StrictModel):
    target: Target
    operation: Literal["update_existing_file"]
    payload: Payload
    preconditions: Preconditions


def canonical_action(action: Action) -> str:
    return json.dumps({key: getattr(action, key).model_dump(mode="json")
                       if isinstance(getattr(action, key), BaseModel) else getattr(action, key)
                       for key in Action.model_fields}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def action_hash(action: Action) -> str:
    return hashlib.sha256(canonical_action(action).encode("utf-8")).hexdigest()


class Proposal(Action):
    proposal_id: Text
    requested_by: Text
    reason: Text
    expected_impact: Text
    risk_level: Literal["low", "medium", "high"]
    approval_required: Literal[True]
    approval_status: Literal["pending"]
    action_sha256: Hash
    expires_at: AwareDatetime
    max_write_operations: Annotated[int, Field(strict=True, ge=1, le=1)]

    @field_validator("approval_required", mode="before")
    @classmethod
    def approval_boolean(cls, value):
        if value is not True:
            raise ValueError("Approval is always required")
        return value

    @model_validator(mode="after")
    def valid_action(self):
        if self.action_sha256 != action_hash(self):
            raise ValueError("Action hash does not match proposal")
        if self.expires_at <= datetime.now(timezone.utc):
            raise ValueError("Proposal has expired")
        return self


def build_proposal(action, *, requested_by, reason, expected_impact, risk_level, policy):
    proposal = Proposal(**action.model_dump(), proposal_id=str(uuid4()), requested_by=requested_by,
                        reason=reason, expected_impact=expected_impact, risk_level=risk_level,
                        approval_required=True, approval_status="pending", max_write_operations=1,
                        action_sha256=action_hash(action), expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    policy.validate_proposal(proposal)
    return proposal


def display_proposal(proposal):
    # Never print replacement content. Escape terminal controls and redact
    # common credential formats in metadata without changing the hashed action.
    value = proposal.model_dump(mode="json")
    value["payload"] = {"replacement_content": "[omitted from safe display]"}
    def sanitize(item):
        if isinstance(item, dict):
            return {key: sanitize(val) for key, val in item.items()}
        if isinstance(item, str):
            if re.search(r"(?is)-----BEGIN .*?PRIVATE KEY|bearer\s+|(?:password|secret|token|api[_-]?key|authorization|credential)\s*[:=]|https?://[^\s/]+:[^\s/]+@", item):
                return "[REDACTED: sensitive metadata]"
            return item[:2000] + ("[truncated]" if len(item) > 2000 else "")
        return item
    value = sanitize(value)
    return "PROPOSAL ONLY — NOT APPROVED — WRITES DISABLED\n" + json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True)
