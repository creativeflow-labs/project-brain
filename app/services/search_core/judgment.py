from __future__ import annotations

import json
import re

from app.config import get_settings
from app.services.openai_client import get_openai_client
from app.services.search_core.types import (
    AnswerFact,
    AnswerReport,
    CurrentStateAssessment,
    EvidenceAssessment,
    EvidencePacket,
    QueryPlan,
)
from app.services.search_core.state_engine import current_state_to_dict

settings = get_settings()


def _contact_values(text: str) -> set[str]:
    values: set[str] = set()
    patterns = (
        r"(?:담당자|연락처)\s*[:：은는]?\s*\**([A-Za-z가-힣][A-Za-z가-힣 ._-]{1,30})",
        r"\bthis is\s+([A-Z][A-Za-z._-]{1,30})",
        r"\bfrom\s+([A-Z][A-Za-z._-]{1,30})",
    )
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            cleaned = re.split(r"[,<(\n]", match.strip())[0].strip(" *.")
            if cleaned:
                values.add(cleaned)
    return values


def _money_values(text: str) -> set[str]:
    values = {
        re.sub(r"\s+", "", value.lower())
        for value in re.findall(
            r"\d+(?:[.,]\d+)?\s*(?:만\s*원|만원|억\s*원|억원|원|million|usd|krw)",
            text,
            flags=re.IGNORECASE,
        )
    }
    lowered = text.lower()
    no_offer_patterns = (
        "제안하지 않았",
        "제안이 없",
        "금액은 없",
        "비용은 없",
        "not proposed",
        "no fee",
        "no budget",
        "provide your rate",
    )
    if any(pattern in lowered for pattern in no_offer_patterns):
        values.add("미제안")
    return values


def _contact_role(packet: EvidencePacket, text: str) -> str:
    lowered = text.lower()
    if any(value in lowered for value in ("최종 승인자", "승인 담당", "approver")):
        return "최종 승인자"
    if any(value in lowered for value in ("내부 담당자", "우리 팀 담당자", "사내 담당자")):
        return "우리 팀 담당자"
    if any(
        value in lowered
        for value in ("제안 담당자", "파트너십", "partnership", "this is", "from:")
    ):
        return "외부 제안 담당자"
    return f"{packet.document.source_type} 담당자 기록"


def _money_role(packet: EvidencePacket, text: str) -> str:
    lowered = text.lower()
    approval_is_uncertain = re.search(
        r"(?:수락|승인|확정)[^\n.!?]{0,18}(?:않|없|미정|대기|필요|못|예정|요청)"
        r"|미(?:확정|승인|수락)",
        lowered,
    )
    if not approval_is_uncertain and any(
        value in lowered for value in ("최종 금액", "확정 금액", "수락", "승인")
    ):
        return "최종 승인 금액"
    if any(
        value in lowered
        for value in ("저희 견적", "우리 측 견적", "our rate", "our quote")
    ):
        return "우리 측 견적"
    if any(
        value in lowered
        for value in (
            "제안하지 않았",
            "제안이 없",
            "provide your rate",
            "협업 제안",
            "collaboration fee",
        )
    ):
        return "상대방 제안 금액"
    return f"{packet.document.source_type} 금액 기록"


def _report_fact(
    plan: QueryPlan, packet: EvidencePacket, value: str, status: str
) -> AnswerFact:
    role = (
        _contact_role(packet, packet.context_text)
        if plan.requested_field == "contact"
        else _money_role(packet, packet.context_text)
        if plan.requested_field == "money"
        else f"{packet.document.source_type} 기록"
    )
    return AnswerFact(
        role=role,
        value=value,
        fact_status=status,
        document_ids=[packet.document.id],
    )


