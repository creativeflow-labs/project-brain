from datetime import datetime

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Chunk, ConversationMessage, Document, Project
from app.services.ingestion import IngestionInput, ingest_document
from app.services.search_core import planner
from app.services.search_core.planner import plan_queries
from app.services.search_core.retrieval import (
    build_evidence_packets,
    retrieve_candidates,
)
from app.services.search_core.collection import assess_scoped_collection
from app.services.search_core.types import EvidenceAssessment, QueryPlan, SearchCandidate
from app.services.search_core.workflow import _retry_node, _route_after_assessment


@pytest.mark.parametrize("question", [
    "그럼 전체 현장의 설비 점검 기록으로 공통 체크리스트를 작성해줘.",
    "그럼 전체 현장의 설비 점검 기록을 비교한 보고서를 작성해줘.",
])
def test_broad_compose_after_named_scope_uses_notion_only_evidence(question):
    init_db()
    with SessionLocal() as db:
        project = Project(name="설비 기록 전용")
        db.add(project)
        db.flush()
        ids = set()
        for site in ("라온", "다온"):
            document, _ = ingest_document(db, IngestionInput(
                project_id=project.id, source_type="notion", external_id=f"site-{site}",
                title=f"{site} 현장 설비 점검 기록",
                text=f"{site} 현장 설비 점검 기록: 안전장치와 잠금 상태를 확인했다.",
            ))
            ids.add(document.id)

        plan = plan_queries(
            question,
            [ConversationMessage(role="user", content="라온 현장의 설비 점검 기록을 찾아줘.")],
        )
        candidates = retrieve_candidates(db, project.id, plan)
        packets = build_evidence_packets(db, project.id, plan, candidates)
        assert (plan.scope_mode, plan.task_mode) == ("collection", "compose")
        assert not plan.locked_entity_terms
        assert plan.resolved_question == question
        assert all("라온" not in query and "협업" not in query for query in plan.search_queries)
        assert {packet.document.id for packet in packets} == ids


def test_compose_collection_can_retry_without_adopting_a_new_target():
    question = "전체 시설의 변경 이력을 비교한 보고서를 작성해줘."
    plan = plan_queries(question, [])
    assessment = EvidenceAssessment(
        status="insufficient", answer="근거 부족", cited_document_ids=[],
        retry_queries=["처음 요청에 없던 단일 시설의 변경 이력"],
    )
    state = {"plan": plan, "assessment": assessment, "retry_count": 0, "packets": []}
    assert _route_after_assessment(state) == "retry"

    updated = _retry_node(state)
    retry_plan = updated["plan"]
    assert retry_plan.scope_mode == "collection"
    assert retry_plan.locked_entity_terms == []
    assert retry_plan.resolved_question == question
    assert len(retry_plan.search_queries) == len(plan.search_queries) + 1
    assert all("단일 시설" not in query for query in retry_plan.search_queries)
    assert _route_after_assessment({**state, **updated}) == "finish"


def test_content_search_preserves_target_period_and_classification(monkeypatch):
    init_db()
    with SessionLocal() as db:
        project = Project(name="복수 근거 설명")
        db.add(project)
        db.commit()
        data = [
            ("라온", "점검", "정기", "2026-07-10", "A 구역은 센서 교체가 필요합니다."),
            ("라온", "점검", "정기", "2026-07-24", "B 구역은 배선을 수리해야 합니다."),
            ("라온", "점검", "수시", "2026-07-10", "정기 점검을 본문에서만 언급합니다."),
            ("다온", "점검", "정기", "2026-07-10", "타 현장에서는 별도 작업을 진행했습니다."),
            ("라온", "점검", "정기", "2026-08-10", "본문의 참고 날짜는 2026-07-24입니다."),
            ("라온", "공지", "정기", "2026-07-10", "동일 조건을 언급하는 별도 공지입니다."),
        ]
        for index, (site, kind, label, date, text) in enumerate(data):
            ingest_document(db, IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id=f"scope-content-{index}",
                title=f"{site} {label} {kind} {index}",
                text=text,
                metadata={
                    "record_type": kind,
                    "ancestor_titles": ["시설 관리", f"현장 {site}"],
                    "occurred_at": date,
                },
            ))

        question = "라온 현장 7월 정기 점검 기록의 내용을 설명해줘."
        draft = QueryPlan(
            question, question, [question], record_type="점검", cardinality="one",
            time_start=datetime.fromisoformat("2026-07-01T00:00:00+09:00"),
            time_end=datetime.fromisoformat("2026-07-31T23:59:59+09:00"),
            time_field="occurred_at", content_scope_filters_supported=True,
            collection_scope_terms=["라온 현장"], collection_title_terms=["정기"],
        )
        monkeypatch.setattr(planner, "_llm_plan", lambda *_: draft)
        plan = plan_queries(question, [])
        candidates = retrieve_candidates(db, project.id, plan)
        packets = build_evidence_packets(db, project.id, plan, candidates)

        assert plan.retrieval_mode == "hybrid"
        assert plan.collection_operation is None
        assert plan.collection_match_count is None
        assert plan.content_scope_match_count == 2
        assert {packet.document.external_id for packet in packets} == {
            "scope-content-0", "scope-content-1",
        }
        assert any("센서 교체" in packet.context_text for packet in packets)
        assert any("배선을 수리" in packet.context_text for packet in packets)
        assert assess_scoped_collection(plan, packets) is None

        # If the structured range cannot be found, ordinary evidence search remains
        # available, without claiming verified coverage or switching to aggregation.
        plan.collection_scope_terms = ["아직 등록되지 않은 현장"]
        assert retrieve_candidates(db, project.id, plan)
        assert plan.content_scope_match_count is None
        assert plan.collection_match_count is None


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


