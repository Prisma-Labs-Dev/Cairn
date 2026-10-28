from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_ARTIFACT_RECOVERIES = 3
ARTIFACT_RECOVERY_CREATOR = "artifact-collector"
ARTIFACT_RECOVERY_PREFIX = "Artifact recovery:"


def validate_artifact_path(value: str) -> str:
    if not value or value != value.strip() or len(value) > 512:
        raise ValueError("artifact path must be a non-empty relative file path")
    if "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("artifact path contains invalid characters")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("artifact path must not be absolute or contain empty, . or .. components")
    return value


class ArtifactRequirement(BaseModel):
    model_config = {"extra": "forbid"}

    path: str
    label: str | None = Field(default=None, max_length=200)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_artifact_path(value)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class ArtifactRecord(BaseModel):
    id: str
    workflow_id: str
    stage_id: str
    project_id: str
    path: str
    label: str | None
    size: int
    sha256: str
    created_at: str
    download_url: str


class ArtifactRecoveryRequest(BaseModel):
    worker: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10000)
