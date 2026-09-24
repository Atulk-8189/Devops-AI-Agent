"""Model-facing repository descriptions must not change upstream constraints."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from src.agent.workflows import openai_tools
import test_openai_flow as fixtures


class RepositoryModelSchemaTests(unittest.TestCase):
    def expose(self, name, field, schema, class_schema):
        upstream = deepcopy(schema)
        upstream["properties"][field]["description"] = "Original MCP description"
        before = deepcopy(upstream)
        args_schema = SimpleNamespace(model_json_schema=lambda: upstream) if class_schema else upstream
        tool = SimpleNamespace(name=name, description="Original tool description", args_schema=args_schema)
        function = openai_tools([tool])[0]["function"]
        exposed = function["parameters"]
        description = exposed["properties"][field]["description"]
        expected = deepcopy(before)
        expected["properties"][field]["description"] = description
        # Full equality proves required fields, enums, defaults and all other fields survive.
        self.assertEqual(exposed, expected)
        self.assertEqual(upstream, before)
        self.assertIsNot(exposed, upstream)
        self.assertIsNot(exposed["properties"][field], upstream["properties"][field])
        self.assertEqual(function["description"], tool.description)
        return description

    def test_file_requires_discovered_id_not_name(self):
        for class_schema in (False, True):
            with self.subTest(class_schema=class_schema):
                description = self.expose("repo_file", "repositoryId", fixtures.RepositoryFileReadTests.schema, class_schema)
                self.assertIn("The exact repository ID returned by successful, complete, unambiguous repository discovery.", description)
                self.assertIn("Do not provide the repository name.", description)
                self.assertIn("Complete repo_repository/list discovery before file access", description)
                self.assertIn("do not infer IDs from pipeline metadata", description)

    def test_filter_distinguishes_mcp_matching_from_local_selection(self):
        for class_schema in (False, True):
            with self.subTest(class_schema=class_schema):
                description = self.expose("repo_repository", "repoNameFilter", fixtures.REPOSITORY_SCHEMA, class_schema)
                self.assertIn("Optional", description)
                self.assertIn("MCP supports case-insensitive substring filtering", description)
                self.assertIn("local verified selection requires the exact, case-sensitive repository name", description)
                self.assertIn("complete discovery", description)

    def test_absent_properties_are_not_invented(self):
        for name in ("repo_file", "repo_repository"):
            with self.subTest(name=name):
                schema = {"type": "object"}
                tool = SimpleNamespace(name=name, description="Read", args_schema=schema)
                self.assertEqual(openai_tools([tool])[0]["function"]["parameters"], schema)