def test_compose_collection_uses_diverse_bounded_document_set():
    init_db()
    with SessionLocal() as db:
        project = Project(name="협업 제안 종합")
        db.add(project)
        db.commit()
        db.refresh(project)
        for index in range(12):
            ingest_document(
                db,
                IngestionInput(
                    project_id=project.id,
                    source_type="gmail",
                    external_id=f"proposal-{index}",
                    title=f"브랜드 {index} 협업 제안",
                    text=(
                        f"브랜드 {index}에서 콘텐츠 협업을 제안합니다. "
                        "산출물, 일정, 보상, 콘텐츠 사용 권한을 확인해 주세요."
                    ),
                    metadata={"thread_id": f"proposal-thread-{index}"},
                ),
            )
        plan = QueryPlan(
            original_question="협업 제안 답장 템플릿을 작성해줘.",
            resolved_question="협업 제안 답장 템플릿을 작성해줘.",
            search_queries=["협업 제안 메일", "협업 제안 조건 보상 일정 산출물"],
            entity_terms=["협업", "제안", "메일"],
            cardinality="all",
            task_mode="compose",
            scope_mode="collection",
        )

        candidates = retrieve_candidates(db, project.id, plan)
        packets = build_evidence_packets(db, project.id, plan, candidates)

        assert len({candidate.document.id for candidate in candidates}) == 8
        assert len(packets) == 8


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
            collection_operation="list",
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


def test_notion_ancestor_title_recalls_child_without_changing_body_evidence():
    init_db()
    with SessionLocal() as db:
        project = Project(name="Notion 상위 경로 검색")
        db.add(project)
        db.commit()
        db.refresh(project)
        child, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id="vibe-coding-child",
                title="김그륜님의 바이브코딩",
                text="에이전트 설계 실습과 현업 적용 사례를 다룹니다.",
                metadata={
                    "ancestor_titles": ["AI 웨비나"],
                    "hierarchy_path": "AI 웨비나 > 김그륜님의 바이브코딩",
                },
            ),
        )
        ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id="generic-work-page",
                title="일반 업무 자료",
                text="반복 업무와 문서 작성 방법을 정리합니다.",
                metadata={"ancestor_titles": ["공유 자료"]},
            ),
        )
        plan = QueryPlan(
            original_question="AI 웨비나의 김그륜 바이브코딩 자료를 알려줘",
            resolved_question="AI 웨비나의 김그륜 바이브코딩 자료를 알려줘",
            search_queries=["AI 웨비나 김그륜 바이브코딩"],
            entity_terms=["ai", "웨비나", "김그륜", "바이브코딩"],
        )

        candidates = retrieve_candidates(db, project.id, plan)
        packets = build_evidence_packets(db, project.id, plan, candidates)

        selected = next(
            candidate
            for candidate in candidates
            if candidate.document.id == child.id
        )
        packet = next(packet for packet in packets if packet.document.id == child.id)
        assert selected.hierarchy_score > 0
        assert "Notion 경로: AI 웨비나 > 김그륜님의 바이브코딩" in (
            packet.context_text
        )
        assert "에이전트 설계 실습" in packet.context_text
