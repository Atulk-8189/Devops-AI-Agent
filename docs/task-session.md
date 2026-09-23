# Optional in-process follow-ups

Existing one-question CLI usage is unchanged. Applications may explicitly own a
session in the same Python process:

```python
from src.agent.task_session import TaskSession

session = TaskSession()
await session.request("Investigate Task Manager")
await session.request("Now check the service", follow_up=True)
session.reset()
```

Use a separate instance per user/task. Calls are serialized per session; there is
no global context, disk storage, database, background process, or approval state.
By default a request starts a new task. Follow-up reuse must be explicit. A newly
named target/topic starts fresh. A small exact reference grammar supports "the
service", "what about the pods?", "that repository", "show that file again", and
"the Terraform module". Repository references require a unique remembered ID/name;
Terraform references select the review route, not a guessed module block. The
collector must rediscover modules from current source. Unresolved ambiguous or
cross-topic references ask for clarification and clear old context. Without prior
context a request runs independently, with no inherited identifiers.
Reset removes all hints. Fifteen minutes of inactivity discards the context;
subsequent requests establish their own identity and intent.

The session passes route-relevant identifiers and the original user objective as
untrusted hints. It never passes observations, health states, source files, model
answers or previous tool messages. AKS always recollects its full existing bounded
evidence; Terraform always rediscovers and rereads source with exact excerpt
validation. ADO discovery state, tool budgets and duplicate-call state are new for
every request. Remembered repository IDs are hints, never usable discovery IDs.
A context-enabled generic request with no successful tool result reports that no
fresh evidence was collected instead of displaying an unsupported model answer.

After completion, fixed check categories and sanitized identifiers become a new
bounded snapshot. Observations describe collection, not verified health or model
conclusions. Unknown/missing steps remain unresolved. The snapshot keeps original
and current user objectives; it replaces rather than accumulates raw histories.
Failures discard context. Unsafe questions are processed without context and are
not retained. Unsafe identifiers/paths are omitted. Compatible observations may
survive a follow-up; scope/topic changes discard the old observations. Deterministic
trimming retains the newest 12 observations (canonical JSON breaks timestamp ties),
drops optional paths before older observations to fit 8,000 bytes, and sets
`context_trimmed`. Unsafe values are rejected before trimming, never sliced. If
mandatory metadata cannot fit, context is discarded without failing the answer.

Every observation remains historical; after five minutes it is explicitly labeled
stale on selection/access. Neither freshness label supplies current evidence, and
reading context does not refresh activity. Explicit project/repository/branch/
cluster/namespace scope references discard prior hints even when the reference
might match; exact references such as "that repository" are the exception, not
permission to reuse discovery authorization. This conservative rule avoids
silently preferring an old target.
Invalid cached schemas or a backwards clock discard session context. Unicode
normalization and extra credential patterns improve rejection, but do not provide
exhaustive secret detection. No authorization or approval state is cached.

Limitations: conservative topic/reference matching, no general conversational
pronoun resolver, no caching, no source or reasoning reuse, no interactive CLI
loop. Existing collector breadth is unchanged (a service follow-up runs the AKS
collector, not a newly narrowed service workflow). Prose secret detection retains
the context-schema limitations: never intentionally provide credentials in a user
objective. Mutable evidence always requires fresh collection; hints do not prove
it is present or current.

Malformed Terraform review output or an AKS diagnosis fallback does not update
context. Raised MCP/authentication failures leave no old context to reuse. New
requests always get new execution counters and duplicate-call state; one task's
history cannot be used to cite files missing from the current Terraform collection.
Existing one-shot routing, policies, proposal/approval expiry, and MCP setup are
unchanged. No persistent-memory feature or interactive CLI loop is present.
