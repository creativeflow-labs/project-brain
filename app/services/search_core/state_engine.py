from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from app.services.search_core.types import (
    CurrentStateAssessment,
    EvidencePacket,
    QueryPlan,
    TimelineEvent,
    WorkState,
)

STATE_LABELS: dict[WorkState, str] = {
    "unknown": "확인되지 않음",
    "planned": "예정",
    "requested": "요청됨",
    "in_progress": "진행 중",
    "waiting_external": "외부 회신 대기",
    "waiting_approval": "내부 승인 대기",
    "revision_requested": "수정 요청",
    "completion_claimed": "완료 주장",
    "completed": "완료 확인",
    "on_hold": "보류",
    "cancelled": "취소",
}

NEXT_ACTIONS: dict[WorkState, str | None] = {
    "unknown": "상태를 직접 확인할 수 있는 최신 기록을 추가하거나 원문을 확인하세요.",
    "planned": "담당자와 착수 일정을 확정하세요.",
    "requested": "요청의 담당자와 처리 기한을 확인하세요.",
    "in_progress": "담당자에게 진행 상황과 다음 완료 조건을 확인하세요.",
    "waiting_external": "외부 회신 여부와 후속 연락 시점을 확인하세요.",
    "waiting_approval": "승인권자의 최종 승인 여부를 확인하세요.",
    "revision_requested": "수정 담당자와 재제출 일정을 확인하세요.",
    "completion_claimed": "승인권자 또는 요청자의 완료 확인을 받아야 합니다.",
    "completed": None,
    "on_hold": "재개 조건과 결정권자를 확인하세요.",
    "cancelled": None,
}

# 더 구체적인 사건을 먼저 매칭합니다. 일반적인 '완료'는 검증된 완료가 아니라
# 수행자의 완료 주장으로 분리합니다.
STATE_PATTERNS: tuple[tuple[WorkState, int, tuple[str, ...]], ...] = (
    (
        "revision_requested",
        4,
        (
            r"수정\s*(?:을\s*)?(?:요청|필요|해\s*달|해주세요)",
            r"보완\s*(?:을\s*)?(?:요청|필요)",
            r"재작업",
            r"반려",
            r"다시\s*(?:작업|제출|보내)",
        ),
    ),
    (
        "cancelled",
        4,
        (r"(?:업무|작업|프로젝트|제안)?.{0,8}(?:취소|중단|철회)(?:했|함|됨|되었|합니다|확정)?",),
    ),
    (
        "on_hold",
        4,
        (r"(?:보류|홀드|일시\s*중지)(?:했|함|됨|되었|합니다|확정)?",),
    ),
    (
        "completed",
        5,
        (
            r"최종\s*(?:승인|컨펌)(?:했|함|됨|되었|합니다|완료)?",
            r"(?:고객|클라이언트|승인권자).{0,20}(?:최종\s*)?(?:승인|컨펌)(?:했|함|됨|되었|합니다)",
            r"(?:완료|납품)\s*(?:를\s*)?(?:확인|승인)(?:했|함|됨|되었|합니다)",
        ),
    ),
    (
        "waiting_approval",
        3,
        (
            r"(?:승인|컨펌)\s*(?:을\s*)?(?:요청|대기|기다)",
            r"(?:최종\s*)?검토\s*(?:를\s*)?(?:요청|중|대기)",
            r"(?:내부|팀장|책임자).{0,12}(?:확인|승인)\s*대기",
        ),
    ),
    (
        "waiting_external",
        3,
        (
            r"(?:외부|고객|클라이언트|거래처).{0,16}(?:회신|답변|확인)\s*(?:을\s*)?(?:대기|기다)",
            r"(?:회신|답변)\s*(?:을\s*)?(?:대기|기다)",
        ),
    ),
    (
        "in_progress",
        2,
        (r"(?:진행|작업|수정|검토)\s*중", r"(?:진행|작업|수정)하고\s*(?:있|계십)"),
    ),
    (
        "completion_claimed",
        1,
        (
            r"상태\s*[:：]\s*(?:완료|done)",
            r"(?:작업|수정|전달|발송|제출|업로드|처리)\s*(?:을\s*)?완료(?:했|함|됨|되었|됐|합니다)?",
            r"완료(?:했|함|됨|되었|됐|했습니다)",
        ),
    ),
    (
        "requested",
        2,
        (r"(?:업무|작업|자료|제안|견적|확인)\s*(?:을\s*)?요청(?:했|함|됨|되었|합니다)",),
    ),
    (
        "planned",
        1,
        (r"(?:진행|작업|전달|발송|제출|착수).{0,14}(?:예정|계획)",),
    ),
)