def _report_headline(plan: QueryPlan, status: str, value: str | None = None) -> str:
    if status == "conflict":
        return {
            "contact": "협업 담당자를 하나로 확정할 수 없습니다.",
            "money": "현재 확정된 협업비를 확인할 수 없습니다.",
            "date": "일정을 하나로 확정할 수 없습니다.",
        }.get(plan.requested_field, "기록이 충돌해 답변을 하나로 확정할 수 없습니다.")
    if status == "insufficient":
        return {
            "contact": "현재 근거만으로 담당자를 확인할 수 없습니다.",
            "money": "현재 근거만으로 확정된 협업비를 확인할 수 없습니다.",
            "date": "현재 근거만으로 일정을 확인할 수 없습니다.",
        }.get(plan.requested_field, "현재 근거만으로 답변을 확인할 수 없습니다.")
    if value:
        return {
            "contact": f"확인된 담당자는 {value}입니다.",
            "money": f"확인된 금액은 {value}입니다.",
            "date": f"확인된 일정은 {value}입니다.",
        }.get(plan.requested_field, f"확인된 값은 {value}입니다.")
    return "프로젝트 근거에서 답변을 확인했습니다."


def _fallback_assessment(
    plan: QueryPlan,
    packets: list[EvidencePacket],
    current_state: CurrentStateAssessment | None = None,
) -> EvidenceAssessment:
    if current_state is not None:
        cited_ids = list(
            dict.fromkeys(
                [
                    *current_state.decisive_document_ids,
                    *current_state.stale_document_ids,
                ]
            )
        )
        positions = {
            packet.document.id: index
            for index, packet in enumerate(packets, start=1)
        }
        decisive_refs = "".join(
            f"[{positions[document_id]}]"
            for document_id in current_state.decisive_document_ids
            if document_id in positions
        )
        stale_refs = "".join(
            f"[{positions[document_id]}]"
            for document_id in current_state.stale_document_ids
            if document_id in positions
        )
        if current_state.evidence_status == "insufficient":
            answer = (
                f"현재 상태는 **{current_state.label}**입니다. {current_state.reason} "
                f"{current_state.next_action or ''}"
            ).strip()
        elif current_state.evidence_status == "conflict":
            answer = (
                f"현재 상태를 하나로 확정할 수 없습니다. {current_state.reason}"
                f"{decisive_refs} {current_state.next_action or ''}"
            ).strip()
        else:
            answer = (
                f"현재 확인된 상태는 **{current_state.label}**입니다. "
                f"{current_state.reason}{decisive_refs}"
            )
            if current_state.stale_document_ids:
                answer += (
                    f" 서로 다른 이전 상태 기록도 남아 있으므로 현재 판정에서는 과거 근거로 분리했습니다."
                    f"{stale_refs}"
                )
            if current_state.next_action:
                answer += f" 다음 확인 사항은 '{current_state.next_action}'입니다."
        return EvidenceAssessment(
            status=current_state.evidence_status,
            answer=answer,
            cited_document_ids=cited_ids,
            conflicts=current_state.conflicts,
            retry_queries=(
                [f"{' '.join(plan.entity_terms[:5])} 최신 상태 승인 수정 완료"]
                if current_state.evidence_status != "supported"
                else []
            ),
        )

    if not packets:
        return EvidenceAssessment(
            status="insufficient",
            answer="질문에 답할 수 있는 프로젝트 근거를 찾지 못했습니다.",
            cited_document_ids=[],
            report=AnswerReport(
                answer_type="insufficient",
                status="insufficient",
                headline=_report_headline(plan, "insufficient"),
                body=(
                    "현재 연결되고 색인된 프로젝트 자료에서 질문과 직접 관련된 "
                    "근거를 찾지 못했습니다. 자료가 존재하지 않는다고 단정할 수는 없습니다."
                ),
                next_action="관련 원문을 연결하거나 검색 범위를 확인하세요.",
            ),
        )

    extractor = {
        "contact": _contact_values,
        "money": _money_values,
    }.get(plan.requested_field)
    values_by_document: list[tuple[EvidencePacket, set[str]]] = []
    if extractor:
        values_by_document = [
            (packet, extractor(packet.context_text)) for packet in packets
        ]
        values_by_document = [
            (packet, values) for packet, values in values_by_document if values
        ]

    all_values = set().union(
        *(values for _, values in values_by_document)
    ) if values_by_document else set()
    report_facts = [
        _report_fact(plan, packet, value, "recorded")
        for packet, values in values_by_document
        for value in sorted(values)
    ]
    values_by_role: dict[str, set[str]] = {}
    sources_by_role: dict[str, set[str]] = {}
    for fact, (packet, _) in zip(
        report_facts,
        [
            (packet, value)
            for packet, values in values_by_document
            for value in sorted(values)
        ],
        strict=True,
    ):
        values_by_role.setdefault(fact.role, set()).add(fact.value.lower())
        sources_by_role.setdefault(fact.role, set()).add(packet.document.source_type)
    conflicting_roles = {
        role
        for role, values in values_by_role.items()
        if len(values) > 1 and len(sources_by_role.get(role, set())) > 1
    }
    if conflicting_roles:
        for fact in report_facts:
            fact.fact_status = (
                "conflicting" if fact.role in conflicting_roles else "recorded"
            )
        details = "; ".join(
            f"{packet.document.source_type} '{packet.document.title}': "
            f"{', '.join(sorted(values))}"
            for packet, values in values_by_document
        )
        return EvidenceAssessment(
            status="conflict",
            answer=(
                "서로 다른 출처에서 같은 항목에 대한 값이 다르게 확인되어 하나로 확정할 수 없습니다. "
                f"{details}. 원문과 최신 기록을 함께 확인해 주세요."
            ),
            cited_document_ids=[
                packet.document.id for packet, _ in values_by_document
            ],
            conflicts=[details],
            retry_queries=[
                f"{' '.join(plan.entity_terms[:5])} {plan.requested_field} 최신 변경 확정"
            ],
            report=AnswerReport(
                answer_type="conflict",
                status="conflict",
                headline=_report_headline(plan, "conflict"),
                body=(
                    "서로 다른 출처에 같은 항목의 값이 다르게 기록되어 있습니다. "
                    "아래 기록을 함께 확인해 주세요."
                ),
                facts=report_facts,
                detail="같은 역할과 항목에 서로 다른 값이 기록되어 있습니다.",
                next_action="최신 원문과 해당 항목의 결정권자를 확인하세요.",
            ),
        )

    if values_by_document:
        rendered = ", ".join(sorted(all_values))
        for fact in report_facts:
            # Regex extraction confirms a record, not a commercial agreement.
            fact.fact_status = (
                "proposed" if plan.requested_field == "money"
                and fact.role in {"우리 측 견적", "상대방 제안 금액"}
                else "recorded" if plan.requested_field == "money"
                else "confirmed"
            )
        role_count = len({fact.role for fact in report_facts})
        headline = (
            "담당자 정보를 역할별로 확인했습니다."
            if plan.requested_field == "contact" and role_count > 1
            else "금액 정보를 역할별로 확인했습니다."
            if plan.requested_field == "money" and role_count > 1
            else _report_headline(plan, "supported", rendered)
        )
        body = f"프로젝트 근거에서 확인된 값은 {rendered}입니다."
        if plan.requested_field == "money":
            headline = "원문에 기록된 금액을 확인했습니다."
            body = (
                f"원문에 기록된 금액은 {rendered}입니다. "
                "이 추출 결과만으로 상대방의 수락이나 최종 합의를 확정할 수는 없습니다."
            )
        return EvidenceAssessment(
            status="supported",
            answer=body,
            cited_document_ids=[
                packet.document.id for packet, _ in values_by_document
            ],
            report=AnswerReport(
                answer_type="direct",
                status="supported",
                headline=headline,
                body=body,
                facts=report_facts,
            ),
        )

    top = packets[0]
    return EvidenceAssessment(
        status="supported",
        answer=(
            "OpenAI API 키가 없어 추출형 답변으로 표시합니다.\n\n"
            f"가장 관련성이 높은 근거는 다음과 같습니다.\n“{top.context_text[:900]}”"
        ),
        cited_document_ids=[top.document.id],
        report=AnswerReport(
            answer_type="direct",
            status="supported",
            headline="가장 관련성이 높은 프로젝트 근거를 찾았습니다.",
            body=(
                "OpenAI API 키가 없어 추출형 답변으로 표시합니다.\n\n"
                f"{top.context_text[:900]}"
            ),
            facts=[
                AnswerFact(
                    role=f"{top.document.source_type} 근거",
                    value=top.context_text[:180],
                    fact_status="recorded",
                    document_ids=[top.document.id],
                )
            ],
        ),
    )


