from __future__ import annotations

from typing import Literal
import unicodedata

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from cairn.server.artifact_models import ArtifactRecord, ArtifactRequirement
from cairn.server.workflow_conditions import (ResultField, StageDependency, CollectionDependency, CollectionEmission, validate_contract, validate_predicate, dependency_stage)


def _artifact_collision_key(path: str) -> str:
    # Shared output paths must remain distinct on macOS filesystems as well as
    # case-sensitive Linux volumes. Preserve the original spelling for delivery.
    return unicodedata.normalize("NFC", path).casefold()


DERIVED_TITLE_MAX_LENGTH = 96


def derive_title(source: str) -> str:
    first_line = source.strip().splitlines()[0]
    title = " ".join(first_line.split())
    if len(title) <= DERIVED_TITLE_MAX_LENGTH:
        return title
    return title[:DERIVED_TITLE_MAX_LENGTH - 1].rstrip() + "…"


def normalize_optional_text(value: str | None) -> str:
    return value.strip() if value is not None else ""


class GoalExpansion(BaseModel):
    model_config = {"extra": "forbid"}

    max_goals: int = Field(default=16, ge=1, le=64, strict=True)
    mode: Literal["all", "one"] = "all"


class GeneratedGoalDefinition(BaseModel):
    """Worker proposals describe independent goals, never executable workflows."""
    model_config = {"extra": "forbid"}

    goal: str = Field(min_length=1, max_length=8_000, pattern=r"\S")
    id: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    title: str | None = Field(default=None, max_length=200)
    kind: Literal["research", "work", "synthesis", "review"] = "work"
    bootstrap_enabled: bool = Field(default=True, strict=True)
    artifacts: list[ArtifactRequirement] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def normalize_goal(self):
        self.goal = self.goal.strip()
        self.title = normalize_optional_text(self.title) or derive_title(self.goal)
        paths = [_artifact_collision_key(artifact.path) for artifact in self.artifacts]
        if len(set(paths)) != len(paths):
            raise ValueError("generated goal artifact paths must be unique")
        return self


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