NEGATION_PATTERN = re.compile(r"(?:않|아니|못|미완료|전혀\s*없|확인되지\s*않)")
DATE_PATTERNS = (
    re.compile(r"(?P<year>20\d{2})[-./년]\s*(?P<month>\d{1,2})[-./월]\s*(?P<day>\d{1,2})"),
    re.compile(r"(?P<month>\d{1,2})[-./월]\s*(?P<day>\d{1,2})"),
)


def _normalise_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _normalise_datetime(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return _normalise_datetime(parsedate_to_datetime(raw))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return _normalise_datetime(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _sentence_datetime(text: str, fallback: datetime | None) -> tuple[datetime | None, str]:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        year = int(groups.get("year") or (fallback.year if fallback else datetime.now().year))
        try:
            return (
                datetime(year, int(groups["month"]), int(groups["day"]), tzinfo=timezone.utc),
                "event",
            )
        except ValueError:
            continue
    return fallback, "record" if fallback else "unknown"


def _document_datetime(packet: EvidencePacket) -> datetime | None:
    metadata = packet.document.metadata_json or {}
    for key in ("date", "event_time", "last_edited_time", "modified_time"):
        parsed = _parse_datetime(metadata.get(key))
        if parsed:
            return parsed
    return _parse_datetime(packet.document.updated_at)


def _sentences(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?。])\s+|[\r\n]+", text)
        if part.strip()
    ]


def _is_negated(sentence: str, match: re.Match[str]) -> bool:
    window = sentence[max(0, match.start() - 12) : min(len(sentence), match.end() + 14)]
    return bool(NEGATION_PATTERN.search(window))


def _actor(packet: EvidencePacket) -> tuple[str | None, str | None]:
    metadata = packet.document.metadata_json or {}
    sender = metadata.get("from")
    if sender:
        return str(sender), "sender"
    return None, None


def extract_timeline_events(packets: list[EvidencePacket]) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    seen: set[tuple[str, WorkState, str]] = set()
    for packet in packets:
        recorded_at = _document_datetime(packet)
        actor, actor_role = _actor(packet)
        for sentence in _sentences(packet.context_text):
            for state, authority, patterns in STATE_PATTERNS:
                matched = False
                for pattern in patterns:
                    match = re.search(pattern, sentence, flags=re.IGNORECASE)
                    if not match or _is_negated(sentence, match):
                        continue
                    occurred_at, time_basis = _sentence_datetime(sentence, recorded_at)
                    excerpt = sentence[:500]
                    key = (packet.document.id, state, excerpt)
                    if key in seen:
                        matched = True
                        break
                    seen.add(key)
                    events.append(
                        TimelineEvent(
                            state=state,
                            label=STATE_LABELS[state],
                            occurred_at=occurred_at,
                            source_type=packet.document.source_type,
                            document_id=packet.document.id,
                            title=packet.document.title,
                            excerpt=excerpt,
                            actor=actor,
                            actor_role=actor_role,
                            time_basis=time_basis,
                            authority=authority,
                        )
                    )
                    matched = True
                    break
                if matched:
                    # 한 문장의 가장 구체적인 상태 하나만 채택합니다.
                    break
    return sorted(
        events,
        key=lambda event: (
            event.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
            event.authority,
        ),
    )


