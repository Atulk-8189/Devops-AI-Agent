# DevOps AI Agent — Project Review

> **Generated:** 2026-09-28  
> **Scope:** Read-only inspection of `/Users/atulkumar/Documents/My AI Agent/devops-ai-agent`  
> **Test results reported here are from a live run during this review session.**

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Folder Structure](#2-folder-structure)
3. [Component Reference](#3-component-reference)
4. [Architecture & Execution Flow](#4-architecture--execution-flow)
5. [Integrations, Models & Configuration](#5-integrations-models--configuration)
6. [Security Boundaries](#6-security-boundaries)
7. [Test Coverage & Results](#7-test-coverage--results)
8. [Git Status & Recent Changes](#8-git-status--recent-changes)
9. [HTTP 429 Rate-Limit Issue Analysis](#9-http-429-rate-limit-issue-analysis)
10. [Known Limitations & Open Questions](#10-known-limitations--open-questions)
11. [Context for ChatGPT](#11-context-for-chatgpt)

---

## 1. Project Overview

A **read-only DevOps AI Agent** that:

- Answers questions about Azure Kubernetes Service (AKS) workloads and cluster health.
- Reads and reviews Terraform configuration from Azure Repos.
- Investigates Azure DevOps pipeline definitions and YAML files.
- Correlates AKS diagnostic evidence with Terraform source code to produce structured remediation proposals.
- Generates fix proposals (write-disabled; proposals only) with approval-gating and content-hash preconditions.

The agent is exposed via two surfaces:
- **CLI** (`run-agent.sh` → `src.agent.main`) — single-question, one-shot.
- **Chainlit web UI** (`frontend/chainlit_app.py`) — conversation UI with live activity panel, execution trace sidebar, and an execution viewer.

---

## 2. Folder Structure

```
devops-ai-agent/
├── .env                        ← Runtime secrets (never committed)
├── requirements.txt            ← Core dependencies
├── run-agent.sh                ← CLI launcher script
├── src/
│   ├── config.py               ← Settings dataclass; loads and validates env vars
│   ├── observability.py        ← Request lifecycle, model_call(), mcp_call(), emit()
│   ├── request_budget.py       ← Per-request limits: model calls, tool calls, chars
│   ├── safe_diagnostics.py     ← Error classification; never logs exception bodies
│   ├── metrics.py              ← In-memory event metrics store
│   ├── model_request_diagnostics.py ← Counts/structure of model request (no content)
│   ├── tool_results.py         ← normalize_successful_tool_result(); size limits
│   ├── agent/
│   │   ├── main.py             ← CLI entry point; configure_logging(); cli()
│   │   ├── orchestration.py    ← orchestrate_request(); route dispatch; MCP lifecycle
│   │   ├── workflows.py        ← All workflow handlers + LangGraph generic loop
│   │   ├── ado_pipeline_yaml.py← Deterministic Azure DevOps YAML investigation
│   │   ├── aks_troubleshooting.py ← AKS evidence collection; AKSDiagnosis model
│   │   ├── aks_events.py       ← Kubernetes event evidence helper
│   │   ├── aks_network.py      ← Network policy evidence helper
│   │   ├── remediation_discovery.py ← Correlate AKS evidence → Terraform resource
│   │   ├── remediation_proposal.py  ← Generate write-disabled Terraform fix proposal
│   │   ├── branch_remediation.py    ← Stage 4: approval-gated branch write (mock only)
│   │   ├── approval.py         ← Approval session, audit log; write disabled
│   │   ├── context.py          ← In-memory TaskContext; observation schema; TTL
│   │   ├── task_session.py     ← TaskProgress; hint_text(); reference topic detection
│   │   ├── proposals.py        ← Proposal, Action, Payload, Target, Preconditions models
│   │   ├── proposal_only.py    ← Proposal-only executor interface
│   │   ├── mock_executor.py    ← MockExecutor / MockRepository (test-only)
│   │   └── repository_discovery.py ← Stateful pagination/verification for repo IDs
│   ├── mcp/
│   │   └── runtime.py          ← MCPRuntime; mcp_connections(); select_allowed_tools()
│   ├── policy/
│   │   ├── policy.py           ← enforce_read_only_policy(); ALLOWED_TOOL_NAMES; constants
│   │   └── write_policy.py     ← WritePolicy model; writes_enabled: Literal[False]
│   └── review/
│       ├── review_schema.py    ← ReviewResponse Pydantic model; review_response_content()
│       ├── review_validation.py← validate_review(); render_review(); retrieved_files()
│       ├── terraform_evidence.py ← gather_terraform_evidence(); reads .tf files
│       ├── terraform_structure.py ← extract_terraform_structure() via python-hcl2
│       └── terraform_relationships.py ← extract_terraform_relationships()
├── frontend/
│   ├── chainlit_app.py         ← Chainlit on_message(); prepare_result(); Investigation
│   ├── formatter.py            ← Layered response formatter (JSON, Terraform, Markdown)
│   ├── trace.py                ← ExecutionTrace data model; step tracking
│   ├── response_safety.py      ← filter_response(); redaction of secrets in answers
│   ├── public/elements/
│   │   ├── ExecutionTrace.jsx  ← Right sidebar; resizable; collapse/reopen
│   │   └── ExecutionViewerButton.jsx ← Full-screen execution viewer trigger
│   └── tests/
│       ├── test_formatter.py   ← 49 formatter tests (including Terraform formatting)
│       ├── test_frontend.py    ← 119 Chainlit app tests + 49 subtests
│       ├── test_investigation.py
│       ├── test_response_safety.py
│       └── test_trace.py
└── tests/                      ← Backend unit and acceptance tests (90+ test files)
    ├── test_routing.py
    ├── test_openai_flow.py
    ├── test_aks_troubleshooting.py
    ├── test_aks_remediation.py
    ├── test_terraform_acceptance.py
    ├── test_ado_pipeline_yaml.py
    ├── test_adversarial.py
    ├── test_policy.py
    ├── test_branch_remediation.py
    ├── test_approval.py
    └── ... (35+ additional test files)
```

---

## 3. Component Reference

### 3.1 `src/config.py` — Settings
- Loads **three required env vars**: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AKS_MCP_PATH`.
- Optional: `REQUEST_DEADLINE_SECONDS` (default 300, range 1–1800), `AZURE_CONFIG_DIR`.
- `Settings.__repr__` is safe — never prints secret values.

### 3.2 `src/agent/orchestration.py` — `orchestrate_request()`
Single public async entry point for all requests.
1. Loads `.env` via dotenv.
2. Validates `Settings`.
3. Creates `AsyncOpenAI` client with `timeout=60.0, max_retries=0`.
4. Initialises `MCPRuntime` (opens stdio sessions to Azure DevOps MCP + AKS MCP).
5. Routes question → handler.
6. Returns `WorkflowResult` (success) or `BoundedResult` (budget exceeded).
7. Always closes client and runtime in `finally` block.

> **Important:** `max_retries=0` — the SDK will NOT auto-retry on 429. A single rate-limit response is a hard failure.

### 3.3 `src/agent/workflows.py` — Workflow Handlers

| Handler | Route key | When |
|---|---|---|
| `handle_aks` | `aks` | Task-manager troubleshooting questions |
| `handle_aks_cluster_health` | `aks_cluster_health` | AKS cluster-wide health questions |
| `handle_aks_remediation` | `aks_remediation` | AKS issues + Terraform correlation |
| `handle_terraform` | `terraform` | Terraform review questions |
| `handle_generic` + LangGraph loop | `generic` | All other questions |
| `handle_pipeline_yaml` (in `ado_pipeline_yaml.py`) | `azure_devops` | Pipeline/YAML investigation |

**Constants:**
- `MODEL = "gpt-5-mini"` (Azure OpenAI deployment name)
- `MAX_TOOL_EXECUTIONS = 5` (per-handler tool budget in generic loop)
- `MAX_MULTIPLE_TOOL_CALL_ATTEMPTS = 3`
- `MAX_REPOSITORY_RECURSION_DEPTH = 3`

**LangGraph generic loop** (`handle_generic`): A `StateGraph` with nodes `agent` → `tools` → `agent`. Stops on: duplicate tool call, tool budget exhausted (5 calls), empty final response.

### 3.4 `src/mcp/runtime.py` — MCPRuntime
Opens **two persistent stdio MCP servers** per request:
- `azure-devops` — `npx -y @azure-devops/mcp atulkmishra8189 --authentication azcli`
- `aks` — `<AKS_MCP_PATH> --access-level readonly --enabled-components az_cli,kubectl --allow-namespaces default`

Optional third:
- `aks-kubectl` — `<AKS_MCP_PATH> --access-level readonly --enabled-components kubectl --allow-namespaces default`

All discovered tools are filtered by `ALLOWED_TOOL_NAMES`. Duplicate tool names cause a hard `DuplicateMCPToolsError`.

### 3.5 `src/policy/policy.py` — Read-Only Enforcement

**Allowed tools and their permitted actions:**

| Tool | Server | Allowed actions |
|---|---|---|
| `pipelines_definition` | `azure-devops` | `list` only |
| `repo_repository` | `azure-devops` | `list` only |
| `repo_file` | `azure-devops` | `list_directory`, `get_content` |
| `az_aks_operations` | `aks` | `show`, `nodepool-list`, `nodepool-show`, `account-list`, `get-versions`, `check-network` |
| `kubectl_resources` | `aks` | `get`, `describe` |
| `kubectl_cluster` | `aks` | `cluster-info`, `api-resources`, `api-versions`, `explain` |
| `call_kubectl` | `aks-kubectl` | `kubectl logs` only (in namespace `default`) |

**Hard constraints enforced in code:**
- `ALLOWED_PROJECT = "My Project"` — project is injected/enforced for every ADO call.
- `ALLOWED_BRANCH = "main"` — branch is injected/enforced for every `repo_file` call.
- `ALLOWED_NAMESPACE = "default"` — namespace enforced for all kubectl operations.
- Shell metacharacters (`; | & < > \` $ ( )`) blocked in all argument parsing.
- `kubectl logs` limited to pod name + namespace + container + `--tail` (1–1000) + `--previous`.
- Kubernetes resources limited to an explicit allowlist (pods, deployments, services, events, nodes, etc.).

### 3.6 `src/policy/write_policy.py` — Write Policy
```python
writes_enabled: Literal[False] = False
```
This is **permanently `False`** and validated at model level. There is no code path to set it to `True`. The `validate_proposal()` method is used only to gate proposal logic, not execute writes.

### 3.7 `frontend/chainlit_app.py` — Chainlit UI
- Single `_REQUEST_LOCK` (`asyncio.Lock`) — one investigation runs at a time.
- Attaches `Activity` panel, `ExecutionTrace` sidebar, and `ExecutionViewerButton` elements.
- Calls `prepare_result()` which applies `response_safety.filter_response()` and `format_response()`.
- Sends two messages: a status/trace message and the formatted answer message.

### 3.8 `frontend/formatter.py` — Response Formatter
Layered dispatch (deterministic, no LLM calls):
1. **Valid JSON** → rich structured formatter (Summary, Observed Evidence, Root Causes, Next Step, Findings). Large/nested fields go into collapsible `<details>` blocks.
2. **Terraform review plain-text** (contains `Terraform discovery:`) → specialist formatter: extracts discovery metadata as a table, lists reviewed files, renders each finding as a titled card with evidence code block (decoding Python repr `\n` escapes).
3. **Markdown-structured** → contextual header + preserve body + Original Response block.
4. **Plain prose** → contextual header + preserve body + Original Response block.

Type labels: 🏗️ Terraform, ☸️ Kubernetes/AKS, 🔄 Azure DevOps, ☁️ Azure, 🔴 Error, 💬 General.

---

## 4. Architecture & Execution Flow

### 4.1 Full Request Lifecycle (User → Response)

```
User message (Chainlit or CLI)
        │
        ▼
orchestrate_request(question)
  ├─ load .env → validate Settings
  ├─ create AsyncOpenAI(max_retries=0, timeout=60s)
  ├─ MCPRuntime.initialize()
  │    ├─ Open stdio → azure-devops MCP
  │    └─ Open stdio → aks MCP
  ├─ route_question(question)  [workflows.py:258]
  │    ├─ pipeline_yaml_request()?         → "azure_devops"
  │    ├─ "investigate pipeline failure"?  → "azure_devops" [uncommitted change]
  │    ├─ is_aks_remediation_question()?   → "aks_remediation"
  │    ├─ is_task_manager_troubleshooting? → "aks"
  │    ├─ is_aks_cluster_health_question?  → "aks_cluster_health"
  │    ├─ "review" + "terraform"?          → "terraform"
  │    └─ default                          → "generic"
  │
  ├─ [route: aks] → handle_aks()
  │    ├─ collect_task_manager_evidence(tools)   [aks_troubleshooting.py]
  │    ├─ checkpoint()
  │    └─ structured_json(AKSDiagnosis schema)   → model call #1
  │
  ├─ [route: aks_cluster_health] → handle_aks_cluster_health()
  │    ├─ collect_aks_cluster_health_evidence(tools)
  │    └─ structured_json(AKSDiagnosis schema)   → model call #1
  │
  ├─ [route: aks_remediation] → handle_aks_remediation()
  │    ├─ collect_task_manager_evidence()         → model call #1 (diagnose_aks)
  │    ├─ gather_terraform_evidence()
  │    ├─ correlate_aks_to_terraform()            [deterministic, no model]
  │    ├─ determine_proposed_replacement()        [deterministic]
  │    └─ generate_remediation_proposal()         [deterministic]
  │
  ├─ [route: terraform] → handle_terraform()
  │    ├─ gather_terraform_evidence(tools)
  │    ├─ extract_terraform_structure() + relationships()
  │    └─ structured_json(ReviewResponse schema)  → model call #1
  │         → validate_review() + render_review()
  │
  ├─ [route: azure_devops] → handle_pipeline_yaml()
  │    ├─ pipelines_definition(list)              → tool call #1
  │    ├─ repo_repository(list)                   → tool call #2
  │    ├─ repo_file(get_content)                  → tool call #3
  │    └─ model_call(PIPELINE_YAML_SYSTEM_PROMPT) → model call #1
  │
  └─ [route: generic] → handle_generic() / LangGraph
       ├─ agent node: model_call() + openai_tools()
       ├─ tools node: execute_tool() → enforce_read_only_policy() → mcp_call()
       ├─ repeat (max 5 tool calls, max 3 multi-call attempts)
       └─ final AIMessage.content → WorkflowResult(answer)
        
        ▼
WorkflowResult(answer) or BoundedResult
        │
        ▼
prepare_result()
  ├─ filter_response(answer)  [response_safety.py]
  └─ format_response(answer)  [formatter.py]
        │
        ▼
cl.Message(content=formatted, elements=[ExecutionViewerButton]).send()
```

### 4.2 Active vs Experimental Components

| Component | Status | Notes |
|---|---|---|
| `handle_aks` | ✅ Active | AKS workload troubleshooting |
| `handle_aks_cluster_health` | ✅ Active | Cluster-wide node/pod health |
| `handle_aks_remediation` | ✅ Active | AKS+Terraform correlation |
| `handle_terraform` | ✅ Active | Terraform review |
| `handle_pipeline_yaml` | ✅ Active | ADO YAML investigation |
| `handle_generic` (LangGraph) | ✅ Active | Free-form questions |
| `branch_remediation.py` | ⚠️ Proposal only | `MockExecutor` used; real writes disabled |
| `approval.py` | ⚠️ Proposal only | Approval session exists; no actual write gate |
| `call_kubectl` (`kubectl logs`) | ✅ Active | Tightly scoped to `kubectl logs ... -n default` |
| `context.py` / `task_session.py` | ✅ Active | In-memory only; 15-min TTL; no persistence |

---

## 5. Integrations, Models & Configuration

### 5.1 Azure OpenAI
- **Endpoint variable:** `AZURE_OPENAI_ENDPOINT`
- **Key variable:** `AZURE_OPENAI_API_KEY`
- **Deployment name:** `gpt-5-mini` (hardcoded in `workflows.py:50`)
- **Client config:** `timeout=60.0`, `max_retries=0` (no SDK-level retry)
- **API path:** `{AZURE_OPENAI_ENDPOINT}/openai/v1/chat/completions`
- **Structured output:** `response_format={"type": "json_schema", ...}` used for AKS diagnosis and Terraform review
- No streaming; single blocking `create()` call per model invocation.

### 5.2 MCP Servers
- **Azure DevOps:** `npx -y @azure-devops/mcp atulkmishra8189 --authentication azcli`
  - Hardcoded ADO organization: `atulkmishra8189`
  - Authentication: Azure CLI (`azcli`)
- **AKS:** Local binary at `AKS_MCP_PATH` with `--access-level readonly`
  - `USE_LEGACY_TOOLS=true` environment variable set
- **Optional `AZURE_CONFIG_DIR`:** Injected into all MCP server environments when set.

### 5.3 Key Environment Variables (names only — no values)

| Variable | Required | Purpose |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | ✅ | Azure OpenAI base URL |
| `AZURE_OPENAI_API_KEY` | ✅ | API key for Azure OpenAI |
| `AKS_MCP_PATH` | ✅ | Path to the local AKS MCP executable |
| `REQUEST_DEADLINE_SECONDS` | No | Per-request timeout (default 300) |
| `AZURE_CONFIG_DIR` | No | Azure CLI config directory |

### 5.4 Budget Limits
| Limit | Value | Source |
|---|---|---|
| Model calls per request | 12 | `MAX_REQUEST_MODEL_CALLS` in `request_budget.py` |
| MCP tool dispatches per request | 64 | `MAX_REQUEST_TOOL_ATTEMPTS` |
| Result chars accumulated | 512,000 | `MAX_REQUEST_RESULT_CHARS` |
| Input chars accumulated | 1,000,000 | `MAX_REQUEST_INPUT_CHARS` |
| Tool calls in generic loop | 5 | `MAX_TOOL_EXECUTIONS` in `workflows.py` |
| AKS collection calls | 12 | `MAX_AKS_COLLECTION_CALLS` in `aks_troubleshooting.py` |
| AKS evidence chars | 24,000 | `MAX_AKS_EVIDENCE_CHARS` |
| Request deadline | 300s (default) | `DEFAULT_REQUEST_DEADLINE_SECONDS` |

### 5.5 Python Dependencies (`requirements.txt`)
```
openai
langgraph
langchain-core
langchain-mcp-adapters
python-dotenv
pydantic
python-hcl2==7.3.1
```
Frontend additionally uses `chainlit` (in `frontend/.venv`).

---

## 6. Security Boundaries

### 6.1 What Can Be Read

| Resource | Scope | Tool |
|---|---|---|
| ADO pipelines | Action `list` only | `pipelines_definition` |
| ADO repositories | Action `list` only | `repo_repository` |
| ADO repo files | Branch `main` only, `My Project` only | `repo_file` |
| AKS cluster info | Show/describe (no shell injection) | `az_aks_operations`, `kubectl_resources`, `kubectl_cluster` |
| Container logs | `kubectl logs` in namespace `default` only, max 1000 lines | `call_kubectl` |

### 6.2 What Cannot Happen

- **No writes**: `writes_enabled: Literal[False]` enforced at Pydantic model level. No code path enables writes.
- **No branch writes to protected branches**: `PROTECTED_BRANCHES = {"main", "master", "release", "production", "prod", "staging", "develop", "dev"}` — enforced in `branch_remediation.py`.
- **No shell injection**: All MCP arguments parsed without `subprocess.run(shell=True)`; shell metacharacters are explicitly blocked.
- **No namespace escape**: Kubernetes operations limited to `default` namespace at policy level, and `--access-level readonly` at MCP server level.
- **No secret exposure**: `Settings.__repr__` is safe. Context schema (`safe_text()`) blocks control characters, URLs, tokens, and known secret patterns. Error classification never logs exception bodies.
- **No arbitrary tool calls**: Tool names not in `ALLOWED_TOOL_NAMES` raise `PermissionError` before any MCP call.
- **No cross-branch file reads**: `repo_file` always has `version=ALLOWED_BRANCH` and `versionType="Branch"` injected.
- **No project scope escape**: `ALLOWED_PROJECT = "My Project"` is injected into every ADO call.
- **No chat history forwarded**: `on_message()` explicitly does not forward history; each investigation is fresh.

### 6.3 Approval Workflow (Proposals Only)

The `branch_remediation.py` / `approval.py` pipeline is designed for future use:
1. `generate_remediation_proposal()` produces a `Proposal` with SHA-256 hash of the canonical action.
2. An `ApprovalSession` requires human confirmation (terminal input or audit log).
3. Even if approved, `MockExecutor` is used in all current integrations — no live writes.
4. `WritePolicy.validate_proposal()` checks project, explicit repository/branch allowlist, and commit SHA precondition.

### 6.4 Potential Gap: ADO Organization Hardcoded in MCP Command
The ADO MCP is launched with `atulkmishra8189` as a hardcoded positional argument. If the MCP server ignores this argument or uses ambient credentials, the scope could be broader than the code assumes. This is a deployment assumption, not a code control.

### 6.5 Potential Gap: LangGraph Generic Loop Tool Budget
The generic loop enforces `MAX_TOOL_EXECUTIONS = 5` per-handler and the global `MAX_REQUEST_TOOL_ATTEMPTS = 64`. If multiple `handle_generic` invocations occurred in one request (not currently possible), the global limit would be the only guard.

---

## 7. Test Coverage & Results

### 7.1 Live Test Run (This Review Session)

```
701 passed, 704 subtests passed in 4.91s
```

Run command: `PYTHONPATH=. .venv/bin/pytest --tb=no -q`

#### Frontend tests only:
```
119 passed, 49 subtests passed
```

#### Formatter tests only:
```
49 passed in 0.02s
```

### 7.2 Test File Inventory

**Backend (`tests/`):**

| File | What it tests |
|---|---|
| `test_routing.py` | `route_question()` dispatch logic |
| `test_openai_flow.py` | Model call, tool loop, error handling |
| `test_aks_troubleshooting.py` | Evidence collection, `AKSDiagnosis` parsing |
| `test_aks_remediation.py` | AKS → Terraform correlation |
| `test_aks_acceptance.py` | End-to-end AKS workflow acceptance |
| `test_ado_pipeline_yaml.py` | `pipeline_yaml_request()`, `handle_pipeline_yaml()` |
| `test_ado_acceptance.py` | End-to-end ADO pipeline workflow |
| `test_terraform_acceptance.py` | End-to-end Terraform review workflow |
| `test_adversarial.py` | Prompt injection, policy bypass attempts |
| `test_policy.py` | `enforce_read_only_policy()` for all tools |
| `test_branch_remediation.py` | Stage 4 approval-gated remediation |
| `test_approval.py` | Approval session state machine |
| `test_config.py` | Settings loading and validation |
| `test_context.py` / `test_context_hardening.py` | TaskContext schema, safe_text |
| `test_orchestration.py` | orchestrate_request() lifecycle |
| `test_mcp_runtime.py` | MCPRuntime state machine |
| `test_request_budget.py` | Budget limits and deadline enforcement |
| `test_remediation_proposal.py` | Proposal generation validation |
| `test_review_validation.py` | Evidence excerpt verification |
| `test_safe_diagnostics.py` | Error classification, no secret leakage |
| `test_tool_results.py` | normalize_successful_tool_result() |
| `test_repository_id_boundary.py` | Repository ID verification guards |
| `test_logging_output.py` | No secrets in log output |
| And 15+ more... | Model diagnostics, metrics, mock executor, etc. |

**Frontend (`frontend/tests/`):**

| File | Tests |
|---|---|
| `test_formatter.py` | 49 tests: JSON, Terraform, Markdown, evidence decoding, type detection |
| `test_frontend.py` | 119 tests: prepare_result(), on_message(), Investigation lifecycle |
| `test_trace.py` | ExecutionTrace step state machine |
| `test_investigation.py` | Investigation history, cross-thread isolation |
| `test_response_safety.py` | Secret redaction in agent responses |

---

## 8. Git Status & Recent Changes

### 8.1 Current Branch and HEAD
```
Branch: main
HEAD: f4bf485 — "Clean up agent launcher and logging output"
Remote: origin/main (same commit — no unpushed commits)
```

### 8.2 Uncommitted Changes

**Modified (not staged, not committed):**
- `src/agent/workflows.py` — adds extra routing logic to `route_question()`

**Diff summary (`git diff src/agent/workflows.py`):**
```python
# Added at route_question(), after the existing pipeline_yaml_request() check:
normalized = question.lower().replace("-", " ")
if ("investigate" in normalized and "pipeline" in normalized and
    ("failure" in normalized or "latest" in normalized) and
    "task manager" not in normalized and
    "runs and logs" not in normalized and
    "develop branch" not in normalized and
    "run the pipeline" not in normalized and
    "any pipeline" not in normalized):
    return "azure_devops"
```
This routes "investigate pipeline failure" questions to `handle_pipeline_yaml` even when they do not match the strict `pipeline_yaml_request()` regex. However, `handle_pipeline_yaml` calls `pipeline_yaml_request()` internally and will return an `IncompleteEvidence("collection_limit")` or similar error when the question doesn't extract a named pipeline.

> ⚠️ **This change is not committed and may cause `handle_pipeline_yaml` to fail with unhelpful error messages for questions that don't have an extractable pipeline name.**

**Untracked files:**
- `.chainlit/` — Chainlit runtime/session data
- `PROJECT_REVIEW.md` — this file
- `frontend/` — entire Chainlit frontend (not committed)

> ⚠️ **The entire `frontend/` directory is untracked.** It is not in version control.

### 8.3 Recent Commit History

| SHA | Message |
|---|---|
| `f4bf485` | Clean up agent launcher and logging output |
| `6619f50` | Complete read-only AKS Terraform remediation workflow |
| `5ec5320` | Add secure AKS container log access |
| `7805c7f` | Fix AKS node pressure condition evaluation |
| `43b678f` | Add read-only AKS cluster health workflow |
| `89833b1` | Improve pipeline YAML investigation grounding |
| `330539a` | Fix Azure DevOps repository ID handoff |
| `ba1710e` | Fix AKS troubleshooting intent routing |
| `d3cdb85` | Add deterministic Azure DevOps pipeline workflow |
| `eeec9c7` | Fix repository discovery ID handoff |

---

## 9. HTTP 429 Rate-Limit Issue Analysis

### 9.1 Reported Scenario
The generic route called `pipelines_definition` and `repo_repository`, and the **third model call** failed with `rate_limit_exceeded`.

### 9.2 Verified Facts

1. **No SDK-level retry.** `AsyncOpenAI(max_retries=0)` is set explicitly in `orchestration.py:58`. A 429 from Azure OpenAI is a hard, unretried exception.

2. **The generic loop (`handle_generic`) can make multiple model calls.** Each agent → tool → agent cycle is one model call. After two tool calls (`pipelines_definition`, `repo_repository`), the third call is the agent processing their results and either calling another tool or returning a final answer. This third call is the one that hit the 429.

3. **Call sequence for the reported scenario:**
   - Model call #1: Agent decides to call `pipelines_definition` (list pipelines).
   - Tool call #1: `pipelines_definition` executed via MCP.
   - Model call #2: Agent decides to call `repo_repository` (list repositories).
   - Tool call #2: `repo_repository` executed via MCP.
   - Model call #3: Agent processes both tool results → **429 here**.

4. **Rate limit error classification.** `safe_diagnostics.py` classifies `rate_limit_error` / `rate_limit_exceeded` into the `model` category. The logged event is `model_call_failed` with `error_category=model` and `reason_code=rate_limit_exceeded`.

5. **The 429 propagates as an unhandled exception.** It is not caught inside `handle_generic`. It propagates up to `observed_request()` which emits `request_failed` and re-raises. The Chainlit app catches it in the generic `except Exception` and calls `classify_error(error)` → shows a safe user message.

### 9.3 Likely Causes (Hypotheses, Not Confirmed)

**H1 — TPM (Tokens Per Minute) quota exhaustion (most likely):**
The previous two model calls may have consumed a large number of tokens (tool call results from listing pipelines and repositories can be verbose). Azure OpenAI TPM quotas reset per minute. If the third call crosses the per-minute limit, a 429 is returned immediately.

**H2 — RPM (Requests Per Minute) exhaustion:**
If multiple simultaneous user sessions or rapid repeated requests hit the same deployment, the request-rate limit can be hit. The global `_REQUEST_LOCK` in `chainlit_app.py` serializes requests within one session but not across concurrent Chainlit sessions.

**H3 — Deployment quota too low for the model/region:**
`gpt-5-mini` deployment may have a low TPM quota on the Azure OpenAI resource. The structured-output call (which must fit the full tool listing in the context) can be token-heavy.

### 9.4 Relevant Code Paths

| File | Line(s) | What |
|---|---|---|
| `src/agent/orchestration.py` | 58 | `max_retries=0` — no SDK retry |
| `src/agent/workflows.py` | 594–618 | `handle_generic`: agent node makes model_call() |
| `src/observability.py` | 207–232 | `model_call()` wrapper; charges budget; calls `_call()` |
| `src/observability.py` | 256–278 | `_call()`: emits `model_call_failed` on exception |
| `src/safe_diagnostics.py` | 13–17 | `rate_limit_exceeded` is a recognized error code |
| `frontend/chainlit_app.py` | ~200 | Generic `except Exception` → safe user message |

### 9.5 Mitigation Options (Not Implemented)

1. **Add exponential backoff for 429:** Wrap `model_call()` in a retry loop that catches `openai.RateLimitError` and waits before retrying. Respect the `Retry-After` header if provided.
2. **Reduce context size:** Tool results from `pipelines_definition` and `repo_repository` can be large. `normalize_successful_tool_result()` applies size limits but `MAX_TOOL_RESULT_CHARS` may still produce large payloads.
3. **Increase Azure OpenAI quota:** Raise the TPM limit for the `gpt-5-mini` deployment in Azure Portal.
4. **Use `handle_pipeline_yaml` instead of generic loop for known pipeline questions:** The deterministic `handle_pipeline_yaml` makes exactly one model call with a bounded prompt, avoiding the multi-call pattern.

---

## 10. Known Limitations & Open Questions

### Limitations

1. **`frontend/` is not in version control.** All Chainlit UI, JSX elements, formatter, and tests are untracked. Any machine wipe would lose frontend work.

2. **Uncommitted routing change in `workflows.py`.** The pipeline-failure routing addition may route questions to `handle_pipeline_yaml` that will fail because no named pipeline can be extracted.

3. **No retry on 429.** A single Azure OpenAI rate-limit event fails the entire request.

4. **Generic loop is entirely LLM-driven for ADO questions.** When a pipeline question does not match `pipeline_yaml_request()`'s strict regex (e.g., questions about "latest pipeline failure" without a named pipeline), it falls through to `handle_generic`, which can make 3–5 model calls and hit rate limits.

5. **`handle_pipeline_yaml` fails silently on non-matching questions.** It calls `pipeline_yaml_request()` which returns `None` if the question doesn't match, causing an unhandled `TypeError` (unpacking `None`). This is a latent bug in the uncommitted routing change.

6. **AKS workload is hardcoded to `task-manager`.** `aks_troubleshooting.py:27` sets `DEPLOYMENT = "task-manager"`. The agent cannot investigate other deployments.

7. **ADO organization hardcoded.** `atulkmishra8189` is hardcoded in `runtime.py`; it cannot be configured without code change.

8. **`writes_enabled` is permanently `False`.** No actual remediation writes are possible. `MockExecutor` is the only implementation of write execution.

9. **In-memory task context only.** No persistence; context is lost on process restart. TTL is 15 minutes.

10. **`gpt-5-mini` model name is hardcoded.** It cannot be changed without editing `workflows.py`.

### Open Questions

1. What is the actual TPM/RPM quota on the Azure OpenAI `gpt-5-mini` deployment? (Needed to assess 429 frequency.)
2. Should the pipeline-failure routing change be committed, revised, or reverted?
3. Should `frontend/` be added to `.gitignore` deliberately, or is it missing from `git add`?
4. Is there a plan to implement actual write operations, or is the remediation stage intentionally proposal-only?
5. Is `atulkmishra8189` the correct/intended ADO organization? Should it be configurable via `.env`?

---

## 11. Context for ChatGPT

### What this project is

A **read-only DevOps AI Agent** for Azure infrastructure. It uses Azure OpenAI (`gpt-5-mini`), Azure DevOps MCP, and an AKS MCP to answer questions, diagnose Kubernetes issues, review Terraform files, and generate (but not apply) fix proposals. It has a Chainlit web UI and a CLI.

### Architecture in one paragraph

`orchestrate_request()` (in `orchestration.py`) is the single entry point. It loads config, opens two MCP stdio servers (Azure DevOps and AKS), routes the question, runs the appropriate workflow handler, and returns a `WorkflowResult`. Routing is a keyword-based Python function (`route_question()`). Specialized workflows (AKS, Terraform, ADO) make a fixed number of MCP tool calls followed by one structured Azure OpenAI call. The generic route uses a LangGraph agent loop (max 5 tool calls, then a final model call). All tool calls go through `enforce_read_only_policy()` which checks tool name, action, project, namespace, and branch. The Chainlit app applies `response_safety.filter_response()` for secret redaction and `format_response()` for structured Markdown rendering before displaying results.

### What works well

- Strong security boundary enforcement (allowlist + policy checks before every tool call).
- Deterministic specialized workflows (AKS, Terraform, ADO pipeline YAML) with bounded model calls.
- Comprehensive test suite (701 tests, all passing, including adversarial injection tests).
- Layered formatter with context-aware rendering for Terraform findings, AKS JSON, and Markdown.

### What needs investigation/improvement

1. **HTTP 429 handling** — `max_retries=0`, no backoff. Any rate-limit from Azure OpenAI fails the request. Recommended: add retry with backoff for `openai.RateLimitError` in `_call()` in `observability.py`.
2. **Uncommitted routing change** — `src/agent/workflows.py` has an unstaged change that routes "investigate pipeline failure" to `handle_pipeline_yaml`, which will fail with a confusing error when no named pipeline is in the question.
3. **`frontend/` not in git** — The entire Chainlit UI (formatter, JSX, tests) is untracked.
4. **Hardcoded ADO organization** — `atulkmishra8189` in `runtime.py` should be an environment variable.
5. **Hardcoded AKS workload** — `task-manager` / `default` in `aks_troubleshooting.py` prevents investigating other deployments.

### Key files to look at for debugging

| Problem | File | Function/Line |
|---|---|---|
| 429 rate limit | `src/observability.py` | `model_call()` L207, `_call()` L256 |
| 429 rate limit | `src/agent/orchestration.py` | L58 (`max_retries=0`) |
| Routing issues | `src/agent/workflows.py` | `route_question()` L258 |
| Routing issues | `src/agent/ado_pipeline_yaml.py` | `pipeline_yaml_request()` |
| Policy enforcement | `src/policy/policy.py` | `enforce_read_only_policy()` |
| Response formatting | `frontend/formatter.py` | `format_response()` |
| Secret redaction | `frontend/response_safety.py` | `filter_response()` |
| Write gate | `src/policy/write_policy.py` | `WritePolicy.writes_enabled` |

---

*End of Project Review. Report location: `/Users/atulkumar/Documents/My AI Agent/devops-ai-agent/PROJECT_REVIEW.md`*