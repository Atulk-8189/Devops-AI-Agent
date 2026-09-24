# Safe diagnostics boundary

Application CLI failures use deterministic categories and fixed reason codes:
validation, policy, authentication, authorization, configuration, MCP, timeout,
model, tool and unexpected. Classification may inspect exception text locally;
diagnostic output never includes that text. Generic returned MCP error messages
are sanitized for every tool, not only repository files. Authentication/security
failures still stop execution rather than becoming recoverable tool errors.

`src/safe_diagnostics.py` provides an allowlisted metadata helper, not a telemetry
system. Unsupported fields are rejected; unsafe values are omitted. Available
fields include event, optional existing request/task IDs, route, tool, operation,
outcome, category, reason code and duration. Request correlation and process-local
metrics are layered on this helper; see [observability](observability.md). No external
logging is introduced. Callers must supply application-owned codes, not prose or
payloads disguised as metadata. Secret-pattern detection is not exhaustive.

Settings repr/str expose only configured/not-configured indicators. Actual values
remain available for authentication as before. Do not log `vars(settings)`,
`dataclasses.asdict(settings)`, environments, request headers or credential fields.
AKS/Terraform collection diagnostics now omit full argument dictionaries, resource
names, selectors and repository paths. Runtime startup failures and duplicate-tool
errors omit arbitrary discovered names and underlying exception strings.

Evidence is a separate boundary: successful tool data and exact Terraform source
remain request-local, and valid source excerpts are not silently rewritten. These
may be sent to the configured model and shown in user-facing answers; this work
does not introduce a comprehensive model-input DLP filter. Never copy those
responses, prompts or answers into operational logs. Existing focused AKS reports,
TaskContext rejection, source validation, and audit content omission remain intact.

Remaining limits: regex classification can misclassify ambiguous errors; unlabeled
secrets cannot be recognized reliably; third-party/MCP stderr is not centrally
filtered; callers can still expose raw exceptions by explicitly printing uncaught
library errors. Full source, authentication objects and failed input bodies must
not be logged. Endpoint restrictions, dependency pinning, external metrics and tracing
are separate work, not implemented here.

## Safe model API error metadata

`model_call_failed` additionally supports `http_status` (integer 400–599),
`api_error_type`, `api_error_code`, and `api_error_param`. Only OpenAI SDK API
exceptions are inspected, using already-parsed structured fields (including an
Azure `error` wrapper). No response body is serialized or parsed from exception
text. Headers, request IDs from the server, messages, inner errors, prompts and
repository content are not copied.

Types/codes must match a small fixed vocabulary in `safe_diagnostics.py`;
parameters must match bounded known API field paths, such as
`messages[9].tool_call_id`. Existing sensitive-value checks also apply. Unknown,
sensitive, malformed or missing values are omitted, not echoed or stringified.
This intentionally may omit new provider codes. Event serialization revalidates
these fields. They are not new metric labels. Public CLI wording, exception
propagation, and authentication/authorization classification remain unchanged.

## Temporary outgoing-request structural diagnostic

Before each model invocation within an observed request, `model_request_structure`
records a content-free projection of the outgoing messages. Its request ID and
`model_call_sequence_number` correlate with model started/completed/failed events.
The sequence resets for every request. This instrumentation does not change or
reject requests, repair pairing, or enable a verbose payload/debug mode.

The projection includes message count/order, fixed role/type labels, text character
counts, null/string/list content classification, tool-call counts, allowlisted tool
names, ID-presence/matching booleans, result counts, and overall pairing/structure
validity. No actual tool-call IDs or hashes are emitted. It never emits prompts,
arguments, schemas/descriptions, repository identifiers/paths, source, credentials,
raw responses, or raw request JSON. Serialized message length is computed locally
and only its integer length is retained; it is not an exact HTTP wire-byte count.

This is a structural check of the application's text/function-call format, not a
complete provider schema validator or a claim that Azure will accept the request.
Snapshots are capped at 128 messages; `snapshot_complete=false` flags a capped or
unavailable list, and totals then cover inspected messages only. Diagnostic
failures cannot stop model execution. No additional persistence or metric labels
are introduced. Remove this temporary instrumentation after diagnosing the HTTP
400. A successful-versus-failed live comparison still requires an independently
authorized live run; offline tests do not establish the cause of the real failure.

## Fixed issue: orphan validation ToolMessage

In `workflows.py`, the generic agent's unknown-tool, schema-validation and
repository-discovery rejection branches previously returned `validation_error`
without adding the assistant message containing the rejected tool call. The
error node then appended an orphan ToolMessage. These branches now retain the
assistant call before the error node appends its matching result. Offline graph
regressions check pairing at each model request, including recovery and stops.

The observed live failure had only two MCP dispatches and a 128-character error
tool message: the repository-discovery prerequisite rejection, not a dispatched
file read. Policy rejection remains a hard failure and sends no subsequent model
request; this fix does not turn security failures into recoverable tool feedback.
