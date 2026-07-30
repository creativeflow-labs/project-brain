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


def _fact_source_types(
    document_ids: list[str], packets_by_id: dict[str, EvidencePacket]
) -> list[str]:
    return list(
        dict.fromkeys(
            packets_by_id[document_id].document.source_type
            for document_id in document_ids
            if document_id in packets_by_id
        )
    )


def _fact_role(
    plan: QueryPlan,
    role: str,
    source_types: list[str],
) -> str:
    if len(source_types) != 1:
        return role
    source = source_types[0]
    lowered = role.lower()
    source_label = {
        "notion": "Notion",
        "gmail": "Gmail",
        "upload": "직접 업로드",
    }.get(source, source)
    if plan.requested_field == "contact":
        if source == "notion":
            return "Notion · 담당자 기록"
        if source == "gmail" and "수신" in lowered:
            return "Gmail · 회신 수신자"
        if source == "gmail":
            return "Gmail · 원문 발신자"
        return f"{source_label} · 담당자 기록"
    if plan.requested_field == "money":
        if source == "notion":
            return "Notion · 금액 기록"
        if source == "gmail" and any(
            marker in lowered for marker in ("우리", "견적", "회신")
        ):
            return "Gmail · 우리 측 견적"
        if source == "gmail":
            return "Gmail · 원문 금액 기록"
        return f"{source_label} · 금액 기록"
    return role


