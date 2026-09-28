# Local DevOps AI Agent chat

This is a thin Chainlit interface to `src.agent.orchestration.orchestrate_request()`.
The existing CLI, `run-agent.sh`, policies, workflows and MCP configuration are unchanged.
There is no separate agent, model configuration, tool execution path or write control here.

## Compatibility and installation

The current root `.venv` uses Python **3.14.7**. Chainlit **2.12.0** declares
`Requires-Python: >=3.10,<3.14.0`, so it cannot be installed into that environment.
Package metadata was checked before implementation. Nothing was installed.
Python 3.13 was not found on the current shell PATH.

Use a separately provisioned Python 3.13 interpreter. From the project root:

```sh
python3.13 -m venv frontend/.venv
frontend/.venv/bin/python -m pip install -r frontend/requirements.txt
frontend/.venv/bin/python -m pip check
```

This installs the existing backend requirements plus pinned Chainlit into the
frontend environment only. It does not update root `requirements.txt` or `.venv`.
The full dependency combination is not yet installation/browser-tested. Root
dependencies remain mostly unpinned; a successful resolver is not a regression test.
Do not bypass Chainlit's Python constraint with `--ignore-requires-python`.

## Launch from the project root

```sh
(cd frontend && .venv/bin/chainlit run chainlit_app.py --host 127.0.0.1 --port 8000 --headless)
```

Open **http://127.0.0.1:8000**. Starting inside `frontend/` keeps Chainlit's config
and generated support files here. Backend orchestration still reads the existing
project-root `.env`; no credential files should be copied into the frontend.
The existing Azure CLI authentication and MCP executable prerequisites still apply.
No model/MCP request is made until a question is submitted.

### Restart after frontend changes

The server keeps Python modules in memory; watch/reload is deliberately disabled.
Editing `chainlit_app.py` or `response_safety.py` does **not** update a running server.
Refreshing a browser tab does not reload Python code either.

After any active investigation finishes, press Ctrl-C in the frontend terminal and
run the launch command above again. Start a new chat to avoid confusing older chat
messages with new responses. Do not enable watch mode as a workaround: reloading
mid-investigation could conflict with MCP cleanup.

At INFO level the new process emits `frontend_response_safety_loaded` with
`implementation=frontend.response_safety.filter_response` and
`revision=targeted-credentials-v1`. Each actual filter decision includes the same
marker, fixed categories, and `display_action=displayed|redacted|withheld`. No answer,
question, credential, matched substring or raw tool data is logged. Absence of INFO
output alone does not prove stale code: logging levels can suppress these markers.

The old message “The final response was withheld because it contains sensitive-looking
text.” is not a withholding branch in the current renderer. Seeing that message from
an older chat/process does not establish a new failure of the current safety filter.

Run only one frontend process, without reload/watch, debug, multiple workers, a
reverse proxy, port forwarding, or a public bind address. The app rejects non-loopback
hosts and watch/debug modes. It is for the trusted local operator, not authenticated
multi-user deployment. Stop with Ctrl-C; do not run it alongside other embedded
agent execution within the same process.

## Behavior and boundaries

- Chat input, queued/running status, Markdown final answers and sanitized errors.
- Presentation is domain-independent: AKS, Azure DevOps, Terraform and generic
  answers use the same renderer. An answer allowed by existing safety controls is
  sent as the **original full string**, with no summarization, rewriting, trimming,
  length cutoff, section extraction, reordering or fixed report template.
- Markdown headings, lists, tables and code fences are handled by Chainlit. Valid
  JSON objects/arrays receive a JSON syntax-highlighting hint, but their original
  text is never reserialized: whitespace, field order, duplicate keys and numeric
  precision remain intact. Long responses are sent in full and remain scrollable;
  no hidden prefix-only preview or truncation is used.
