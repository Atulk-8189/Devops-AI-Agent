import json
import unittest
from unittest.mock import patch
from langchain_core.messages import ToolMessage
from src.review.terraform_evidence import gather_terraform_evidence, MAX_TERRAFORM_DISCOVERY_DEPTH
from src.review.review_validation import retrieved_files


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def tools(self, calls, repositories=None, fail=False, directories=None):
        code = 'resource "example" "x" {\r\n  count = 1  \r\n}\r\n'

        class FakeTool:
            def __init__(self, name):
                self.name = name

            async def ainvoke(self, call):
                calls.append(call)
                args = call['args']
                if self.name == 'repo_repository':
                    rows = repositories if repositories is not None else [{'name': 'My Project', 'id': 'discovered-id'}]
                    value = json.dumps(rows[args['skip']:args['skip'] + args['top']])
                elif args['action'] == 'list_directory':
                    default = {'/terraform': [
                        {'path': path} for path in [
                            '/terraform/main.tf', '/terraform/./main.tf',
                            '/terraform/README.md',
                            '/terraform/example.tfvars', '/outside/no.tf',
                            '/terraform/.terraform/modules/vendor.tf',
                        ]
                    ] + [{'path': '/terraform/folder.tf', 'isFolder': True},
                         {'path': '/terraform/modules', 'isFolder': True}],
                    '/terraform/modules': [{'path': '/terraform/modules/x.tf'}]}
                    value = json.dumps({'items': (directories if directories is not None else default).get(args['path'], [])})
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
                         ['list', 'list_directory', 'list_directory', 'list_directory', 'get_content', 'get_content'])
        self.assertEqual(calls[0]['name'], 'repo_repository')
        self.assertEqual(calls[1]['args']['path'], '/terraform')
        self.assertFalse(calls[1]['args']['recursive'])
        self.assertEqual(calls[1]['args']['recursionDepth'], 1)
        paths = [c['args']['path'] for c in calls if c['args']['action'] == 'get_content']
        self.assertEqual(paths, ['/terraform/main.tf', '/terraform/modules/x.tf'])
        for call in calls[1:]:
            self.assertEqual(call['args']['repositoryId'], 'discovered-id')
            self.assertEqual(call['args']['project'], 'My Project')
            self.assertEqual(call['args']['version'], 'main')
            self.assertEqual(call['args']['versionType'], 'Branch')
        self.assertEqual(retrieved_files(messages), {path: code for path in paths})
        self.assertEqual(messages.discovery['selected_files'], paths)
        self.assertFalse(messages.discovery['incomplete'])

    async def test_repository_pagination_finds_exact_match_on_later_page(self):
        calls = []
        tools, _ = self.tools(calls, repositories=[{'name': 'Other', 'id': 'other'}, {'name': 'My Project', 'id': 'selected'}])
        with patch('src.review.terraform_evidence.REPOSITORY_PAGE_SIZE', 1):
            result = await gather_terraform_evidence(tools, log=lambda _: None)
        pages = [c['args'] for c in calls if c['name'] == 'repo_repository']
        self.assertEqual([p['skip'] for p in pages], [0, 1, 2])
        self.assertTrue(all(p['top'] == 1 and p['project'] == 'My Project' for p in pages))
        self.assertEqual(result.discovery['repository_id'], 'selected')

    async def test_repository_page_limit_never_accepts_unverified_unique_match(self):
        calls = []
        tools, _ = self.tools(calls)
        with patch('src.review.terraform_evidence.REPOSITORY_PAGE_SIZE', 1), \
             patch('src.review.terraform_evidence.MAX_REPOSITORY_DISCOVERY_PAGES', 1):
            with self.assertRaisesRegex(ValueError, 'discovery incomplete'):
                await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual(len(calls), 1)

    async def test_ambiguous_match_across_pages_is_rejected(self):
        calls = []
        tools, _ = self.tools(calls, repositories=[{'name': 'My Project', 'id': 'a'}, {'name': 'My Project', 'id': 'b'}])
        with patch('src.review.terraform_evidence.REPOSITORY_PAGE_SIZE', 1), self.assertRaisesRegex(ValueError, 'Ambiguous'):
            await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertTrue(all(c['name'] == 'repo_repository' for c in calls))

    async def test_depth_limit_reports_skipped_subdirectories(self):
        directories = {
            '/terraform': [{'path': '/terraform/main.tf'}, {'path': '/terraform/a', 'isFolder': True}],
            '/terraform/a': [{'path': '/terraform/a/b', 'isFolder': True}],
            '/terraform/a/b': [{'path': '/terraform/a/b/nested.tf'}, {'path': '/terraform/a/b/deeper', 'isFolder': True}],
        }
        calls = []
        tools, _ = self.tools(calls, directories=directories)
        result = await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual(MAX_TERRAFORM_DISCOVERY_DEPTH, 3)
        self.assertEqual(result.discovery['selected_files'], ['/terraform/a/b/nested.tf', '/terraform/main.tf'])
        self.assertEqual(result.discovery['skipped_directories'], ['/terraform/a/b/deeper'])
        self.assertTrue(result.discovery['incomplete'])
        self.assertEqual(result.discovery['limit_reasons'], ['depth_limit'])
        self.assertTrue(all(not c['args']['recursive'] and c['args']['recursionDepth'] == 1
                            for c in calls if c['args']['action'] == 'list_directory'))

    async def test_directory_call_limit_is_explicit_and_deterministic(self):
        calls = []
        tools, _ = self.tools(calls)
        with patch('src.review.terraform_evidence.MAX_TERRAFORM_DIRECTORY_LISTINGS', 1):
            result = await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual(result.discovery['selected_files'], ['/terraform/main.tf'])
        self.assertEqual(result.discovery['skipped_directories'], ['/terraform/folder.tf', '/terraform/modules'])
        self.assertEqual(result.discovery['limit_reasons'], ['directory_listing_limit'])

    async def test_file_order_deduplication_and_exclusions(self):
        entries = [{'path': '/terraform/' + name} for name in
                   ['z.tf', './a.tf', 'a.tf', 'terraform.tfstate', 'vars.tfvars', 'main.tf.json', 'plan', 'README.md']]
        entries += [{'path': '/terraform/.terraform', 'isFolder': True}]
        for items in (entries, list(reversed(entries))):
            with self.subTest(reverse=items != entries):
                calls = []
                tools, _ = self.tools(calls, directories={'/terraform': items})
                result = await gather_terraform_evidence(tools, log=lambda _: None)
                self.assertEqual(result.discovery['selected_files'], ['/terraform/a.tf', '/terraform/z.tf'])
                self.assertEqual([c['args']['path'] for c in calls if c['args']['action'] == 'get_content'], result.discovery['selected_files'])

    async def test_missing_exact_repository_is_rejected(self):
        calls = []
        tools, _ = self.tools(calls, repositories=[{'name': 'My Project copy', 'id': 'other'}])
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual(len(calls), 1)

    async def test_failed_directory_response_is_not_used_for_file_reads(self):
        calls = []
        tools, _ = self.tools(calls)
        original = tools[1].ainvoke

        async def failed_listing(call):
            result = await original(call)
            result.status = 'error'
            return result

        tools[1].ainvoke = failed_listing
        with self.assertRaisesRegex(RuntimeError, 'MCP tool failed'):
            await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual([c['args']['action'] for c in calls], ['list', 'list_directory'])

    async def test_unexpected_deep_entry_cannot_bypass_one_level_traversal(self):
        calls = []
        tools, _ = self.tools(calls, directories={'/terraform': [{'path': '/terraform/a/b/c/d.tf'}]})
        with self.assertRaisesRegex(ValueError, 'one-level'):
            await gather_terraform_evidence(tools, log=lambda _: None)
        self.assertEqual(len(calls), 2)

    async def test_policy_rejection_prevents_execution(self):
        calls = []
        tools, _ = self.tools(calls)
        with patch('src.review.terraform_evidence.enforce_read_only_policy', side_effect=PermissionError('blocked')):
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
