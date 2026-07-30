from datetime import datetime

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Chunk, Document, Project
from app.services.ingestion import IngestionInput, ingest_document
from app.services.search_core.retrieval import (
    build_evidence_packets,
    retrieve_candidates,
)
from app.services.search_core.collection import assess_scoped_collection
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


def test_time_bounded_collection_excludes_documents_from_other_months():
    init_db()
    with SessionLocal() as db:
        project = Project(name="기간 목록 검색")
        db.add(project)
        db.commit()
        db.refresh(project)
        june, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id="video-june",
                title="크랩 6월 영상",
                text="영상 제목: 6월 피비 소개\n업로드 시간: 2026-06-18 09:30",
                metadata={"published_at": "2026-06-18T09:30:00+09:00"},
            ),
        )
        december, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id="video-december",
                title="크랩 12월 영상",
                text="영상 제목: 12월 피비 소개\n업로드 시간: 2026-12-17 20:00",
                metadata={"published_at": "2026-12-17T20:00:00+09:00"},
            ),
        )
        plan = QueryPlan(
            original_question="2026년 6월 영상 제목과 업로드 시간은?",
            resolved_question="2026년 6월 영상 제목과 업로드 시간은?",
            search_queries=["크랩 유튜브 영상 제목 업로드 시간"],
            entity_terms=["크랩", "유튜브", "영상"],
            requested_field="date",
            requested_fields=["title", "published_at"],
            cardinality="all",
            time_start=datetime.fromisoformat("2026-06-01T00:00:00+09:00"),
            time_end=datetime.fromisoformat("2026-06-30T23:59:59+09:00"),
            time_field="published_at",
        )

        candidates = retrieve_candidates(db, project.id, plan)

        assert {candidate.document.id for candidate in candidates} == {june.id}
        assert december.id not in {candidate.document.id for candidate in candidates}


def test_scoped_video_collection_excludes_receipts_and_returns_complete_list():
    init_db()
    with SessionLocal() as db:
        project = Project(name="범위 지정 영상 검색")
        db.add(project)
        db.commit()
        db.refresh(project)
        expected_ids: set[str] = set()
        for day, hour, title in (
            (2, 20, "첫 번째 영상"),
            (4, 21, "두 번째 영상"),
            (6, 8, "세 번째 영상"),
        ):
            metadata = {
                "collection_id": "youtube-production-list",
                "collection_title": "크랩 유튜브 제작리스트",
                "record_type": "video",
                "record_title": title,
                "recorded_upload_at": f"2026-06-{day:02d}T{hour:02d}:00:00+09:00",
            }
            if day == 2:
                metadata.pop("record_type")
                metadata.pop("record_title")
            document, _ = ingest_document(
                db,
                IngestionInput(
                    project_id=project.id,
                    source_type="notion",
                    external_id=f"video-{day}",
                    title=f"26.06.{day:02d}(오후{hour if hour <= 12 else hour - 12:02d}시) EP{day}. 영상 기록",
                    text=f"📌 제목\n{title}\n📌 설명\n영상 설명",
                    metadata=metadata,
                ),
            )
            expected_ids.add(document.id)
        ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id="youtube-ad-receipt",
                title="YouTube Ads 6월 영수증",
                text="결제일: 2026-06-03\n금액: 30만 원",
                metadata={"date": "2026-06-03T10:00:00+09:00"},
            ),
        )
        ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="gmail",
                external_id="youtube-collaboration-email",
                title="YouTube 영상 협업 이메일",
                text="2026년 6월 영상 협업을 제안합니다.",
                metadata={"date": "2026-06-05T10:00:00+09:00"},
            ),
        )
        plan = QueryPlan(
            original_question="6월 크랩 유튜브 영상 전체 목록은?",
            resolved_question="2026년 6월 크랩 유튜브 영상 전체 목록은?",
            search_queries=["크랩 유튜브 2026년 6월 영상 제목 업로드 시간"],
            entity_terms=["크랩", "유튜브", "youtube"],
            requested_field="date",
            requested_fields=["title", "published_at"],
            cardinality="all",
            time_start=datetime.fromisoformat("2026-06-01T00:00:00+09:00"),
            time_end=datetime.fromisoformat("2026-06-30T23:59:59+09:00"),
            time_field="published_at",
            retrieval_mode="scoped",
            record_type="video",
        )

        candidates = retrieve_candidates(db, project.id, plan)
        packets = build_evidence_packets(db, project.id, plan, candidates)
        assessment = assess_scoped_collection(plan, packets)

        assert {candidate.document.id for candidate in candidates} == expected_ids
        assert assessment is not None
        assert assessment.status == "supported"
        assert "총 3건" in assessment.answer
        assert all(title in assessment.answer for title in ("첫 번째 영상", "두 번째 영상", "세 번째 영상"))
        assert "영수증" not in assessment.answer
