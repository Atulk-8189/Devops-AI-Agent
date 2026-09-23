# Local structured observability

The `src.observability` standard-library logger emits JSON metadata at INFO level.
It installs no handler or file sink: an embedding application can attach a local
handler using normal Python logging configuration. Terminal answers are unchanged.
This is local structured observability only. External metrics, OpenTelemetry, dashboards,
and external log shipping are future work.

Each invocation of `main` generates one request ID. An existing task ID is included
when available. Context-local correlation isolates concurrent requests and is reset
on exit, including failures. The selected route accompanies subsequent events.
Actual tool-call IDs are retained; model calls have no fabricated tool IDs.

Events cover request started/completed/failed, route selected, model call
started/completed/failed, tool attempt, policy decision, MCP dispatch/result/failure,
evidence collection/result, and duplicate/budget execution stops. Completion means
the application returned, not that the model answer is correct or evidence complete.
MCP completion similarly does not assert valid Kubernetes or Terraform evidence.

The schema supports event, UTC timestamp, level, request_id, task_id, route,
tool_call_id, mcp_server, tool_name, operation, purpose_code, policy_decision,
reason_code, outcome, duration_ms, attempt_count, dispatch_count, remaining_budget,
evidence_status, truncated and error_category. Unavailable fields are omitted.
Attempts count policy checks; dispatches count actual tool invocations. Model and
dispatch duration includes SDK-level retries. Purpose is a safe code, never model
reasoning. Evidence status reflects collection coverage, not verified conclusions.

Unknown fields are rejected and unsafe values omitted using safe-diagnostics
protections. No prompts, responses, full arguments/results, repository or Terraform
source, Kubernetes objects, pod logs, credentials, proposal content, or approval
confirmation details are recorded. Secret detection is conservative, not exhaustive;
the primary boundary is recording selected metadata only. Error text is classified
locally and never serialized. Logging failures do not change application behavior.

Limitations: no durable trace, token accounting, per-server startup timing, or
model-answer correctness assessment. Existing non-event diagnostics remain. Direct
collector calls outside `main` have no fabricated request correlation. Policy
allow events do not imply schema validation or discovery prerequisites passed.

## Request-wide deadline and aggregate bounds

The same in-process request owner now holds a budget. Its metadata counters use
the `RequestUsage` schema also available on `RequestContext`; they never become
TaskContext memory. Start time, monotonic elapsed time and remaining deadline are
tracked for the entire request, not reset when routes/components change.

Defaults:

| Outer boundary | Limit |
| --- | --- |
| Whole-request deadline | 300 seconds |
| Model calls | 12 |
| Tool attempts (policy checks) | 64 |
| MCP dispatches | 64 |
| Aggregate returned content | 512,000 characters |
| Aggregate model/tool input | 1,000,000 characters |

Set `REQUEST_DEADLINE_SECONDS` in the existing environment configuration (integer
1–1800). Other limits are named constants in `src/request_budget.py`. Five minutes
allows the existing eight-call healthy AKS path and local MCP startup; 64 attempts
allows bounded Terraform discovery plus file reads. These are ceilings, not targets.
The generic five-execution budget, AKS twelve-call budget, Terraform discovery and
pagination limits, per-result/message bounds, and TaskContext limits still apply.
Component limits constrain individual workflows; outer limits constrain their total
work and repeated model input, even if every individual operation is small.

Checks occur before policy attempts, model calls, dispatches, result normalization,
review/diagnosis processing, and final answers. An asyncio deadline also cancels
in-flight async work, including startup. Existing cleanup is run. An individual
transport/SDK timeout remains distinguishable from the whole-request deadline.

Input accounting covers model messages/tool schemas/options and dispatched tool
arguments. Results include model content/tool-call arguments and MCP message
content, including error content. Strings are counted by character, with structural
overhead for containers; this is not wire-byte or token accounting. Measurement
stops at the remaining cap, rejects unmeasurable/deep structures, and never logs
payloads. Repeated conversation input counts again on every model call.

When a bound is crossed, no more collection or model diagnosis occurs. A fixed
terminal message explicitly reports incomplete evidence. Non-session callers
receive a `BoundedResult` containing counters and bounded, untrusted previews of
previously accepted successful tool results. Oversized results are not accepted.
Previews are request-local, hidden from repr/logs, not verified conclusions, and
may be truncated by the existing 8,000-character envelope limit. They must not
replace exact Terraform evidence. Session calls return no progress update, so
partial results cannot enter TaskContext. No automatic continuation or retry occurs.

