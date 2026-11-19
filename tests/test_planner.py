import json
from types import SimpleNamespace

import pytest

from app.models import ConversationMessage
from app.services.search_core import planner
from app.services.search_core.planner import extend_plan_for_retry, plan_queries


def test_month_collection_question_creates_time_bounded_search_plan():
    plan = plan_queries(
        "2026년 6월에 발행된 크랩 유튜브 영상 제목을 업로드 시간과 함께 알려줘.",
        [],
    )

    assert plan.cardinality == "all"
    assert plan.time_start is not None
    assert plan.time_end is not None
    assert (plan.time_start.year, plan.time_start.month, plan.time_start.day) == (
        2026,
        6,
        1,
    )
    assert (plan.time_end.year, plan.time_end.month, plan.time_end.day) == (
        2026,
        6,
        30,
    )
    assert plan.time_field == "published_at"
    assert {"title", "published_at"}.issubset(plan.requested_fields)
    assert plan.retrieval_mode == "hybrid"
    assert plan.record_type is None


def test_rejection_reason_question_does_not_activate_state_engine():
    plan = plan_queries("그 프로젝트가 왜 까였는지 알고 있니?", [])

    assert plan.is_state_query is False
    assert "reason" in plan.requested_fields


def test_explicit_current_state_question_activates_state_engine():
    plan = plan_queries("그 프로젝트의 현재 상태가 뭐야?", [])

    assert plan.is_state_query is True


def test_generic_collaboration_template_does_not_inject_fixed_search_queries():
    question = "협업제안 메일을 분석해서 우리쪽에서 전달할 답장 스타일을 템플릿으로 만들어줘."

    plan = plan_queries(question, [])

    assert plan.task_mode == "compose"
    assert plan.scope_mode == "open"
    assert plan.retrieval_mode == "hybrid"
    assert plan.locked_entity_terms == []
    assert plan.search_queries[0] == question
    assert all("파트너십 제안 이메일" not in query for query in plan.search_queries)


@pytest.mark.parametrize("channel", ["크랩", "새봄"])
def test_monthly_list_does_not_use_a_channel_name_fast_path(channel, monkeypatch):
    seen = []
    monkeypatch.setattr(planner, "_llm_plan", lambda q, _: seen.append(q))
    question = f"2026년 6월 {channel} 영상 제목과 업로드 시간을 전부 알려줘."
    assert plan_queries(question, []).retrieval_mode == "hybrid"
    assert seen == [question]


