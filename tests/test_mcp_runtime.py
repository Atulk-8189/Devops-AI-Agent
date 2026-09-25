import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch
from dataclasses import replace

from src.mcp.runtime import MCPRuntime, MCPRuntimeError, mcp_connections
from src.config import Settings, load_settings


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

    def test_configured_azure_directory_is_the_only_added_environment_value(self):
        settings = Settings("https://example.test", "test-key", "/opt/aks-mcp")
        baseline = mcp_connections(settings)
        configured = replace(settings, azure_config_dir="/tmp/isolated azure config")
        with patch.dict("os.environ", {"AZURE_CONFIG_DIR": "/tmp/not-the-settings-value",
                                       "UNRELATED_SECRET": "not-forwarded", "AZURE_OPENAI_API_KEY": "not-forwarded"}):
            connections = mcp_connections(configured)
        self.assertEqual(connections["azure-devops"]["env"], {"AZURE_CONFIG_DIR": configured.azure_config_dir})
        self.assertEqual(connections["aks"]["env"], {"USE_LEGACY_TOOLS": "true", "AZURE_CONFIG_DIR": configured.azure_config_dir})
        self.assertEqual(connections["aks-kubectl"]["env"], {"AZURE_CONFIG_DIR": configured.azure_config_dir})
        # All original server commands, arguments, transports and settings stay identical.
        del connections["azure-devops"]["env"]
        del connections["aks"]["env"]["AZURE_CONFIG_DIR"]
        del connections["aks-kubectl"]["env"]
        self.assertEqual(connections, baseline)
        self.assertNotIn("env", baseline["azure-devops"])
        self.assertEqual(baseline["aks"]["env"], {"USE_LEGACY_TOOLS": "true"})
        self.assertNotIn("env", baseline["aks-kubectl"])

    def test_unconfigured_settings_do_not_inherit_directory_from_parent(self):
        for directory in (None, ""):
            with self.subTest(directory=directory), patch.dict("os.environ", {"AZURE_CONFIG_DIR": "/tmp/parent"}):
                connections = mcp_connections(Settings("https://example.test", "test-key", "/opt/aks-mcp", azure_config_dir=directory))
            self.assertNotIn("env", connections["azure-devops"])
            self.assertEqual(connections["aks"]["env"], {"USE_LEGACY_TOOLS": "true"})
            self.assertNotIn("env", connections["aks-kubectl"])

    async def test_runtime_passes_configured_directory_to_client_and_preserves_lifecycle(self):
        settings = load_settings({"AZURE_OPENAI_ENDPOINT": "https://example.test", "AZURE_OPENAI_API_KEY": "test-key",
                                  "AKS_MCP_PATH": "/opt/aks-mcp", "AZURE_CONFIG_DIR": "/tmp/ci-azure"})
        with patch("src.mcp.runtime.MultiServerMCPClient", return_value=self.client) as factory:
            runtime = MCPRuntime(settings=settings, tool_loader=self.runtime.tool_loader, log=lambda _: None)
        factory.assert_called_once_with(mcp_connections(settings), handle_tool_errors=False)
        for connection in factory.call_args.args[0].values():
            self.assertEqual(connection["env"]["AZURE_CONFIG_DIR"], "/tmp/ci-azure")
        await runtime.initialize()
        await runtime.close()
        self.assertEqual(self.client.opens, ["azure-devops", "aks"])
        self.assertEqual(self.client.closes, ["aks", "azure-devops"])
