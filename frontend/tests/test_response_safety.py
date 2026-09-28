"""Synthetic final-answer fixtures only; never load real responses or credentials."""
import json
import unittest
from unittest.mock import patch

from frontend.response_safety import (
    filter_response, log_loaded_filter, WITHHELD, NOTICE,
    FILTER_IMPLEMENTATION, FILTER_REVISION,
)


class ResponseSafetyTests(unittest.TestCase):
    def test_normal_aks_health_prose_and_technical_details_are_unchanged(self):
        cases = [
            "## Observed facts\nTask Manager: 2/2 pods ready. No current failure observed.\n"
            "## Missing evidence\nSecrets and credentials were not inspected. Connectivity unverified.",
            "node aks-agentpool-12345678-vmss000000 Ready=True; MemoryPressure=False; "
            "DiskPressure=False. pod task-manager-7b84598d5c-abc12 Running, restarts=3 (historical).",
            "Service 10.0.12.34; endpoint 192.168.1.25:8080; IPv6 fd00::10. "
            "LoadBalancer assigned; external connectivity not tested.",
            "Resource /subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/rg-task-manager/providers/"
            "Microsoft.ContainerService/managedClusters/task-manager. "
            "See https://portal.azure.com and /terraform/main.tf:25.",
            "Authorization was denied. The token expired; no API key or connection string was collected.",
            'password = var.database_password\napi_key = "${var.api_key}"\ntoken = "$(ADO_TOKEN)"',
            'password = "[REDACTED]"\napi_key = [REDACTED]',
            'password = "[redacted]"\napi_key = [redacted]',
            "secretName: task-manager-config\nkind: Secret\nNo data was retrieved.",
        ]
        for text in cases:
            with self.subTest(text=text):
                result = filter_response(text)
                self.assertEqual(result.outcome, "allowed")
                self.assertEqual(result.text, text)

    def test_credentials_redacted_with_explicit_notice(self):
        cases = [
            ('password="synthetic-pass-value"', "synthetic-pass-value"),
            ('{"api_key": "synthetic-key-value", "healthy": true}', "synthetic-key-value"),
            ("DB_PASSWORD: correct horse battery staple", "correct horse battery staple"),
            ("password=a;second-part", "a;second-part"),
            ("**client_secret**: `synthetic-secret-value`", "synthetic-secret-value"),
            ("Authorization: Bearer synthetic-bearer-value", "synthetic-bearer-value"),
            ("Proxy-Authorization: Basic c3ludGhldGljOnBhc3N3b3Jk", "c3ludGhldGljOnBhc3N3b3Jk"),
            ("Server=db;Password=synthetic-db-value;Encrypt=True", "synthetic-db-value"),
            ("AccountKey=synthetic-storage-value;EndpointSuffix=core.windows.net", "synthetic-storage-value"),
            ("https://user:synthetic-uri-value@host.example/db", "synthetic-uri-value"),
            ("https://host.example/blob?sv=1&sig=synthetic-signature&sp=r", "synthetic-signature"),
            ("client-key-data: c3ludGhldGljLXByaXZhdGUta2V5", "c3ludGhldGljLXByaXZhdGUta2V5"),
            ("Token value ghp_" + "a" * 36, "ghp_" + "a" * 36),
            ("Key value sk-" + "a" * 30, "sk-" + "a" * 30),
            ("Key AKIA" + "A" * 16, "AKIA" + "A" * 16),
            ("JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.syntheticSignature", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.syntheticSignature"),
        ]
        for text, value in cases:
            with self.subTest(text=text):
                result = filter_response(text)
                self.assertEqual(result.outcome, "redacted")
                self.assertTrue(result.text.startswith(NOTICE))
                self.assertIn("[REDACTED]", result.text)
                self.assertNotIn(value, result.text)

    def test_mixed_details_are_preserved_and_repeated_value_removed(self):
        text = 'Node Ready=True\npassword="synthetic-pass-value"\nEcho synthetic-pass-value\nService 10.0.0.1 healthy'
        result = filter_response(text)
        self.assertEqual(result.outcome, "redacted")
        self.assertIn("Node Ready=True", result.text)
        self.assertIn("Service 10.0.0.1 healthy", result.text)
        self.assertNotIn("synthetic-pass-value", result.text)

    def test_uncertain_boundaries_withhold_with_safe_message(self):
        cases = [
            "-----BEGIN PRIVATE KEY-----\nsynthetic-private-material\n-----END PRIVATE KEY-----",
            "kind: Secret\nstringData:\n  custom: synthetic-secret-value",
            '{"kind": "Secret", "data": {"arbitrary": "c3ludGhldGlj"}}',
            "password: |\n  synthetic-multiline-value",
            'password="unterminated-value',
            "password: 'first''second'",
            "password: {nested: synthetic-value}",
            "password=[REDACTED]synthetic-suffix",
            "ｐａｓｓｗｏｒｄ=synthetic-value",
            "pass\u200bword=synthetic-value",
            "\x1b[31munsafe-control",
        ]
        for text in cases:
            with self.subTest(text=text):
                result = filter_response(text)
                self.assertEqual(result.outcome, "withheld")
                self.assertEqual(result.text, WITHHELD)
                self.assertNotIn("synthetic", result.text)

    def test_diagnostics_only_contain_fixed_categories(self):
        with self.assertLogs("frontend.response_safety", level="INFO") as captured:
            filter_response("Secrets not inspected; https://portal.azure.com\nNode Ready=True")
            filter_response('password="synthetic-value"')
        records = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual(records[0]["outcome"], "allowed")
        self.assertEqual(records[0]["legacy_triggers"], ["legacy_keyword", "legacy_url"])
        self.assertEqual(records[1]["categories"], ["credential_assignment"])
        serialized = json.dumps(records)
        for forbidden in ("synthetic-value", "portal.azure.com", "Ready=True", "password"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(set(records[0]), {"event", "outcome", "categories", "legacy_triggers",
                                          "implementation", "revision", "display_action"})

    def test_loaded_filter_marker_is_fixed_metadata_only(self):
        with self.assertLogs("frontend.response_safety", level="INFO") as captured:
            log_loaded_filter()
        self.assertEqual(json.loads(captured.records[0].getMessage()), {
            "event": "frontend_response_safety_loaded",
            "implementation": FILTER_IMPLEMENTATION,
            "revision": FILTER_REVISION,
        })
        with patch("frontend.response_safety.LOGGER.info", side_effect=RuntimeError("private error")):
            log_loaded_filter()  # Diagnostic failure must not prevent startup.

    def test_logging_failure_does_not_bypass_redaction(self):
        with patch("frontend.response_safety.LOGGER.info", side_effect=RuntimeError("private error")):
            result = filter_response("password=synthetic-value")
        self.assertEqual(result.outcome, "redacted")
        self.assertNotIn("synthetic-value", result.text)


if __name__ == "__main__":
    unittest.main()