class Fact(BaseModel):
    id: str
    description: str


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    verification_enabled: bool = False
    pending_review_id: str | None = None
    created_at: str
    reason: ProjectReason | None = None
    workflow_id: str | None = None
    workflow_stage_id: str | None = None

    @property
    def runtime_id(self) -> str:
        return self.workflow_id or self.id


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    workflow_deleted_at: str | None = None
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    required_artifacts: list[ArtifactRequirement] = Field(default_factory=list)
    expansion: GoalExpansion | None = None
    choice_option: bool = False
    result_contract: dict[str, ResultField] = Field(default_factory=dict)


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = None
    origin: str | None = None
    goal: str = Field(min_length=1, pattern=r"\S")
    bootstrap_enabled: bool = True
    verification_enabled: bool = Field(default=False, strict=True)
    hints: list[CreateHintInline] | None = None

    @field_validator("goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @model_validator(mode="after")
    def normalize_metadata(self):
        self.title = normalize_optional_text(self.title) or derive_title(self.goal)
        self.origin = normalize_optional_text(self.origin)
        return self


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str
    producer_agent_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    producer_session_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    goals: list[GeneratedGoalDefinition] | None = Field(default=None, max_length=64)
    selected_goal: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"\S")
    selection_reason: str | None = Field(default=None, min_length=1, max_length=8_000, pattern=r"\S")
    result: dict | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateMetadataRequest(BaseModel):
    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"anyOf": [{"required": ["title"]}, {"required": ["origin"]}]},
    }

    title: str | SkipJsonSchema[None] = Field(default_factory=lambda: None, min_length=1, pattern=r"\S")
    origin: str | SkipJsonSchema[None] = Field(default_factory=lambda: None, min_length=1, pattern=r"\S")

    @field_validator("title", "origin", mode="before")
    @classmethod
    def validate_nonempty_metadata(cls, value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("metadata updates must provide nonempty strings")
        return value.strip()

    @model_validator(mode="after")
    def require_metadata_field(self):
        if not self.model_fields_set:
            raise ValueError("provide at least one of title or origin")
        return self


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


class WorkflowStageDefinition(BaseModel):
    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"anyOf": [
            {"required": ["goal"], "properties": {"goal": {"type": "string", "pattern": r"\S"}}},
            {"required": ["artifacts"], "properties": {"artifacts": {"type": "array", "minItems": 1}}},
        ]},
    }

    id: str = Field(default_factory=lambda: "stage_1", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    title: str | None = None
    goal: str = ""
    kind: Literal["research", "work", "synthesis", "review"] = "work"
    layer: str | None = None
    depends_on: list[str | StageDependency | CollectionDependency] = Field(default_factory=list, max_length=128)
    result: dict[str, ResultField] = Field(default_factory=dict)
    emits: CollectionEmission | None = None
    bootstrap_enabled: bool = True
    artifacts: list[ArtifactRequirement] = Field(default_factory=list, max_length=32)
    expansion: GoalExpansion | None = None
    verification_enabled: bool = Field(default=False, strict=True)

    @field_validator("layer")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def validate_goal_and_outputs(self):
        validate_contract(self.result)
        if self.emits is not None and (self.expansion is None or self.expansion.mode != "all"):
            raise ValueError("emits requires expansion mode all")
        self.goal = self.goal.strip()
        title_source = self.goal
        if not self.goal:
            if not self.artifacts:
                raise ValueError("a stage needs a goal or at least one required artifact")
            self.goal = "Produce the required outputs: " + ", ".join(
                item.label or item.path for item in self.artifacts
            ) + "."
            title_source = self.artifacts[0].label or self.artifacts[0].path
        self.title = normalize_optional_text(self.title) or derive_title(title_source)
        paths = [_artifact_collision_key(item.path) for item in self.artifacts]
        if len(set(paths)) != len(paths):
            raise ValueError("artifact paths must be unique within a stage")
        return self


class CreateWorkflowRequest(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = None
    origin: str | None = None
    stages: list[WorkflowStageDefinition] = Field(min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def assign_missing_stage_ids(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("stages"), list):
            return value
        stages = []
        for index, item in enumerate(value["stages"], 1):
            if isinstance(item, dict) and "id" not in item:
                item = {**item, "id": f"stage_{index}"}
            elif isinstance(item, WorkflowStageDefinition) and "id" not in item.model_fields_set:
                item = item.model_copy(update={"id": f"stage_{index}"})
            stages.append(item)
        return {**value, "stages": stages}

    @model_validator(mode="after")
    def validate_dependencies(self):
        ids = {stage.id for stage in self.stages}
        if len(ids) != len(self.stages):
            duplicates = sorted(stage_id for stage_id in ids if sum(stage.id == stage_id for stage in self.stages) > 1)
            raise ValueError(f"duplicate stage IDs: {', '.join(duplicates)}; provide distinct explicit IDs when position-generated IDs collide")
        artifact_paths = [_artifact_collision_key(artifact.path) for stage in self.stages for artifact in stage.artifacts]
        if len(set(artifact_paths)) != len(artifact_paths):
            raise ValueError("artifact paths must be unique across workflow stages")
        definitions = {stage.id: stage for stage in self.stages}
        emissions = [stage.emits.collection for stage in self.stages if stage.emits]
        if len(set(emissions)) != len(emissions):
            raise ValueError("collection names must be unique")
        collections = {stage.emits.collection: stage.id for stage in self.stages if stage.emits}
        remaining = {}
        for stage in self.stages:
            targets = []
            for dependency in stage.depends_on:
                if isinstance(dependency, CollectionDependency) and dependency.collection not in collections:
                    raise ValueError(f"unknown collection for {stage.id}")
                target = dependency_stage(dependency, collections)
                if target not in ids:
                    raise ValueError(f"unknown dependency for {stage.id}")
                if target in targets:
                    raise ValueError(f"duplicate dependencies for {stage.id}")
                targets.append(target)
                if isinstance(dependency, StageDependency) and dependency.when:
                    validate_predicate(dependency, definitions)
            remaining[stage.id] = set(targets)
        resolved: set[str] = set()
        while remaining:
            ready = {key for key, dependencies in remaining.items() if dependencies <= resolved}
            if not ready:
                raise ValueError("stage dependencies must not contain a cycle")
            resolved.update(ready)
            remaining = {key: dependencies for key, dependencies in remaining.items() if key not in ready}
        self.title = normalize_optional_text(self.title) or derive_title(self.stages[0].title)
        self.origin = normalize_optional_text(self.origin)
        return self


class WorkflowMeta(BaseModel):
    id: str
    title: str
    origin: str
    status: Literal["active", "stopped", "completed"]
    created_at: str
    deleted_at: str | None = None


class WorkflowSummary(WorkflowMeta):
    stage_count: int
    completed_stage_count: int
    active_stage_count: int
    not_applicable_stage_count: int = 0


class StageCompletion(BaseModel):
    project_id: str
    description: str
    from_: list[str] = Field(alias="from")
    completed_at: str
    goals: list[GeneratedGoalDefinition] | None = None
    selected_goal: str | None = None
    selection_reason: str | None = None
    result: dict | None = None

    model_config = {"populate_by_name": True}


class StageHandoff(BaseModel):
    stage_id: str
    project_id: str
    fact_count: int
    intent_count: int
    artifact_count: int
    context_url: str


class WorkflowStage(WorkflowStageDefinition):
    pending_review_id: str | None = None
    status: Literal["blocked", "active", "stopped", "completed", "not_applicable"]
    not_applicable_reason: str | None = None
    resolved_at: str | None = None
    project_id: str | None
    completion: StageCompletion | None
    handoffs: list[StageHandoff]
    collected_artifacts: list[ArtifactRecord] = Field(default_factory=list)
    artifact_recovery_attempts: int = Field(default=0, ge=0)
    artifact_error: str | None = None
    history_index_url: str | None = None
    generated_by_stage_id: str | None = None
    expansion_recovery_attempts: int = Field(default=0, ge=0)
    expansion_error: str | None = None
    result_recovery_attempts: int = 0
    result_error: str | None = None
    option_state: Literal["pending", "selected", "succeeded", "unavailable", "skipped"] | None = None
    option_reason: str | None = None
    option_history: list[dict] = Field(default_factory=list)
    choice_error: str | None = None
    choice_satisfied: bool | None = None


class UnavailableRequest(BaseModel):
    model_config = {"extra": "forbid", "populate_by_name": True}
    from_: list[str] = Field(alias="from", min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=8_000, pattern=r"\S")
    worker: str = Field(min_length=1, max_length=200, pattern=r"\S")

    @field_validator("from_")
    @classmethod
    def validate_sources(cls, value):
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("Evidence references must be nonblank and unique")
        return value


class RetryChoiceRequest(BaseModel):
    model_config = {"extra": "forbid"}
    goal_id: str = Field(min_length=1, max_length=64, pattern=r"\S")
    reason: str = Field(min_length=1, max_length=8_000, pattern=r"\S")


class WorkflowDetail(BaseModel):
    workflow: WorkflowMeta
    stages: list[WorkflowStage]
    collections: list[dict] = Field(default_factory=list)
