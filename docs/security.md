# Security model

The application is read-only and fails closed. Azure DevOps is limited to project
`My Project`, branch `main`, repository listing, pipeline-definition listing,
directory listing, and file-content reads.

AKS permits only allowlisted inspection operations. Kubernetes reads are limited to
`get` and `describe`, the `default` namespace, and a small resource allowlist.
Secrets, identity/RBAC resources, shell syntax, command execution, and mutation
operations are rejected before tool execution.

Do not commit `.env`, API keys, Azure PATs, passwords, certificates, or private
keys. The supplied `.gitignore` excludes common local environments, caches, logs,
build outputs, and certificate/key extensions.

Configuration is loaded from the local `.env`/environment at startup. The endpoint,
API key, and AKS MCP path are validated before the agent starts. The API key is passed
only to the Azure OpenAI client and is never printed or logged. When configured in
Settings, `AZURE_CONFIG_DIR` is explicitly forwarded to both MCP subprocesses.
Only that configured variable is added; the parent environment is not copied wholesale.
Existing SDK default inheritance and the AKS `USE_LEGACY_TOOLS` setting are unchanged.

MCP discovery is separate from execution: only the six policy-approved tool names are
exposed, duplicate names fail initialization, and policy validation runs before each
agent-selected call. MCP sessions are closed on successful and failed requests.

## Step 9 adversarial and failure validation

These are deterministic, offline simulations using mocked OpenAI/MCP calls and the
in-memory mock executor. They are not attacks against production systems or a claim
that prompt injection is solved. No real writes, Terraform commands, Azure permission
changes, MCP configuration changes, or new dependencies were used.

Coverage combines `tests/test_adversarial.py` with the existing suites:

| Category | Boundaries exercised |
| --- | --- |
| Injection and malicious output | Fake system/developer messages stay inside tool data; model attempts to select forbidden tools are rejected; malformed, deeply nested, oversized, ANSI and credential-like content |
| Arguments and scope | Missing/unknown fields, types/enums, schema limits, project and namespace rejection, command syntax, Secrets, traversal-like proposal paths; Terraform source filtering remains covered by its evidence suite |
| Work exhaustion | Duplicate/multiple calls, loops, component budgets, aggregate input/results/model/attempt/dispatch caps, deadline cancellation and preservation of bounded evidence (`test_request_budget`, `test_openai_flow`) |
| Context | Poisoned/malformed and sensitive observations, conflicting scope, stale history excluded from prompts, reset/expiry and session isolation (`test_context_hardening`, `test_task_session_e2e`) |
| Diagnostics and metrics | Authentication/authorization/startup/transport/model/unexpected failures; metadata-only events, fixed metric labels and sink-failure isolation (`test_safe_diagnostics`, `test_observability`, `test_metrics`) |
| Cross-request isolation | Distinct request IDs, independent budgets and task sessions, one failing request alongside another request |
| Approval and mock writes | Tool/model confirmation text cannot create approval; hash/target/policy mismatch, expiry, consumption and single use remain covered by `test_approval` and `test_mock_executor`; network/subprocess guards protect mock-only tests |
| MCP identity | An allowlisted name from the wrong server is rejected even when there is no duplicate |

Two small defensive fixes resulted from failing regression tests:

- Discovery now checks each approved tool name against its expected MCP session
  using the existing `TOOL_SERVERS` mapping, before exposing any tools. Duplicate
  detection still runs first. This prevents a different server impersonating an
  approved tool name; it does not authenticate the server binary itself.
- Generic normalization bounds structural depth at 64 and catches normalization
  failures. Unusable evidence gets a fixed unknown/incomplete envelope, not a raw
  recursion exception or invented content. Exact Terraform source validation and
  dedicated collectors are unchanged.

Remaining scope and trust gaps:

- Generic `az_aks_operations` read arguments do not have a configured subscription
  or resource-group allowlist. Kubectl namespace checks and schema rejection of
  unsupported parameters are not substitutes for Azure resource authorization.
  Selecting authorized Azure scopes requires a separate policy decision; no new
  values or permissions are inferred in this test step.
- Read-only repository paths are not a general content-sensitivity boundary;
  Terraform filtering and proposal path validation have narrower purposes.
- Mocked model behavior verifies message roles and enforced tool boundaries, not
  the behavior of every live model or semantic truth of its answers. Tool evidence
  can still contain sensitive content; exact source is not blindly redacted.
- Secret-pattern detection is not exhaustive. Local process compromise, malicious
  installed server binaries, third-party stderr, Azure RBAC, endpoint security,
  live network failures and supply-chain integrity remain outside these simulations.
- Cooperative cancellation, post-receipt result limits, and process-local telemetry
  retain their documented limitations. Sanitized MCP startup failures may lose
  underlying authentication detail. Library exceptions may still propagate to
  embedding callers; the CLI applies safe diagnostics rather than printing them.

Step 9's requested local controls and validation can be considered complete, with
these limitations recorded—not a production security certification or permission
to enable real writes.
