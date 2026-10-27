from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class ReviewIdentity(BaseModel):
    model_config = {"extra": "forbid"}
    agent_id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    session_id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    worker: str = Field(min_length=1, max_length=200, pattern=r"\S")


class ReviewLeaseRequest(ReviewIdentity):
    lease_token: str = Field(min_length=1)


class ReviewDecisionRequest(ReviewLeaseRequest):
    cli_session_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    decision: Literal["approved", "rejected"]
    feedback: str = Field(min_length=1, max_length=16000, pattern=r"\S")
    evidence: list[str] = Field(default_factory=list, max_length=64)

    @field_validator('evidence')
    @classmethod
    def validate_evidence(cls, values):
        if any(not item.strip() or len(item) > 4000 for item in values):
            raise ValueError('Evidence items must contain nonblank text and at most 4000 characters')
        return [item.strip() for item in values]

    @model_validator(mode='after')
    def require_approval_evidence(self):
        if self.decision == 'approved' and not self.evidence:
            raise ValueError('Approval requires at least one observed evidence item')
        return self


class CompletionClaim(BaseModel):
    id: str
    project_id: str
    workflow_id: str | None
    project_title: str
    status: Literal["pending", "approved", "rejected"]
    created_at: str
    producer_agent_id: str
    producer_session_id: str
    producer_worker: str
    completion: dict
    snapshot: dict
    reviewer_agent_id: str | None = None
    reviewer_session_id: str | None = None
    reviewer_worker: str | None = None
    reviewer_cli_session_id: str | None = None
    lease_token: str | None = None
    last_heartbeat_at: str | None = None
    decided_at: str | None = None
    feedback: str | None = None
    evidence: list[str] = Field(default_factory=list)
    review_attempts: int = 0


class CompletionClaimSummary(BaseModel):
    id: str
    project_id: str
    workflow_id: str | None
    project_title: str
    status: Literal['pending', 'approved', 'rejected']
    created_at: str
    producer_agent_id: str
    producer_session_id: str
    producer_worker: str
    completion_description: str
    reviewer_agent_id: str | None = None
    reviewer_session_id: str | None = None
    reviewer_worker: str | None = None
    feedback: str | None = None
    review_attempts: int


class ReviewPage(BaseModel):
    items: list[CompletionClaimSummary]
    total: int
    offset: int
    limit: int
    next_offset: int | None