def _same_time(left: TimelineEvent, right: TimelineEvent) -> bool:
    if left.occurred_at is None or right.occurred_at is None:
        return left.occurred_at is None and right.occurred_at is None
    return left.occurred_at == right.occurred_at


def _reason_for(event: TimelineEvent) -> str:
    if event.state == "completion_claimed":
        return (
            f"'{event.title}'에는 업무 완료 기록이 있지만 최종 승인 또는 요청자의 완료 확인은 "
            "근거에서 확인되지 않았습니다."
        )
    return f"가장 최근의 결정적 기록은 '{event.title}'의 {event.label} 사건입니다."


def assess_current_state(
    plan: QueryPlan, packets: list[EvidencePacket]
) -> CurrentStateAssessment | None:
    if not plan.is_state_query:
        return None

    events = extract_timeline_events(packets)
    if not events:
        return CurrentStateAssessment(
            state="unknown",
            label=STATE_LABELS["unknown"],
            evidence_status="insufficient",
            as_of=None,
            reason="검색된 근거에서 업무 상태를 직접 나타내는 사건을 확인하지 못했습니다.",
            next_action=NEXT_ACTIONS["unknown"],
        )

    latest = events[-1]
    competing = [
        event
        for event in events[:-1]
        if event.document_id != latest.document_id
        and event.state != latest.state
        and _same_time(event, latest)
    ]

    # 시간 정보가 없거나 같은 시점의 서로 다른 원문이 충돌하면 임의로 최신값을 만들지 않습니다.
    if competing:
        decisive = [*competing[-2:], latest]
        for event in decisive:
            event.decisive = True
        detail = " / ".join(
            f"{event.source_type} '{event.title}': {event.label}" for event in decisive
        )
        return CurrentStateAssessment(
            state="unknown",
            label="상태 충돌",
            evidence_status="conflict",
            as_of=latest.occurred_at,
            reason=f"같은 시점의 기록이 서로 다른 상태를 나타냅니다. {detail}",
            next_action="결정권자 또는 최신 원문을 확인해 현재 상태를 확정하세요.",
            decisive_document_ids=list(
                dict.fromkeys(event.document_id for event in decisive)
            ),
            conflicts=[detail],
            timeline=events,
        )

    latest.decisive = True
    stale_ids = list(
        dict.fromkeys(
            event.document_id
            for event in events[:-1]
            if event.document_id != latest.document_id and event.state != latest.state
        )
    )
    return CurrentStateAssessment(
        state=latest.state,
        label=latest.label,
        evidence_status="supported",
        as_of=latest.occurred_at,
        reason=_reason_for(latest),
        next_action=NEXT_ACTIONS[latest.state],
        decisive_document_ids=[latest.document_id],
        stale_document_ids=stale_ids,
        timeline=events,
    )


def current_state_to_dict(assessment: CurrentStateAssessment) -> dict:
    return {
        "state": assessment.state,
        "label": assessment.label,
        "evidence_status": assessment.evidence_status,
        "as_of": assessment.as_of.isoformat() if assessment.as_of else None,
        "reason": assessment.reason,
        "next_action": assessment.next_action,
        "responsible": assessment.responsible,
        "approver": assessment.approver,
        "decisive_document_ids": assessment.decisive_document_ids,
        "stale_document_ids": assessment.stale_document_ids,
        "conflicts": assessment.conflicts,
        "timeline": [
            {
                "state": event.state,
                "label": event.label,
                "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
                "source_type": event.source_type,
                "document_id": event.document_id,
                "title": event.title,
                "excerpt": event.excerpt,
                "actor": event.actor,
                "actor_role": event.actor_role,
                "time_basis": event.time_basis,
                "decisive": event.decisive,
            }
            for event in assessment.timeline
        ],
    }
