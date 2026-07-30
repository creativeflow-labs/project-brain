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


def test_deterministic_collections_skip_llm_planning(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("deterministic collection must not call the LLM planner")

    monkeypatch.setattr(planner, "_llm_plan", fail_if_called)

    template = plan_queries("협업 제안 답장 템플릿을 작성해줘.", [])
    june_videos = plan_queries(
        "2026년 6월 크랩 유튜브 영상 제목과 업로드 시간을 전부 알려줘.", []
    )

    assert template.scope_mode == "collection"
    assert june_videos.retrieval_mode == "scoped"


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
    assert plan.record_type == "video"
    assert plan.cardinality == "all"
    assert plan.retrieval_mode == "scoped"


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
        "협업제안 메일을 분석해서 답장 템플릿을 만들어줘.", []
    )
    original_queries = list(plan.search_queries)

    extend_plan_for_retry(plan, ["GODtuber.ai 협업 조건 확인"])

    assert plan.search_queries == original_queries
