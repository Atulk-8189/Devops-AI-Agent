# Context contracts

`src/agent/context.py` contains frozen, strict Pydantic schemas and pure helpers.
The optional in-process integration is described in `task-session.md`. Existing
tool calls, collectors, permissions, approvals and LangGraph execution controls
are unchanged. Nothing is persisted; no database or extra summarization call is involved.

RequestContext stores bounded progress metadata: message IDs/roles/stages (not
message bodies or reasoning prose), counters, duplicate-call IDs (not arguments),
collection statuses, timestamps and validation outcomes. It is not a substitute
for the existing request-local execution controls or discovery authorization.
Its aggregate limit is 16,000 serialized UTF-8 bytes.

TaskContext stores a caller-supplied task ID, user objective, topic, scoped hints,
paths, check/question categories, and at most 12 observations. Aggregate size is
limited to 8,000 bytes using deterministic compact ASCII-escaped JSON. Observations
are fixed-vocabulary facts, hypotheses or unknown/missing evidence, each with source,
scope, timestamp and completeness. Every observation is historical, even immediately
after creation. There is no generic free-text observation or instruction field.

Callers supply timezone-aware `now` values; helpers normalize timestamps to UTC.
Selection requires matching topic and the entire scope, rejects expired context,
and does not refresh activity. Adding a validated observation refreshes activity
but cannot revive an expired task. Expiry occurs at 15 minutes of inactivity.
Reset requires a new task ID/objective and clears observations, checks and resource
hints, retaining only project/organization and topic. Proposal expiry is independent.

Unknown fields, raw structured blobs, credential-looking text, common injection
phrases, opaque long token-like strings and excessive sizes are rejected rather
than silently truncated. Arbitrary unlabeled secrets in user prose cannot be
perfectly identified by a schema/regex: callers must supply non-sensitive user
objectives and identifiers, never copy model or tool prose into them. The narrow
observation vocabulary avoids this problem for reusable observations. Rejection
can be conservative, including legitimate long names or sensitive-word questions.
Do not log raw validation exceptions or input payloads.

Context contains no allowed repository IDs, approvals, policies or authority.
Hints never replace fresh repository discovery, current-request source validation,
or fresh Kubernetes collection. Counter bounds are serialization limits, not changes
to execution budgets. A future adapter must select safe fields explicitly and
must not serialize raw graph state or resurrect authorization from these objects.
