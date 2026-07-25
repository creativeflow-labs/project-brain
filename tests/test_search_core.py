from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Chunk, Document, Project
from app.services.ingestion import IngestionInput, ingest_document
from app.services.search_core.retrieval import build_evidence_packets
from app.services.search_core.types import QueryPlan, SearchCandidate


def test_gmail_thread_context_is_expanded_and_quoted_copy_is_removed():
    init_db()
    with SessionLocal() as db:
        project = Project(name="Gmail 스레드 복원")
        db.add(project)
        db.commit()
        db.refresh(project)
        original, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="gmail",
                external_id="thread-original",
                title="오로라 유료 협업 제안",
                text="저는 박다은 담당자입니다. 협업 금액을 제안해 주세요.",
                metadata={
                    "thread_id": "aurora-thread",
                    "from": "박다은 <daeun@example.com>",
                    "date": "2026-07-20",
                },
            ),
        )
        reply, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="gmail",
                external_id="thread-reply",
                title="내부 발신 답장",
                text=(
                    "저희 견적은 200만 원입니다.\n"
                    "On Mon, Jul 20, 2026, 박다은 wrote:\n"
                    "저는 박다은 담당자입니다. 협업 금액을 제안해 주세요."
                ),
                metadata={
                    "thread_id": "aurora-thread",
                    "from": "Sue",
                    "date": "2026-07-21",
                },
            ),
        )
        anchor = db.scalar(
            select(Chunk).where(Chunk.document_id == original.id)
        )
        assert anchor is not None
        packets = build_evidence_packets(
            db,
            project.id,
            QueryPlan(
                original_question="오로라 담당자는?",
                resolved_question="오로라 유료 협업 담당자는?",
                search_queries=["오로라 유료 협업 담당자"],
                entity_terms=["오로라"],
                requested_field="contact",
            ),
            [
                SearchCandidate(
                    chunk=anchor,
                    document=original,
                    score=0.9,
                )
            ],
        )

        assert {packet.document.id for packet in packets} == {original.id, reply.id}
        reply_packet = next(
            packet for packet in packets if packet.document.id == reply.id
        )
        assert "저희 견적은 200만 원입니다." in reply_packet.context_text
        assert "wrote:" not in reply_packet.context_text


def test_search_packets_never_cross_project_boundary():
    init_db()
    with SessionLocal() as db:
        first = Project(name="첫 프로젝트")
        second = Project(name="둘째 프로젝트")
        db.add_all([first, second])
        db.commit()
        ingest_document(
            db,
            IngestionInput(
                project_id=first.id,
                source_type="gmail",
                external_id="first-message",
                title="격리 자료",
                text="첫 프로젝트의 정보",
                metadata={"thread_id": "shared-looking-thread"},
            ),
        )
        second_document, _ = ingest_document(
            db,
            IngestionInput(
                project_id=second.id,
                source_type="gmail",
                external_id="second-message",
                title="둘째 자료",
                text="둘째 프로젝트의 정보",
                metadata={"thread_id": "shared-looking-thread"},
            ),
        )
        anchor = db.scalar(
            select(Chunk).where(Chunk.document_id == second_document.id)
        )
        assert anchor is not None
        packets = build_evidence_packets(
            db,
            second.id,
            QueryPlan(
                original_question="정보는?",
                resolved_question="둘째 프로젝트 정보는?",
                search_queries=["둘째 프로젝트 정보"],
            ),
            [SearchCandidate(chunk=anchor, document=second_document, score=0.8)],
        )
        assert {packet.document.project_id for packet in packets} == {second.id}
