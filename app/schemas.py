from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentOut(BaseModel):
    id: str
    source_type: str
    title: str
    filename: str | None
    mime_type: str
    source_url: str | None
    warnings_json: list
    created_at: datetime

    model_config = {"from_attributes": True}


class AskRequest(BaseModel):
    project_id: str
    question: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=12)
    thread_id: str | None = None


class Citation(BaseModel):
    document_id: str
    title: str
    source_type: str
    locator: str | None
    source_url: str | None
    excerpt: str
    score: float


WorkStateCode = Literal[
    "unknown",
    "planned",
    "requested",
    "in_progress",
    "waiting_external",
    "waiting_approval",
    "revision_requested",
    "completion_claimed",
    "completed",
    "on_hold",
    "cancelled",
]


class TimelineEventOut(BaseModel):
    state: WorkStateCode
    label: str
    occurred_at: datetime | None
    source_type: str
    document_id: str
    title: str
    excerpt: str
    actor: str | None = None
    actor_role: str | None = None
    time_basis: Literal["event", "record", "unknown"]
    decisive: bool = False


class CurrentStateOut(BaseModel):
    state: WorkStateCode
    label: str
    evidence_status: Literal["supported", "conflict", "insufficient"]
    as_of: datetime | None
    reason: str
    next_action: str | None
    responsible: str | None = None
    approver: str | None = None
    decisive_document_ids: list[str] = Field(default_factory=list)
    stale_document_ids: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    timeline: list[TimelineEventOut] = Field(default_factory=list)


class AnswerFactOut(BaseModel):
    role: str
    value: str
    fact_status: Literal[
        "confirmed",
        "conflicting",
        "proposed",
        "recorded",
        "unverified",
        "outdated",
    ]
    citation_document_ids: list[str] = Field(default_factory=list)


class AnswerViewOut(BaseModel):
    type: Literal["direct", "conflict", "insufficient", "state"]
    status: Literal["supported", "conflict", "insufficient"]
    headline: str
    body: str | None = None
    facts: list[AnswerFactOut] = Field(default_factory=list)
    detail: str | None = None
    next_action: str | None = None


class AskResponse(BaseModel):
    answer: str
    mode: str
    citations: list[Citation]
    thread_id: str
    evidence_status: Literal["supported", "conflict", "insufficient"]
    resolved_question: str
    trace_id: str
    conflicts: list[str] = Field(default_factory=list)
    current_state: CurrentStateOut | None = None
    answer_view: AnswerViewOut


class NotionImportRequest(BaseModel):
    project_id: str
    integration_token: str = Field(min_length=10)
    page_ids: list[str] = Field(min_length=1, max_length=20)


class NotionConnectRequest(BaseModel):
    project_id: str
    access_token: str = Field(min_length=10)
    auth_mode: Literal["personal_access_token", "internal_access_token"] = (
        "personal_access_token"
    )
    auto_sync_enabled: bool = True
    sync_interval_minutes: int = Field(default=60, ge=15, le=1440)


class NotionSyncRequest(BaseModel):
    project_id: str
    force: bool = False


class GmailSyncRequest(BaseModel):
    project_id: str
    query: str = Field(default="newer_than:30d", max_length=500)
    max_results: int = Field(default=30, ge=1, le=100)
