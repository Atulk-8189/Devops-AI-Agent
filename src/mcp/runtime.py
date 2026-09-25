"""Persistent, policy-filtered MCP sessions for the agent process lifetime."""
import asyncio
import logging
import os
from pathlib import Path
from contextlib import AsyncExitStack

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from src.policy.policy import ALLOWED_TOOL_NAMES, TOOL_SERVERS
from src.config import Settings, load_settings


class MCPRuntimeError(RuntimeError):
    """Raised when the persistent MCP runtime cannot become ready."""


class DuplicateMCPToolsError(MCPRuntimeError):
    pass


def mcp_connections(settings: Settings | None = None):
    """Return the fixed local stdio configuration without storing credentials."""
    if settings is None:
        try:
            settings = load_settings()
        except Exception:
            # Keep this helper independently useful for diagnostics/tests; the
            # application entry point performs complete startup validation.
            aks_path = os.getenv("AKS_MCP_PATH")
            if not aks_path:
                raise MCPRuntimeError("AKS_MCP_PATH must name the local AKS MCP executable")
            settings = type("MCPSettings", (), {"aks_mcp_path": str(Path(aks_path).expanduser())})()
    connections = {
        "azure-devops": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@azure-devops/mcp", "atulkmishra8189",
                     "--authentication", "azcli"],
        },
        "aks": {
            "transport": "stdio",
            "command": settings.aks_mcp_path,
            "args": ["--access-level", "readonly", "--enabled-components", "az_cli,kubectl",
                     "--allow-namespaces", "default"],
            "env": {"USE_LEGACY_TOOLS": "true"},
        },
        "aks-kubectl": {
            "transport": "stdio",
            "command": settings.aks_mcp_path,
            "args": ["--access-level", "readonly", "--enabled-components", "kubectl",
                     "--allow-namespaces", "default"],
        },
    }
    azure_config_dir = getattr(settings, "azure_config_dir", None)
    if azure_config_dir:
        for connection in connections.values():
            connection.setdefault("env", {})["AZURE_CONFIG_DIR"] = azure_config_dir
    return connections


def select_allowed_tools(discovered):
    """Fail closed when any MCP servers expose an ambiguous tool name."""
    names = [tool.name for tool in discovered]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise DuplicateMCPToolsError("Duplicate MCP tool names discovered")
    return [tool for tool in discovered if tool.name in ALLOWED_TOOL_NAMES]


class MCPRuntime:
    """Keeps explicit stdio sessions open and binds LangChain tools to them."""

    def __init__(self, client=None, tool_loader=load_mcp_tools, log=None, settings=None):
        self.log = log or logging.getLogger(__name__).info
        self.client = client or MultiServerMCPClient(mcp_connections(settings), handle_tool_errors=False)
        self.tool_loader = tool_loader
        self._exit_stack = AsyncExitStack()
        self._lock = asyncio.Lock()
        self.sessions = {}
        self.tools = []
        self.tool_metadata = {}
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
                servers = [
                    ("azure-devops", "Azure DevOps MCP ready"),
                    ("aks", "AKS MCP ready"),
                ]
                if getattr(self.client, "connections", None) and "aks-kubectl" in self.client.connections:
                    servers.append(("aks-kubectl", "AKS Kubectl MCP ready"))
                for server_name, label in servers:
                    try:
                        session = await self._exit_stack.enter_async_context(
                            self.client.session(server_name)
                        )
                    except Exception as exc:
                        raise MCPRuntimeError(f"{server_name} MCP startup failed") from exc
                    self.sessions[server_name] = session
                    self.log(label)

                discovered = []
                bindings = []
                for server_name, session in self.sessions.items():
                    loaded = await self.tool_loader(session, server_name=server_name)
                    discovered.extend(loaded)
                    bindings.extend((tool.name, server_name) for tool in loaded)
                selected = select_allowed_tools(discovered)
                if any(name in TOOL_SERVERS and TOOL_SERVERS[name] != server for name, server in bindings):
                    raise MCPRuntimeError("MCP tool server binding mismatch")
                self.tools = selected
                self.tool_metadata = {tool.name: {"description": tool.description}
                                      for tool in self.tools}
                self.log(f"MCP tools discovered: {len(self.tools)}")
                self.state = "ready"
                self.log("MCP runtime ready")
            except Exception as exc:
                self.state = "failed"
                await self._exit_stack.aclose()
                self.sessions = {}
                self.tools = []
                self.tool_metadata = {}
                if isinstance(exc, DuplicateMCPToolsError):
                    raise MCPRuntimeError("Duplicate MCP tool names discovered") from None
                if isinstance(exc, MCPRuntimeError):
                    raise MCPRuntimeError("MCP runtime initialization failed") from None
                raise MCPRuntimeError("MCP runtime initialization failed") from None

    async def close(self):
        """Close every active MCP session and its local subprocess cleanly."""
        async with self._lock:
            if self.state == "closed":
                return
            await self._exit_stack.aclose()
            self.sessions = {}
            self.tools = []
            self.tool_metadata = {}
            self.state = "closed"
            self.log("MCP runtime closed")