def test_explicit_contact_and_money_questions_skip_llm_planning(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("explicit fact lookup must not call the LLM planner")

    monkeypatch.setattr(planner, "_llm_plan", fail_if_called)

    contact = plan_queries("MagicLight.AI 협업 담당자는 누구야?", [])
    money = plan_queries("MagicLight.AI 유료 협업비용이 얼마야?", [])

    assert contact.requested_field == "contact"
    assert money.requested_field == "money"
    assert contact.scope_mode == money.scope_mode == "entity"


def test_explicit_entity_resets_previous_collection_scope(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("explicit fact lookup must use deterministic planning")

    monkeypatch.setattr(planner, "_llm_plan", fail_if_called)
    prior = ConversationMessage(
        thread_id="thread",
        role="user",
        content="크랩 유튜브 6월 영상 목록을 알려줘.",
        resolved_question="2026년 6월 크랩 유튜브 영상 전체 목록을 알려줘.",
    )

    plan = plan_queries("magiclight 협업 담당자는?", [prior])

    assert plan.resolved_question == "magiclight 협업 담당자는?"
    assert plan.scope_mode == "entity"
    assert plan.requested_field == "contact"
    assert plan.time_start is None
    assert plan.time_end is None
    assert plan.record_type is None
    assert plan.cardinality == "one"
    assert plan.retrieval_mode == "hybrid"
    assert all("6월" not in query for query in plan.search_queries)


def test_referential_follow_up_keeps_previous_collection_scope():
    prior = ConversationMessage(
        thread_id="thread",
        role="user",
        content="크랩 유튜브 6월 영상 목록을 알려줘.",
        resolved_question="2026년 6월 크랩 유튜브 영상 전체 목록을 알려줘.",
    )

    plan = plan_queries("그럼 영상은 몇 개야?", [prior])

    assert "2026년 6월" in plan.resolved_question
    assert plan.time_start is not None
    assert plan.record_type is None
    assert plan.cardinality == "all"
    assert plan.retrieval_mode == "hybrid"


def test_referential_contact_question_keeps_previous_entity():
    prior = ConversationMessage(
        thread_id="thread",
        role="user",
        content="MagicLight 협업비는 얼마야?",
        resolved_question="MagicLight 협업의 유료 협업비는 얼마인가?",
    )

    plan = plan_queries("그 담당자는?", [prior])

    assert plan.scope_mode == "inherited"
    assert "magiclight" in plan.locked_entity_terms
    assert plan.requested_field == "contact"


def test_follow_up_resolution_does_not_accumulate_previous_suffixes():
    prior = ConversationMessage(
        thread_id="thread",
        role="user",
        content="그 담당자는?",
        resolved_question=(
            "MagicLight 협업의 유료 협업비는 얼마인가? "
            "/ 후속 질문: 그 담당자는?"
        ),
    )

    plan = plan_queries("그럼 비용은?", [prior])

    assert plan.resolved_question.count("/ 후속 질문:") == 1
    assert plan.resolved_question.endswith("그럼 비용은?")


def test_explicit_brand_template_locks_entity_scope():
    plan = plan_queries("MagicLight.AI 협업 제안에 보낼 답장 초안을 작성해줘.", [])

    assert plan.task_mode == "compose"
    assert plan.scope_mode == "entity"
    assert any(term.startswith("magiclight") for term in plan.locked_entity_terms)


def test_explicit_follow_up_template_inherits_previous_entity():
    prior = ConversationMessage(
        thread_id="thread",
        role="user",
        content="MagicLight 협업비는 얼마야?",
        resolved_question="MagicLight 유료 협업비는 얼마인가?",
    )

    plan = plan_queries("그 메일에 답장 초안을 작성해줘.", [prior])

    assert plan.task_mode == "compose"
    assert plan.scope_mode == "inherited"
    assert "magiclight" in plan.locked_entity_terms


def test_compose_retry_cannot_drift_to_another_brand():
    plan = plan_queries(
        "전체 협업제안 메일을 분석해서 답장 템플릿을 만들어줘.", []
    )
    original_queries = list(plan.search_queries)
    retry = extend_plan_for_retry(plan, ["GODtuber.ai 협업 조건 확인"])
    assert retry.search_queries[:-1] == original_queries
    assert plan.search_queries == original_queries
    assert "GODtuber" not in " ".join(retry.search_queries)


@pytest.mark.parametrize(("question", "scope", "lock"), [
    ("그럼 전체 협업 메일을 분석해서 공통 답장 초안을 만들어줘.", "collection", []),
    ("그럼 전체 프로젝트의 승인기록을 보여줘.", "collection", []),
    ("이번엔 모든 현장의 점검 문서로 공통 양식을 작성해줘.", "collection", []),
    ("그럼 청명 현장의 점검 기록 전체를 정리해줘.", "entity", ["청명 현장"]),
    ("그럼 MagicLight 전체 메일을 정리해줘.", "entity", ["magiclight"]),
    ("그럼 전체", "collection", []),
    ("그럼 승인 날짜는?", "inherited", ["라온 현장"]),
])
def test_scope_uses_current_question_before_inheriting(question, scope, lock):
    previous = ConversationMessage(
        role="user", content="라온 현장의 점검 기록을 보여줘.",
    )
    plan = plan_queries(question, [previous])
    assert (plan.scope_mode, plan.locked_entity_terms) == (scope, lock)
    assert ("라온" in plan.resolved_question) == (scope == "inherited")
    assert any("라온" in query for query in plan.search_queries) == (scope == "inherited")
    if "협업" not in question:
        assert all("협업" not in query and "메일" not in query
                   for query in plan.search_queries) or "메일" in question


def test_existing_llm_plan_does_not_receive_past_entity_for_explicit_broad_scope(monkeypatch):
    question = "그럼 전체 설계 문서에서 점검 기준을 정리해줘."
    previous = ConversationMessage(role="user", content="라온 현장의 설계 문서를 찾아줘.")
    calls = []

    def create(**kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(output_text=json.dumps({
            "resolved_question": question,
            "search_queries": ["설계 문서 점검 기준"],
            "entity_terms": ["설계 문서"],
            "requested_field": "general",
            "requested_fields": [],
            "cardinality": "all",
            "time_start": None, "time_end": None, "time_field": None,
            "is_state_query": False,
            "collection_title_terms": [],
            "collection_filters_supported": False,
            "scope_mode": "collection",
            "scope_terms": [],
        }))

    monkeypatch.setattr(planner.settings, "openai_api_key", "mock")
    monkeypatch.setattr(planner, "get_openai_client",
                        lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    plan = plan_queries(question, [previous])
    assert len(calls) == 1
    assert calls[0]["history"] == []
    assert plan.scope_mode == "collection"
    assert plan.search_queries == [question, "설계 문서 점검 기준"]


def test_content_range_includes_the_entire_last_day_without_changing_other_paths():
    assert planner._parse_plan_datetime("2026-07-31", end_of_day=True).isoformat() == (
        "2026-07-31T23:59:59.999999+09:00"
    )
    assert planner._parse_plan_datetime("2026-07-31").isoformat() == (
        "2026-07-31T00:00:00+09:00"
    )
    assert planner._parse_plan_datetime("2026-07-31T15:00:00+09:00", end_of_day=True).hour == 15
