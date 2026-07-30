from app.models import Document
from app.services.search_core.judgment import assess_evidence
from app.services.search_core.reporting import validate_answer_report
from app.services.search_core.types import (
    AnswerFact,
    AnswerReport,
    CurrentStateAssessment,
    EvidenceAssessment,
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
