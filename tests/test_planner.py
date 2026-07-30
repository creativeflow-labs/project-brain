from app.models import ConversationMessage
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
    assert plan.retrieval_mode == "scoped"
    assert plan.record_type == "video"


def test_rejection_reason_question_does_not_activate_state_engine():
    plan = plan_queries("그 프로젝트가 왜 까였는지 알고 있니?", [])

    assert plan.is_state_query is False
    assert "reason" in plan.requested_fields


def test_explicit_current_state_question_activates_state_engine():
    plan = plan_queries("그 프로젝트의 현재 상태가 뭐야?", [])

    assert plan.is_state_query is True


def test_generic_collaboration_template_uses_deterministic_collection_contract():
    question = "협업제안 메일을 분석해서 우리쪽에서 전달할 답장 스타일을 템플릿으로 만들어줘."

    plan = plan_queries(question, [])

    assert plan.task_mode == "compose"
    assert plan.scope_mode == "collection"
    assert plan.cardinality == "all"
    assert plan.retrieval_mode == "hybrid"
    assert plan.locked_entity_terms == []
    assert plan.search_queries == [
        question,
        "협업 제안 메일",
        "파트너십 제안 이메일",
        "협업 제안 회신 조건 보상 일정 산출물 권한",
    ]


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
        "협업제안 메일을 분석해서 답장 템플릿을 만들어줘.", []
    )
    original_queries = list(plan.search_queries)

    extend_plan_for_retry(plan, ["GODtuber.ai 협업 조건 확인"])

    assert plan.search_queries == original_queries
