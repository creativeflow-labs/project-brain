from app.services.search_core.planner import plan_queries


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
