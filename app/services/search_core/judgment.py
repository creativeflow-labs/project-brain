from __future__ import annotations

import json
import re

from openai import OpenAI

from app.config import get_settings
from app.services.search_core.types import (
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
                values.add(cleaned.lower())
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
    source_types = {
        packet.document.source_type for packet, _ in values_by_document
    }
    if len(all_values) > 1 and len(source_types) > 1:
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
        )

    if values_by_document:
        rendered = ", ".join(sorted(all_values))
        return EvidenceAssessment(
            status="supported",
            answer=f"프로젝트 근거에서 확인된 값은 {rendered}입니다.",
            cited_document_ids=[
                packet.document.id for packet, _ in values_by_document
            ],
        )

    top = packets[0]
    return EvidenceAssessment(
        status="supported",
        answer=(
            "OpenAI API 키가 없어 추출형 답변으로 표시합니다.\n\n"
            f"가장 관련성이 높은 근거는 다음과 같습니다.\n“{top.context_text[:900]}”"
        ),
        cited_document_ids=[top.document.id],
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
            "cited_document_ids",
            "conflicts",
            "retry_queries",
        ],
        "additionalProperties": False,
    }
    try:
        response = OpenAI(api_key=settings.openai_api_key).responses.create(
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
                "insufficient다. 답변의 핵심 주장 뒤에는 입력 "
                "배열 순서에 따른 [1] 형식의 인용을 붙인다. 충돌·부족 시 추가 검색어를 제안한다."
            ),
            input=json.dumps(
                {
                    "question": plan.resolved_question,
                    "requested_field": plan.requested_field,
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
        assessment = EvidenceAssessment(
            status=payload["status"],
            answer=payload["answer"].strip(),
            cited_document_ids=cited_ids,
            conflicts=payload["conflicts"],
            retry_queries=[
                query.strip() for query in payload["retry_queries"] if query.strip()
            ],
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