- Progress and frontend-generated error/safety notices are separate messages,
  never added to the agent answer. Existing security redaction/withholding is an
  explicit exception to verbatim display: a separate **Frontend notice** explains
  that the display is modified or unavailable. The original unsafe response is
  not exposed via a second view. No new content filters were added for presentation.
- Backend evidence truncation, budgets or missing evidence remain whatever the
  backend reported; the UI cannot restore evidence the agent did not return.
- One shared async lock serializes requests across all chats in the process,
  including backend cleanup. This avoids the backend's global MCP subprocess
  registry conflicting between simultaneous requests.
- Every message is a new investigation. No chat transcript, historical observation,
  `TaskContext`, approval or browser-provided authorization is sent to the agent.
- No raw logs, tool arguments/results, prompts, reasoning, environment values or
  exception messages are forwarded to the browser. Only the backend's final answer
  is considered for display; bounded outcomes do not expose retained evidence.
- Final-answer filtering is frontend-only. Ordinary technical words, resource IDs,
  IPs, URLs without credentials, status messages and Terraform references are not
  automatically sensitive. Credential assignments, recognizable token formats,
  authorization headers, URL credentials and signed query values are redacted.
  Redaction is explicitly labeled: the displayed copy is not exact source evidence;
  backend source, validation, metadata/context filtering and policies are untouched.
- Private keys, Kubernetes Secret data, ambiguous/multiline credential values and
  credential matches whose Unicode normalization changes offsets are withheld.
  Control/hidden characters also fail closed. Some unquoted credential fields are
  removed to end of line so a multiword value cannot partially leak.
- `frontend_response_filter` logs only fixed outcome/category labels and categories
  that would have triggered the old filter (keyword, URL, long identifier). It never
  logs matches, snippets, values or response bodies. No failed live response is
  retained by this feature. Enable INFO for `frontend.response_safety` to see it.
- Detection is not exhaustive: arbitrary unlabeled secrets, encoded data and
  unfamiliar credential formats may be missed. Credential-shaped harmless examples
  may be redacted. No credential is checked against a live service. Do not submit
  credentials. HTML rendering, uploads, audio and Chainlit-managed MCP remain off.
- Default `RequestServices` are used: no branch executor or approval interface is
  supplied. Existing read-only policy and budgets remain authoritative.
- No data layer, persistent chat memory or external telemetry integration is added.
  Chainlit keeps active chat state in memory and may generate local support files.
  Existing backend terminal logging is unchanged and is not a browser log feed.
- This first version does not stream tokens, expose detailed tool progress/evidence,
  or implement follow-up memory. These need separate review; it does not capture
  global stdout or change backend observability to simulate them.
- Cancellation propagates to orchestration and its existing cleanup. Browser stop,
  disconnect and SDK cleanup behavior still need validation in a compatible runtime.

## Offline adapter tests

From the project root, using the existing environment (no Chainlit install needed):

```sh
.venv/bin/python -B -m unittest discover -s frontend/tests -v
```

These tests stub the Chainlit boundary and orchestrator; they make no model/MCP calls.
They validate the adapter, not a running Chainlit server or live infrastructure.

## Investigation interface

