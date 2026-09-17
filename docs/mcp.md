# MCP runtime

The runtime starts two local stdio servers:

- Azure DevOps MCP through `npx -y @azure-devops/mcp … --authentication azcli`.
- AKS MCP at the executable named by `AKS_MCP_PATH`.

`AKS_MCP_PATH` is required and is intentionally not given a machine-specific
default. It must point outside this repository. Azure CLI authentication and AKS
cluster access are supplied by the local environment, not by repository files.

Sessions and discovered, policy-filtered tools persist for the hosting Python
process. Call `shutdown_mcp_runtime()` on application shutdown.
