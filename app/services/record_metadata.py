from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


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
    metadata = metadata or {}
    stored = metadata.get("record_type")
    if isinstance(stored, str) and stored:
        return stored

    combined = _normalise_text(f"{title}\n{text}")
    lowered = combined.lower()
    has_video_sections = "📌 제목" in combined and "📌 설명" in combined
    has_video_fields = any(
        marker in lowered
        for marker in ("영상 제목:", "업로드 시간:", "게시 시간:", "published_at")
    )
    has_episode_title = bool(
        re.search(r"\bep\s*\d+(?:-\d+)?\b", lowered)
        and parse_recorded_datetime(title, metadata)
    )
    if has_video_sections or has_video_fields or has_episode_title:
        return "video"
    return None


def extract_record_title(
    document_title: str, text: str, metadata: dict | None = None
) -> str:
    metadata = metadata or {}
    stored = metadata.get("record_title")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()

    lines = [_normalise_text(line).strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if line.rstrip(":：") != "📌 제목":
            continue
        for candidate in lines[index + 1 :]:
            if not candidate or candidate.strip("-— ") == "":
                continue
            if candidate.startswith("📌"):
                break
            return candidate

    for pattern in (
        r"(?:영상\s*제목|제목)\s*[:：]\s*(.+)",
        r"record_title\s*[:：]\s*(.+)",
    ):
        match = re.search(pattern, _normalise_text(text), flags=re.IGNORECASE)
        if match:
            return match.group(1).splitlines()[0].strip()
    return document_title.strip()
