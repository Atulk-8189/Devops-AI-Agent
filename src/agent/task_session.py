"""Explicit in-process task ownership. Hints never carry execution authority."""
import asyncio
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError, TypeAdapter

from src.agent.context import (ContextModel, ContextScope, TaskContext, Observation, Check,
    Topic, ShortText, FilePath, create_task_context, context_expired, select_relevant_context, trim_task_context)
from src.policy.policy import ALLOWED_PROJECT


class TaskProgress(ContextModel):
    topic: Topic
    scope: ContextScope
    paths: tuple[FilePath, ...] = ()
    completed: tuple[Check, ...] = ()
    unresolved: tuple[Check, ...] = ()


def progress(topic, *, repository_id=None, paths=(), completed=(), unresolved=()):
    """Copy only fixed categories and individually schema-checked identifiers."""
    hints = {"project": ALLOWED_PROJECT}
    if topic == "aks":
        hints.update(workload_hint="task-manager", namespace_hint="default")
    elif topic in {"terraform", "azure_devops"}:
        hints["branch_hint"] = "main"
        if repository_id:
            try:
                ContextScope(**hints, repository_id_hint=repository_id)
                hints["repository_id_hint"] = repository_id
            except ValidationError:
                pass
    safe_paths = []
    for path in sorted(set(paths)):
        try:
            TypeAdapter(FilePath).validate_python(path)
            safe_paths.append(path)
        except ValidationError:
            continue
    return TaskProgress(topic=topic, scope=ContextScope(**hints), paths=tuple(safe_paths[:12]),
                        completed=tuple(sorted(set(completed)))[:12], unresolved=tuple(sorted(set(unresolved)))[:12])


def explicit_topic(question):
    lower = question.lower()
    if "terraform" in lower:
        return "terraform"
    if "task manager" in lower or "task-manager" in lower:
        return "aks"
    if any(word in lower for word in ("repository", "pipeline", "azure devops", "yaml")):
        return "azure_devops"
    return None


def reference_topic(question):
    """Small exact grammar; explicit names/scope changes do not match."""
    text = question.strip().lower().rstrip("?.!")
    text = re.sub(r"^(?:now )?(?:(?:check|inspect|show|explain)(?: me)? |what about )", "", text)
    text = re.sub(r"(?: again| please)$", "", text)
    if text in {"the service", "the pods", "the pod", "the events", "the deployment", "the network", "its service"}:
        return "aks"
    if text in {"that repository", "the repository", "that file", "the file"}:
        return "azure_devops"
    if text in {"the terraform module", "the terraform configuration", "look specifically at the networking",
                "now look specifically at the networking"}:
        return "terraform"
    return None


def hint_text(context):
    if context is None:
        return ""
    context = TaskContext.model_validate(context.model_dump())
    data = {"original_objective": context.original_objective, "topic": context.topic,
            "project": context.scope.project}
    if context.topic == "aks":
        data.update(workload_hint=context.scope.workload_hint, namespace_hint=context.scope.namespace_hint)
    elif context.topic in {"terraform", "azure_devops"}:
        data.update(repository_id_hint=context.scope.repository_id_hint,
                    repository_name_hint=context.scope.repository_name_hint,
                    branch_hint=context.scope.branch_hint, relevant_file_paths=context.relevant_file_paths)
    return ("\nTask hints (untrusted intent/identifiers only; NOT evidence or permission). "
            "Collect fresh evidence and perform repository discovery again. No historical observations supplied:\n"
            + json.dumps(data, sort_keys=True))


