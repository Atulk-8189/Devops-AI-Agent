import unittest

from src.review.terraform_structure import extract_terraform_structure


class TerraformStructureTests(unittest.TestCase):
    def test_all_supported_blocks_and_exact_lines(self):
        text = ('# comment\nresource "azurerm_example" "app" {\n  name = var.name\n}\n'
                'variable "name" {}\noutput "id" { value = azurerm_example.app.id }\n'
                'provider "azurerm" { features {} }\nmodule "network" { source = "./modules/network" }\n')
        result = extract_terraform_structure({"/terraform/main.tf": text})
        self.assertEqual(result["parse_status"], "complete")
        self.assertEqual(result["resources"], [{"type": "azurerm_example", "name": "app", "file": "/terraform/main.tf", "start_line": 2, "end_line": 4}])
        for category, name, line in (("variables", "name", 5), ("outputs", "id", 6),
                                     ("providers", "azurerm", 7), ("modules", "network", 8)):
            with self.subTest(category=category):
                entry = result[category][0]
                self.assertEqual(entry["name"], name)
                self.assertEqual((entry["start_line"], entry["end_line"]), (line, line))
        self.assertEqual(result["modules"][0]["source"], "./modules/network")

    def test_nested_braces_comments_heredoc_and_crlf_locations(self):
        text = ('/* resource "fake" "fake" {} */\r\nresource "test" "real" {\r\n'
                ' value = <<EOF\r\nresource "fake" "fake" {}\r\nEOF\r\n'
                ' nested { values = { a = "}" } }\r\n}\r\n')
        result = extract_terraform_structure({"a.tf": text})
        self.assertEqual(result["parse_status"], "complete")
        self.assertEqual(len(result["resources"]), 1)
        self.assertEqual(result["resources"][0]["end_line"], 7)

    def test_dynamic_module_source_is_unknown_without_evaluation(self):
        for expression in ('var.module_source', '"./${var.name}"', '"%{ if true }x%{ endif }"'):
            with self.subTest(expression=expression):
                result = extract_terraform_structure({"a.tf": 'module "m" { source = ' + expression + ' }'})
                self.assertEqual(result["parse_status"], "partial")
                self.assertIsNone(result["modules"][0]["source"])
                self.assertEqual(result["modules"][0]["source_status"], "unknown")

    def test_malformed_file_does_not_produce_guessed_blocks(self):
        result = extract_terraform_structure({"a.tf": 'resource "x" "a" {}\nresource "x" "b" {'})
        self.assertEqual(result["parse_status"], "failed")
        self.assertEqual(result["resources"], [])
        self.assertNotIn('resource "x"', str(result))

    def test_unsupported_blocks_and_labels_are_partial(self):
        for text in ('locals { x = 1 }', 'resource "only_one_label" {}', 'data "x" "y" {}'):
            with self.subTest(text=text):
                result = extract_terraform_structure({"a.tf": text})
                self.assertEqual(result["parse_status"], "partial")
                self.assertTrue(result["files"][0]["unparsed"])

    def test_multiple_files_are_deterministic_and_preserve_originals(self):
        files = {"b.tf": 'variable "second" {}', "a.tf": 'variable "first" {}\nvariable "third" {}'}
        original = dict(files)
        result = extract_terraform_structure(files)
        self.assertEqual(result, extract_terraform_structure(dict(reversed(list(files.items())))))
        self.assertEqual([v["name"] for v in result["variables"]], ["first", "third", "second"])
        self.assertEqual(files, original)

    def test_valid_and_failed_files_report_partial_coverage(self):
        result = extract_terraform_structure({"a.tf": 'output "x" { value = 1 }', "b.tf": 'output {'})
        self.assertEqual(result["parse_status"], "partial")
        self.assertEqual(len(result["outputs"]), 1)
