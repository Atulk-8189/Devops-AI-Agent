# DevOps AI Agent

A Gemini-only, read-only DevOps assistant for Azure DevOps repository investigation,
AKS investigation, and evidence-backed Terraform reviews. It uses LangGraph to
orchestrate local Model Context Protocol (MCP) tools; it does not apply Terraform,
mutate Kubernetes resources, or change Azure resources.

## Architecture

The agent uses Gemini `gemini-3.8-flash` through the Google Gen AI SDK. LangGraph
routes a request through `START → agent → tools → agent → END`; automatic SDK tool
execution is disabled, and the agent accepts at most one MCP call per model turn.

The persistent MCP runtime opens Azure DevOps and AKS stdio sessions once per Python
process, discovers tools once, filters them through the allowlist, and reuses the
session-bound tools. An embedding application must call
`src.agent.main.shutdown_mcp_runtime()` when it stops. The module entry point does
this automatically.

See [architecture details](docs/architecture.md), [MCP configuration](docs/mcp.md),
and the [security model](docs/security.md).

## Terraform review workflow

A request containing both “review” and “terraform” triggers the deterministic
review path: repository discovery, recursive `/terraform` listing, one read of each
Terraform source file (excluding `.terraform`), Gemini structured review, then exact
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

- `GOOGLE_API_KEY`: Gemini API key.
- `AKS_MCP_PATH`: absolute path to the locally installed AKS MCP executable.

Never commit `.env`, API keys, Azure PATs, passwords, or private keys. Node.js/npx,
an authenticated Azure CLI session for Azure DevOps, and the AKS MCP executable are
required for live use. AKS investigation also needs Azure CLI/kubeconfig access to
the intended cluster. Credentials remain outside this repository.

The Azure DevOps MCP is started locally with Azure CLI authentication for the
configured organization. AKS MCP is also a local stdio subprocess, configured for
`readonly` access, `az_cli,kubectl` components, and the `default` namespace.

## Run

```sh
.venv/bin/python -m src.agent.main
```

The entry point performs one Terraform review rather than opening an interactive
terminal loop. For embedding, call the async
`src.agent.main.main(question=...)`; non-review questions use the Gemini/LangGraph
tool-calling graph.

## Tests

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use mocked MCP and Gemini calls; they make no live API requests.

## Current limitations and future remediation

The implementation permits only the configured project (`My Project`) and branch
(`main`). It has no write, remediation, or persistent conversation-storage feature.
Any future write access should be a separate, explicitly approved workflow with
scoped identities, dry-run/plan support, human confirmation, audit logging, and
tests for every mutation boundary.