def _llm_assessment(
    plan: QueryPlan,
    packets: list[EvidencePacket],
    current_state: CurrentStateAssessment | None = None,
) -> EvidenceAssessment | None:
    if not settings.openai_api_key or not packets:
        return None
    evidence = [
        {
            "document_id": packet.document.id,
            "source_type": packet.document.source_type,
            "title": packet.document.title,
            "source_url": packet.document.source_url,
            "content": packet.context_text,
        }
        for packet in packets
    ]
    schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["supported", "conflict", "insufficient"],
            },
            "answer": {"type": "string"},
            "headline": {"type": "string"},
            "detail": {"type": ["string", "null"]},
            "facts": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "value": {"type": "string"},
                        "fact_status": {
                            "type": "string",
                            "enum": [
                                "confirmed",
                                "conflicting",
                                "proposed",
                                "recorded",
                                "unverified",
                                "outdated",
                            ],
                        },
                        "citation_document_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "role",
                        "value",
                        "fact_status",
                        "citation_document_ids",
                    ],
                    "additionalProperties": False,
                },
            },
            "next_action": {"type": ["string", "null"]},
            "cited_document_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "conflicts": {"type": "array", "items": {"type": "string"}},
            "retry_queries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
            },
        },
        "required": [
            "status",
            "answer",
            "headline",
            "detail",
            "facts",
            "next_action",
            "cited_document_ids",
            "conflicts",
            "retry_queries",
        ],
        "additionalProperties": False,
    }
    try:
        client = get_openai_client()
        if client is None:
            return None
        response = client.responses.create(
            model=settings.openai_chat_model,
            reasoning={"effort": "none"},
            store=False,
            instructions=(
                "당신은 팀 업무 사실 판정기다. 오직 제공된 원문 근거만 사용한다. 대화의 이전 AI 답변은 "
                "절대 사실 근거로 취급하지 않는다. 같은 사건·같은 항목의 값이 다르면 conflict로 판정하고 "
                "어느 하나를 임의로 선택하지 않는다. Gmail이 항상 우선이거나 Notion이 항상 우선인 규칙은 "
                "없다. 외부 제안, 내부 메모, 발신 답장처럼 기록의 역할을 구분한다. '금액 미제안', "
                "'검색에서 금액을 못 찾음', '사용자가 답장으로 제시한 금액'을 서로 다른 사실로 다룬다. "
                "상태 질문에는 함께 제공된 current_state 판정을 우선 적용하고, 완료 주장과 완료 승인을 "
                "구분한다. 완료 뒤에 더 최신 수정 요청이 있으면 업무는 다시 열린 상태다. 문서의 수정 "
                "시각과 본문 사건 시각을 혼동하지 않는다. 근거가 직접 지지하면 supported, 부족하면 "
                "insufficient다. answer는 사용자의 원래 요청을 직접 수행하는 최종 답변이다. 질문이 설명을 "
                "요구하면 자연스러운 설명문으로, 전체 목록을 요구하면 근거 안에서 해당 항목을 빠짐없이 "
                "목록으로, 이메일이나 문서 초안을 요구하면 실제로 사용할 수 있는 초안 형태로 작성한다. "
                "task_mode가 compose이면 검증 보고서보다 사용자가 바로 쓸 결과물을 중심으로 작성한다. "
                "scope_mode가 collection이면 특정 회사 하나를 임의로 대표 사례로 선택하지 말고 여러 근거의 "
                "공통 패턴을 종합해 재사용 가능한 템플릿을 만든다. 이때 서로 다른 회사의 조건 차이는 충돌이 "
                "아니며, 회사마다 달라지는 값은 자리표시자로 둔다. "
                "모든 답변을 검증 보고서 형식으로 바꾸지 않는다. headline은 상태 알림이 필요할 때 사용할 "
                "짧은 요약이다. facts는 충돌 비교나 상태 판정처럼 구조화가 실제로 도움이 될 때만 최대 "
                "4개를 넣고, 일반 답변·설명·목록·초안에서는 비워도 된다. 담당자는 외부 연락 담당자, 우리 팀 "
                "담당자, 승인자를 구분하고, 금액은 상대방 제안, 우리 측 견적, 내부 메모, 최종 승인 "
                "금액을 구분한다. 역할이 다른 값은 충돌이 아니다. 제안·견적만 있고 수락 근거가 없으면 "
                "확정 금액으로 쓰지 않는다. conflict이면 충돌 사실을 먼저 알린 뒤에도 사용자가 요청한 "
                "작업은 가능한 범위에서 계속 수행하고, 확인되지 않은 값은 자리표시자로 둔다. insufficient이면 "
                "현재 검색에서 찾지 못한 것과 실제로 존재하지 않는 것을 구분한다. 답변의 핵심 주장 뒤에는 "
                "입력 배열 순서에 따른 [1] 형식의 인용을 붙인다. 충돌·부족 시에만 필요한 추가 확인을 "
                "next_action과 retry_queries에 제안한다."
            ),
            input=json.dumps(
                {
                    "question": plan.resolved_question,
                    "task_mode": plan.task_mode,
                    "scope_mode": plan.scope_mode,
                    "locked_entity_terms": plan.locked_entity_terms,
                    "requested_field": plan.requested_field,
                    "requested_fields": plan.requested_fields,
                    "cardinality": plan.cardinality,
                    "time_range": (
                        {
                            "start": plan.time_start.isoformat(),
                            "end": plan.time_end.isoformat(),
                            "field": plan.time_field,
                        }
                        if plan.time_start is not None and plan.time_end is not None
                        else None
                    ),
                    "current_state": (
                        current_state_to_dict(current_state)
                        if current_state is not None
                        else None
                    ),
                    "evidence": evidence,
                },
                ensure_ascii=False,
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "project_brain_evidence_assessment",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
        payload = json.loads(response.output_text)
        valid_ids = {packet.document.id for packet in packets}
        cited_ids = [
            document_id
            for document_id in payload["cited_document_ids"]
            if document_id in valid_ids
        ]
        facts = [
            AnswerFact(
                role=item["role"].strip(),
                value=item["value"].strip(),
                fact_status=item["fact_status"],
                document_ids=[
                    document_id
                    for document_id in item["citation_document_ids"]
                    if document_id in valid_ids
                ],
            )
            for item in payload["facts"]
            if item["role"].strip() and item["value"].strip()
        ]
        cited_ids = list(
            dict.fromkeys(
                [
                    *cited_ids,
                    *(document_id for fact in facts for document_id in fact.document_ids),
                ]
            )
        )
        assessment = EvidenceAssessment(
            status=payload["status"],
            answer=payload["answer"].strip(),
            cited_document_ids=cited_ids,
            conflicts=payload["conflicts"],
            retry_queries=[
                query.strip() for query in payload["retry_queries"] if query.strip()
            ],
            report=AnswerReport(
                answer_type=(
                    "conflict"
                    if payload["status"] == "conflict"
                    else "insufficient"
                    if payload["status"] == "insufficient"
                    else "direct"
                ),
                status=payload["status"],
                headline=payload["headline"].strip(),
                body=payload["answer"].strip(),
                facts=facts,
                detail=payload["detail"].strip() if payload["detail"] else None,
                next_action=(
                    payload["next_action"].strip()
                    if payload["next_action"]
                    else None
                ),
            ),
        )
        if current_state is not None:
            assessment.status = current_state.evidence_status
            assessment.cited_document_ids = list(
                dict.fromkeys(
                    [
                        *assessment.cited_document_ids,
                        *current_state.decisive_document_ids,
                        *current_state.stale_document_ids,
                    ]
                )
            )
            assessment.conflicts = list(
                dict.fromkeys([*assessment.conflicts, *current_state.conflicts])
            )
        return assessment
    except Exception:
        return None


def assess_evidence(
    plan: QueryPlan,
    packets: list[EvidencePacket],
    current_state: CurrentStateAssessment | None = None,
) -> EvidenceAssessment:
    # 상태 질문은 규칙 엔진이 계산한 정규 상태를 그대로 답변 계약으로 사용합니다.
    # 자유 생성 답변이 판정 결과와 반대되는 상태명을 다시 만드는 것을 막기 위한
    # v0.4 알파의 안전 경계이며, 설명 생성은 이후 검증기를 통과한 경우에만 허용합니다.
    if current_state is not None:
        return _fallback_assessment(plan, packets, current_state)
    return _llm_assessment(plan, packets, current_state) or _fallback_assessment(
        plan, packets, current_state
    )
