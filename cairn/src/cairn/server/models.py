from __future__ import annotations

from typing import Literal
import unicodedata

from pydantic import BaseModel, Field, field_validator, model_validator

from cairn.server.artifact_models import ArtifactRecord, ArtifactRequirement


def _artifact_collision_key(path: str) -> str:
    # Shared output paths must remain distinct on macOS filesystems as well as
    # case-sensitive Linux volumes. Preserve the original spelling for delivery.
    return unicodedata.normalize("NFC", path).casefold()


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
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    required_artifacts: list[ArtifactRequirement] = Field(default_factory=list)


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
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


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
    model_config = {"extra": "forbid"}

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    title: str
    goal: str = ""
    kind: Literal["research", "work", "synthesis", "review"] = "work"
    layer: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    bootstrap_enabled: bool = True
    artifacts: list[ArtifactRequirement] = Field(default_factory=list, max_length=32)

    @field_validator("title", "layer")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def validate_goal_and_outputs(self):
        self.goal = self.goal.strip()
        if not self.goal:
            if not self.artifacts:
                raise ValueError("a stage needs a goal or at least one required artifact")
            self.goal = "Produce the required outputs: " + ", ".join(
                item.label or item.path for item in self.artifacts
            ) + "."
        paths = [_artifact_collision_key(item.path) for item in self.artifacts]
        if len(set(paths)) != len(paths):
            raise ValueError("artifact paths must be unique within a stage")
        return self


class CreateWorkflowRequest(BaseModel):
    model_config = {"extra": "forbid"}

    title: str
    origin: str
    stages: list[WorkflowStageDefinition] = Field(min_length=1, max_length=128)

    @field_validator("title", "origin")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def validate_dependencies(self):
        ids = {stage.id for stage in self.stages}
        if len(ids) != len(self.stages):
            raise ValueError("stage ids must be unique")
        artifact_paths = [_artifact_collision_key(artifact.path) for stage in self.stages for artifact in stage.artifacts]
        if len(set(artifact_paths)) != len(artifact_paths):
            raise ValueError("artifact paths must be unique across workflow stages")
        for stage in self.stages:
            if len(set(stage.depends_on)) != len(stage.depends_on):
                raise ValueError(f"duplicate dependencies for {stage.id}")
            if not set(stage.depends_on) <= ids:
                raise ValueError(f"unknown dependency for {stage.id}")
        remaining = {stage.id: set(stage.depends_on) for stage in self.stages}
        resolved: set[str] = set()
        while remaining:
            ready = {key for key, dependencies in remaining.items() if dependencies <= resolved}
            if not ready:
                raise ValueError("stage dependencies must not contain a cycle")
            resolved.update(ready)
            remaining = {key: dependencies for key, dependencies in remaining.items() if key not in ready}
        return self


class WorkflowMeta(BaseModel):
    id: str
    title: str
    origin: str
    status: Literal["active", "stopped", "completed"]
    created_at: str


class WorkflowSummary(WorkflowMeta):
    stage_count: int
    completed_stage_count: int
    active_stage_count: int


class StageCompletion(BaseModel):
    project_id: str
    description: str
    from_: list[str] = Field(alias="from")
    completed_at: str

    model_config = {"populate_by_name": True}


class StageHandoff(BaseModel):
    stage_id: str
    project_id: str
    completion_description: str
    from_: list[str] = Field(alias="from")
    facts: list[Fact]
    artifacts: list[ArtifactRecord] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class WorkflowStage(WorkflowStageDefinition):
    status: Literal["blocked", "active", "stopped", "completed"]
    project_id: str | None
    completion: StageCompletion | None
    handoffs: list[StageHandoff]
    collected_artifacts: list[ArtifactRecord] = Field(default_factory=list)
    artifact_recovery_attempts: int = Field(default=0, ge=0)
    artifact_error: str | None = None


class WorkflowDetail(BaseModel):
    workflow: WorkflowMeta
    stages: list[WorkflowStage]
