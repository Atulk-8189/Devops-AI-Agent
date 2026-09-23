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
