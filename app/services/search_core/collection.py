from __future__ import annotations

from datetime import datetime

from app.services.record_metadata import (
    SEOUL,
    extract_record_title,
    parse_recorded_datetime,
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
        or plan.record_type != "video"
    ):
        return None
    positions = {
        packet.document.id: index for index, packet in enumerate(packets, start=1)
    }
    records: list[tuple[datetime, str, EvidencePacket]] = []
    for packet in packets:
        metadata = packet.document.metadata_json or {}
        recorded_at = parse_recorded_datetime(packet.document.title, metadata)
        if recorded_at is None:
            continue
        if plan.time_start and recorded_at < plan.time_start:
            continue
        if plan.time_end and recorded_at > plan.time_end:
            continue
        record_title = extract_record_title(
            packet.document.title, packet.context_text, metadata
        )
        records.append((recorded_at, record_title, packet))

    if not records:
        return None
    records.sort(key=lambda item: (item[0], item[1]))
    source_label = next(
        (
            str(packet.document.metadata_json.get("collection_title"))
            for _, _, packet in records
            if (packet.document.metadata_json or {}).get("collection_title")
        ),
        "현재 동기화된 Notion 제작리스트",
    )
    lines = [
        f"{source_label} 기준으로 해당 기간의 영상 기록은 총 {len(records)}건입니다.",
        "",
    ]
    for recorded_at, title, packet in records:
        reference = positions[packet.document.id]
        lines.append(
            f"- {recorded_at.astimezone(SEOUL).strftime('%Y-%m-%d %H:%M')} | "
            f"{title} [{reference}]"
        )
    lines.extend(
        [
            "",
            "위 시각은 Notion 제작리스트에 기록된 업로드 시각 기준입니다. "
            "실제 YouTube 공개 완료 여부는 YouTube Studio 미연동으로 별도 검증이 필요합니다.",
        ]
    )
    body = "\n".join(lines)
    document_ids = [packet.document.id for _, _, packet in records]
    return EvidenceAssessment(
        status="supported",
        answer=body,
        cited_document_ids=document_ids,
        report=AnswerReport(
            answer_type="direct",
            status="supported",
            headline=f"해당 기간의 영상 기록 {len(records)}건을 확인했습니다.",
            body=body,
        ),
    )
