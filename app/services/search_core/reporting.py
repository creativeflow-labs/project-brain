from __future__ import annotations

import re

from app.services.search_core.types import (
    AnswerFact,
    AnswerReport,
    CurrentStateAssessment,
    EvidenceAssessment,
    EvidencePacket,
    QueryPlan,
)

def _plain_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[*_`#>]", "", value)
    cleaned = re.sub(r"\[(\d+)]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([.,!?。])", r"\1", cleaned)
    if not cleaned:
        return None
    return cleaned[:limit].rstrip()


def _fallback_headline(plan: QueryPlan, status: str) -> str:
    question = plan.resolved_question.lower()
    field = (
        "협업 담당자를"
        if plan.requested_field == "contact" and "협업" in question
        else "담당자를"
        if plan.requested_field == "contact"
        else "확정된 협업비를"
        if plan.requested_field == "money" and "협업" in question
        else "확정된 금액을"
        if plan.requested_field == "money"
        else "일정을"
        if plan.requested_field == "date"
        else "현재 상태를"
        if plan.requested_field == "status"
        else "답변을"
    )
    if status == "conflict":
        if plan.requested_field == "money":
            return (
                "현재 확정된 협업비를 확인할 수 없습니다."
                if "협업" in question
                else "현재 확정된 금액을 확인할 수 없습니다."
            )
        return f"{field} 하나로 확정할 수 없습니다."
    if status == "insufficient":
        return f"현재 근거만으로 {field} 확인할 수 없습니다."
    return f"{field} 프로젝트 근거에서 확인했습니다."


def _state_report(current_state: CurrentStateAssessment) -> AnswerReport:
    if current_state.evidence_status == "conflict":
        headline = "현재 업무 상태를 하나로 확정할 수 없습니다."
    elif current_state.evidence_status == "insufficient":
        headline = "현재 업무 상태는 확인이 필요합니다."
    else:
        headline = f"현재 업무 상태는 {current_state.label}입니다."

    facts: list[AnswerFact] = []
    for event in reversed(current_state.timeline):
        if len(facts) >= 4:
            break
        if event.decisive:
            fact_status = (
                "conflicting"
                if current_state.evidence_status == "conflict"
                else "confirmed"
            )
        elif event.document_id in current_state.stale_document_ids:
            fact_status = "outdated"
        else:
            fact_status = "recorded"
        facts.append(
            AnswerFact(
                role=f"{event.source_type} 상태 기록",
                value=event.label,
                fact_status=fact_status,
                document_ids=[event.document_id],
            )
        )
    return AnswerReport(
        answer_type="state",
        status=current_state.evidence_status,
        headline=headline,
        facts=facts,
        detail=current_state.reason,
        next_action=current_state.next_action,
    )


def validate_answer_report(
    plan: QueryPlan,
    assessment: EvidenceAssessment,
    packets: list[EvidencePacket],
    current_state: CurrentStateAssessment | None,
) -> AnswerReport:
    if current_state is not None:
        candidate = _state_report(current_state)
    elif assessment.report is not None:
        candidate = assessment.report
    else:
        candidate = AnswerReport(
            answer_type=(
                "conflict"
                if assessment.status == "conflict"
                else "insufficient"
                if assessment.status == "insufficient"
                else "direct"
            ),
            status=assessment.status,
            headline=_fallback_headline(plan, assessment.status),
            detail=assessment.answer,
        )

    valid_ids = {packet.document.id for packet in packets}
    facts: list[AnswerFact] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for fact in candidate.facts:
        role = _plain_text(fact.role, 70)
        value = _plain_text(fact.value, 180)
        document_ids = list(
            dict.fromkeys(
                document_id
                for document_id in fact.document_ids
                if document_id in valid_ids
            )
        )
        if not role or not value:
            continue
        if not document_ids and fact.fact_status != "unverified":
            continue
        key = (role, value, tuple(document_ids))
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            AnswerFact(
                role=role,
                value=value,
                fact_status=fact.fact_status,
                document_ids=document_ids,
            )
        )
        if len(facts) >= 4:
            break

    status = assessment.status
    if current_state is not None:
        status = current_state.evidence_status
    answer_type = (
        "state"
        if current_state is not None
        else "conflict"
        if status == "conflict"
        else "insufficient"
        if status == "insufficient"
        else "direct"
    )

    headline = _plain_text(candidate.headline, 100)
    if status == "conflict" and plan.requested_field in {
        "contact",
        "money",
        "date",
    }:
        headline = _fallback_headline(plan, status)
    elif status == "conflict" and not any(
        marker in (headline or "")
        for marker in ("확정할 수 없", "확인할 수 없", "충돌", "서로 다")
    ):
        # 안전 판정이 결론 문장에서 다시 단정형으로 바뀌지 않게 고정합니다.
        headline = _fallback_headline(plan, status)
    elif status == "insufficient" and not any(
        marker in (headline or "")
        for marker in ("확인할 수 없", "확인 필요", "근거 부족")
    ):
        headline = _fallback_headline(plan, status)
    elif not headline:
        headline = _fallback_headline(plan, status)

    detail = _plain_text(candidate.detail, 320)
    next_action = _plain_text(candidate.next_action, 180)
    if status != "supported" and not next_action:
        next_action = "최신 원문과 결정권자의 확인 기록을 추가로 확인하세요."

    return AnswerReport(
        answer_type=answer_type,
        status=status,
        headline=headline,
        facts=facts,
        detail=detail,
        next_action=next_action,
    )


def render_answer_text(
    report: AnswerReport, document_positions: dict[str, int] | None = None
) -> str:
    lines = [report.headline]
    for fact in report.facts:
        references = ""
        if document_positions:
            positions = list(
                dict.fromkeys(
                    document_positions[document_id]
                    for document_id in fact.document_ids
                    if document_id in document_positions
                )
            )
            references = "".join(f"[{position}]" for position in positions)
        lines.append(f"- {fact.role}: {fact.value}{references}")
    if report.detail and report.detail != report.headline:
        lines.append(report.detail)
    if report.next_action:
        lines.append(f"다음 확인: {report.next_action}")
    return "\n".join(lines)


def answer_report_to_dict(report: AnswerReport) -> dict:
    return {
        "type": report.answer_type,
        "status": report.status,
        "headline": report.headline,
        "facts": [
            {
                "role": fact.role,
                "value": fact.value,
                "fact_status": fact.fact_status,
                "citation_document_ids": fact.document_ids,
            }
            for fact in report.facts
        ],
        "detail": report.detail,
        "next_action": report.next_action,
    }


def report_document_ids(report: AnswerReport) -> list[str]:
    return list(
        dict.fromkeys(
            document_id
            for fact in report.facts
            for document_id in fact.document_ids
        )
    )
