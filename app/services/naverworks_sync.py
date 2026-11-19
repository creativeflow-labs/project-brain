"""Normalize the monitoring CSV verified against the Korean account export."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.connectors.naverworks import NaverWorksError, inspect_csv
from app.services.ingestion import IngestionInput, ingest_document

REQUIRED_COLUMNS = {"날짜", "보낸 사람", "받는 사람", "채널 ID", "메시지"}
MAX_SYNC_MESSAGES = 1000


def message_inputs(data: bytes, project_id: str, channel_id: str,
                   start: datetime, end: datetime, channel_name: str = "") -> list[IngestionInput]:
    summary = inspect_csv(data)
    if not REQUIRED_COLUMNS.issubset(summary["columns"]):
        raise NaverWorksError("메시지 CSV의 필수 열이 다릅니다. 적재하지 않았습니다.")
    if summary["rows"] > MAX_SYNC_MESSAGES:
        raise NaverWorksError("1회 검색 적재는 최대 1,000건입니다. 조회 기간을 줄여 주세요.")
    messages: dict[str, IngestionInput] = {}
    for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
        if row["채널 ID"].strip() != channel_id:
            raise NaverWorksError("요청한 대화방과 다른 메시지가 반환되어 적재하지 않았습니다.")
        try:
            occurred_at = datetime.fromisoformat(row["날짜"].strip().replace("Z", "+00:00"))
            if occurred_at.utcoffset() is None or not start <= occurred_at <= end:
                raise ValueError("outside requested period")
        except ValueError:
            raise NaverWorksError("메시지 시각이 없거나 조회 범위와 다릅니다. 적재하지 않았습니다.") from None
        occurred_at = occurred_at.astimezone(timezone.utc)
        body = row["메시지"].strip()
        if not body:
            continue
        sender, recipient = row["보낸 사람"].strip(), row["받는 사람"].strip()
        # The export has no message ID. Hash immutable exported values so an
        # overlapping or repeated period does not create duplicate documents.
        identity = json.dumps([channel_id, occurred_at.isoformat(), sender, recipient, body], ensure_ascii=False)
        external_id = f"{channel_id}:{hashlib.sha256(identity.encode()).hexdigest()}"
        local_time = occurred_at.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
        room = channel_name.strip() or channel_id
        text = (f"출처: 네이버웍스 메신저\n대화방: {room}\n"
                f"메시지 시각: {local_time} (Asia/Seoul)\n보낸 사람: {sender}\n"
                f"받는 사람: {recipient}\n\n메시지:\n{body}")
        messages[external_id] = IngestionInput(
            project_id=project_id, source_type="naverworks", external_id=external_id,
            title=f"네이버웍스 · {room[:120]} · {local_time}", text=text,
            source_url="https://talk.worksmobile.com/",
            metadata={"channel_id": channel_id, "channel_name": channel_name.strip(),
                      "sender": sender, "recipient": recipient, "recorded_at": occurred_at.isoformat(),
                      "sent_at": occurred_at.isoformat(), "occurred_at": occurred_at.isoformat(),
                      "record_type": "message", "source_link_scope": "messenger_home"},
        )
    return list(messages.values())


def ingest_messages(db: Session, items: list[IngestionInput]) -> dict:
    changed = 0
    for item in items:
        _, updated = ingest_document(db, item)
        changed += int(updated)
    return {"processed_messages": len(items), "changed_messages": changed,
            "unchanged_messages": len(items) - changed}
