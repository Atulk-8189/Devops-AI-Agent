# Mock controlled-write lifecycle

`src/agent/mock_executor.py` is an in-memory simulation, not a production executor.
It is deliberately not wired to the CLI, MCP, Azure DevOps, Kubernetes, or Terraform.
The existing CLI continues to record approval and stop; `writes_enabled` remains
false and cannot be enabled.

An application/test can seed a `MockRepository` with a target, commit and content,
then pass a proposal, terminal-approved `ApprovalSession`, and current `WritePolicy`
to `MockExecutor.execute`. The executor requires the same audit log as the session.
It does not accept external storage implementations or standalone approval records.

The candidate proposal is checked for changes but never used as execution arguments.
Only the session's stored snapshot is applied. Revalidation covers proposal, approval,
expiry, exact IDs/hashes, full policy fingerprint/version, allowlists and preconditions.
Both original content hash and simulated current commit must match; missing targets
are precondition failures. The in-memory commit is opaque and deterministic, not a
real Git commit.

The session and repository are locked across validation, dispatch and consumption:

1. Revalidate and compare preconditions.
2. Durably audit `execution_started`.
3. Dispatch the in-memory replacement.
4. Consume approval exactly once, after dispatch.
5. Audit success, read the in-memory target, verify contents, and audit verification.

Validation, expiry, policy and precondition failures do not consume approval. The
`fail_dispatch` test switch simulates failure before a mutation is accepted and also
does not consume approval. Verification switches simulate mismatch or unavailable
reads after successful dispatch; these leave approval consumed, never rollback or
perform a second write, and report `verification_failed`.

Audit failures before dispatch prevent mutation. Failures after dispatch return
`audit_failed` and poison the session to prevent reuse even if the consumed event
could not be persisted. The in-memory mutation may already have occurred. No audit
system can guarantee recording an event while its storage is failing.

Results have strict status/error enums, changed and verification indicators, and
target fingerprints instead of potentially sensitive names/paths. Audit entries use
the existing JSONL hash chain and include an execution ID. They never include source
contents or raw exceptions. An absent approval is rejected and audited with unavailable
identity placeholders rather than trusting arbitrary model-supplied identifiers.

Limitations: session-local simulation only, no crash recovery, no real server-side
conditional updates, no network failure reconciliation, no production verification.
Local audit files retain the existing owner-tampering limitation. None of these
results authorize a real operation.
