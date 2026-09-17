# Architecture

`src.agent.main` is the application entry point. It creates a Gemini client and a
LangGraph graph, and owns the process-lifetime MCP runtime. Gemini is the only LLM
provider in this repository.

`src.mcp.runtime` opens local Azure DevOps and AKS stdio MCP sessions once,
discovers their tools, rejects duplicate names, and exposes only policy-allowed
tools. `src.policy.policy` normalizes Azure DevOps requests and fails closed on
unauthorized operations.

Terraform reviews bypass model-directed repository browsing. The review collector
discovers the repository, lists `/terraform`, reads each Terraform source once, and
preserves the returned text as evidence. Gemini returns a three-finding structured
response; schema and validation modules resolve the specified evidence lines and
verify each excerpt against the files read during that request.
