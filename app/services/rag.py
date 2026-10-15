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
from app.schemas import AnswerViewOut, AskResponse, Citation, CurrentStateOut
from app.version import APP_VERSION
from app.services.search_core.reporting import (
    answer_report_to_dict,
    render_answer_text,
    report_document_ids,
    validate_answer_report,
)
from app.services.search_core.conversation import assistant_identity_answer
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
    packet_positions = {
        packet.document.id: (position, packet)
        for position, packet in enumerate(packets, start=1)
    }
    selected_with_positions: list[tuple[int, EvidencePacket]] = []
    selected_ids: set[str] = set()
    for document_id in cited_document_ids:
        item = packet_positions.get(document_id)
        if item and document_id not in selected_ids:
            selected_with_positions.append(item)
            selected_ids.add(document_id)
    for position in sorted(referenced_positions):
        packet = packets[position - 1]
        if packet.document.id not in selected_ids:
            selected_with_positions.append((position, packet))
            selected_ids.add(packet.document.id)
    selected_with_positions = selected_with_positions[:top_k]
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
            document_version=packet.document.content_hash,
            document_updated_at=packet.document.updated_at,
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
    answer_report = validate_answer_report(plan, assessment, packets, current_state)
    report_ids = report_document_ids(answer_report)
    display_document_ids = list(
        dict.fromkeys([*report_ids, *assessment.cited_document_ids])
    )
    packet_positions = {
        packet.document.id: position
        for position, packet in enumerate(packets, start=1)
    }
    answer_report_payload = answer_report_to_dict(answer_report)
    answer, citations = _answer_and_citations(
        render_answer_text(answer_report, packet_positions),
        packets,
        display_document_ids,
        (
            settings.search_scoped_record_limit
            if plan.retrieval_mode == "scoped"
            else top_k
        ),
    )
    answer_report_payload["body"] = answer
    citation_snapshot = [citation.model_dump(mode="json") for citation in citations]

    if assistant_identity_answer(question):
        mode = "assistant_info"
    elif not packets:
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
            "search_core_version": APP_VERSION,
            "search_queries": plan.search_queries,
            "entity_terms": plan.entity_terms,
            "requested_field": plan.requested_field,
            "requested_fields": plan.requested_fields,
            "cardinality": plan.cardinality,
            "time_range": (
                {
                    "start": plan.time_start.isoformat(),
                    "end": plan.time_end.isoformat(),
                    "field": plan.time_field,
                }
                if plan.time_start is not None and plan.time_end is not None
                else None
            ),
            "is_state_query": plan.is_state_query,
            "task_mode": plan.task_mode,
            "scope_mode": plan.scope_mode,
            "locked_entity_terms": plan.locked_entity_terms,
            "retrieval_mode": plan.retrieval_mode,
            "record_type": plan.record_type,
            "candidate_count": result.get("candidate_count", 0),
            "evidence_document_ids": [
                packet.document.id for packet in packets
            ],
            "cited_document_ids": display_document_ids,
            "judgment_document_ids": assessment.cited_document_ids,
            "retry_count": result.get("retry_count", 0),
            "timings_ms": result.get("timings_ms", {}),
            "current_state": current_state_payload,
            "answer_view": answer_report_payload,
            "citation_snapshot": citation_snapshot,
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
    assistant_message = ConversationMessage(
        thread_id=thread.id,
        role="assistant",
        content=answer,
        evidence_json={
            "status": assessment.status,
            "trace_id": trace.id,
            "document_ids": display_document_ids,
            "citations": citation_snapshot,
            "conflicts": assessment.conflicts,
            "current_state": current_state_payload,
            "answer_view": answer_report_payload,
        },
    )
    db.add(assistant_message)
    db.flush()
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
        answer_id=assistant_message.id,
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
        answer_view=AnswerViewOut.model_validate(answer_report_payload),
    )