`request_bounded` and the final bounded outcome contain only correlation metadata,
budget_type, limit, current_usage, remaining_budget and reason codes
(`deadline_exceeded` or `aggregate_limit`). Current usage on a bound event includes
the attempted charge; stored counters retain accepted usage. No source or partial
evidence is emitted in events. Bounded results are not complete investigations.

Limits apply to application requests through `main`; standalone helper calls retain
their existing component behavior. Synchronous Python processing cannot be forcibly
preempted; checks catch overruns at the next boundary. Cancellation/cleanup depends
on cooperative SDK/MCP code and may extend wall-clock shutdown beyond the deadline.
The result cap acts after transport receipt, not as a streaming network/memory cap.
No external metrics, external logging, durable storage, permissions or write capabilities
are introduced.

## In-process metrics

`src.metrics.snapshot()` returns a detached JSON-compatible snapshot. It has six
groups: requests, model, tools, mcp, safety, and evidence. Each named counter
contains `total` and independent `dimensions` breakdowns. For example:

```json
{"model": {"calls": {"total": 2, "dimensions": {"route": {"generic": 2}}}}}
```

The existing event emitter forwards the same sanitized event to a small, lock-
protected aggregator before logging it. No raw event or payload is retained.
Metrics work even when no INFO logging handler is enabled. Metrics and logging
failures are isolated from each other and from request execution.

| Group | Counters and meaning |
| --- | --- |
| requests | started, completed (including bounded returns), failed, bounded_incomplete |
| model | calls started, failures, stopped by admission limits or an in-flight request deadline |
| tools | attempts, dispatches, successes, failures, policy_rejected |
| mcp | dispatches, successes, failures; same transport boundaries as tool dispatch outcomes |
| safety | schema_validation_failures, duplicate_stops, budget_exhaustion, deadline_exhaustion, classified_errors |
| evidence | collection attempts, successes, failures, truncated, bounded |

Terminal request events count incomplete requests once when earlier events reported
partial/unavailable/truncated evidence or an execution stop. Request-level incompleteness
is separate from exceptions and from correctness: a completed request is not necessarily
a successful diagnosis. Classification counts are classified *events*, not unique errors;
one failure may be classified at more than one boundary. Safety exhaustion is counted at
the stop event, not again at the terminal request event. Evidence successes mean returned
collection results, not proof of healthy infrastructure or valid model conclusions.
The evidence bounded counter includes partial/truncated result events and aggregate
result-size stops. Route collection and individual tool events have different granularity;
these counters are not unique Kubernetes resource or repository file counts.

Only fixed vocabulary is allowed as dimensions:

- route: generic, aks, terraform, azure_devops
- tool_name: names in the existing read-only tool allowlist
- mcp_server: aks, azure-devops
- error_category: existing safe-diagnostic categories

Breakdowns are independent rather than arbitrary label combinations. Unknown values
are omitted, never used as new labels. Request starts often have no route yet. IDs,
timestamps, reason text, object names, paths, arguments, prompts, responses, source,
credentials, proposals, and approval details are not metric labels or stored values.
Metrics retain only integers and predefined names, deliberately excluding detailed
request content to prevent leakage and unbounded cardinality.

Counters are cumulative for the current process lifetime, shared safely across requests,
and reset on process restart. `src.metrics.reset()` explicitly clears all counters under
the same lock (useful in tests or an embedding application); requests never reset them.
An independent `Metrics()` instance starts empty. Snapshots cannot modify live counters.
There is no persistence, database, network endpoint, exporter, dashboard, or dependency.
OpenTelemetry, external telemetry/metrics backends, and durable storage remain future work.

Remaining gaps: no latency histograms, token/cost counters, cross-process totals, or
durable history. Counters cover instrumented application requests, not unobserved direct
helper calls. MCP transport success does not imply accepted or complete evidence.

## Adversarial validation

The offline suite injects credential-like values, fake roles/instructions, malformed
and oversized evidence, forbidden tool requests, tool/model failures and exhausted
budgets. It checks that events exclude payloads, metrics retain only fixed labels,
concurrent request IDs/budgets remain isolated, and failing sinks do not interrupt
execution. Tests exercise simulated failures, not real infrastructure attacks.
See [security validation and remaining gaps](security.md#step-9-adversarial-and-failure-validation).
