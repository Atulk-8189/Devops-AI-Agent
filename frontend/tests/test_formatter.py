"""Unit tests for the frontend response formatter."""
import json
import time
import unittest

from frontend.formatter import (
    extract_command,
    format_command_instruction,
    format_generic_value,
    format_likely_root_causes,
    format_observed_evidence,
    format_response,
    format_review_findings,
    format_summary,
    is_structured_json,
    # New helpers tested directly
    _decode_evidence_repr,
    _render_discovery_metadata_table,
    _parse_terraform_finding_block,
    _format_terraform_review_text,
    _guess_code_language,
)


class FormatterTests(unittest.TestCase):
    def test_is_structured_json(self):
        self.assertTrue(is_structured_json('{"key": "value"}'))
        self.assertTrue(is_structured_json('  [1, 2, 3]  \n'))
        self.assertTrue(is_structured_json('{"nested": {"a": [true, null]}}'))
        self.assertFalse(is_structured_json('{"broken": '))
        self.assertFalse(is_structured_json('just plain text'))
        self.assertFalse(is_structured_json('"quoted string"'))
        self.assertFalse(is_structured_json('12345'))
        self.assertFalse(is_structured_json(''))
        self.assertFalse(is_structured_json('   '))
        self.assertFalse(is_structured_json(None))
        self.assertFalse(is_structured_json(123))

    def test_extract_command(self):
        self.assertEqual(
            extract_command("Run `kubectl get pods -n default` to check status."),
            "kubectl get pods -n default",
        )
        self.assertEqual(
            extract_command('Execute "az acr repository list --name devopsacr" to view tags.'),
            "az acr repository list --name devopsacr",
        )
        self.assertEqual(
            extract_command("Review with 'terraform plan -out=tfplan'"),
            "terraform plan -out=tfplan",
        )
        self.assertEqual(
            extract_command("Run:\nkubectl logs pod-1 -c app"),
            "kubectl logs pod-1 -c app",
        )
        # Should not extract if code fence already present
        self.assertNil = self.assertIsNone(
            extract_command("Run the following:\n```sh\nkubectl get nodes\n```")
        )
        # No command keywords
        self.assertIsNone(extract_command("Check the deployment status in the portal."))

    def test_format_command_instruction(self):
        text = "Run `kubectl describe pod task-manager -n default` for more events."
        formatted = format_command_instruction(text)
        self.assertIn("```sh\nkubectl describe pod task-manager -n default\n```", formatted)

        already_fenced = "Run this:\n```sh\nkubectl get pods\n```"
        self.assertEqual(format_command_instruction(already_fenced), already_fenced)

        no_cmd = "Check Azure Monitor alerts."
        self.assertEqual(format_command_instruction(no_cmd), no_cmd)

    def test_format_summary(self):
        self.assertEqual(format_summary("Cluster is healthy."), "Cluster is healthy.")
        self.assertEqual(format_summary(["Point 1", "Point 2"]), "- Point 1\n- Point 2")
        self.assertEqual(format_summary({"status": "Degraded", "pods": 2}), "- **status**: Degraded\n- **pods**: 2")
        self.assertEqual(format_summary(42), "42")

    def test_format_observed_evidence(self):
        # List of strings
        evidence_list = ["Pod 1 is Running", "Pod 2 is ImagePullBackOff"]
        self.assertEqual(
            format_observed_evidence(evidence_list),
            "- Pod 1 is Running\n- Pod 2 is ImagePullBackOff",
        )
        # List of dicts
        evidence_dicts = [{"pod": "p1", "ready": True}, {"pod": "p2", "ready": False}]
        formatted = format_observed_evidence(evidence_dicts)
        self.assertIn("- **pod**: p1", formatted)
        self.assertIn("- **ready**: True", formatted)
        # Empty list
        self.assertEqual(format_observed_evidence([]), "_No observed evidence reported._")
        # String
        self.assertEqual(format_observed_evidence("Single log line"), "Single log line")

    def test_format_likely_root_causes(self):
        # Structured causes with hypothesis and supporting evidence
        causes = [
            {
                "hypothesis": "Image tag does not exist",
                "supporting_evidence": "ACR returned 404 Not Found",
            },
            {
                "hypothesis": "Missing imagePullSecret",
                "supporting_evidence": ["Denied 401 Unauthorized", "Secret not mounted"],
                "severity": "High",
            },
        ]
        formatted = format_likely_root_causes(causes)
        self.assertIn("> #### Hypothesis 1: Image tag does not exist", formatted)
        self.assertIn("> **Supporting evidence:**\n> - ACR returned 404 Not Found", formatted)
        self.assertIn("> #### Hypothesis 2: Missing imagePullSecret", formatted)
        self.assertIn("> **severity**: High", formatted)
        self.assertIn("> - Denied 401 Unauthorized", formatted)
        self.assertIn("> - Secret not mounted", formatted)

        # List of plain strings
        str_causes = ["DNS failure", "NetworkPolicy blocking traffic"]
        formatted_str = format_likely_root_causes(str_causes)
        self.assertIn("> #### Hypothesis 1\n> DNS failure", formatted_str)
        self.assertIn("> #### Hypothesis 2\n> NetworkPolicy blocking traffic", formatted_str)

        # Empty list
        self.assertEqual(format_likely_root_causes([]), "_No likely root causes identified._")

    def test_format_generic_value(self):
        data = {
            "str_val": "text",
            "int_val": 42,
            "bool_val": False,
            "none_val": None,
            "list_val": [1, "two", None],
            "nested_dict": {"sub": "val"},
            "empty_list": [],
            "empty_dict": {},
        }
        formatted = format_generic_value(data)
        self.assertIn("- **str_val**: text", formatted)
        self.assertIn("- **int_val**: 42", formatted)
        self.assertIn("- **bool_val**: false", formatted)
        self.assertIn("- **none_val**: null", formatted)
        self.assertIn("- **empty_list**:\n  _(empty list)_", formatted)
        self.assertIn("- **empty_dict**:\n  _(empty object)_", formatted)
        self.assertIn("- **sub**: val", formatted)

    def test_format_review_findings(self):
        findings = [
            {
                "Finding": "Hardcoded ACR credentials in main.tf",
                "File": "terraform/main.tf",
                "Evidence line": 45,
                "Evidence end line": 48,
                "Why it matters": "Exposes credentials in plaintext source code.",
                "Verification needed": "Verify whether managed identity can be used instead.",
                "Confidence": "High",
                "Evidence": 'admin_password = "supersecret"',
            }
        ]
        formatted = format_review_findings(findings)
        self.assertIn("#### 1. Hardcoded ACR credentials in main.tf", formatted)
        self.assertIn("`terraform/main.tf` (line 45-48)", formatted)
        self.assertIn("**Why it matters:** Exposes credentials in plaintext source code.", formatted)
        self.assertIn("**Confidence:** High", formatted)
        self.assertIn('```\nadmin_password = "supersecret"\n```', formatted)

    def test_format_response_structured_object(self):
        payload = {
            "summary": "AKS cluster has 1 unhealthy node.",
            "observed_evidence": ["Node aks-agentpool-1 NotReady"],
            "likely_root_causes": [{"hypothesis": "Disk pressure", "evidence": "Kubelet has DiskPressure"}],
            "recommended_next_diagnostic_step": "Run `kubectl describe node aks-agentpool-1` to check conditions.",
            "extra_info": "Review cluster metrics",
        }
        json_text = json.dumps(payload, indent=2)
        formatted = format_response(json_text)

        self.assertIn("### 📋 Executive Summary\nAKS cluster has 1 unhealthy node.", formatted)
        self.assertIn("### 🔍 Observed Evidence\n- Node aks-agentpool-1 NotReady", formatted)
        self.assertIn("### ⚠️ Likely Root Causes", formatted)
        self.assertIn("Disk pressure", formatted)
        self.assertIn("### 💡 Recommended Next Diagnostic Step", formatted)
        self.assertIn("```sh\nkubectl describe node aks-agentpool-1\n```", formatted)
        self.assertIn("### extra_info\nReview cluster metrics", formatted)
        self.assertIn("<details>\n<summary><strong>Raw response</strong></summary>", formatted)
        self.assertIn(json_text, formatted)

    def test_format_response_review_findings_array(self):
        findings = [
            {
                "Finding": "Insecure ingress configuration",
                "File": "ingress.tf",
                "Why it matters": "Allows HTTP traffic.",
            }
        ]
        json_text = json.dumps(findings)
        formatted = format_response(json_text)

        self.assertIn("### 🔎 Review Findings", formatted)
        self.assertIn("Insecure ingress configuration", formatted)
        self.assertIn("<details>\n<summary><strong>Raw response</strong></summary>", formatted)
        self.assertIn(json_text, formatted)

    # ------------------------------------------------------------------
    # New tests: layered formatter for non-JSON content
    # ------------------------------------------------------------------

    def test_format_response_plain_text_gets_header_and_original_block(self):
        """Plain prose must get a contextual header and a collapsible original-response block."""
        plain = "The AKS cluster is healthy and all pods are running."
        formatted = format_response(plain)

        # Content must be fully preserved
        self.assertIn(plain, formatted)
        # Must have a contextual header
        self.assertIn("###", formatted)
        # Must have an original-response block
        self.assertIn("<details>", formatted)
        self.assertIn("<summary><strong>Original Response</strong></summary>", formatted)
        self.assertIn("</details>", formatted)

    def test_format_response_malformed_json_treated_as_plain(self):
        """Malformed JSON that is not valid must be treated as plain text without crashing."""
        malformed = '{"summary": "incomplete'
        formatted = format_response(malformed)
        # Content preserved
        self.assertIn(malformed, formatted)
        # Must have an original block (treated as plain)
        self.assertIn("<details>", formatted)

    def test_format_response_markdown_structure_preserved(self):
        """Markdown-structured responses must keep all headings, lists, and code fences."""
        md = (
            "## Findings\n\n"
            "- Pod `api-server` is CrashLoopBackOff\n"
            "- Node `aks-node-1` is NotReady\n\n"
            "### Next Steps\n\n"
            "```sh\n"
            "kubectl describe pod api-server -n default\n"
            "```\n"
        )
        formatted = format_response(md)

        self.assertIn("## Findings", formatted)
        self.assertIn("- Pod `api-server` is CrashLoopBackOff", formatted)
        self.assertIn("```sh\nkubectl describe pod api-server -n default\n```", formatted)
        self.assertIn("<details>", formatted)
        self.assertIn("<summary><strong>Original Response</strong></summary>", formatted)

    def test_format_response_terraform_type_detected(self):
        """Terraform content must get the Terraform icon/label in the header."""
        tf_text = (
            'resource "azurerm_kubernetes_cluster" "aks" {\n'
            '  name = "my-cluster"\n'
            "  location = var.location\n"
            "}\n"
        )
        formatted = format_response(tf_text)
        self.assertIn("\U0001f3d7\ufe0f", formatted)  # 🏗️
        self.assertIn("Terraform", formatted)
        self.assertIn(tf_text, formatted)
        self.assertIn("<details>", formatted)

    def test_format_response_kubernetes_type_detected(self):
        """Kubernetes-related content must get the kubernetes icon/label."""
        k8s_text = "The kubectl output shows the pod status as ImagePullBackOff on the AKS cluster."
        formatted = format_response(k8s_text)
        self.assertIn("\u2638\ufe0f", formatted)  # ☸️
        self.assertIn("Kubernetes", formatted)
        self.assertIn(k8s_text, formatted)

    def test_format_response_azure_type_detected(self):
        """Azure-specific content must get the Azure icon/label."""
        azure_text = (
            "The Azure Resource Group 'rg-prod' has 5 resources. "
            "Subscription: 00000000-0000-0000-0000-000000000000."
        )
        formatted = format_response(azure_text)
        self.assertIn("\u2601\ufe0f", formatted)  # ☁️
        self.assertIn("Azure", formatted)
        self.assertIn(azure_text, formatted)

    def test_format_response_error_type_detected(self):
        """Error/exception content must get the error icon/label."""
        error_text = "An Exception occurred: ConnectionError - Failed to reach the cluster endpoint."
        formatted = format_response(error_text)
        self.assertIn("\U0001f534", formatted)  # 🔴
        self.assertIn(error_text, formatted)

    def test_format_response_original_content_always_present(self):
        """The full original content must appear in the Original Response block for all types."""
        long_response = "Azure DevOps pipeline 'build-prod' failed on stage Deploy.\n" * 20
        formatted = format_response(long_response)
        self.assertIn(long_response, formatted)
        self.assertIn("<details>", formatted)
        self.assertIn("Original Response", formatted)

    def test_format_response_no_additional_llm_calls(self):
        """format_response must be a pure, deterministic function with no external calls."""
        text = "Repository 'devops-repo' has 3 open PRs targeting the main branch."
        t0 = time.monotonic()
        result = format_response(text)
        elapsed = time.monotonic() - t0
        # Deterministic: should complete well under 1 second
        self.assertLess(elapsed, 1.0)
        self.assertIn(text, result)

    def test_format_response_nested_json_preserved(self):
        """Deeply nested JSON objects must have all values preserved in the output."""
        payload = {
            "summary": "Cluster investigation complete.",
            "details": {
                "node_count": 3,
                "unhealthy_nodes": ["node-1", "node-2"],
                "config": {"auto_scaling": True, "max_nodes": 10},
            },
        }
        json_text = json.dumps(payload, indent=2)
        formatted = format_response(json_text)

        self.assertIn("Cluster investigation complete.", formatted)
        self.assertIn("node_count", formatted)
        self.assertIn("node-1", formatted)
        self.assertIn("auto_scaling", formatted)
        self.assertIn(json_text, formatted)

    def test_format_response_mixed_markdown_with_code_and_tables(self):
        """Mixed Markdown with tables, code blocks, and prose must be fully preserved."""
        md = (
            "## Repository Summary\n\n"
            "| Branch | Status | PRs |\n"
            "|--------|--------|-----|\n"
            "| main   | Green  | 2   |\n"
            "| dev    | Red    | 5   |\n\n"
            "### Code Snippet\n\n"
            "```yaml\n"
            "trigger:\n"
            "  branches:\n"
            "    include: ['main']\n"
            "```\n\n"
            "Please review the above configuration.\n"
        )
        formatted = format_response(md)
        self.assertIn("## Repository Summary", formatted)
        self.assertIn("| Branch | Status | PRs |", formatted)
        self.assertIn("```yaml", formatted)
        self.assertIn("<details>", formatted)
        self.assertIn("Original Response", formatted)