The welcome screen provides four responsive capability cards, each submitting its
exact suggested question through Chainlit's normal user-message flow. Target
selection and policy remain backend-owned. Custom elements follow the
[Chainlit custom element API](https://docs.chainlit.io/api-reference/elements/custom).

Progress listens to the existing `src.observability` structured local events and
allowlisted orchestration lifecycle log templates while holding the existing
process-wide request lock. A context-local listener accepts
only known stage labels, route categories, and coverage flags. It never forwards
log text, tool arguments, tool output, or model reasoning. No token streaming or
external exporters are added. Stages may repeat when the agent makes more calls.
No event means no claimed progress; completion means execution returned, not that
the infrastructure is healthy.

Connection status is Unknown / Status unavailable for all three services because
existing interfaces provide no reliable current authentication/connection snapshot.
Configuration, successful analysis, and tool discovery are not treated as access
verification. Connected and Disconnected are deliberately not claimed. No probes
or privileged operations are performed to populate the UI.

Each status message stores investigation metadata in the existing SQLite step
metadata and exposes an Investigation details side element. This includes frontend
UTC request timestamps, backend-selected category when emitted, read-only mode,
explicit coverage limits, and selected evidence fields from display-approved JSON
answers. Agent-reported targets are labelled as such. Unstructured answers are
preserved without heuristic scope extraction; details absent from the output stay
unknown. Resume relies on Chainlit restoring stored steps and elements; no previous
investigation state is fed into a new request. Historical metadata is not a current
connection verification.

The existing formatter and exact raw-response attachment remain in place. The
pre-existing credential redaction/withholding security exception is unchanged;
therefore exact preservation cannot be promised for sensitive responses. No backend,
MCP, authorization, or security-policy modifications are included.

Validation (without starting a server):

```sh
.venv/bin/python -m pytest frontend/tests -q
.venv/bin/python -m pytest tests frontend/tests -q
frontend/.venv/bin/python -m unittest discover -s frontend/tests -q
```

Browser layout and real service access require separate manual validation; the
frontend was not launched during implementation.

## Live activity panel

Investigation status now includes a compact, collapsible activity history, persisted
with its status message and custom element in the existing local SQLite history.
The current stage is prominent; in-flight calls show a spinner, matched successful
completion events show a checkmark, and failures/stops have distinct indicators.
An unmatched call is labelled "Completion event not observed" when the request
ends, never silently marked successful. Repeated calls retain their execution order.

The frontend projects these existing events without changing backend execution:

| Existing event or exact application log template | Displayed activity |
| --- | --- |
| `request_started` | Investigation initialized |
| `agent startup: initializing Azure OpenAI and MCP runtime` | Starting MCP connections and tool discovery |
| `tool discovery complete: %d allowed tools` | MCP initialization and tool discovery returned |
| `route_selected` | Investigation category selected |
| `model_call_started` | Sending request / requesting additional analysis from LLM; waiting for response |
| `model_call_completed`, `model_call_failed`, `model_call_stopped` | Model response received, failed, or stopped |
| `mcp_dispatch` | Executing the allowlisted tool, with its known purpose and operation |
| `mcp_result`, `mcp_failure` | Receiving MCP response or tool failure |
| `evidence_result` with failure outcome | Evidence unavailable |
| `request_bounded`, `execution_stopped` | Collection limit reached; awaiting cleanup |
| `cleanup_failed` | Connection cleanup failed |
| `agent shutdown complete` | MCP and model client cleanup completed |
| `request_completed`, `request_failed` | Completed, bounded/stopped, or safely described failure |

MCP calls are paired by `dispatch_count`; LLM calls use
`model_call_sequence_number`. The frontend also observes direct request return,
exception, and cancellation when necessary. Listeners and their logging levels are
restored inside the existing serialization lock, after orchestration cleanup.
Only closed labels and allowlisted tool/operation/category fields enter the panel;
exception text, log arguments, raw tool arguments/results, and model content do not.

Elapsed values use the local monotonic clock, measured from frontend request receipt;
call durations measure the interval between observed start and completion events.
Values are explicitly labelled "at last event" and freeze in stored history. There
is no timer-driven stage advancement or fabricated network send acknowledgment.
Sending and waiting are one stage because only the model invocation boundary is
observable; later model invocations are labelled additional analysis.

Not currently observable: per-service connection/authentication verification,
separate evidence-processing intervals, and the start of connection shutdown.
Tool events also do not identify the resource kind (for example pods), so a general
`kubectl_resources` call is labelled Kubernetes resources, not "listing pods".
No unsupported stage is inserted. Tracking these additional boundaries would require
backend instrumentation and separate approval. The existing cleanup-completed log
is used only after cleanup actually returns; the earlier human-readable
"Investigation complete. Generating response..." log is intentionally not used.
