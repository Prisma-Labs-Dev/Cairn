from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

RUNTIME_FIELDS = ("max_workers", "max_running_projects", "max_project_workers")
RuntimePositive = Annotated[int, Field(strict=True, gt=0)]
RuntimeOverride = Annotated[int, Field(strict=True, ge=1, le=256)]
RuntimeCount = Annotated[int, Field(strict=True, ge=0)]


class RuntimeLimits(BaseModel):
    model_config = {"extra": "forbid"}

    max_workers: RuntimePositive
    max_running_projects: RuntimePositive
    max_project_workers: RuntimePositive


class RuntimeOverrides(BaseModel):
    model_config = {"extra": "forbid"}

    max_workers: RuntimeOverride | None = None
    max_running_projects: RuntimeOverride | None = None
    max_project_workers: RuntimeOverride | None = None


class RuntimeOverrideUpdate(RuntimeOverrides):
    @model_validator(mode="after")
    def require_update(self):
        if not self.model_fields_set:
            raise ValueError("provide at least one runtime limit; use null to restore its YAML value")
        return self


class RuntimeSettingsState(BaseModel):
    settings: RuntimeOverrides
    settings_revision: RuntimeCount


class RuntimeCounts(BaseModel):
    model_config = {"extra": "forbid"}

    running_tasks: RuntimeCount
    running_projects: RuntimeCount
    admitted_projects: RuntimeCount


class RuntimeWorker(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=256)
    type: Literal["claudecode", "codex", "pi", "mock"]
    task_types: list[Literal["bootstrap", "reason", "explore"]] = Field(min_length=1, max_length=3)
    max_running: RuntimePositive
    running: RuntimeCount
    cooldown: bool


class DispatcherReport(BaseModel):
    model_config = {"extra": "forbid"}

    configured: RuntimeLimits
    effective: RuntimeLimits
    applied_settings_revision: RuntimeCount
    stale_after_seconds: Annotated[int, Field(strict=True, ge=30)]
    counts: RuntimeCounts
    workers: list[RuntimeWorker] = Field(max_length=512)


class RuntimeDispatcher(DispatcherReport):
    id: str
    last_seen: str
    online: bool
    pending: bool
    desired: RuntimeLimits
    worker_capacity: RuntimeCount
    effective_worker_capacity: RuntimeCount
    available_worker_slots: RuntimeCount
    capacity_by_task_type: dict[str, int]


class RuntimeOverview(RuntimeSettingsState):
    scope: Literal["per_dispatcher"] = "per_dispatcher"
    dispatchers: list[RuntimeDispatcher]
