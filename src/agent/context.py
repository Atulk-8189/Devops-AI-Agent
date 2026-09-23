"""In-memory context contracts; never an authorization source.

Reusable observations use fixed vocabulary, not model-authored prose. Sources
are provenance labels, never executable tools, instructions, or permission grants.
"""
import json
import posixpath
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import (AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field,
                      StringConstraints, model_validator)

MAX_TASK_CONTEXT_BYTES = 8000
MAX_REQUEST_CONTEXT_BYTES = 16000
MAX_OBSERVATIONS = 12
TASK_INACTIVITY_TTL = timedelta(minutes=15)
OBSERVATION_STALE_AFTER = timedelta(minutes=5)
Topic = Literal["generic", "azure_devops", "aks", "terraform"]
Completeness = Literal["complete", "partial", "unknown", "missing"]


def safe_text(value):
    # Fail closed rather than persisting a possibly lossy redaction. This is
    # defense in depth, not a general-purpose secret detection guarantee.
    inspected = unicodedata.normalize("NFKC", value)
    if (any(unicodedata.category(c).startswith("C") for c in value) or any(c in value for c in '{}[]`')
            or re.search(r"(?i)password|passwd|secret|token|credential|connection.?string|"
                         r"api.?key|authorization|approval|action_sha256|bearer|private.?key|"
                         r"ignore.*instruction|system.?prompt|system.?instruction|override.*policy|"
                         r"grant.*permission|https?://|[A-Za-z0-9+/=_-]{40,}|"
                         r"\b(?:pwd|accountkey|sharedaccesssignature|sig)\s*=|"
                         r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://|"
                         r"\bbasic\s+[A-Za-z0-9+/=]+|\b(?:sk-|ghp_|github_pat_|AKIA|ASIA)|"
                         r"\beyJ[A-Za-z0-9_-]*\.|\b(?:stringData|client[_-]?secret)\b", inspected)):
        raise ValueError("Unsafe context text")
    return value


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128,
                         pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._:/@-]*$"), AfterValidator(safe_text)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"\S"), AfterValidator(safe_text)]


def repository_path(value):
    safe_text(value)
    if not value.startswith("/") or value.startswith("//") or posixpath.normpath(value) != value or "\\" in value:
        raise ValueError("Expected normalized repository path")
    return value


FilePath = Annotated[str, StringConstraints(min_length=2, max_length=256), AfterValidator(repository_path)]


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


Timestamp = Annotated[AwareDatetime, AfterValidator(utc)]


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ContextScope(ContextModel):
    project: Identifier
    organization: Identifier | None = None
    repository_id_hint: Identifier | None = None
    repository_name_hint: Identifier | None = None
    branch_hint: Identifier | None = None
    cluster_hint: Identifier | None = None
    workload_hint: Identifier | None = None
    namespace_hint: Identifier | None = None


Check = Literal["repository_discovery", "pipeline_listing", "directory_listing", "file_read",
                "deployment_health", "pod_health", "service_health", "events", "terraform_structure",
                "terraform_relationships", "terraform_review"]


class Observation(ContextModel):
    kind: Literal["observed_fact", "hypothesis", "unknown", "missing"]
    check: Check
    outcome: Literal["healthy", "unhealthy", "present", "absent", "collected", "incomplete",
                     "possible_configuration_issue", "not_established", "unavailable"]
    source: Literal["repository_discovery", "aks_collector", "terraform_collector", "structural_parser",
                    "relationship_extractor", "model_hypothesis"]
    scope: ContextScope
    observed_at: Timestamp
    completeness: Completeness
    historical: Literal[True] = True
    freshness: Literal["historical", "stale"] = "historical"

    @model_validator(mode="after")
    def qualified(self):
        if self.historical is not True:
            raise ValueError("Observations are historical only")
        if self.kind == "hypothesis":
            if self.outcome != "possible_configuration_issue" or self.source != "model_hypothesis":
                raise ValueError("Hypotheses must remain explicitly qualified")
        elif self.source == "model_hypothesis" or self.outcome == "possible_configuration_issue":
            raise ValueError("Model output cannot establish observed facts")
        if self.kind in {"unknown", "missing"} and (self.outcome not in {"not_established", "unavailable", "absent"}
                                                   or self.completeness != self.kind):
            raise ValueError("Unknown/missing evidence cannot assert health")
        return self


