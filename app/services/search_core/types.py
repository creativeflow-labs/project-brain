from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypedDict

from sqlalchemy.orm import Session

from app.models import Chunk, ConversationMessage, Document

EvidenceStatus = Literal["supported", "conflict", "insufficient"]
AnswerType = Literal["direct", "conflict", "insufficient", "state"]
TaskMode = Literal["answer", "compose"]
ScopeMode = Literal["open", "entity", "inherited", "collection"]
PresentationMode = Literal["adaptive", "structured", "artifact"]
SearchCardinality = Literal["one", "many", "all"]
RetrievalMode = Literal["hybrid", "scoped"]
FactStatus = Literal[
    "confirmed",
    "conflicting",
    "proposed",
    "recorded",
    "unverified",
    "outdated",
]
WorkState = Literal[
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


@dataclass
class QueryPlan:
    original_question: str
    resolved_question: str
    search_queries: list[str]
    entity_terms: list[str] = field(default_factory=list)
    requested_field: str = "general"
    requested_fields: list[str] = field(default_factory=list)
    cardinality: SearchCardinality = "one"
    time_start: datetime | None = None
    time_end: datetime | None = None
    time_field: str | None = None
    is_state_query: bool = False
    retrieval_mode: RetrievalMode = "hybrid"
    record_type: str | None = None
    task_mode: TaskMode = "answer"
    scope_mode: ScopeMode = "open"
    locked_entity_terms: list[str] = field(default_factory=list)


@dataclass
class SearchCandidate:
    chunk: Chunk
    document: Document
    score: float
    vector_score: float = 0.0
    lexical_score: float = 0.0
    exact_score: float = 0.0


@dataclass
class EvidencePacket:
    document: Document
    anchor_chunks: list[Chunk]
    context_text: str
    score: float
    matched_queries: list[str] = field(default_factory=list)


@dataclass
class TimelineEvent:
    state: WorkState
    label: str
    occurred_at: datetime | None
    source_type: str
    document_id: str
    title: str
    excerpt: str
    actor: str | None = None
    actor_role: str | None = None
    time_basis: Literal["event", "record", "unknown"] = "unknown"
    authority: int = 1
    decisive: bool = False


@dataclass
class CurrentStateAssessment:
    state: WorkState
    label: str
    evidence_status: EvidenceStatus
    as_of: datetime | None
    reason: str
    next_action: str | None
    responsible: str | None = None
    approver: str | None = None
    decisive_document_ids: list[str] = field(default_factory=list)
    stale_document_ids: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)


@dataclass
class AnswerFact:
    role: str
    value: str
    fact_status: FactStatus
    document_ids: list[str] = field(default_factory=list)


@dataclass
class AnswerReport:
    answer_type: AnswerType
    status: EvidenceStatus
    headline: str
    body: str | None = None
    facts: list[AnswerFact] = field(default_factory=list)
    detail: str | None = None
    next_action: str | None = None
    presentation: PresentationMode = "adaptive"
    warning: str | None = None


@dataclass
class EvidenceAssessment:
    status: EvidenceStatus
    answer: str
    cited_document_ids: list[str]
    conflicts: list[str] = field(default_factory=list)
    retry_queries: list[str] = field(default_factory=list)
    report: AnswerReport | None = None


class SearchState(TypedDict, total=False):
    db: Session
    project_id: str
    question: str
    history: list[ConversationMessage]
    top_k: int
    plan: QueryPlan
    packets: list[EvidencePacket]
    current_state: CurrentStateAssessment | None
    assessment: EvidenceAssessment
    retry_count: int
    candidate_count: int
