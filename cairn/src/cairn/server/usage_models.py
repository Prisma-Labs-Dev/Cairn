from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "cache_write_input_tokens", "output_tokens")
CORE_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens")


class UsageReading(BaseModel):
    model_config = {"extra": "forbid"}
    model: str | None = Field(default=None, max_length=200)
    model_source: Literal["reported", "configured", "unknown"]
    input_tokens: int | None = Field(default=None, ge=0, le=9_000_000_000_000, strict=True)
    cached_input_tokens: int | None = Field(default=None, ge=0, le=9_000_000_000_000, strict=True)
    uncached_input_tokens: int | None = Field(default=None, ge=0, le=9_000_000_000_000, strict=True)
    cache_write_input_tokens: int | None = Field(default=None, ge=0, le=9_000_000_000_000, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=9_000_000_000_000, strict=True)

    @model_validator(mode="after")
    def validate_totals(self):
        if (self.model is None) != (self.model_source == "unknown"):
            raise ValueError("unknown model provenance must use a null model")
        if all(value is not None for value in (self.input_tokens, self.cached_input_tokens, self.uncached_input_tokens)):
            if self.input_tokens != self.cached_input_tokens + self.uncached_input_tokens:
                raise ValueError("input tokens must equal cached plus uncached input")
        if self.cache_write_input_tokens is not None and self.uncached_input_tokens is not None:
            if self.cache_write_input_tokens > self.uncached_input_tokens:
                raise ValueError("cache writes are part of uncached input")
        return self


class UsageAttempt(BaseModel):
    model_config = {"extra": "forbid"}
    attempt_id: UUID
    project_id: str = Field(min_length=1, max_length=100)
    worker_name: str = Field(min_length=1, max_length=200)
    worker_type: Literal["codex", "claudecode", "pi", "mock"]
    reasoning_effort: str | None = Field(default=None, max_length=32)
    phase: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    status: Literal["success", "failed", "cancelled", "timed_out", "start_failed", "process_error"]
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0, strict=True)
    readings: list[UsageReading] = Field(min_length=1, max_length=128)
    notes: list[str] = Field(default_factory=list, max_length=20)
    coverage: Literal["reported", "partial", "unreported"] = "unreported"

    @field_validator("started_at", "finished_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps require a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("notes")
    @classmethod
    def safe_note_codes(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 80 or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in value) for value in values):
            raise ValueError("usage notes must be short codes, never raw worker output")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def derive_coverage(self):
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        known = any(getattr(reading, field) is not None for reading in self.readings for field in TOKEN_FIELDS)
        complete = all(getattr(reading, field) is not None for reading in self.readings for field in CORE_TOKEN_FIELDS)
        incomplete_notes = {"incomplete_process", "claude_main_loop_only", "claude_resume_scope_unknown",
            "claude_resume_baseline_missing", "claude_counter_reset", "pi_error_zero_usage",
            "partial_cli_stream", "invalid_usage_fields", "model_limit", "claude_subagents_unavailable",
            "claude_session_resets_unaccounted"}
        self.coverage = "unreported" if not known else "reported" if complete and not incomplete_notes.intersection(self.notes) else "partial"
        return self


class UsageReporter(BaseModel):
    model_config = {"extra": "forbid"}
    reporter_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    pending_attempts: int = Field(ge=0, strict=True)
    running_attempts: int = Field(ge=0, strict=True)
    rejected_attempts: int = Field(ge=0, strict=True)