class TerraformFormatterTests(unittest.TestCase):
    """Tests for the Terraform review plain-text formatter and its helpers."""

    # ── evidence repr decoding ─────────────────────────────────────────────

    def test_decode_evidence_repr_single_quotes(self):
        """Single-quoted repr evidence must have \\n converted to real newlines."""
        raw = "Confirmed exact excerpt: 'admin_password = \"secret\"\\n  admin_username = \"admin\"'"
        result = _decode_evidence_repr(raw)
        self.assertIn("admin_password", result)
        self.assertIn("\n", result)          # real newline
        self.assertNotIn("\\n", result)      # not a literal backslash-n

    def test_decode_evidence_repr_double_quotes(self):
        """Double-quoted repr evidence must also decode correctly."""
        raw = 'Confirmed exact excerpt: "foo = bar\\n  baz = qux"'
        result = _decode_evidence_repr(raw)
        self.assertIn("foo = bar", result)
        self.assertIn("\n", result)

    def test_decode_evidence_repr_no_wrapper(self):
        """Text with no repr wrapper is returned unchanged."""
        plain = "some terraform code here"
        self.assertEqual(_decode_evidence_repr(plain), plain)

    def test_decode_evidence_repr_escaped_backslash(self):
        """\\\\n (escaped backslash) must not produce a spurious newline."""
        raw = "Confirmed exact excerpt: 'path = C:\\\\Users\\\\foo'"
        result = _decode_evidence_repr(raw)
        self.assertIn("C:\\Users\\foo", result)

    # ── discovery metadata table ───────────────────────────────────────────

    def test_render_discovery_metadata_table_basic(self):
        discovery = {
            "repository_id": "abc-123",
            "commit_sha": "deadbeef",
            "incomplete": False,
        }
        table = _render_discovery_metadata_table(discovery)
        self.assertIn("| Field | Value |", table)
        self.assertIn("abc-123", table)
        self.assertIn("deadbeef", table)
        self.assertIn("No", table)       # incomplete=False → "No"

    def test_render_discovery_metadata_table_incomplete_true(self):
        discovery = {"incomplete": True, "error": "timeout"}
        table = _render_discovery_metadata_table(discovery)
        self.assertIn("Yes", table)
        self.assertIn("timeout", table)

    def test_render_discovery_metadata_table_empty(self):
        self.assertEqual(_render_discovery_metadata_table({}), "")

    def test_render_discovery_metadata_table_non_dict(self):
        self.assertEqual(_render_discovery_metadata_table("not a dict"), "")

    # ── finding block parser ───────────────────────────────────────────────

    def test_parse_terraform_finding_block_all_fields(self):
        block = (
            "Finding: Hardcoded credentials\n"
            "Evidence: Confirmed exact excerpt: 'admin_password = \"secret\"'\n"
            "File: /terraform/main.tf\n"
            "Why it matters: Unverified interpretation: Plaintext secrets.\n"
            "Verification needed: Use Key Vault.\n"
            "Confidence: Inference\n"
        )
        result = _parse_terraform_finding_block(block)
        self.assertEqual(result["Finding"], "Hardcoded credentials")
        self.assertIn("Confirmed exact excerpt", result["Evidence"])
        self.assertEqual(result["File"], "/terraform/main.tf")
        self.assertIn("Plaintext secrets", result["Why it matters"])
        self.assertEqual(result["Confidence"], "Inference")

    def test_parse_terraform_finding_block_minimal(self):
        block = "Finding: Missing tags\nFile: /terraform/main.tf\n"
        result = _parse_terraform_finding_block(block)
        self.assertEqual(result["Finding"], "Missing tags")
        self.assertEqual(result["File"], "/terraform/main.tf")
        self.assertNotIn("Evidence", result)

    # ── code language guesser ──────────────────────────────────────────────

    def test_guess_code_language_terraform(self):
        self.assertEqual(_guess_code_language('resource "azurerm_rg" "rg" {}'), "hcl")

    def test_guess_code_language_yaml(self):
        self.assertEqual(_guess_code_language("apiVersion: apps/v1\nkind: Deployment"), "yaml")

    def test_guess_code_language_json(self):
        self.assertEqual(_guess_code_language('{"key": "value"}'), "json")

    def test_guess_code_language_shell(self):
        self.assertEqual(_guess_code_language("kubectl get pods -n default"), "sh")

    def test_guess_code_language_unknown(self):
        self.assertEqual(_guess_code_language("some random text"), "")

    # ── full Terraform review text formatter ───────────────────────────────

    _SAMPLE_TF_REVIEW = (
        'Terraform discovery: {"repository_id": "repo-abc", "commit_sha": "c0ffee", "incomplete": false}\n'
        'Validation files: /terraform/main.tf, /terraform/variables.tf\n'
        '\n'
        "Finding: Hardcoded ACR credentials\n"
        "Evidence: Confirmed exact excerpt: 'admin_password = \"secret\"\\n  admin_username = \"admin\"'\n"
        "File: /terraform/main.tf\n"
        "Why it matters: Unverified interpretation: Exposes credentials in plaintext.\n"
        "Verification needed: Use Azure Key Vault references instead.\n"
        "Confidence: Inference\n"
        "\n"
        "Finding: Missing resource lock\n"
        "Evidence: Confirmed exact excerpt: 'resource \"azurerm_kubernetes_cluster\" \"aks\" {'\n"
        "File: /terraform/main.tf\n"
        "Why it matters: Unverified interpretation: Cluster could be deleted accidentally.\n"
        "Verification needed: Add azurerm_management_lock.\n"
        "Confidence: Inference\n"
    )

    def test_format_terraform_review_header(self):
        out = _format_terraform_review_text(self._SAMPLE_TF_REVIEW)
        self.assertIn("🏗️ Terraform Review", out)

    def test_format_terraform_review_discovery_table(self):
        out = _format_terraform_review_text(self._SAMPLE_TF_REVIEW)
        self.assertIn("📦 Repository Discovery", out)
        self.assertIn("repo-abc", out)
        self.assertIn("c0ffee", out)
        self.assertIn("| Field | Value |", out)

    def test_format_terraform_review_files_list(self):
        out = _format_terraform_review_text(self._SAMPLE_TF_REVIEW)
        self.assertIn("📁 Files Reviewed", out)
        self.assertIn("`/terraform/main.tf`", out)
        self.assertIn("`/terraform/variables.tf`", out)

    def test_format_terraform_review_finding_cards(self):
        out = _format_terraform_review_text(self._SAMPLE_TF_REVIEW)
        self.assertIn("🔎 Findings (2)", out)
        self.assertIn("#### 1. Hardcoded ACR credentials", out)
        self.assertIn("#### 2. Missing resource lock", out)

    def test_format_terraform_review_evidence_decoded(self):
        """Evidence repr must be decoded — real newline, no Python quote wrapper."""
        out = _format_terraform_review_text(self._SAMPLE_TF_REVIEW)
        self.assertIn("admin_password", out)
        # The decoded evidence should be in a code block, not in Python repr form
        self.assertNotIn("Confirmed exact excerpt:", out.split("Original Response")[0])

    def test_format_terraform_review_interpretation_label(self):
        """'Unverified interpretation:' prefix must be stripped and shown as a callout."""
        out = _format_terraform_review_text(self._SAMPLE_TF_REVIEW)
        self.assertIn("Exposes credentials in plaintext.", out)
        self.assertIn("Unverified interpretation", out)   # still mentioned, but in a callout
        # Should NOT appear as raw "Unverified interpretation: ..." at the start of the Why line
        self.assertNotIn("**Why it matters:** Unverified interpretation:", out)

    def test_format_terraform_review_original_preserved(self):
        """Original response must be fully preserved in the collapsible block."""
        out = _format_terraform_review_text(self._SAMPLE_TF_REVIEW)
        self.assertIn("Original Response", out)
        self.assertIn("<details>", out)
        self.assertIn("</details>", out)
        # The raw text (key parts) must still be in the original block
        self.assertIn("Terraform discovery:", out)
        self.assertIn("repo-abc", out)

    def test_format_terraform_review_no_findings(self):
        """When no findings are present the output says so clearly."""
        text = (
            'Terraform discovery: {"repository_id": "repo-x", "incomplete": false}\n'
            'Validation files: /terraform/main.tf\n'
            'No evidence-validated findings to display.\n'
        )
        out = _format_terraform_review_text(text)
        self.assertIn("No evidence-validated findings", out)
        self.assertIn("Original Response", out)

    # ── end-to-end via format_response ────────────────────────────────────

    def test_format_response_routes_terraform_review(self):
        """format_response() must route Terraform review text to the specialist formatter."""
        out = format_response(self._SAMPLE_TF_REVIEW)
        self.assertIn("🏗️ Terraform Review", out)
        self.assertIn("📦 Repository Discovery", out)
        self.assertIn("repo-abc", out)
        self.assertIn("🔎 Findings", out)

    def test_format_response_terraform_discovery_raw_json_not_inline(self):
        """The raw JSON blob must NOT appear as an inline paragraph in the main body."""
        out = format_response(self._SAMPLE_TF_REVIEW)
        # The raw JSON line should only be in the original block, not unformatted in the body
        body = out.split("<details>")[0]  # everything before the first collapsible
        self.assertNotIn('"repository_id": "repo-abc"', body)

    def test_format_response_large_json_field_collapsed(self):
        """Large fields in JSON responses must go inside a collapsible block."""
        long_list = ["item"] * 50
        payload = {"summary": "Quick check.", "details": long_list}
        out = format_response(json.dumps(payload))
        self.assertIn("### 📋 Executive Summary", out)
        # The large field should be in a <details> block
        self.assertIn("<details>", out)
        self.assertIn("<summary><strong>details</strong></summary>", out)
        # But content must still be present
        self.assertIn("item", out)

    def test_format_response_evidence_code_block_syntax_highlight(self):
        """HCL evidence must get an hcl fence; YAML evidence a yaml fence."""
        hcl_finding = [{
            "Finding": "No lock",
            "File": "main.tf",
            "Evidence": 'resource "azurerm_rg" "rg" {\n  name = "rg"\n}',
            "Why it matters": "Risk.",
            "Verification needed": "Check it.",
            "Confidence": "Inference",
        }]
        out = format_response(json.dumps(hcl_finding))
        self.assertIn("```hcl", out)

    def test_format_response_escaped_newlines_rendered_as_code(self):
        """\\n sequences in the Terraform review evidence must become real newlines in the code block."""
        tf_text = (
            'Terraform discovery: {"repository_id": "r1", "incomplete": false}\n'
            'Validation files: /terraform/main.tf\n'
            "Finding: Bad config\n"
            "Evidence: Confirmed exact excerpt: 'line1\\n  line2\\n  line3'\n"
            "File: /terraform/main.tf\n"
            "Why it matters: Unverified interpretation: Concern.\n"
            "Verification needed: Verify.\n"
            "Confidence: Inference\n"
        )
        out = format_response(tf_text)
        # The code block content must have real newlines, not backslash-n
        code_block_start = out.find("```")
        code_block_end = out.find("```", code_block_start + 3) + 3
        code_segment = out[code_block_start:code_block_end]
        self.assertIn("line1", code_segment)
        self.assertIn("line2", code_segment)
        # Real newline between lines, not literal \n
        self.assertIn("line1\n", code_segment)


