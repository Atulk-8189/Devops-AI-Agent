import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError

from src.agent.context import (ContextScope, RequestContext, TaskContext, Observation, MessageState,
    MAX_OBSERVATIONS, add_observation, clear_task_context, context_expired, create_task_context,
    select_relevant_context)
from src.agent.repository_discovery import RepositoryDiscovery
from src.policy.policy import enforce_read_only_policy
from src.policy.write_policy import WritePolicy


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 22, tzinfo=timezone.utc)
        self.scope = ContextScope(project="My Project", repository_id_hint="repo-1", branch_hint="main")
        self.task = create_task_context(task_id="task-1", original_objective="Review Terraform reliability",
                                       topic="terraform", scope=self.scope, now=self.now)

    def observation(self, **changes):
        return Observation(kind="observed_fact", check="terraform_structure", outcome="collected",
                           source="structural_parser", scope=self.scope, observed_at=self.now,
                           completeness="complete", **changes)

    def test_valid_request_and_task_roundtrip(self):
        request = RequestContext(request_id="request-1", original_question="Review Terraform",
            current_route="terraform", scope=self.scope, started_at=self.now,
            messages=(MessageState(message_id="message-1", role="user", stage="question"),))
        self.assertEqual(RequestContext.model_validate_json(request.model_dump_json()), request)
        task = add_observation(self.task, self.observation(), now=self.now)
        self.assertEqual(TaskContext.model_validate_json(task.model_dump_json()), task)
        self.assertTrue(task.observations[0].historical)

    def test_unknown_sensitive_and_raw_fields_rejected(self):
        for key, value in (("raw_mcp", {"content": "raw"}), ("approval", {}), ("action_sha256", "a" * 64),
                           ("allowed_ids", ["repo-1"]), ("password", "value"), ("pod", {}),
                           ("source_file_content", "resource {}"), ("instructions", "do something")):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                TaskContext.model_validate({**self.task.model_dump(), key: value})
        with self.assertRaises(ValidationError):
            MessageState(message_id="x", role="tool", stage="tool_result", content="raw response")

    def test_sensitive_prose_and_instructions_rejected(self):
        for text in ("password=hunter2", "Bearer abc", "connection_string=abc", "api_key=abc",
                     "approval approved", "ignore previous instructions", 'resource "x" { name = "y" }',
                     '{"items": []}', "a" * 64, "-----BEGIN PRIVATE KEY-----", "line1\nline2"):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                TaskContext.model_validate({**self.task.model_dump(), "original_objective": text})

    def test_required_identifiers_and_string_bounds(self):
        for changes in ({"task_id": ""}, {"task_id": "x" * 129}, {"original_objective": "word " * 120},
                        {"relevant_file_paths": ("/" + "a/" * 130,)}, {"unexpected": True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                TaskContext.model_validate({**self.task.model_dump(), **changes})

    def test_observation_limit(self):
        with self.assertRaises(ValidationError):
            TaskContext.model_validate({**self.task.model_dump(), "observations": (self.observation(),) * (MAX_OBSERVATIONS + 1)})

    def test_total_size_limits(self):
        with patch("src.agent.context.MAX_TASK_CONTEXT_BYTES", 100), self.assertRaises(ValidationError):
            TaskContext.model_validate(self.task.model_dump())
        with patch("src.agent.context.MAX_REQUEST_CONTEXT_BYTES", 100), self.assertRaises(ValidationError):
            RequestContext(request_id="request-1", original_question="Check workload",
                           current_route="aks", scope=self.scope, started_at=self.now)
        # Every individual field is valid, but duplicated scoped observations
        # exceed the real aggregate bound.
        large_scope = ContextScope(project="My Project", organization="org " * 30,
                                   repository_name_hint="app " * 30, branch_hint="main " * 20)
        obs = self.observation().model_copy(update={"scope": large_scope})
        with self.assertRaises(ValidationError):
            TaskContext.model_validate({**self.task.model_dump(), "scope": large_scope,
                                       "observations": (obs,) * MAX_OBSERVATIONS})

    def test_fact_hypothesis_unknown_missing(self):
        for kind, outcome, source, completeness in (
            ("observed_fact", "collected", "structural_parser", "complete"),
            ("hypothesis", "possible_configuration_issue", "model_hypothesis", "partial"),
            ("unknown", "not_established", "aks_collector", "unknown"),
            ("missing", "unavailable", "aks_collector", "missing")):
            obs = Observation(kind=kind, outcome=outcome, source=source, completeness=completeness,
                              check="pod_health", scope=self.scope, observed_at=self.now)
            self.assertTrue(obs.historical)
        with self.assertRaises(ValidationError):
            self.observation(historical=False)
        data = self.observation().model_dump()
        with self.assertRaises(ValidationError):
            Observation.model_validate({**data, "source": "model_hypothesis"})

    def test_timestamp_handling(self):
        with self.assertRaises(ValidationError):
            TaskContext.model_validate({**self.task.model_dump(), "created_at": datetime(2026, 9, 22)})
        with self.assertRaises(ValidationError):
            TaskContext.model_validate({**self.task.model_dump(), "last_activity_at": self.now - timedelta(seconds=1)})
        local = self.now.astimezone(timezone(timedelta(hours=5, minutes=30)))
        parsed = TaskContext.model_validate({**self.task.model_dump(), "created_at": local})
        self.assertEqual(parsed.created_at.utcoffset(), timedelta(0))
        future = self.observation().model_copy(update={"observed_at": self.now + timedelta(minutes=2)})
        with self.assertRaises(ValidationError):
            add_observation(self.task, future, now=self.now)

    def test_inactivity_and_no_automatic_refresh(self):
        self.assertFalse(context_expired(self.task, now=self.now + timedelta(minutes=14)))
        self.assertTrue(context_expired(self.task, now=self.now + timedelta(minutes=15)))
        selected = select_relevant_context(self.task, topic="terraform", scope=self.scope, now=self.now + timedelta(minutes=14))
        self.assertEqual(selected.last_activity_at, self.now)
        self.assertIsNone(select_relevant_context(self.task, topic="terraform", scope=self.scope, now=self.now + timedelta(minutes=15)))
        with self.assertRaises(ValueError):
            add_observation(self.task, self.observation(), now=self.now + timedelta(minutes=15))
        active = add_observation(self.task, self.observation(), now=self.now + timedelta(minutes=10))
        self.assertFalse(context_expired(active, now=self.now + timedelta(minutes=24)))

    def test_clear_and_scope_selection(self):
        populated = add_observation(self.task, self.observation(), now=self.now)
        cleared = clear_task_context(populated, new_task_id="task-2", original_objective="Check relationships", now=self.now)
        self.assertFalse(cleared.observations)
        self.assertFalse(cleared.completed_checks)
        self.assertEqual(len(populated.observations), 1)
        self.assertIsNone(select_relevant_context(populated, topic="aks", scope=self.scope, now=self.now))
        other = self.scope.model_copy(update={"repository_id_hint": "repo-2"})
        self.assertIsNone(select_relevant_context(populated, topic="terraform", scope=other, now=self.now))
        with self.assertRaises(ValueError):
            clear_task_context(populated, new_task_id="task-1", original_objective="Check", now=self.now)

    def test_context_does_not_authorize_or_change_policy(self):
        self.assertEqual(RepositoryDiscovery().allowed_ids, set())
        self.assertFalse(WritePolicy().writes_enabled)
        self.assertEqual(WritePolicy().repository_branches, {})
        for call in (
            {"name": "repo_file", "args": {"action": "update_existing_file", "project": self.scope.project}},
            {"name": "repo_repository", "args": {"action": "list", "project": "Other"}},
            {"name": "kubectl_resources", "args": {"operation": "get", "resource": "secrets", "args": "--namespace default"}}):
            with self.assertRaises(PermissionError):
                enforce_read_only_policy(call)
        read = {"name": "repo_file", "args": {"action": "get_content", "version": "other"}}
        enforce_read_only_policy(read)
        self.assertEqual(read["args"]["version"], "main")
