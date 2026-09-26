#!/usr/bin/env bash
#
# run-agent.sh - Launcher for the DevOps AI Agent
#
set -euo pipefail

# 1. Resolve project root directory regardless of invocation location
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# 2. Check for required virtual environment
VENV_DIR="$PROJECT_ROOT/.venv"
if [ ! -d "$VENV_DIR" ] || [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Error: Virtual environment not found at '$VENV_DIR'." >&2
    echo "Please create and configure it first:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# 3. Check for required configuration file (.env)
# The application loads .env internally via dotenv in orchestrate_request,
# so we verify existence without duplicating environment loading in the shell.
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo "Error: Configuration file '$PROJECT_ROOT/.env' not found." >&2
    echo "Please create it from the template:" >&2
    echo "  cp .env.example .env" >&2
    echo "Then configure AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, and AKS_MCP_PATH." >&2
    exit 1
fi

# 4. Activate virtual environment
source "$VENV_DIR/bin/activate"

# 5. Ensure project root is in PYTHONPATH
export PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

# 6. Execute the agent entry point with all passed arguments
exec python -m src.agent.main "$@"
