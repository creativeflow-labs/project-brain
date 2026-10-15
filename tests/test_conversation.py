import pytest

from app.services.search_core import planner, retrieval, judgment
from app.services.search_core.conversation import assistant_identity_answer


@pytest.mark.parametrize("question", ["안녕, 너 이름이 뭐야?", "얀녕, 너 이름이 뭐니?", "너는 누구야?"])
def test_identity_does_not_use_project_search_or_model(monkeypatch, question):
    def unexpected(*args, **kwargs):
        raise AssertionError("Product identity must not search private data or call a model")
    monkeypatch.setattr(planner, "_llm_plan", unexpected)
    monkeypatch.setattr(retrieval.embedding_service, "embed_queries", unexpected)
    monkeypatch.setattr(judgment, "_llm_assessment", unexpected)
    plan = planner.plan_queries(question, [])
    assert plan.search_queries == []
    assert retrieval.retrieve_candidates(None, "project", plan) == []
    assessment = judgment.assess_evidence(plan, [])
    assert "피비(PB)" in assessment.answer
    assert assessment.cited_document_ids == []
    assert assessment.status == "supported"


@pytest.mark.parametrize("question", ["SK 다이렉트 프로젝트의 의사결정자가 누구야?", "안녕, 협업 담당자는 누구야?", "너 이름이 뭐야? 그리고 협업비는?", "김민수 이름이 뭐야?", "프로젝트 이름이 뭐야?"])
def test_identity_never_intercepts_work_questions(question):
    assert assistant_identity_answer(question) is None
