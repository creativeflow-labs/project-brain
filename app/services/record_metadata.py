from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")
STRUCTURED_DATE_FIELDS = {
    "published_at": ("published_at", "recorded_upload_at"),
    "received_at": ("received_at",),
    "sent_at": ("sent_at",),
    "updated_at": ("updated_at", "last_edited_time"),
    "occurred_at": ("occurred_at",),
}


def structured_record_date(metadata: dict, field: str | None) -> datetime | None:
    for key in STRUCTURED_DATE_FIELDS.get(field or "occurred_at", ()):
        value = metadata.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed.replace(tzinfo=SEOUL) if parsed.tzinfo is None else parsed
    return None


def _normalise_text(value: str) -> str:
    return value.replace("\u00a0", " ").replace("\u202f", " ")


def parse_recorded_datetime(
    title: str, metadata: dict | None = None
) -> datetime | None:
    metadata = metadata or {}
    for key in (
        "recorded_upload_at",
        "recorded_at",
        "published_at",
        "upload_time",
        "uploaded_at",
    ):
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.replace(tzinfo=SEOUL) if parsed.tzinfo is None else parsed
        except ValueError:
            continue

    normalised = _normalise_text(title)
    date_match = re.search(
        r"(?<!\d)(?P<year>20\d{2}|\d{2})\s*[.-]\s*"
        r"(?P<month>1[0-2]|0?[1-9])\s*[.-]\s*"
        r"(?P<day>3[01]|[12]\d|0?[1-9])(?!\d)",
        normalised,
    )
    if not date_match:
        return None
    year = int(date_match.group("year"))
    if year < 100:
        year += 2000

    hour = 0
    minute = 0
    time_match = re.search(
        r"(?P<period>새벽|오전|오후|저녁)?\s*(?P<hour>[01]?\d|2[0-3])\s*시"
        r"(?:\s*(?P<minute>[0-5]?\d)\s*분)?",
        normalised[date_match.end() :],
    )
    if time_match:
        hour = int(time_match.group("hour"))
        minute = int(time_match.group("minute") or 0)
        period = time_match.group("period")
        if period in {"오후", "저녁"} and hour < 12:
            hour += 12
        elif period in {"오전", "새벽"} and hour == 12:
            hour = 0
    try:
        return datetime(
            year,
            int(date_match.group("month")),
            int(date_match.group("day")),
            hour,
            minute,
            tzinfo=SEOUL,
        )
    except ValueError:
        return None


def infer_record_type(title: str, text: str, metadata: dict | None = None) -> str | None:
    """Return a declared record type; prose formats do not prove a type."""
    metadata = metadata or {}
    stored = metadata.get("record_type")
    if isinstance(stored, str) and stored.strip():
        return stored.strip().casefold()
    return None


def extract_record_title(
    document_title: str, text: str, metadata: dict | None = None
) -> str:
    metadata = metadata or {}
    stored = metadata.get("record_title")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()

    return document_title.strip()
