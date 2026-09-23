# Proposal-only mode

Run explicitly with operator-owned local inputs:

```sh
python -m src.agent.proposal_only REQUEST.json POLICY.json
```

This entry point performs existing read-only MCP collection, validates and displays
a proposal, then stops. It has no executor, approval prompt, write API, or Terraform
command. The normal agent routes and MCP configuration are unchanged.

Policy file (substitute an explicitly authorized repository ID):

```json
{
  "writes_enabled": false,
  "repository_branches": {"your-repository-id": ["main"]}
}
```

The default allowlist is empty. `writes_enabled: true` is rejected in this stage.
An allowlist permits a proposal only, never a write. Repository discovery does not
populate this allowlist. Keep policy files under operator control, not model control.

Request file shape (replace placeholder hashes with actual values):

```json
{
  "question": "Improve this Terraform file's explanatory comment",
  "expected_impact": "Comment-only source change; any automation effects remain unverified",
  "action": {
    "target": {
      "project": "My Project",
      "repository_id": "your-repository-id",
      "branch": "main",
      "path": "/terraform/main.tf"
    },
    "operation": "update_existing_file",
    "payload": {"replacement_content": "exact complete replacement text\n"},
    "preconditions": {
      "expected_commit": "40 lowercase hexadecimal characters",
      "original_content_sha256": "64 lowercase hexadecimal characters"
    }
  }
}
```

The application generates the proposal ID, local requester identity, pending
approval status, conservative high risk, five-minute expiry, and canonical action
hash. The hash covers target, operation, exact replacement text, and preconditions.
No shell-command field is accepted; replacement text is inert file data.

Current evidence integration deliberately reuses the Terraform collector: the
repository must be the uniquely discovered repository named `My Project`, and the
target must be a retrieved `.tf` file under `/terraform` on `main`. `main` is not
automatically allowlisted. Other branches can be represented by the schema but
the CLI refuses them rather than silently substituting main-branch evidence.

The original-content hash is checked against the current request's retrieved text
(UTF-8). The commit ID is operator supplied and explicitly marked **not verified**;
the existing tools do not provide a commit-bound snapshot here. No execution may
rely on this evidence in a future stage without fresh, commit-bound verification.

Display omits replacement content, escapes terminal controls, bounds metadata,
and redacts credential-looking metadata. Do not put secrets in descriptive fields:
pattern-based redaction cannot recognize every unlabeled secret. CLI failures use
a fixed safe message rather than raw model-validation or MCP exceptions. No
proposal persistence or execution is implemented.

## Optional human approval recording

```sh
python -m src.agent.proposal_only REQUEST.json POLICY.json --approval-audit NEW-AUDIT.jsonl
```

The audit path must not already exist. Use a trusted private directory outside
the repository. The file is created owner-only and receives append-only JSONL
events with timestamps, event IDs, and a SHA-256 chain. An existing file is never
overwritten or loaded as approval authority. Local owners can still tamper with
their files; the chain is not a substitute for a remote trusted audit service.

Only interactive terminal input can record approval:

```text
APPROVE <exact-proposal-id> <exact-action-sha256>
```

Piped input, EOF, an empty response, or any other command denies approval. The
application obtains the actor from the OS user identity, not proposal text.
Inspect the exact operator-owned request before approving: the safe terminal
summary deliberately omits replacement contents and is not an executable diff.

Approval binds the full proposal snapshot (including expiry), action hash, ID,
and policy configuration/version. Policy defaults to version `1`; version changes
or allowlist changes invalidate the approval. Policy is rechecked after terminal
input and before consumption. Expiry is never extended by the approval layer.

States: pending → approved/denied/expired/invalidated; approved → consumed/expired/
invalidated. Consumption is an in-memory state transition only, with no tool or
executor. A second consumption cannot succeed. CLI approval exits without consuming.
All approval authority disappears at process exit; a restart requires a new
proposal and fresh approval. Audit history does not resurrect approved records.

Events include creation, request, approval, denial, expiry, invalidation, and
consumption. Audit reasons are fixed application codes; contents and free-form
proposal descriptions are excluded. Target fields are fingerprints rather than
raw values to prevent sensitive paths or names from leaking. Match fingerprints
against the operator-owned proposal when investigating history. Audit failures
poison the session and prevent approval or consumption. No automatic expiry
background process exists: expiry is recorded on the next attempted transition.

**Actual writes remain disabled. No Azure DevOps/Kubernetes writes or Terraform
commands are available through this approval layer.**
