from concurrent.futures import Future
from dataclasses import replace
from threading import Event

import pytest

from app.services.embeddings import EmbeddingService, settings as embedding_settings
from app.services.search_core import prefetch, workflow
from app.services.search_core.planner import can_prefetch_embedding
from app.services.search_core.types import QueryPlan


def finished(value=None, error=None):
    future = Future()
    if error:
        future.set_exception(error)
    else:
        future.set_result(value)
    return future


def test_exact_match_only_and_fail_open():
    assert prefetch.take_ready(finished([1.0]), "name", ["name"]) == {"name": [1.0]}
    assert prefetch.take_ready(finished([1.0]), "name", ["other name"]) == {}
    assert prefetch.take_ready(finished(error=RuntimeError()), "name", ["name"]) == {}
    pending = Future()
    assert prefetch.take_ready(pending, "name", ["name"]) == {}
    assert pending.cancelled()


@pytest.mark.parametrize("question", [
    "그럼, 유료 협업비는?", "얀녕, 너 이름이 뭐니?",
    "magiclight 협업 담당자는?",
    "크랩 유튜브 6월 영상 제목을 업로드 시간을 포함해서 리스트로 제시해줘.",
    "협업제안 메일을 분석해서 답장 템플릿을 만들어줘.",
])
def test_existing_special_routes_are_excluded(question):
    assert not can_prefetch_embedding(question)


def test_independent_llm_question_is_eligible():
    assert can_prefetch_embedding("김그륜님의 바이브코딩관련 자료를 더 자세히 정리해줘.")


def test_workflow_switch_and_request_isolation(monkeypatch):
    question = "independent question"
    plan = QueryPlan(question, question, [question, "expanded"])
    calls = []
    monkeypatch.setattr(workflow, "can_prefetch_embedding", lambda _: True)
    monkeypatch.setattr(workflow, "start_prefetch", lambda q: calls.append(q) or finished([1.0]))
    monkeypatch.setattr(workflow, "plan_queries", lambda *_: plan)
    state = {"question": question, "history": []}
    monkeypatch.setattr(workflow.settings, "search_embedding_prefetch", False)
    assert workflow._plan_node(state)["prepared_embeddings"] == {}
    assert not calls
    monkeypatch.setattr(workflow.settings, "search_embedding_prefetch", True)
    assert workflow._plan_node(state)["prepared_embeddings"] == {question: [1.0]}
    monkeypatch.setattr(workflow, "plan_queries", lambda *_: replace(plan, search_queries=["other"]))
    assert workflow._plan_node(state)["prepared_embeddings"] == {}


def test_prepared_embedding_preserves_order_and_embeds_only_missing(monkeypatch):
    service = EmbeddingService()
    vector = [0.5] * embedding_settings.embedding_dimensions
    other = [0.25] * embedding_settings.embedding_dimensions
    calls = []
    monkeypatch.setattr(service, "embed_many", lambda texts: calls.append(texts) or [other] * len(texts))
    assert service.embed_queries(["raw", "extra", "raw"], prepared={"raw": vector}) == [vector, other, vector]
    assert calls == [["extra"]]
    assert service.embed_queries(["raw"]) == [vector]
    assert calls == [["extra"]]


@pytest.mark.parametrize("bad", [[1.0], [float("nan")] * embedding_settings.embedding_dimensions])
def test_bad_vectors_fall_back(monkeypatch, bad):
    service = EmbeddingService()
    vector = [0.1] * embedding_settings.embedding_dimensions
    monkeypatch.setattr(service, "embed_many", lambda texts: [vector] * len(texts))
    assert service.embed_queries(["raw"], prepared={"raw": bad}) == [vector]


def test_prefetch_does_not_fill_cache_until_consumed():
    service = EmbeddingService()
    service.client = None
    vector = service.prefetch_query("raw")
    assert "raw" not in service._query_cache
    assert service.embed_queries(["raw"], prepared={"raw": vector}) == [vector]


def test_worker_limit_skips_instead_of_queuing(monkeypatch):
    release = Event()
    monkeypatch.setattr(prefetch.embedding_service, "prefetch_query", lambda _: release.wait(2) or [])
    first = second = None
    try:
        first = prefetch.start_prefetch("one")
        second = prefetch.start_prefetch("two")
        assert first is not None and second is not None
        assert prefetch.start_prefetch("three") is None
    finally:
        release.set()
        for future in [first, second]:
            if future is not None:
                future.result(timeout=3)
