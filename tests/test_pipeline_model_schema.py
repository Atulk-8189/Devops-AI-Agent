"""Model-facing narrowing only; upstream schemas and policy remain authoritative."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from jsonschema import validate, ValidationError

from src.agent.workflows import openai_tools
from src.policy.policy import enforce_read_only_policy
from test_openai_flow import PIPELINE_DEFINITION_SCHEMA


class PipelineModelSchemaTests(unittest.TestCase):
    def test_only_list_exposed_without_mutating_upstream_schema(self):
        for class_schema in (False, True):
            with self.subTest(class_schema=class_schema):
                upstream = deepcopy(PIPELINE_DEFINITION_SCHEMA)
                before = deepcopy(upstream)
                schema = SimpleNamespace(model_json_schema=lambda: upstream) if class_schema else upstream
                tool = SimpleNamespace(name="pipelines_definition", description="Pipeline reads", args_schema=schema)
                exposed = openai_tools([tool])[0]["function"]["parameters"]
                self.assertEqual(exposed["properties"]["action"]["enum"], ["list"])
                self.assertEqual(exposed["required"].count("action"), 1)
                self.assertNotIn("list_revisions", exposed["properties"]["action"]["enum"])
                expected = deepcopy(before)
                expected["properties"]["action"]["enum"] = ["list"]
                self.assertEqual(exposed, expected)
                self.assertEqual(upstream, before)
                self.assertIsNot(exposed["properties"]["action"], upstream["properties"]["action"])
                validate({"action": "list", "project": "My Project"}, exposed)
                with self.assertRaises(ValidationError):
                    validate({"action": "list_revisions", "project": "My Project"}, exposed)

    def test_other_tool_schema_unchanged(self):
        schema = deepcopy(PIPELINE_DEFINITION_SCHEMA)
        tool = SimpleNamespace(name="repo_repository", description="Repository reads", args_schema=schema)
        self.assertEqual(openai_tools([tool])[0]["function"]["parameters"], schema)

    def test_minimal_schema_is_narrowed_without_mutation(self):
        schema = {"type": "object"}
        tool = SimpleNamespace(name="pipelines_definition", description="Read", args_schema=schema)
        exposed = openai_tools([tool])[0]["function"]["parameters"]
        self.assertEqual(exposed["properties"]["action"], {"type": "string", "enum": ["list"]})
        self.assertEqual(exposed["required"], ["action"])
        self.assertEqual(schema, {"type": "object"})

    def test_optional_action_becomes_required_without_changing_project_contract(self):
        for required in (None, [], ["project"], ["action"], ["action", "project"]):
            with self.subTest(required=required):
                schema = deepcopy(PIPELINE_DEFINITION_SCHEMA)
                if required is None:
                    schema.pop("required")
                else:
                    schema["required"] = list(required)
                before = deepcopy(schema)
                tool = SimpleNamespace(name="pipelines_definition", description="Read", args_schema=schema)
                exposed = openai_tools([tool])[0]["function"]["parameters"]
                self.assertIn("action", exposed["properties"])
                self.assertEqual(exposed["properties"]["action"]["enum"], ["list"])
                self.assertEqual(exposed["required"].count("action"), 1)
                self.assertEqual("project" in exposed["required"], "project" in (required or []))
                self.assertEqual(exposed["properties"]["project"], before["properties"]["project"])
                with self.assertRaises(ValidationError):
                    validate({"project": "My Project"}, exposed)
                if "project" not in (required or []):
                    validate({"action": "list"}, exposed)
                validate({"action": "list", "project": "My Project"}, exposed)
                self.assertEqual(schema, before)

    def test_execution_policy_still_enforces_action_and_project(self):
        for arguments in ({"project": "My Project"}, {},
                          {"action": "list_revisions", "project": "My Project"},
                          {"action": "list", "project": "Other"}):
            with self.subTest(arguments=arguments), self.assertRaises(PermissionError):
                enforce_read_only_policy({"name": "pipelines_definition", "args": arguments})
        for arguments in ({"action": "list"}, {"action": "list", "project": "My Project"}):
            with self.subTest(arguments=arguments):
                call = {"name": "pipelines_definition", "args": arguments}
                enforce_read_only_policy(call)
                self.assertEqual(call["args"], {"action": "list", "project": "My Project"})