class ExecutiveSummaryTests(unittest.TestCase):
    """Tests for executive summary extraction and rendering."""

    def test_extract_executive_summary_from_json(self):
        """Executive summary should be extracted from JSON summary field."""
        from frontend.formatter import _extract_executive_summary
        payload = {
            "summary": "The AKS cluster is experiencing disk pressure on node-1, causing pod evictions.",
            "observed_evidence": ["Node node-1 has DiskPressure=True"],
        }
        json_text = json.dumps(payload)
        summary = _extract_executive_summary(json_text)
        self.assertIsNotNone(summary)
        self.assertIn("AKS cluster", summary)
        self.assertIn("disk pressure", summary)

    def test_extract_executive_summary_from_plain_text(self):
        """Executive summary should be extracted from plain text first paragraph."""
        from frontend.formatter import _extract_executive_summary
        text = (
            "The investigation found that the Azure DevOps pipeline 'build-prod' "
            "failed during the deployment stage due to a missing service connection. "
            "The root cause appears to be an expired service principal credential.\n\n"
            "## Details\n\n"
            "More details here..."
        )
        summary = _extract_executive_summary(text)
        self.assertIsNotNone(summary)
        self.assertIn("Azure DevOps pipeline", summary)
        self.assertIn("build-prod", summary)

    def test_format_response_plain_text_includes_executive_summary(self):
        """Plain text responses with enough content should get an executive summary."""
        plain = (
            "The AKS cluster health check revealed that node aks-agentpool-1 is "
            "experiencing disk pressure which has caused several pods to be evicted. "
            "The cluster autoscaler has not yet provisioned replacement nodes.\n\n"
            "## Recommended Actions\n\n"
            "1. Investigate disk usage on the affected node\n"
            "2. Consider manually scaling the node pool"
        )
        formatted = format_response(plain)
        self.assertIn("### 📋 Executive Summary", formatted)
        self.assertIn("AKS cluster health check", formatted)
        self.assertIn("disk pressure", formatted)

    def test_format_response_markdown_includes_executive_summary(self):
        """Markdown responses with enough content should get an executive summary."""
        md = (
            "## Investigation Results\n\n"
            "The Azure Resource Group 'rg-prod' contains 15 resources with 3 in a "
            "degraded state. The primary issue is the storage account 'stproddata' "
            "which has exceeded its capacity quota.\n\n"
            "### Affected Resources\n\n"
            "- Storage account: stproddata (quota exceeded)\n"
            "- App Service: app-prod (depends on storage)"
        )
        formatted = format_response(md)
        self.assertIn("### 📋 Executive Summary", formatted)
        self.assertIn("Azure Resource Group", formatted)
        self.assertIn("rg-prod", formatted)

    def test_no_executive_summary_for_short_content(self):
        """Short responses should not get an executive summary section."""
        short = "The cluster is healthy."
        formatted = format_response(short)
        self.assertNotIn("### 📋 Executive Summary", formatted)
        self.assertIn("The cluster is healthy.", formatted)


