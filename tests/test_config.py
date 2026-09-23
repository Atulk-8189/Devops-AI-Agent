import unittest

from src.config import ConfigurationError, load_settings


class ConfigurationTests(unittest.TestCase):
    def test_required_configuration_is_validated_without_revealing_values(self):
        with self.assertRaisesRegex(ConfigurationError, "AZURE_OPENAI_API_KEY") as caught:
            load_settings({"AZURE_OPENAI_ENDPOINT": "https://example.test", "AKS_MCP_PATH": "/tmp/aks"})
        self.assertNotIn("super-secret", str(caught.exception))

    def test_settings_preserve_fixed_model_and_paths(self):
        settings = load_settings({
            "AZURE_OPENAI_ENDPOINT": "https://example.test/",
            "AZURE_OPENAI_API_KEY": "super-secret",
            "AKS_MCP_PATH": "~/bin/aks-mcp",
            "AZURE_CONFIG_DIR": "/tmp/azure",
        })
        self.assertEqual(settings.model, "gpt-5-mini")
        self.assertEqual(settings.azure_openai_endpoint, "https://example.test")
        self.assertTrue(settings.aks_mcp_path.endswith("/bin/aks-mcp"))
        self.assertEqual(settings.azure_config_dir, "/tmp/azure")
