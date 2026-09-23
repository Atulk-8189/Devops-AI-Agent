import unittest

from src.review.terraform_relationships import extract_terraform_relationships


class TerraformRelationshipTests(unittest.TestCase):
    def extract(self, source):
        return extract_terraform_relationships({"/terraform/main.tf": source})

    def test_resource_variable_module_and_output_references(self):
        result = self.extract('''resource "azurerm_subnet" "app" {}
variable "environment" {}
module "network" { source = "./network" }
resource "azurerm_network_interface" "app" {
  subnet = azurerm_subnet.app.id
  environment = var.environment
  network = module.network.vnet_id
}
output "subnet" { value = azurerm_subnet.app.id }
output "network" { value = module.network.vnet_id }
output "environment" { value = var.environment }
provider "azurerm" { environment = var.environment }
module "consumer" { source = "./consumer" environment = var.environment }
''')
        edges = [(r["from"], r["to"], r["line"]) for r in result["relationships"]]
        self.assertEqual(edges, [
            ("azurerm_network_interface.app", "azurerm_subnet.app", 5),
            ("azurerm_network_interface.app", "var.environment", 6),
            ("azurerm_network_interface.app", "module.network", 7),
            ("output.subnet", "azurerm_subnet.app", 9),
            ("output.network", "module.network", 10),
            ("output.environment", "var.environment", 11),
            ("provider.azurerm", "var.environment", 12),
            ("module.consumer", "var.environment", 13),
        ])
        self.assertTrue(all(r["source_file"] == "/terraform/main.tf" for r in result["relationships"]))

    def test_static_local_source_connects_only_collected_directory(self):
        result = extract_terraform_relationships({
            "/terraform/main.tf": 'module "network" { source = "./modules/network" }',
            "/terraform/modules/network/main.tf": 'resource "x" "network" {}',
        })
        self.assertEqual(result["relationship_status"], "complete")
        self.assertEqual(result["relationships"], [{"from": "module.network", "to": "/terraform/modules/network",
                         "relationship": "local_source", "source_file": "/terraform/main.tf",
                         "module_directory": "/terraform", "line": 1}])

    def test_external_missing_and_escaping_sources_are_unresolved(self):
        for source in ("./missing", "../../outside", "registry/module/provider", "https://example.invalid/module"):
            with self.subTest(source=source):
                result = self.extract('module "m" { source = "' + source + '" }')
                self.assertEqual(result["relationships"], [])
                self.assertEqual(result["relationship_status"], "partial")

    def test_dynamic_and_ambiguous_expressions_are_not_inferred(self):
        for expression in ('x.a[var.key].id', 'x.a[*].id', 'var.key ? x.a.id : x.a.id',
                           '"${x.a.id}"', 'upper(var.key)', '[for v in x.a : v.id]',
                           '(x.a).id', '{ var.key = x.a.id }'):
            with self.subTest(expression=expression):
                result = self.extract('resource "x" "a" {}\nvariable "key" {}\n'
                                      'output "o" { value = ' + expression + ' }')
                self.assertEqual(result["relationships"], [])
                self.assertTrue(result["unresolved_references"])
                self.assertEqual(result["relationship_status"], "partial")

    def test_multiple_references_and_nested_block(self):
        result = self.extract('variable "a" {}\nvariable "b" {}\n'
                              'resource "x" "r" { values = [var.b, var.a, var.a] nested { value = var.b } }')
        self.assertEqual({r["to"] for r in result["relationships"]}, {"var.a", "var.b"})
        self.assertEqual(len(result["relationships"]), 2)
        self.assertEqual(result["relationship_status"], "complete")

    def test_literals_comments_and_booleans_do_not_create_references(self):
        result = self.extract('variable "a" {}\n# var.a\n'
                              'output "o" { value = "var.a and x.fake.id" enabled = true absent = null }')
        self.assertEqual(result["relationships"], [])
        self.assertEqual(result["relationship_status"], "complete")

    def test_module_scope_does_not_leak_across_directories(self):
        result = extract_terraform_relationships({
            "/terraform/main.tf": 'output "o" { value = x.child.id }',
            "/terraform/child/main.tf": 'resource "x" "child" {}',
        })
        self.assertEqual(result["relationships"], [])
        self.assertEqual(result["unresolved_references"][0]["reason"], "target_not_collected_or_unsupported")

    def test_duplicate_declarations_and_module_sources_are_ambiguous(self):
        result = self.extract('resource "x" "a" {}\nresource "x" "a" {}\noutput "o" { value = x.a.id }')
        self.assertEqual(result["relationships"], [])
        self.assertIn("ambiguous_target", [r["reason"] for r in result["unresolved_references"]])
        result = extract_terraform_relationships({
            "/terraform/main.tf": 'module "m" { source = "./child" source = "./child" }',
            "/terraform/child/main.tf": 'variable "a" {}',
        })
        self.assertEqual(result["relationships"], [])
        self.assertEqual(result["relationship_status"], "partial")

    def test_malformed_and_unsupported_files_are_partial(self):
        for source in ('resource "x" "a" {', 'locals { a = 1 }', 'output "o" { value = unknown }'):
            with self.subTest(source=source):
                result = self.extract(source)
                self.assertEqual(result["relationships"], [])
                self.assertEqual(result["relationship_status"], "partial")

    def test_deterministic_cross_file_references_preserve_original_evidence(self):
        files = {"/terraform/z.tf": 'output "o" {\r\n value = var.a\r\n}',
                 "/terraform/a.tf": 'variable "a" {}'}
        original = dict(files)
        result = extract_terraform_relationships(files)
        self.assertEqual(result, extract_terraform_relationships(dict(reversed(list(files.items())))))
        self.assertEqual(files, original)
        self.assertEqual(result["relationships"][0]["source_file"], "/terraform/z.tf")
        self.assertEqual(result["relationships"][0]["line"], 2)
        self.assertEqual(result["relationship_status"], "complete")
