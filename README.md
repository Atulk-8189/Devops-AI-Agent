# DevOps AI Agent

An Azure OpenAI-only, read-only DevOps assistant for Azure DevOps repository investigation,
AKS investigation, and evidence-backed Terraform reviews. It uses LangGraph to
orchestrate local Model Context Protocol (MCP) tools; it does not apply Terraform,
mutate Kubernetes resources, or change Azure resources.

## Architecture

Local metadata-only structured events and request/tool correlation are described in
[Structured observability](docs/observability.md). No prompts, tool payloads, source
files, or credentials are logged by this mechanism; no external exporter is installed.

The agent uses Azure OpenAI with `gpt-5-mini` through the OpenAI Python SDK. LangGraph
routes a request through `START → agent → tools → agent → END`; automatic SDK tool
execution is disabled, and the agent accepts at most one MCP call per model turn.

For each question, MCP opens Azure DevOps and AKS stdio sessions, discovers and
allowlists their tools, then closes both sessions when the question finishes.

Optional related requests can share an in-process `TaskSession`; see
[Task session usage and security boundaries](docs/task-session.md). Pass
`follow_up=True` explicitly to reuse safe intent/identifier hints, or call
`session.reset()` to clear them. Observations are historical (stale after five
minutes), and sessions expire after fifteen minutes of inactivity. Memory never
authorizes tools or replaces fresh AKS, Azure DevOps, or Terraform evidence.
There is no database or persistent conversation storage; one-shot CLI usage is unchanged.

Request responsibilities are separated without introducing a second router:

- **Router — “Which capability should handle the request?”** The existing
  `route_question()` logic is unchanged, now housed in `agent/workflows.py` and
  re-exported from `agent/main.py` for compatibility.
- **Orchestrator — “How do we coordinate execution?”**
  `agent/orchestration.py::orchestrate_request()` validates optional task context,
  configures the existing request budget, owns runtime/client lifetime, invokes
  the router, and dispatches to one workflow. It delegates request-local state,
  deadlines, events, metrics and failure classification to existing utilities.
- **Handler/workflow — “How do we perform the investigation?”**
  `agent/workflows.py` contains the extracted generic graph and AKS/Terraform route
  adapters. Dedicated collectors, parsers, validators, MCP execution and policy
  enforcement remain in their existing modules.
- **Entry point — input and presentation.** `agent/main.py` parses the CLI request,
  invokes the orchestrator, prints the returned answer and preserves exit codes.

Workflows return a `WorkflowResult` with answer text and optional safe task-progress
metadata. Bounded requests return the existing `BoundedResult`; only the entry point
prints its fixed incomplete message, and task sessions receive no progress update.
Hard failures still propagate through lifecycle cleanup to the CLI's safe error
renderer. No extra model calls or new failure-recovery policy are introduced.

`RequestServices` is a small immutable dependency bundle, not a plugin framework;
it preserves the existing entry-point test seams without global dependency swapping.
Explicit `TaskSession` follow-ups still call `main()` and use the same context
selection/reset/expiry behavior. Final answers are printed after orchestration and
cleanup return; existing collector diagnostics are unchanged.

The workflow module deliberately retains the pre-existing helper code together;
splitting it into per-capability modules is not part of this change. Compatibility
re-exports and the legacy evidence-only helper remain in `main.py`. See the
[security model](docs/security.md) for unchanged safety boundaries.

## Terraform review workflow

A request containing both “review” and “terraform” triggers the deterministic
review path: repository discovery, recursive `/terraform` listing, one read of each
Terraform source file (excluding `.terraform`), Azure OpenAI structured review, then exact
evidence validation. The schema requires exactly three findings. Evidence line
numbers are resolved against the original file text. A match verifies only the
excerpt; recommendations and risks are labelled `Inference` and require additional
verification.

## Setup

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Set the placeholders in `.env`:

- `AZURE_OPENAI_ENDPOINT`: Azure AI Services endpoint, without `/openai/v1`.
- `AZURE_OPENAI_API_KEY`: Azure OpenAI API key.
- `AKS_MCP_PATH`: absolute path to the locally installed AKS MCP executable.

Never commit `.env`, API keys, Azure PATs, passwords, or private keys. Node.js/npx,
an authenticated Azure CLI session for Azure DevOps, and the AKS MCP executable are
required for live use. AKS investigation also needs Azure CLI/kubeconfig access to
the intended cluster. Credentials remain outside this repository.

At startup, the application validates `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_API_KEY`, and `AKS_MCP_PATH`. When configured in Settings,
`AZURE_CONFIG_DIR` is explicitly forwarded to both MCP subprocesses for Azure CLI
authentication. This does not copy the entire parent environment; the existing AKS
`USE_LEGACY_TOOLS` setting and SDK default environment inheritance remain unchanged.
Secret values are never logged.

The Azure DevOps MCP is started locally with Azure CLI authentication for the
configured organization. AKS MCP is also a local stdio subprocess, configured for
`readonly` access, `az_cli,kubectl` components, and the `default` namespace.

## Run

```sh
.venv/bin/python -m src.agent.main
```

Without a question, the entry point intentionally performs its default Terraform
review rather than opening an interactive terminal loop. To run one supplied
question, pass it as a quoted positional argument:

```sh
.venv/bin/python -m src.agent.main "Why is my Task Manager application not working?"
```

For embedding, call the async
`src.agent.main.main(question=...)`; non-review questions use the Azure OpenAI/LangGraph
tool-calling graph.

### Evidence-only Task Manager diagnostics

To inspect the deterministic, read-only AKS evidence collection without making a
Azure OpenAI request, call `task_manager_evidence_only()`. It returns a focused,
credential-redacted report rather than raw MCP output:

```python
import asyncio
import json

from src.agent.main import task_manager_evidence_only


async def inspect_task_manager():
    report = await task_manager_evidence_only()
    print(json.dumps(report, indent=2))


asyncio.run(inspect_task_manager())
```

This performs only the existing policy-checked `get` and `describe` calls in the
`default` namespace. It does not call Azure OpenAI, retrieve logs or Secrets, execute
commands, probe HTTP endpoints, or access PostgreSQL.

## Tests

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use mocked MCP and Azure OpenAI calls; they make no live API requests. Runtime
startup failures identify the affected MCP server without exposing credentials, and
shutdown is performed in a `finally` block.

## Current limitations and future remediation

The implementation permits only the configured project (`My Project`) and branch
(`main`). It has no write, remediation, or persistent conversation-storage feature.
Any future write access should be a separate, explicitly approved workflow with
scoped identities, dry-run/plan support, human confirmation, audit logging, and
tests for every mutation boundary.
