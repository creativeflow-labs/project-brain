import json
from types import SimpleNamespace

import pytest

from app.models import Document
from app.services.search_core import judgment, planner
from app.services.search_core.judgment import assess_evidence
from app.services.search_core.reporting import render_answer_text, validate_answer_report
from app.services.search_core.types import (
    AnswerFact,
    AnswerReport,
    CurrentStateAssessment,
    EvidenceAssessment,
    EvidenceNote,
    EvidencePacket,
    QueryPlan,
)


def _document(document_id: str, source_type: str, title: str) -> Document:
    return Document(
        id=document_id,
        project_id="project-reporting",
        source_type=source_type,
        external_id=document_id,
        title=title,
        content_hash=document_id.ljust(64, "0")[:64],
        metadata_json={},
    )


def _packet(document: Document, text: str) -> EvidencePacket:
    return EvidencePacket(
        document=document,
        anchor_chunks=[],
        context_text=text,
        score=0.9,
    )


def test_material_note_from_assessment_is_rendered_in_normal_answer(monkeypatch):
    question = "검수가 왜 지연됐어?"
    plan = QueryPlan(question, question, [question], requested_fields=["reason"])
    packets = [
        _packet(_document("inspection", "notion", "점검 기록"), "검수 장비가 도착하지 않았다."),
        _packet(_document("handover", "naverworks", "인수인계 메모"), "교대 인력도 부족했다."),
    ]
    payload = {
        "status": "supported", "answer": "검수 장비 입고가 늦었습니다. [1]",
        "headline": "검수 지연 사유", "detail": None, "facts": [],
        "evidence_note": {"text": "교대 인력 부족도 기록됐으나, 지연에 미친 영향은 확인되지 않습니다.",
                          "citation_document_ids": ["handover", "unknown"]},
        "next_action": None, "cited_document_ids": ["inspection"],
        "conflicts": [], "retry_queries": [],
    }
    monkeypatch.setattr(judgment.settings, "openai_api_key", "stub")
    monkeypatch.setattr(judgment, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_: SimpleNamespace(output_text=json.dumps(payload)))
    ))
    outcome = assess_evidence(plan, packets)
    report = validate_answer_report(plan, outcome, packets, None)
    assert report.status == "supported"
    assert report.presentation == "adaptive"
    assert report.evidence_note.document_ids == ["handover"]
    assert render_answer_text(report, {"inspection": 1, "handover": 2}) == (
        "검수 장비 입고가 늦었습니다. [1]\n\n"
        "교대 인력 부족도 기록됐으나, 지연에 미친 영향은 확인되지 않습니다. [2]"
    )


@pytest.mark.parametrize(("status", "body", "source_id"), [
    ("supported", "이미 본문에 있는 정보입니다. [1]", "record"),
    ("supported", "확인된 원인입니다. [1]", "missing"),
    ("conflict", "두 기록이 직접 모순됩니다. [1]", "record"),
])
def test_note_does_not_duplicate_body_or_appear_without_valid_evidence(status, body, source_id):
    question = "처리 결과를 설명해줘."
    plan = QueryPlan(question, question, [question])
    packet = _packet(_document("record", "notion", "업무 기록"), "근거가 있는 내용")
    assessment = EvidenceAssessment(
        status=status, answer=body, cited_document_ids=["record"],
        report=AnswerReport(
            answer_type="direct", status=status, headline="업무 기록", body=body,
            evidence_note=EvidenceNote(text="이미 본문에 있는 정보입니다.", document_ids=[source_id]),
        ),
    )
    report = validate_answer_report(plan, assessment, [packet], None)
    assert report.status == status
    assert report.evidence_note is None
    assert render_answer_text(report, {"record": 1}) == body


