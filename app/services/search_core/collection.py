from __future__ import annotations

from datetime import datetime

from app.services.record_metadata import (
    SEOUL,
    extract_record_title,
    structured_record_date,
)
from app.services.search_core.types import (
    AnswerReport,
    EvidenceAssessment,
    EvidencePacket,
    QueryPlan,
)


def assess_scoped_collection(
    plan: QueryPlan, packets: list[EvidencePacket]
) -> EvidenceAssessment | None:
    if (
        plan.retrieval_mode != "scoped"
        or plan.cardinality != "all"
        or not plan.record_type
    ):
        return None
    operation = plan.collection_operation
    if operation not in {"count", "list"}:
        return None
    undated_note = (
        f"\n\n날짜가 확인되지 않아 기간 포함 여부를 판단할 수 없는 색인 기록 "
        f"{plan.collection_undated_count}건은 집계에서 제외했습니다."
        if plan.collection_undated_count else ""
    )
    if operation == "count":
        # This count was taken over matching documents BEFORE evidence limits.
        if plan.collection_match_count is None:
            return None
        count = plan.collection_match_count
        references = list(range(1, min(len(packets), 3) + 1))
        citations = "".join(f" [{index}]" for index in references)
        body = f"현재 색인된 해당 유형의 기록 중 질문 조건에 맞는 기록은 총 {count}건입니다.{citations}"
        if plan.collection_title_terms:
            body += "\n\n제목 포함 조건: " + ", ".join(plan.collection_title_terms)
        body += "\n\n색인된 자료 기준이며, 원본 서비스 전체 건수를 의미하지 않습니다." + undated_note
        return EvidenceAssessment(
            status="supported",
            answer=body,
            cited_document_ids=[packet.document.id for packet in packets[:3]],
            report=AnswerReport(
                answer_type="direct",
                status="supported",
                headline=f"질문 조건에 맞는 기록 {count}건을 확인했습니다.",
                body=body,
            ),
        )
    positions = {
        packet.document.id: index for index, packet in enumerate(packets, start=1)
    }
    records: list[tuple[datetime | None, str, EvidencePacket]] = []
    for packet in packets:
        metadata = packet.document.metadata_json or {}
        recorded_at = structured_record_date(metadata, plan.time_field)
        record_title = extract_record_title(
            packet.document.title, packet.context_text, metadata
        )
        records.append((recorded_at, record_title, packet))

    if not records:
        return None
    records.sort(key=lambda item: (item[0] is None, item[0] or datetime.max.replace(tzinfo=SEOUL), item[1]))
    lines = [
        f"현재 색인 기준으로 조건에 맞는 기록은 총 {len(records)}건입니다.",
        "",
    ]
    for recorded_at, title, packet in records:
        reference = positions[packet.document.id]
        prefix = f"{recorded_at.astimezone(SEOUL):%Y-%m-%d %H:%M} | " if recorded_at else ""
        lines.append(f"- {prefix}{title} [{reference}]")
    lines.extend(
        [
            "",
            "색인된 자료 기준이며, 원본 서비스의 공개·발행 여부를 별도로 확인한 것은 아닙니다.",
        ]
    )
    body = "\n".join(lines) + undated_note
    if plan.collection_match_count is not None and plan.collection_match_count > len(records):
        body = body.replace(
            f"조건에 맞는 기록은 총 {len(records)}건입니다.",
            f"조건에 맞는 기록 중 아래 {len(records)}건을 표시합니다.", 1,
        )
        body += (
            f"\n\n조건에 맞는 동기화 기록은 {plan.collection_match_count}건입니다. "
            "표시 제한 또는 시각 미기재로 일부 기록이 목록에서 생략되었습니다."
        )
    document_ids = [packet.document.id for _, _, packet in records]
    return EvidenceAssessment(
        status="supported",
        answer=body,
        cited_document_ids=document_ids,
        report=AnswerReport(
            answer_type="direct",
            status="supported",
            headline=f"기록 {len(records)}건을 표시합니다.",
            body=body,
        ),
    )
