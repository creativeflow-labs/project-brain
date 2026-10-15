from dataclasses import replace

from app.db import SessionLocal, init_db
from app.models import Project
from app.services.ingestion import IngestionInput, ingest_document
from app.services.search_core import planner, retrieval
from app.services.search_core.collection import assess_scoped_collection
from app.services.search_core.types import QueryPlan


def video_plan(question, **kwargs):
    return QueryPlan(original_question=question, resolved_question=question,
        search_queries=[question], entity_terms=["크랩"], cardinality="all",
        retrieval_mode="scoped", record_type="video",
        collection_filters_supported=True, **kwargs)


def test_content_question_never_uses_video_list_renderer(monkeypatch):
    question = "인스타 자동화 영상 관련 내용을 알려줘."
    monkeypatch.setattr(planner, "_llm_plan", lambda *args: video_plan(question))
    plan = planner.plan_queries(question, [])
    assert plan.retrieval_mode == "hybrid"
    assert plan.cardinality == "many"
    assert assess_scoped_collection(video_plan(question), []) is None


def test_unsupported_filters_do_not_enter_deterministic_collection(monkeypatch):
    question = "실제로 공개된 크랩 영상은 몇 편이야?"
    monkeypatch.setattr(planner, "_llm_plan", lambda *args: replace(
        video_plan(question), collection_filters_supported=False))
    assert planner.plan_queries(question, []).retrieval_mode == "hybrid"


def test_filtered_count_is_before_limit_and_ignores_description_mentions(monkeypatch):
    init_db()
    monkeypatch.setattr(retrieval.settings, "search_scoped_record_limit", 2)
    with SessionLocal() as db:
        project = Project(name="영상 필터 집계 테스트")
        db.add(project)
        db.commit()
        for index, title in enumerate(["AI 실전편 A", "AI 실전편 B", "AI 실전편 C", "AI 개념편 D"]):
            ingest_document(db, IngestionInput(project_id=project.id, source_type="notion",
                external_id=f"filter-video-{index}", title=title,
                text=f"📌 제목\n{title}\n📌 설명\n실전편도 참고하세요",
                metadata={"record_type": "video", "record_title": title,
                    "collection_id": "video-test", "collection_title": "크랩 제작리스트",
                    "recorded_upload_at": f"2026-06-{index+1:02d}T20:00:00+09:00"}))
        plan = video_plan("크랩 업로드 영상에서 실전편 영상 개수는?",
                          collection_title_terms=["실전편"])
        candidates = retrieval.retrieve_candidates(db, project.id, plan)
        packets = retrieval.build_evidence_packets(db, project.id, plan, candidates)
        assert plan.collection_match_count == 3
        assert len(packets) == 2
        assert all("개념편" not in c.document.title for c in candidates)
        answer = assess_scoped_collection(plan, packets)
        assert answer is not None
        assert "총 3건" in answer.answer
        assert "총 2건" not in answer.answer
        listing = replace(plan, original_question="실전편 영상 목록을 알려줘")
        answer = assess_scoped_collection(listing, packets)
        assert "아래 2건" in answer.answer
        assert "생략" in answer.answer
        empty = replace(plan, collection_title_terms=["없는 분류"])
        candidates = retrieval.retrieve_candidates(db, project.id, empty)
        assert not candidates
        assert "총 0건" in assess_scoped_collection(empty, []).answer