def _contact_conflict_detail(facts: list[AnswerFact]) -> str | None:
    notion_fact = next(
        (fact for fact in facts if fact.role == "Notion · 담당자 기록"), None
    )
    gmail_fact = next(
        (fact for fact in facts if fact.role == "Gmail · 원문 발신자"), None
    )
    if notion_fact is None or gmail_fact is None:
        return None
    notion_emails = set(
        re.findall(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            notion_fact.value,
            flags=re.IGNORECASE,
        )
    )
    gmail_emails = set(
        re.findall(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            gmail_fact.value,
            flags=re.IGNORECASE,
        )
    )
    if not notion_emails.intersection(gmail_emails):
        return None
    notion_match = re.match(r"([가-힣]{2,4}|[A-Za-z][A-Za-z'.-]*)", notion_fact.value)
    gmail_match = re.match(r"([가-힣]{2,4}|[A-Za-z][A-Za-z'.-]*)", gmail_fact.value)
    notion_name = notion_match.group(1) if notion_match else ""
    gmail_name = gmail_match.group(1) if gmail_match else ""
    if not notion_name or not gmail_name or notion_name.lower() == gmail_name.lower():
        return None
    return (
        f"Notion 기록의 {notion_name} 표기에도 이메일 주소는 {gmail_name}의 주소로 "
        "동일하게 적혀 있어, 최신 담당자 확인이 필요합니다."
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


def _body_text(value: str | None, limit: int = 6000) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    return cleaned[:limit].rstrip() or None


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
        body=current_state.reason,
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
    if current_state is not None and plan.is_state_query:
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
            body=assessment.answer,
            detail=assessment.answer,
        )

    valid_ids = {packet.document.id for packet in packets}
    packets_by_id = {packet.document.id: packet for packet in packets}
    facts: list[AnswerFact] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    seen_contact_sources: set[str] = set()
    candidate_facts = list(candidate.facts)
    if plan.requested_field == "contact":
        contact_source_order = {"notion": 0, "gmail": 1, "upload": 2}

        def contact_fact_order(fact: AnswerFact) -> tuple[int, int]:
            sources = _fact_source_types(fact.document_ids, packets_by_id)
            role = fact.role.lower()
            reply_penalty = int(any(marker in role for marker in ("수신", "회신")))
            return (
                contact_source_order.get(sources[0], 9) if sources else 9,
                reply_penalty,
            )

        candidate_facts.sort(key=contact_fact_order)

    for fact in candidate_facts:
        raw_role = _plain_text(fact.role, 70)
        value = _plain_text(fact.value, 180)
        document_ids = list(
            dict.fromkeys(
                document_id
                for document_id in fact.document_ids
                if document_id in valid_ids
            )
        )
        source_types = _fact_source_types(document_ids, packets_by_id)
        role = _fact_role(plan, raw_role or "기록", source_types)
        if not role or not value:
            continue
        if not document_ids and fact.fact_status != "unverified":
            continue
        key = (role, value, tuple(document_ids))
        if key in seen:
            continue
        if plan.requested_field == "contact":
            source_key = source_types[0] if len(source_types) == 1 else "mixed"
            if source_key in seen_contact_sources:
                continue
            seen_contact_sources.add(source_key)
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

    if plan.requested_field in {"contact", "money"}:
        source_order = {"notion": 0, "gmail": 1, "upload": 2}

        def fact_order(fact: AnswerFact) -> tuple[int, str]:
            sources = _fact_source_types(fact.document_ids, packets_by_id)
            return (source_order.get(sources[0], 9) if sources else 9, fact.role)

        facts.sort(key=fact_order)

    status = assessment.status
    if current_state is not None and plan.is_state_query:
        status = current_state.evidence_status
    answer_type = (
        "state"
        if current_state is not None and plan.is_state_query
        else "conflict"
        if status == "conflict"
        else "insufficient"
        if status == "insufficient"
        else "direct"
    )

    headline = _plain_text(candidate.headline, 100)
    if (
        plan.task_mode != "compose"
        and status == "conflict"
        and plan.requested_field in {"contact", "money", "date"}
    ):
        headline = _fallback_headline(plan, status)
    elif plan.task_mode != "compose" and status == "conflict" and not any(
        marker in (headline or "")
        for marker in ("확정할 수 없", "확인할 수 없", "충돌", "서로 다")
    ):
        # 안전 판정이 결론 문장에서 다시 단정형으로 바뀌지 않게 고정합니다.
        headline = _fallback_headline(plan, status)
    elif plan.task_mode != "compose" and status == "insufficient" and not any(
        marker in (headline or "")
        for marker in ("확인할 수 없", "확인 필요", "근거 부족")
    ):
        headline = _fallback_headline(plan, status)
    elif not headline:
        headline = _fallback_headline(plan, status)

    detail = _plain_text(candidate.detail, 320)
    if status == "conflict" and plan.requested_field == "contact":
        detail = _contact_conflict_detail(facts) or detail
    next_action = _plain_text(candidate.next_action, 180)
    if status != "supported" and not next_action:
        next_action = "최신 원문과 결정권자의 확인 기록을 추가로 확인하세요."

    body = _body_text(candidate.body or assessment.answer)
    if not body:
        body = detail or headline

    presentation = (
        "artifact"
        if plan.task_mode == "compose"
        else "structured"
        if status != "supported" or answer_type == "state"
        else "adaptive"
    )
    warning = None
    if plan.task_mode == "compose" and status == "conflict":
        warning = (
            "일부 근거가 서로 달라 확인되지 않은 값은 초안에서 확정하지 않았습니다."
        )
    elif plan.task_mode == "compose" and status == "insufficient":
        warning = (
            "직접 확인할 근거가 부족한 항목은 초안에서 자리표시자로 남겼습니다."
        )

    return AnswerReport(
        answer_type=answer_type,
        status=status,
        headline=headline,
        body=body,
        facts=facts,
        detail=detail,
        next_action=next_action,
        presentation=presentation,
        warning=warning,
    )


def render_answer_text(
    report: AnswerReport, document_positions: dict[str, int] | None = None
) -> str:
    if report.body:
        return report.body
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
        "body": report.body,
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
        "presentation": report.presentation,
        "warning": report.warning,
    }


def report_document_ids(report: AnswerReport) -> list[str]:
    return list(
        dict.fromkeys(
            document_id
            for fact in report.facts
            for document_id in fact.document_ids
        )
    )