def bounded(model, limit):
    size = len(json.dumps(model.model_dump(mode="json"), sort_keys=True, ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8"))
    if size > limit:
        raise ValueError("Context size limit exceeded")
    return model


class TaskContext(ContextModel):
    task_id: Identifier
    original_objective: ShortText
    current_objective: ShortText | None = None
    topic: Topic
    scope: ContextScope
    relevant_file_paths: Annotated[tuple[FilePath, ...], Field(max_length=12)] = ()
    completed_checks: Annotated[tuple[Check, ...], Field(max_length=12)] = ()
    # Categorical questions prevent model-authored instructions entering memory.
    unresolved_questions: Annotated[tuple[Check, ...], Field(max_length=12)] = ()
    observations: Annotated[tuple[Observation, ...], Field(max_length=MAX_OBSERVATIONS)] = ()
    created_at: Timestamp
    last_activity_at: Timestamp
    completeness: Completeness = "unknown"
    context_trimmed: bool = False

    @model_validator(mode="after")
    def valid_task(self):
        if self.last_activity_at < self.created_at:
            raise ValueError("Invalid activity timestamp")
        if any(o.scope != self.scope or not self.created_at <= o.observed_at <= self.last_activity_at for o in self.observations):
            raise ValueError("Observation scope or timestamp does not match task")
        return bounded(self, MAX_TASK_CONTEXT_BYTES)


class MessageState(ContextModel):
    message_id: Identifier
    role: Literal["user", "assistant", "tool"]
    stage: Literal["question", "planning", "tool_result", "final", "validation_error"]
    # No message content, raw arguments, or hidden reasoning.


class CollectionState(ContextModel):
    checks: Annotated[tuple[Check, ...], Field(max_length=12)] = ()
    completeness: Completeness = "unknown"
    collected_at: Timestamp | None = None
    outcome: Literal["not_collected", "collected", "missing", "unknown", "failed"] = "not_collected"


class RequestUsage(ContextModel):
    """Request-local counters only; never reusable task memory."""
    model_calls: Annotated[int, Field(ge=0)] = 0
    tool_attempts: Annotated[int, Field(ge=0)] = 0
    mcp_dispatches: Annotated[int, Field(ge=0)] = 0
    result_chars: Annotated[int, Field(ge=0)] = 0
    input_chars: Annotated[int, Field(ge=0)] = 0


class RequestContext(ContextModel):
    request_id: Identifier
    original_question: ShortText
    current_route: Topic
    scope: ContextScope
    started_at: Timestamp
    usage: RequestUsage = Field(default_factory=RequestUsage)
    last_collection_at: Timestamp | None = None
    messages: Annotated[tuple[MessageState, ...], Field(max_length=32)] = ()
    tool_execution_count: Annotated[int, Field(ge=0, le=100)] = 0
    multiple_call_attempts: Annotated[int, Field(ge=0, le=100)] = 0
    duplicate_call_ids: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    repository_discovery: CollectionState = Field(default_factory=CollectionState)
    aks_investigation: CollectionState = Field(default_factory=CollectionState)
    terraform_review: CollectionState = Field(default_factory=CollectionState)
    validation_results: Annotated[tuple[Literal["valid", "invalid_schema", "unknown", "incomplete", "failed"], ...], Field(max_length=32)] = ()
    completeness: Completeness = "unknown"

    @model_validator(mode="after")
    def valid_request(self):
        times = [self.last_collection_at, self.repository_discovery.collected_at,
                 self.aks_investigation.collected_at, self.terraform_review.collected_at]
        if any(t is not None and t < self.started_at for t in times):
            raise ValueError("Collection predates request")
        return bounded(self, MAX_REQUEST_CONTEXT_BYTES)


def create_task_context(*, task_id, original_objective, topic, scope, now):
    now = utc(now)
    return TaskContext(task_id=task_id, original_objective=original_objective, topic=topic,
                       scope=scope, created_at=now, last_activity_at=now)


def context_expired(context, *, now):
    context = TaskContext.model_validate(context.model_dump())
    now = utc(now)
    if now < context.last_activity_at:
        raise ValueError("Clock precedes task activity")
    return now - context.last_activity_at >= TASK_INACTIVITY_TTL


def add_observation(context, observation, *, now):
    if context_expired(context, now=now):
        raise ValueError("Task expired; create a new task")
    # Revalidate external objects rather than trusting model_copy/model_construct.
    observation = Observation.model_validate(observation.model_dump())
    return trim_task_context({**context.model_dump(), "last_activity_at": utc(now),
                                      "observations": (*context.observations, observation)})


def observation_freshness(observation, *, now):
    """Recent observations are still historical, never current evidence."""
    observation = Observation.model_validate(observation.model_dump())
    age = utc(now) - observation.observed_at
    if age < timedelta(0):
        raise ValueError("Observation is in the future")
    return "stale" if age >= OBSERVATION_STALE_AFTER else "historical"


def trim_task_context(data):
    """Validate all values first, then discard whole oldest observations.

    Equal timestamps use canonical JSON as a stable tie-breaker. Unsafe values
    must fail even if they would otherwise have been discarded by trimming.
    """
    data = dict(data)
    observations = [Observation.model_validate(o.model_dump() if isinstance(o, Observation) else o)
                    for o in data.get("observations", ())]
    paths = data.get("relevant_file_paths", ())
    from pydantic import TypeAdapter
    paths = tuple(TypeAdapter(FilePath).validate_python(p) for p in paths)
    base = TaskContext.model_validate({**data, "observations": (), "relevant_file_paths": ()})
    for observation in observations:
        if observation.scope != base.scope or not base.created_at <= observation.observed_at <= base.last_activity_at:
            TaskContext.model_validate({**base.model_dump(), "observations": (observation,)})
    observations.sort(key=lambda o: (o.observed_at, json.dumps(o.model_dump(mode="json"), sort_keys=True)), reverse=True)
    trimmed = len(observations) > MAX_OBSERVATIONS or len(paths) > 12
    observations = observations[:MAX_OBSERVATIONS]
    paths = tuple(sorted(set(paths)))[:12]
    candidate = {**base.model_dump(), "observations": observations, "relevant_file_paths": paths}
    def encoded_size():
        value = {**candidate, "observations": [o.model_dump(mode="json") for o in observations]}
        # Base timestamps must use the same JSON representation as bounded().
        value.update(created_at=base.model_dump(mode="json")["created_at"],
                     last_activity_at=base.model_dump(mode="json")["last_activity_at"])
        return len(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode())
    candidate["context_trimmed"] = base.context_trimmed or trimmed
    while encoded_size() > MAX_TASK_CONTEXT_BYTES and (paths or observations):
        # Prefer preserving recent observations over optional path hints.
        if paths:
            paths = paths[:-1]
            candidate["relevant_file_paths"] = paths
        else:
            observations.pop()
        candidate["context_trimmed"] = True
    candidate["observations"] = tuple(observations)
    return TaskContext.model_validate(candidate)


def select_relevant_context(context, *, topic, scope, now):
    """Return hints/historical observations, never current evidence. No refresh."""
    if context_expired(context, now=now) or context.topic != topic or context.scope != scope:
        return None
    return TaskContext.model_validate({**context.model_dump(), "observations": tuple(
        Observation.model_validate({**o.model_dump(), "freshness": observation_freshness(o, now=now)})
        for o in context.observations)})


def clear_task_context(context, *, new_task_id, original_objective, now):
    """New identity/objective, no observations or carried collection authority."""
    if new_task_id == context.task_id:
        raise ValueError("Reset requires a new task ID")
    return create_task_context(task_id=new_task_id, original_objective=original_objective,
                               topic=context.topic, scope=ContextScope(project=context.scope.project,
                               organization=context.scope.organization), now=now)