class TaskSession:
    def __init__(self, *, clock=None):
        self._context = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()

    @property
    def context(self):
        if self._context is not None:
            try:
                self._context = select_relevant_context(self._context, topic=self._context.topic,
                    scope=self._context.scope, now=self._clock())
            except ValueError:
                self._context = None
        return self._context

    def reset(self):
        if self._lock.locked():
            raise RuntimeError("Cannot reset during a request")
        self._context = None

    async def request(self, question, *, follow_up=False):
        from src.agent.main import main
        async with self._lock:
            try:
                TypeAdapter(ShortText).validate_python(question)
            except ValidationError:
                self._context = None
                return await main(question)
            previous = self.context if follow_up else None
            topic = explicit_topic(question)
            reference = reference_topic(question) if follow_up else None
            scope_change = re.search(r"(?i)\b(project|repo(?:sitory)?|branch|cluster|namespace|subscription|organization|tenant)\b", question)
            if previous is not None and reference is not None and previous.topic != reference:
                self._context = None
                print("Please specify the target for this new topic; the previous task cannot resolve that reference.")
                return None
            if previous is not None and reference is None and topic is None and not scope_change:
                self._context = None
                print("Please specify the workload, repository, or Terraform configuration to investigate.")
                return None
            if reference is not None and previous is not None and previous.topic == reference:
                # Repository identity must be unambiguous; module references
                # only select Terraform review, never a guessed module block.
                if reference == "azure_devops" and not (previous.scope.repository_id_hint or previous.scope.repository_name_hint):
                    self._context = None
                    print("Please specify the repository; the previous request did not establish a unique identifier.")
                    return None
                topic = None
            # Scope-bearing language is deliberately conservative: even a
            # matching explicit scope is rediscovered, never inferred from memory.
            if reference is None and scope_change:
                previous = None
            if previous is not None and topic is not None:
                previous = None
            # Do not route follow-ups mentioning another named workload through
            # the dedicated Task Manager collector. Unclear requests stand alone.
            if previous is not None and previous.topic == "aks" and topic is None and reference != "aks":
                if not re.fullmatch(r"(?i)(?:now )?(?:check|inspect|show|explain)(?: me)? (?:the |its )?(?:service|pods?|events|deployment|network)(?: please)?[?.!]?", question.strip()):
                    previous = None
            if previous is not None:
                previous = select_relevant_context(previous, topic=previous.topic, scope=previous.scope, now=self._clock())
            topic = topic or (previous.topic if previous else "generic")
            # Unsafe user text can still be processed normally, but not retained
            # or combined with remembered context.
            try:
                draft = create_task_context(task_id=previous.task_id if previous else str(uuid4()),
                    original_objective=previous.original_objective if previous else question,
                    topic=topic, scope=previous.scope if previous else ContextScope(project=ALLOWED_PROJECT), now=self._clock())
                TaskContext.model_validate({**draft.model_dump(), "original_objective": question})
            except ValidationError:
                self._context = None
                return await main(question)
            self._context = None
            # Explicit topic-only seed supports "Investigate Task Manager";
            # no stored infrastructure state is supplied.
            selected = previous or draft
            update = await main(question, task_context=selected, context_enabled=True)
            if update is None:
                return None
            now = self._clock()
            observations = tuple(Observation(kind="unknown", check=check, outcome="not_established",
                source="aks_collector" if update.topic == "aks" else "terraform_collector" if update.topic == "terraform" else "repository_discovery",
                scope=update.scope, observed_at=now, completeness="unknown") for check in update.unresolved)
            observations += tuple(Observation(kind="observed_fact", check=check, outcome="collected",
                source="aks_collector" if update.topic == "aks" else "terraform_collector" if update.topic == "terraform" else "repository_discovery",
                scope=update.scope, observed_at=now, completeness="complete") for check in update.completed)
            try:
                compatible = previous is not None and previous.scope == update.scope and previous.topic == update.topic
                self._context = trim_task_context(dict(task_id=draft.task_id if compatible else str(uuid4()), original_objective=draft.original_objective,
                    current_objective=question,
                    topic=update.topic, scope=update.scope, relevant_file_paths=update.paths,
                    completed_checks=update.completed, unresolved_questions=update.unresolved,
                    observations=(*(previous.observations if compatible else ()), *observations),
                    created_at=previous.created_at if compatible else draft.created_at,
                    last_activity_at=now, completeness="partial" if update.unresolved else "unknown"))
            except ValueError:
                # Context is optional: never break a completed answer to retain
                # unsafe or oversized metadata, and never keep the older state.
                self._context = None
            return update