class AzureDevOpsFormatterTests(unittest.TestCase):
    """Tests for the Azure DevOps pipeline formatter."""

    def test_format_ado_pipeline_with_discovery(self):
        """Azure DevOps pipeline output with discovery metadata should be formatted."""
        text = (
            'Pipeline discovery: {"repository_id": "repo-123", "pipeline_id": "456", "branch": "main"}\n'
            "Pipeline: build-prod\n"
            "Status: Failed\n"
            "Stage: Deploy\n"
            "Error: Service connection not found"
        )
        from frontend.formatter import _format_ado_pipeline_text
        out = _format_ado_pipeline_text(text)
        self.assertIn("🔄 Azure DevOps Pipeline Investigation", out)
        self.assertIn("📦 Pipeline Discovery", out)
        self.assertIn("repo-123", out)
        self.assertIn("456", out)
        self.assertIn("main", out)
        self.assertIn("build-prod", out)
        self.assertIn("Failed", out)
        self.assertIn("Deploy", out)
        self.assertIn("Original Response", out)

    def test_format_response_routes_ado_pipeline(self):
        """format_response() must route Azure DevOps pipeline text to the specialist formatter."""
        text = (
            'Pipeline discovery: {"repository_id": "repo-abc", "pipeline_id": "789"}\n'
            "Pipeline: release-staging\n"
            "Status: Succeeded"
        )
        out = format_response(text)
        self.assertIn("🔄 Azure DevOps Pipeline Investigation", out)
        self.assertIn("📦 Pipeline Discovery", out)
        self.assertIn("repo-abc", out)

    def test_format_ado_pipeline_json_fallback(self):
        """JSON pipeline data should use JSON formatter."""
        text = (
            'Pipeline discovery: {"repository_id": "repo-123"}\n'
            '{"pipeline_name": "build-test", "status": "Failed", "stages": [{"name": "Build", "status": "Succeeded"}, {"name": "Deploy", "status": "Failed"}]}'
        )
        out = format_response(text)
        self.assertIn("🔄 Azure DevOps Pipeline Investigation", out)
        self.assertIn("build-test", out)
        self.assertIn("Deploy", out)


