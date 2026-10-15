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
from app.services.search_core.collection_contract import video_operation


def assess_scoped_collection(
    plan: QueryPlan, packets: list[EvidencePacket]
) -> EvidenceAssessment | None:
    if (
        plan.retrieval_mode != "scoped"
        or plan.cardinality != "all"
        or plan.record_type != "video"
    ):
        return None
    operation = video_operation(plan.original_question)
    if operation not in {"count", "list"}:
        return None
    if operation == "count":
        # This count was taken over matching documents BEFORE evidence limits.
        if plan.collection_match_count is None:
            return None
        count = plan.collection_match_count
        references = list(range(1, min(len(packets), 3) + 1))
        citations = "".join(f" [{index}]" for index in references)
        body = f"현재 동기화된 Notion 영상 기록 중 질문 조건에 맞는 기록은 총 {count}건입니다.{citations}"
        if plan.collection_title_terms:
            body += "\n\n제목 포함 조건: " + ", ".join(plan.collection_title_terms)
        body += "\n\n동기화된 기록 기준이며, 실제 YouTube 공개 완료 여부는 별도 확인이 필요합니다."
        return EvidenceAssessment(
            status="supported",
            answer=body,
            cited_document_ids=[packet.document.id for packet in packets[:3]],
            report=AnswerReport(
                answer_type="direct",
                status="supported",
                headline=f"질문 조건에 맞는 영상 기록 {count}건을 확인했습니다.",
                body=body,
            ),
        )
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
    if plan.collection_match_count is not None and plan.collection_match_count > len(records):
        body = body.replace(
            f"영상 기록은 총 {len(records)}건입니다.",
            f"영상 기록 중 아래 {len(records)}건을 표시합니다.", 1,
        )
        body += (
            f"\n\n조건에 맞는 동기화 기록은 {plan.collection_match_count}건입니다. "
            "표시 제한 또는 업로드 시각 미기재로 일부 기록이 목록에서 생략되었습니다."
        )
    document_ids = [packet.document.id for _, _, packet in records]
    return EvidenceAssessment(
        status="supported",
        answer=body,
        cited_document_ids=document_ids,
        report=AnswerReport(
            answer_type="direct",
            status="supported",
            headline=f"영상 기록 {len(records)}건을 표시합니다.",
            body=body,
        ),
    )
