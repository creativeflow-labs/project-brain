from app.models import Document
from app.services.search_core.judgment import assess_evidence
from app.services.search_core.reporting import validate_answer_report
from app.services.search_core.types import (
    AnswerFact,
    AnswerReport,
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
