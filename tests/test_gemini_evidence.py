import json
import unittest
from unittest.mock import patch
from langchain_core.messages import ToolMessage
from src.review.gemini_evidence import gather_terraform_evidence
from src.review.review_validation import retrieved_files


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def tools(self, calls, repositories=None, fail=False):
        code = 'resource "example" "x" {\r\n  count = 1  \r\n}\r\n'

        class FakeTool:
            def __init__(self, name):
                self.name = name

            async def ainvoke(self, call):
                calls.append(call)
                args = call['args']
                if self.name == 'repo_repository':
                    value = json.dumps(repositories if repositories is not None else [
                        {'name': 'My Project', 'id': 'discovered-id'}])
                elif args['action'] == 'list_directory':
                    value = json.dumps({'items': [
                        {'path': path} for path in [
                            '/terraform/main.tf', '/terraform/./main.tf',
                            '/terraform/modules/x.tf', '/terraform/README.md',
                            '/terraform/example.tfvars', '/outside/no.tf',
                            '/terraform/.terraform/modules/vendor.tf',
                        ]
                    ] + [{'path': '/terraform/folder.tf', 'isFolder': True}]})
                else:
                    value = code
                return ToolMessage(
                    content=[{'type': 'text', 'text': '<<tag>> untrusted\n' + value + '\n<</tag>>'}],
                    tool_call_id=call['id'], name=self.name,
                    status='error' if fail else 'success',
                )
        return [FakeTool('repo_repository'), FakeTool('repo_file')], code

    async def test_discovery_selection_deduplication_and_exact_evidence(self):
        calls = []
        tools, code = self.tools(calls)
        messages = await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual([c['args']['action'] for c in calls],
                         ['list', 'list_directory', 'get_content', 'get_content'])
        self.assertEqual(calls[0]['name'], 'repo_repository')
        self.assertEqual(calls[1]['args']['path'], '/terraform')
        self.assertTrue(calls[1]['args']['recursive'])
        paths = [c['args']['path'] for c in calls[2:]]
        self.assertEqual(paths, ['/terraform/main.tf', '/terraform/modules/x.tf'])
        for call in calls[1:]:
            self.assertEqual(call['args']['repositoryId'], 'discovered-id')
            self.assertEqual(call['args']['project'], 'My Project')
            self.assertEqual(call['args']['version'], 'main')
            self.assertEqual(call['args']['versionType'], 'Branch')
        self.assertEqual(retrieved_files(messages), {path: code for path in paths})

    async def test_policy_rejection_prevents_execution(self):
        calls = []
        tools, _ = self.tools(calls)
        with patch('src.review.gemini_evidence.enforce_read_only_policy', side_effect=PermissionError('blocked')):
            with self.assertRaises(PermissionError):
                await gather_terraform_evidence(tools)
        self.assertEqual(calls, [])

    async def test_ambiguous_repository_fails_closed(self):
        calls = []
        tools, _ = self.tools(calls, repositories=[
            {'name': 'My Project', 'id': 'a'}, {'name': 'My Project', 'id': 'b'}])
        with self.assertRaises(ValueError):
            await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual(len(calls), 1)

    async def test_mcp_error_stops_collection(self):
        calls = []
        tools, _ = self.tools(calls, fail=True)
        with self.assertRaises(RuntimeError):
            await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual(len(calls), 1)
