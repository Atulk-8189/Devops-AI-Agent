import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from src.mcp.runtime import MCPRuntime, MCPRuntimeError, mcp_connections


class Tool:
    def __init__(self, name):
        self.name = name
        self.description = name
        self.args_schema = {"type": "object", "properties": {}}


class FakeClient:
    def __init__(self, fail_server=None):
        self.opens = []
        self.closes = []
        self.fail_server = fail_server

    @asynccontextmanager
    async def session(self, name):
        self.opens.append(name)
        if name == self.fail_server:
            raise RuntimeError("session failed")
        try:
            yield f"{name}-session"
        finally:
            self.closes.append(name)


class MCPRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeClient()
        self.loads = []

        async def loader(session, *, server_name):
            self.loads.append((session, server_name))
            return {
                "azure-devops": [Tool("repo_file")],
                "aks": [Tool("kubectl_resources")],
            }[server_name]

        self.runtime = MCPRuntime(client=self.client, tool_loader=loader, log=lambda _: None)

    async def test_initializes_and_discovers_once_then_reuses_tools(self):
        await self.runtime.initialize()
        first_tools = self.runtime.tools
        await self.runtime.initialize()
        self.assertTrue(self.runtime.ready)
        self.assertIs(first_tools, self.runtime.tools)
        self.assertEqual(self.client.opens, ["azure-devops", "aks"])
        self.assertEqual(self.loads, [
            ("azure-devops-session", "azure-devops"),
            ("aks-session", "aks"),
        ])

    async def test_shutdown_closes_active_sessions(self):
        await self.runtime.initialize()
        await self.runtime.close()
        self.assertEqual(self.client.closes, ["aks", "azure-devops"])
        self.assertFalse(self.runtime.ready)

    async def test_failed_session_is_reported_and_closed(self):
        runtime = MCPRuntime(client=FakeClient(fail_server="aks"), log=lambda _: None)
        with self.assertRaisesRegex(MCPRuntimeError, "initialization failed"):
            await runtime.initialize()
        self.assertEqual(runtime.state, "failed")
        with self.assertRaisesRegex(MCPRuntimeError, "previously failed"):
            await runtime.initialize()

    async def test_duplicate_names_fail_before_tools_are_exposed(self):
        async def duplicate_loader(session, *, server_name):
            return [Tool("repo_file")]

        runtime = MCPRuntime(client=FakeClient(), tool_loader=duplicate_loader, log=lambda _: None)
        with self.assertRaisesRegex(MCPRuntimeError, "Duplicate MCP tool names"):
            await runtime.initialize()
        self.assertEqual(runtime.tools, [])

    async def test_policy_filtering_happens_before_tools_are_exposed(self):
        async def mixed_loader(session, *, server_name):
            return [Tool("repo_file"), Tool("repo_file_delete")] if server_name == "azure-devops" else [Tool("kubectl_config")]

        runtime = MCPRuntime(client=FakeClient(), tool_loader=mixed_loader, log=lambda _: None)
        await runtime.initialize()
        self.assertEqual([tool.name for tool in runtime.tools], ["repo_file"])

    async def test_aks_executable_must_be_explicitly_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(MCPRuntimeError, "AKS_MCP_PATH"):
                mcp_connections()

        with patch.dict("os.environ", {"AKS_MCP_PATH": "/opt/aks-mcp"}, clear=True):
            self.assertEqual(mcp_connections()["aks"]["command"], "/opt/aks-mcp")