@pytest.mark.parametrize(("values", "second_quote", "expected"), [
    (("부품 미입고", "작업 인력 부족"), "주된 지연 사유는 작업 인력 부족입니다.", "conflict"),
    (("부품 미입고", "부품 미입고"), "주된 지연 사유는 부품 미입고입니다.", "supported"),
    (("부품 미입고", "작업 인력 부족"), "원문에 없는 설명입니다.", "supported"),
])
def test_recorded_value_difference_is_independent_of_evidence_order(
    monkeypatch, values, second_quote, expected,
):
    question = "출고 검수가 왜 밀렸어?"
    plan = QueryPlan(question, question, [question], requested_fields=["reason"])
    first_quote = "출고 검수가 늦은 이유는 부품 미입고입니다."
    packets = [
        _packet(_document("inspection-a", "notion", "시설 기록 1"), first_quote),
        _packet(_document("inspection-b", "notion", "시설 기록 2"),
                f"출고 검수의 주된 지연 사유는 {values[1]}입니다."),
    ]
    payload = {
        "status": "supported", "answer": "부품 미입고로 검수가 늦었습니다. [1]",
        "headline": "검수 지연 사유", "detail": None, "facts": [], "evidence_note": None,
        "compared_values": [{
            "item": "출고 검수 지연 사유",
            "subject_anchor": "출고 검수",
            "observations": [
                {"value": values[0], "document_id": "inspection-a", "quote": first_quote},
                {"value": values[1], "document_id": "inspection-b", "quote": second_quote},
            ],
        }],
        "next_action": None, "cited_document_ids": ["inspection-a"],
        "conflicts": [], "retry_queries": [],
    }
    monkeypatch.setattr(judgment.settings, "openai_api_key", "stub")
    monkeypatch.setattr(judgment, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_: SimpleNamespace(output_text=json.dumps(payload)))
    ))

    for evidence in (packets, list(reversed(packets))):
        outcome = assess_evidence(plan, evidence)
        report = validate_answer_report(plan, outcome, evidence, None)
        assert report.status == expected
        if expected == "conflict":
            assert report.presentation == "structured"
            assert set(outcome.cited_document_ids) == {"inspection-a", "inspection-b"}
            assert all(value in report.body for value in values)


@pytest.mark.parametrize(("titles", "anchor", "expected"), [
    (("동관 점검", "서관 점검"), "분기 기록", "insufficient"),
    (("동관 점검", "동관 점검"), "동관 점검", "conflict"),
])
def test_collection_conflict_requires_identity_of_the_individual_record(
    monkeypatch, titles, anchor, expected,
):
    question = "3월 정기 점검은?"
    start, end = planner._time_range(question)
    plan = QueryPlan(question, question, [question], cardinality="all", time_start=start, time_end=end)
    quotes = [f"분기 기록 · {title} 시각: {time}" for title, time in zip(titles, ("07:00", "20:00"))]
    packets = [
        _packet(_document(str(i), "notion", title), quote)
        for i, (title, quote) in enumerate(zip(titles, quotes))
    ]
    payload = {
        "status": "conflict", "answer": "서로 충돌합니다.", "headline": "기록 충돌",
        "detail": None, "facts": [], "evidence_note": None, "next_action": None,
        "compared_values": [{
            "item": "점검 시각", "subject_anchor": anchor,
            "observations": [
                {"value": value, "document_id": str(i), "quote": quotes[i]}
                for i, value in enumerate(("07:00", "20:00"))
            ],
        }],
        "cited_document_ids": ["0", "1"], "conflicts": ["시각 불일치"], "retry_queries": [],
    }
    monkeypatch.setattr(judgment.settings, "openai_api_key", "stub")
    monkeypatch.setattr(judgment, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_: SimpleNamespace(output_text=json.dumps(payload)))
    ))
    assessment = assess_evidence(plan, packets)
    assert assessment.status == expected
    if expected == "insufficient":
        assert "충돌" not in assessment.answer
        assert assessment.conflicts == []


def _contact_plan() -> QueryPlan:
    return QueryPlan(
        original_question="협업 담당자가 누구야?",
        resolved_question="MagicLight 협업 담당자가 누구야?",
        search_queries=["MagicLight 협업 담당자"],
        entity_terms=["magiclight"],
        requested_field="contact",
    )


def test_same_contact_role_with_different_values_is_conflict():
    notion = _packet(
        _document("notion-contact", "notion", "협업 기록"),
        "협업 제안 담당자: Brown",
    )
    gmail = _packet(
        _document("gmail-contact", "gmail", "협업 메일"),
        "협업 제안 담당자: Emily",
    )

    assessment = assess_evidence(_contact_plan(), [notion, gmail])

    assert assessment.status == "conflict"
    assert assessment.report is not None
    assert assessment.report.headline == "협업 담당자를 하나로 확정할 수 없습니다."
    assert {fact.value for fact in assessment.report.facts} == {"Brown", "Emily"}


def test_different_contact_roles_are_not_false_conflict():
    notion = _packet(
        _document("notion-owner", "notion", "내부 업무 기록"),
        "우리 팀 담당자: Brown",
    )
    gmail = _packet(
        _document("gmail-external", "gmail", "외부 제안 메일"),
        "협업 제안 담당자: Emily",
    )

    assessment = assess_evidence(_contact_plan(), [notion, gmail])

    assert assessment.status == "supported"
    assert assessment.report is not None
    assert {fact.role for fact in assessment.report.facts} == {
        "우리 팀 담당자",
        "외부 제안 담당자",
    }


