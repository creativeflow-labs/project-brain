from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    ConversationMessage,
    ConversationThread,
    SearchTrace,
    WorkStateSnapshot,
    utcnow,
)
from app.schemas import AskResponse, Citation, CurrentStateOut
from app.services.search_core.state_engine import current_state_to_dict
from app.services.search_core.types import EvidencePacket
from app.services.search_core.workflow import search_graph

settings = get_settings()


def _load_thread(
    db: Session, project_id: str, thread_id: str | None
) -> ConversationThread:
    if thread_id:
        thread = db.get(ConversationThread, thread_id)
        if not thread or thread.project_id != project_id:
            raise ValueError("이 프로젝트에서 사용할 수 없는 대화 ID입니다.")
        return thread
    thread = ConversationThread(project_id=project_id)
    db.add(thread)
    db.flush()
    return thread


def _load_history(db: Session, thread_id: str) -> list[ConversationMessage]:
    messages = db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.thread_id == thread_id)
        .order_by(ConversationMessage.created_at.desc())
        .limit(settings.search_history_messages)
    ).all()
    return list(reversed(messages))


def _answer_and_citations(
    answer: str,
    packets: list[EvidencePacket],
    cited_document_ids: list[str],
    top_k: int,
) -> tuple[str, list[Citation]]:
    referenced_positions = {
        int(value)
        for value in re.findall(r"\[(\d+)]", answer)
        if 1 <= int(value) <= len(packets)
    }
    cited_ids = set(cited_document_ids)
    selected_with_positions = [
        (position, packet)
        for position, packet in enumerate(packets, start=1)
        if position in referenced_positions or packet.document.id in cited_ids
    ][:top_k]
    if not selected_with_positions and packets:
        selected_with_positions = [(1, packets[0])]

    position_mapping = {
        old_position: new_position
        for new_position, (old_position, _) in enumerate(
            selected_with_positions, start=1
        )
    }

    def replace_reference(match: re.Match[str]) -> str:
        old_position = int(match.group(1))
        new_position = position_mapping.get(old_position)
        return f"[{new_position}]" if new_position else ""

    rewritten_answer = re.sub(r"\[(\d+)]", replace_reference, answer)
    citations = [
        Citation(
            document_id=packet.document.id,
            title=packet.document.title,
            source_type=packet.document.source_type,
            locator=" · ".join(
                dict.fromkeys(
                    chunk.locator or "본문" for chunk in packet.anchor_chunks[:3]
                )
            ),
            source_url=packet.document.source_url,
            excerpt=packet.context_text[:1200],
            score=round(packet.score, 4),
        )
        for _, packet in selected_with_positions
    ]
    return rewritten_answer, citations


def answer_question(
    db: Session,
    project_id: str,
    question: str,
    top_k: int,
    thread_id: str | None = None,
) -> AskResponse:
    thread = _load_thread(db, project_id, thread_id)
    history = _load_history(db, thread.id)
    result = search_graph.invoke(
        {
            "db": db,
            "project_id": project_id,
            "question": question,
            "history": history,
            "top_k": top_k,
            "retry_count": 0,
        }
    )
    plan = result["plan"]
    packets = result.get("packets", [])
    assessment = result["assessment"]
    current_state = result.get("current_state")
    current_state_payload = (
        current_state_to_dict(current_state) if current_state is not None else None
    )
    answer, citations = _answer_and_citations(
        assessment.answer, packets, assessment.cited_document_ids, top_k
    )

    if not packets:
        mode = "no_evidence"
    elif settings.openai_api_key:
        mode = "generative"
    else:
        mode = "extractive"

    trace = SearchTrace(
        thread_id=thread.id,
        project_id=project_id,
        status=assessment.status,
        trace_json={
            "search_core_version": "0.4.0-alpha.1",
            "search_queries": plan.search_queries,
            "entity_terms": plan.entity_terms,
            "requested_field": plan.requested_field,
            "candidate_count": result.get("candidate_count", 0),
            "evidence_document_ids": [
                packet.document.id for packet in packets
            ],
            "cited_document_ids": assessment.cited_document_ids,
            "retry_count": result.get("retry_count", 0),
            "current_state": current_state_payload,
        },
    )
    db.add(trace)
    db.flush()
    db.add(
        ConversationMessage(
            thread_id=thread.id,
            role="user",
            content=question.strip(),
            resolved_question=plan.resolved_question,
        )
    )
    db.add(
        ConversationMessage(
            thread_id=thread.id,
            role="assistant",
            content=answer,
            evidence_json={
                "status": assessment.status,
                "trace_id": trace.id,
                "document_ids": assessment.cited_document_ids,
                "conflicts": assessment.conflicts,
                "current_state": current_state_payload,
            },
        )
    )
    if current_state is not None:
        db.add(
            WorkStateSnapshot(
                project_id=project_id,
                thread_id=thread.id,
                subject_text=plan.resolved_question,
                state=current_state.state,
                as_of=current_state.as_of,
                assessment_json=current_state_payload,
            )
        )
    thread.updated_at = utcnow()
    db.commit()

    return AskResponse(
        answer=answer,
        mode=mode,
        citations=citations,
        thread_id=thread.id,
        evidence_status=assessment.status,
        resolved_question=plan.resolved_question,
        trace_id=trace.id,
        conflicts=assessment.conflicts,
        current_state=(
            CurrentStateOut.model_validate(current_state_payload)
            if current_state_payload is not None
            else None
        ),
    )
