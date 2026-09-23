"""Proposal scope only; no write permission is granted by this module."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WritePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    # Deliberately cannot be enabled until an executor is separately designed.
    writes_enabled: Literal[False] = False
    policy_version: str = Field(default="1", pattern=r"^[0-9]+(?:\.[0-9]+)*$")
    repository_branches: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("writes_enabled", mode="before")
    @classmethod
    def disabled_only(cls, value):
        if value is not False:
            raise ValueError("Actual writes cannot be enabled in proposal-only mode")
        return value

    @field_validator("repository_branches")
    @classmethod
    def explicit_scope(cls, value):
        if any(not repo.strip() or repo == "*" or any(not branch.strip() or branch == "*" for branch in branches)
               for repo, branches in value.items()):
            raise ValueError("Only explicit repository and branch names are allowed")
        return value

    def validate_proposal(self, proposal):
        from src.agent.proposals import Proposal
        # Revalidate even if a caller used model_copy/model_construct to bypass
        # the schema, and reject expired or mutated actions at this boundary.
        proposal = Proposal.model_validate(proposal.model_dump())
        if proposal.operation != "update_existing_file" or proposal.max_write_operations != 1:
            raise PermissionError("Unsupported proposal operation or count")
        if proposal.target.project != "My Project":
            raise PermissionError("Project is not authorized")
        branches = self.repository_branches.get(proposal.target.repository_id, [])
        if proposal.target.branch not in branches:
            raise PermissionError("Repository and branch must be explicitly allowlisted")
        if not proposal.preconditions.expected_commit or not proposal.preconditions.original_content_sha256:
            raise PermissionError("Preconditions are required")
