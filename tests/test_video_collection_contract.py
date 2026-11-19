from dataclasses import replace
from datetime import datetime

from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.models import Document, Project
from app.services.ingestion import IngestionInput, ingest_document
from app.services.record_metadata import infer_record_type
from app.services.search_core import planner, retrieval
from app.services.search_core.collection import assess_scoped_collection
from app.services.search_core.types import QueryPlan


def test_repeated_list_reuses_verified_scope_but_reads_latest_records(monkeypatch):
    question = "점검 기록 목록을 보여줘."
    plan = QueryPlan(
        question, question, [question], requested_fields=["title"], cardinality="all",
        record_type="inspection", collection_operation="list", collection_filters_supported=True,
    )
    planner_calls = []

    def plan_once(current, history):
        planner_calls.append(current)
        assert not history
        return replace(plan)

    monkeypatch.setattr(planner, "_llm_plan", plan_once)
    with TestClient(app) as client, SessionLocal() as db:
        project = Project(name="점검 목록 반복 테스트")
        db.add(project)
        db.commit()

        def add_record(title):
            ingest_document(db, IngestionInput(
                project_id=project.id, source_type="upload", external_id=title,
                title=title, text=f"{title} 현장 점검", metadata={"record_type": "inspection"},
            ))

        add_record("기존 점검표")
        request = {"project_id": project.id, "question": question}
        first = client.post("/api/ask", json=request)
        assert first.status_code == 200
        assert "총 1건" in first.json()["answer"]
        add_record("추가 점검표")
        for _ in range(2):
            repeat = client.post("/api/ask", json={**request, "thread_id": first.json()["thread_id"]})
            assert repeat.status_code == 200
            assert repeat.json()["evidence_status"] == "supported"
            assert "총 2건" in repeat.json()["answer"]
            assert "추가 점검표" in repeat.json()["answer"]
        assert planner_calls == [question]


def test_legacy_scoped_list_requires_one_verified_parent_and_discloses_missing_date():
    question = "북관 현장 3월 정기 점검은?"
    plan = QueryPlan(
        question, question, [question], cardinality="all", retrieval_mode="scoped",
        record_type="inspection", collection_operation="list",
        collection_scope_terms=["북관 현장"], collection_title_terms=["정기"],
        time_field="occurred_at",
        time_start=datetime.fromisoformat("2026-03-01T00:00:00+09:00"),
        time_end=datetime.fromisoformat("2026-03-31T23:59:59+09:00"),
    )

    def row(doc_id, parent_id, title, occurred_at=None):
        return (None, Document(
            id=doc_id, source_type="notion", title=title, metadata_json={
                "metadata_schema_version": 3, "record_type": "inspection",
                "hierarchy_complete": True, "parent_id": parent_id,
                "ancestor_titles": ["[현장] 북관"], "occurred_at": occurred_at,
            },
        ))

    dated = row("dated", "parent", "정기 점검 A", "2026-03-12")
    undated = row("undated", "parent", "정기 점검 B")
    unrelated = row("other-kind", "parent", "수시 점검")
    assert retrieval._scoped_document_ids([dated, undated, unrelated], plan) == {
        "dated", "other-kind",
    }
    assert plan.collection_undated_count == 1
    another_parent = row("duplicate-group", "another-parent", "정기 점검 C", "2026-03-13")
    assert retrieval._scoped_document_ids([dated, another_parent], plan) is None


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
        video_plan(question, collection_operation="count"), collection_filters_supported=False))
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
        plan = video_plan("크랩 업로드 영상에서 실전편 영상 개수는?", collection_operation="count",
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
        listing = replace(plan, original_question="실전편 영상 목록을 알려줘", collection_operation="list")
        answer = assess_scoped_collection(listing, packets)
        assert "아래 2건" in answer.answer
        assert "생략" in answer.answer
        empty = replace(plan, collection_title_terms=["없는 분류"])
        candidates = retrieval.retrieve_candidates(db, project.id, empty)
        assert not candidates
        assert "총 0건" in assess_scoped_collection(empty, []).answer


def test_exact_collection_is_not_limited_to_notion_or_largest_group(monkeypatch):
    init_db()
    with SessionLocal() as db:
        project = Project(name="분산 기록 집계")
        db.add(project)
        db.commit()
        for index, (source, scope, collection) in enumerate([
            ("notion", "열매", "a"), ("upload", "열매", "b"),
            ("upload", "열매연구소", "c"), ("notion", "보름", "d"),
        ]):
            ingest_document(db, IngestionInput(
                project_id=project.id, source_type=source, external_id=f"inspection-{index}",
                title=f"{scope} 점검", text="현장 설비 점검 기록",
                metadata={"record_type": "inspection", "collection_id": collection,
                          "collection_title": scope},
            ))
        monkeypatch.setattr(retrieval.embedding_service, "embed_queries",
                            lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no embedding needed")))
        question = "열매의 점검 기록은 몇 건이야?"
        plan = QueryPlan(question, question, [question], cardinality="all", record_type="inspection",
                         retrieval_mode="scoped", collection_operation="count",
                         collection_scope_terms=["열매"])
        candidates = retrieval.retrieve_candidates(db, project.id, plan)
        assert plan.collection_match_count == 2
        assert {item.document.source_type for item in candidates} == {"notion", "upload"}
        broad = replace(plan, collection_scope_terms=[])
        retrieval.retrieve_candidates(db, project.id, broad)
        assert broad.collection_match_count == 4


def test_unmapped_or_legacy_collection_never_claims_an_exact_total():
    init_db()
    assert infer_record_type("면담 기록", "📌 제목\n면담 기록\n📌 설명\n현장 점검") is None
    assert infer_record_type("26.06.01 EP123", "면담 기록") is None
    with SessionLocal() as db:
        project = Project(name="기존 비정형 자료 검증")
        db.add(project)
        db.commit()
        ingest_document(db, IngestionInput(
            project_id=project.id, source_type="notion", external_id="legacy-record",
            title="기존 영상 작업 기록", text="영상 설명",
            metadata={"metadata_schema_version": 3, "record_type": "video",
                      "collection_title": "새봄"},
        ))
        for name in ("새봄", "다른 채널"):
            question = f"{name}의 영상이 몇 편이야?"
            plan = QueryPlan(question, question, [question], cardinality="all", record_type="video",
                             retrieval_mode="scoped", collection_operation="count",
                             collection_scope_terms=[name])
            assert retrieval.retrieve_candidates(db, project.id, plan)
            assert plan.retrieval_mode == "hybrid"
            assert plan.collection_match_count is None
            assert assess_scoped_collection(plan, []) is None
