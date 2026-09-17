"""Persistent, policy-filtered MCP sessions for the agent process lifetime."""
import asyncio
import os
from contextlib import AsyncExitStack

from google.genai import types
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from src.policy.policy import ALLOWED_TOOL_NAMES


class MCPRuntimeError(RuntimeError):
    """Raised when the persistent MCP runtime cannot become ready."""


def mcp_connections():
    """Return the fixed local stdio configuration without storing credentials."""
    aks_mcp_path = os.getenv("AKS_MCP_PATH")
    if not aks_mcp_path:
        raise MCPRuntimeError(
            "AKS_MCP_PATH must name the local AKS MCP executable. "
            "Do not store the executable in this repository."
        )
    aks_mcp_path = os.path.expanduser(aks_mcp_path)
    return {
        "azure-devops": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@azure-devops/mcp", "atulkmishra8189",
                     "--authentication", "azcli"],
        },
        "aks": {
            "transport": "stdio",
            "command": aks_mcp_path,
            "args": ["--access-level", "readonly", "--enabled-components", "az_cli,kubectl",
                     "--allow-namespaces", "default"],
            "env": {"USE_LEGACY_TOOLS": "true"},
        },
    }


def select_allowed_tools(discovered):
    """Fail closed when any MCP servers expose an ambiguous tool name."""
    names = [tool.name for tool in discovered]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise MCPRuntimeError(f"Duplicate MCP tool names discovered: {duplicates}")
    return [tool for tool in discovered if tool.name in ALLOWED_TOOL_NAMES]


class MCPRuntime:
    """Keeps explicit stdio sessions open and binds LangChain tools to them."""

    def __init__(self, client=None, tool_loader=load_mcp_tools, log=print):
        self.client = client or MultiServerMCPClient(mcp_connections(), handle_tool_errors=False)
        self.tool_loader = tool_loader
        self.log = log
        self._exit_stack = AsyncExitStack()
        self._lock = asyncio.Lock()
        self.sessions = {}
        self.tools = []
        self.declarations = []
        self.state = "new"

    @property
    def ready(self):
        return self.state == "ready"

    async def initialize(self):
        """Open both MCP sessions once and discover policy-approved tools once."""
        async with self._lock:
            if self.ready:
                return
            if self.state == "failed":
                raise MCPRuntimeError("MCP runtime initialization previously failed")
            if self.state == "closed":
                raise MCPRuntimeError("MCP runtime is closed")
            self.state = "initializing"
            self.log("MCP runtime initializing")
            try:
                for server_name, label in (
                    ("azure-devops", "Azure DevOps MCP ready"),
                    ("aks", "AKS MCP ready"),
                ):
                    session = await self._exit_stack.enter_async_context(
                        self.client.session(server_name)
                    )
                    self.sessions[server_name] = session
                    self.log(label)

                discovered = []
                for server_name, session in self.sessions.items():
                    discovered.extend(await self.tool_loader(session, server_name=server_name))
                self.tools = select_allowed_tools(discovered)
                self.declarations = self._build_declarations(self.tools)
                self.log(f"MCP tools discovered: {len(self.tools)}")
                self.state = "ready"
                self.log("MCP runtime ready")
            except Exception as exc:
                self.state = "failed"
                await self._exit_stack.aclose()
                self.sessions = {}
                self.tools = []
                self.declarations = []
                raise MCPRuntimeError(f"MCP runtime initialization failed: {exc}") from exc

    @staticmethod
    def _build_declarations(tools):
        declarations = []
        for tool in tools:
            schema = tool.args_schema
            if not isinstance(schema, dict):
                schema = schema.model_json_schema()
            declarations.append(types.FunctionDeclaration(
                name=tool.name, description=tool.description,
                parameters_json_schema=schema,
            ))
        return declarations

    async def close(self):
        """Close every active MCP session and its local subprocess cleanly."""
        async with self._lock:
            if self.state == "closed":
                return
            await self._exit_stack.aclose()
            self.sessions = {}
            self.tools = []
            self.declarations = []
            self.state = "closed"
            self.log("MCP runtime closed")