def test_extractive_money_quote_is_not_a_confirmed_agreement():
    plan = QueryPlan(original_question="협업비는?", resolved_question="협업비는?",
                     search_queries=["협업비"], requested_field="money")
    packet = _packet(_document("quote", "gmail", "회신 견적"),
                     "저희 견적은 200만원입니다. 상대방의 수락은 아직 없습니다.")
    assessment = assess_evidence(plan, [packet])
    fact = assessment.report.facts[0]
    assert fact.role == "우리 측 견적"
    assert fact.fact_status == "proposed"
    assert "확정할 수는 없습니다" in assessment.answer


def test_extractive_money_record_is_recorded_even_if_it_mentions_approval():
    plan = QueryPlan(original_question="금액?", resolved_question="금액?",
                     search_queries=["금액"], requested_field="money")
    packet = _packet(_document("amount", "notion", "금액 기록"),
                     "최종 금액: 200만원, 승인 완료로 기재")
    assessment = assess_evidence(plan, [packet])
    assert assessment.report.facts[0].fact_status == "recorded"


def test_missing_price_is_not_labeled_as_a_proposed_price():
    plan = QueryPlan(original_question="협업비는?", resolved_question="협업비는?",
                     search_queries=["협업비"], requested_field="money")
    packet = _packet(_document("no-price", "gmail", "협업 제안"),
                     "협업 제안입니다. 금액은 제안하지 않았습니다.")
    assessment = assess_evidence(plan, [packet])
    assert assessment.report.facts[0].value == "미제안"
    assert assessment.report.facts[0].fact_status == "recorded"


def test_report_validator_removes_markdown_and_blocks_decisive_conflict_headline():
    document = _document("conflict-source", "notion", "충돌 근거")
    packet = _packet(document, "담당자: Brown")
    assessment = EvidenceAssessment(
        status="conflict",
        answer="**Brown**입니다.",
        cited_document_ids=[document.id],
        report=AnswerReport(
            answer_type="direct",
            status="supported",
            headline="**담당자는 Brown입니다.**",
            facts=[
                AnswerFact(
                    role="**Notion 담당자**",
                    value="**Brown**",
                    fact_status="conflicting",
                    document_ids=[document.id],
                )
            ],
        ),
    )

    report = validate_answer_report(_contact_plan(), assessment, [packet], None)

    assert report.status == "conflict"
    assert report.answer_type == "conflict"
    assert report.headline == "협업 담당자를 하나로 확정할 수 없습니다."
    assert "**" not in report.facts[0].role
    assert "**" not in report.facts[0].value


def test_report_validator_deduplicates_same_gmail_contact_and_orders_sources():
    notion_document = _document("notion-contact", "notion", "협업 기록")
    gmail_original_document = _document(
        "gmail-original", "gmail", "협업 제안 원문"
    )
    gmail_reply_document = _document("gmail-reply", "gmail", "협업 회신")
    packets = [
        _packet(notion_document, "담당자: Brown, magiclight.emily@magiclightai.com"),
        _packet(
            gmail_original_document,
            "발신자: Emily, magiclight.emily@magiclightai.com",
        ),
        _packet(
            gmail_reply_document,
            "수신자: Emily, magiclight.emily@magiclightai.com",
        ),
    ]
    assessment = EvidenceAssessment(
        status="conflict",
        answer="담당자 기록이 다릅니다.",
        cited_document_ids=[
            gmail_original_document.id,
            notion_document.id,
            gmail_reply_document.id,
        ],
        report=AnswerReport(
            answer_type="conflict",
            status="conflict",
            headline="협업 담당자를 하나로 확정할 수 없습니다.",
            facts=[
                AnswerFact(
                    role="외부 연락 담당자",
                    value="Emily, Creator Partnerships | Magiclight.ai; magiclight.emily@magiclightai.com",
                    fact_status="confirmed",
                    document_ids=[gmail_original_document.id],
                ),
                AnswerFact(
                    role="Notion 담당자",
                    value="Brown, Creator Partnerships | Magiclight.ai; magiclight.emily@magiclightai.com",
                    fact_status="conflicting",
                    document_ids=[notion_document.id],
                ),
                AnswerFact(
                    role="회신 수신자",
                    value="Emily, magiclight.emily@magiclightai.com",
                    fact_status="confirmed",
                    document_ids=[gmail_reply_document.id],
                ),
            ],
            detail="Notion의 이름과 Gmail의 이름은 다르지만 이메일 주소는 같습니다.",
            next_action="이메일 주소로 현재 담당자를 확인하세요.",
        ),
    )

    report = validate_answer_report(_contact_plan(), assessment, packets, None)

    assert len(report.facts) == 2
    assert [fact.role for fact in report.facts] == [
        "Notion · 담당자 기록",
        "Gmail · 원문 발신자",
    ]
    assert [fact.document_ids for fact in report.facts] == [
        [notion_document.id],
        [gmail_original_document.id],
    ]
    assert report.detail == (
        "Notion 기록의 Brown 표기에도 이메일 주소는 Emily의 주소로 동일하게 "
        "적혀 있어, 최신 담당자 확인이 필요합니다."
    )


