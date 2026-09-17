# Security model

The application is read-only and fails closed. Azure DevOps is limited to project
`My Project`, branch `main`, repository listing, pipeline-definition listing,
directory listing, and file-content reads.

AKS permits only allowlisted inspection operations. Kubernetes reads are limited to
`get` and `describe`, the `default` namespace, and a small resource allowlist.
Secrets, identity/RBAC resources, shell syntax, command execution, and mutation
operations are rejected before tool execution.

Do not commit `.env`, API keys, Azure PATs, passwords, certificates, or private
keys. The supplied `.gitignore` excludes common local environments, caches, logs,
build outputs, and certificate/key extensions.