class FindingCardTests(unittest.TestCase):
    """Tests for enhanced finding card rendering."""

    def test_format_review_findings_all_fields(self):
        """Finding cards should render all fields when present."""
        findings = [{
            "Finding": "Missing resource tags",
            "File": "terraform/main.tf",
            "Evidence line": 10,
            "Evidence end line": 15,
            "Why it matters": "Tags are required for cost allocation and governance.",
            "Verification needed": "Confirm tagging policy with platform team.",
            "Confidence": "High",
            "Evidence": 'resource "azurerm_resource_group" "rg" {\n  name = "rg"\n}',
        }]
        formatted = format_review_findings(findings)
        self.assertIn("#### 1. Missing resource tags", formatted)
        self.assertIn("`terraform/main.tf` (line 10-15)", formatted)
        self.assertIn("**Why it matters:** Tags are required for cost allocation and governance.", formatted)
        self.assertIn("**Verification needed:** Confirm tagging policy with platform team.", formatted)
        self.assertIn("**Confidence:** High", formatted)
        self.assertIn("```hcl", formatted)
        self.assertIn('resource "azurerm_resource_group"', formatted)

    def test_format_review_findings_extra_fields_preserved(self):
        """Extra fields in findings should be preserved in output."""
        findings = [{
            "Finding": "Insecure configuration",
            "File": "config.yaml",
            "Why it matters": "Exposes sensitive data.",
            "Verification needed": "Review security policy.",
            "Confidence": "Medium",
            "Evidence": "password: secret123",
            "CustomField": "Custom value",
            "Severity": "Critical",
        }]
        formatted = format_review_findings(findings)
        self.assertIn("**CustomField:** Custom value", formatted)
        self.assertIn("**Severity:** Critical", formatted)


if __name__ == "__main__":
    unittest.main()