def test_cause_question_keeps_adaptive_answer_instead_of_state_report():
    document = _document("rejection-reason", "notion", "시안 피드백")
    packet = _packet(document, "타깃 연령대와 맞지 않아 2차 시안이 거절되었습니다.")
    plan = QueryPlan(
        original_question="그 프로젝트가 왜 까였어?",
        resolved_question="그 프로젝트가 왜 거절되었어?",
        search_queries=["프로젝트 거절 이유"],
        entity_terms=["프로젝트"],
        requested_field="status",
        requested_fields=["reason"],
        is_state_query=False,
    )
    assessment = EvidenceAssessment(
        status="supported",
        answer="2차 시안은 타깃 연령대와 맞지 않아 거절되었습니다. [1]",
        cited_document_ids=[document.id],
        report=AnswerReport(
            answer_type="direct",
            status="supported",
            headline="2차 시안의 거절 이유를 확인했습니다.",
            body="2차 시안은 타깃 연령대와 맞지 않아 거절되었습니다. [1]",
        ),
    )
    current_state = CurrentStateAssessment(
        state="completed",
        label="완료 확인",
        evidence_status="supported",
        as_of=None,
        reason="완료 기록이 있습니다.",
        next_action=None,
    )

    report = validate_answer_report(plan, assessment, [packet], current_state)

    assert report.answer_type == "direct"
    assert report.body == "2차 시안은 타깃 연령대와 맞지 않아 거절되었습니다. [1]"
    assert "완료 확인" not in report.body


def test_supported_adaptive_body_preserves_requested_document_shape():
    document = _document("draft-source", "gmail", "협업 제안 메일")
    packet = _packet(document, "협업 조건을 확인한 뒤 회신해 주세요.")
    plan = QueryPlan(
        original_question="답장 메일 초안을 작성해줘.",
        resolved_question="협업 제안에 답장할 메일 초안을 작성해줘.",
        search_queries=["협업 제안 메일"],
    )
    body = "제목: 협업 제안 회신\n\n안녕하세요.\n제안 내용을 확인했습니다. [1]\n\n감사합니다."
    assessment = EvidenceAssessment(
        status="supported",
        answer=body,
        cited_document_ids=[document.id],
        report=AnswerReport(
            answer_type="direct",
            status="supported",
            headline="협업 제안 회신 초안입니다.",
            body=body,
        ),
    )

    report = validate_answer_report(plan, assessment, [packet], None)

    assert report.body == body


def test_compose_conflict_preserves_artifact_shape_with_compact_warning():
    document = _document("draft-conflict", "gmail", "협업 제안 메일")
    packet = _packet(document, "협업 조건은 확인이 필요합니다.")
    plan = QueryPlan(
        original_question="협업 제안 답장 템플릿을 작성해줘.",
        resolved_question="협업 제안 답장 템플릿을 작성해줘.",
        search_queries=["협업 제안 메일"],
        task_mode="compose",
        scope_mode="collection",
    )
    body = "제목: [브랜드명] 협업 제안 회신\n\n안녕하세요.\n제안 조건을 검토하겠습니다. [1]"
    assessment = EvidenceAssessment(
        status="conflict",
        answer=body,
        cited_document_ids=[document.id],
        conflicts=["사례별 협업비가 다릅니다."],
        report=AnswerReport(
            answer_type="conflict",
            status="conflict",
            headline="협업 제안 검토 회신 템플릿",
            body=body,
            detail="사례별 협업비가 다릅니다.",
        ),
    )

    report = validate_answer_report(plan, assessment, [packet], None)

    assert report.status == "conflict"
    assert report.presentation == "artifact"
    assert report.headline == "협업 제안 검토 회신 템플릿"
    assert report.body == body
    assert report.warning == (
        "일부 근거가 서로 달라 확인되지 않은 값은 초안에서 확정하지 않았습니다."
    )
