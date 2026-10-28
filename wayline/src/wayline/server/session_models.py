"""Bounded transport for Wayline-owned Pi conversations and their invocations."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Annotated
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

CHUNK_BYTES = 256 * 1024
SESSION_BYTES = 16 * 1024 * 1024
EVENT_BYTES = 4 * 1024 * 1024
SessionStatus = Literal['running', 'success', 'failed', 'cancelled', 'timed_out', 'start_failed', 'process_error', 'interrupted']
GraphId = Annotated[str, Field(min_length=1, max_length=100)]
CaptureStatus = Literal['waiting', 'capturing', 'complete', 'truncated', 'unavailable']


class SessionInvocation(BaseModel):
    model_config = {'extra': 'forbid'}
    id: UUID
    conversation_id: UUID
    project_id: str = Field(min_length=1, max_length=100)
    worker_name: str = Field(min_length=1, max_length=200)
    phase: str = Field(pattern=r'^[a-z0-9_]{1,64}$')
    intent_id: str | None = Field(default=None, max_length=100)
    source_fact_ids: list[GraphId] = Field(default_factory=list, max_length=1000)
    started_at: datetime
    heartbeat_at: datetime | None = None
    dispatcher_instance_id: UUID | None = None
    finished_at: datetime | None = None
    status: SessionStatus = 'running'
    capture_status: CaptureStatus = 'waiting'
    source_error: str | None = Field(default=None, max_length=500)
    delivery_status: Literal['pending', 'delivered'] = 'pending'
    delivery_session_bytes: int = Field(default=0, ge=0, le=SESSION_BYTES, strict=True)
    delivery_event_bytes: int = Field(default=0, ge=0, le=EVENT_BYTES, strict=True)
    events_truncated: bool = False
    cancel_reason: str | None = Field(default=None, max_length=200)

    @field_validator('started_at', 'finished_at', 'heartbeat_at')
    @classmethod
    def utc(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('session timestamps require a timezone')
        return value.astimezone(timezone.utc) if value is not None else None


class SessionChunk(BaseModel):
    model_config = {'extra': 'forbid'}
    offset: int = Field(ge=0, le=SESSION_BYTES, strict=True)
    data: str = Field(max_length=((CHUNK_BYTES + 2) // 3) * 4)


class SessionResult(BaseModel):
    model_config = {'extra': 'forbid'}
    project_id: str = Field(min_length=1, max_length=100)
    result_fact_ids: list[GraphId] = Field(default_factory=list, max_length=1000)
    result_intent_ids: list[GraphId] = Field(default_factory=list, max_length=1000)
