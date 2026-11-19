"""Self-only, explicit, immutable approvals. Never execute model instructions."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.connectors.naverworks import NaverWorksConnector, NaverWorksError, NaverWorksDeliveryError
from app.models import ConnectorCredential, MessageDelivery
from app.services.security import decrypt_json
from app.services.delivery_intent import DeliveryIntent, delivery_intent


def load_sender(db: Session, project_id: str) -> dict:
    saved = db.scalar(select(ConnectorCredential).where(
        ConnectorCredential.project_id == project_id, ConnectorCredential.provider == "naverworks"))
    if not saved:
        raise HTTPException(409, "네이버웍스 연결이 필요합니다.")
    try:
        values = decrypt_json(saved.encrypted_payload)
    except Exception:
        raise HTTPException(409, "네이버웍스 연결 정보를 확인해 주세요.") from None
    if not all(values.get(k) for k in ("bot_id", "self_user_id", "self_name")):
        raise HTTPException(409, "본인 전용 PB 봇 발송 설정이 아직 완료되지 않았습니다.")
    return values


def sender_snapshot(values: dict) -> dict:
    return {key: str(values[key]) for key in ("bot_id", "self_user_id", "self_name")}


def split_message(text: str) -> list[str]:
    if not text.strip() or len(text) > 10000:
        raise HTTPException(400, "전송문은 비어 있지 않은 10,000자 이하여야 합니다. 요청 범위를 줄여 주세요.")
    # Count UTF-16 units conservatively, including emoji; keep each API request
    # below the documented 2,000-character limit. No silent truncation.
    chunks, current, size = [], "", 0
    for char in text:
        units = len(char.encode("utf-16-le")) // 2
        if size + units > 1850:
            chunks.append(current)
            current, size = "", 0
        current += char
        size += units
    if current:
        chunks.append(current)
    if len(chunks) > 6:
        raise HTTPException(400, "전송문이 6개 메시지를 초과합니다. 요청 범위를 줄여 주세요.")
    if len(chunks) == 1:
        return chunks
    return [f"[PB {i}/{len(chunks)}]\n{chunk}" for i, chunk in enumerate(chunks, 1)]


def public_delivery(action: MessageDelivery) -> dict:
    payload = action.payload_json
    return {"id": action.id, "status": action.status, "recipient": payload["self_name"],
            "recipient_id": payload["self_user_id"], "text": payload["text"],
            "part_count": len(payload["parts"]), "completed_parts": action.completed_parts,
            "expires_at": action.expires_at.isoformat(), "message": action.result_message,
            "warning": payload.get("warning", "")}


def create_delivery(db: Session, project_id: str, text: str, warning: str = "") -> dict:
    values = load_sender(db, project_id)
    token = secrets.token_urlsafe(32)
    action = MessageDelivery(project_id=project_id, approval_hash=hashlib.sha256(token.encode()).hexdigest(),
        payload_json={**sender_snapshot(values), "text": text, "parts": split_message(text), "warning": warning},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15))
    db.add(action)
    db.commit()
    db.refresh(action)
    return {**public_delivery(action), "approval_token": token}


def approved_action(db: Session, project_id: str, action_id: str, token: str) -> MessageDelivery:
    action = db.get(MessageDelivery, action_id)
    if not action or action.project_id != project_id:
        raise HTTPException(404, "이 프로젝트의 전송 요청을 찾을 수 없습니다.")
    if not secrets.compare_digest(action.approval_hash, hashlib.sha256(token.encode()).hexdigest()):
        raise HTTPException(403, "전송 승인 정보를 확인해 주세요.")
    return action


def confirm_delivery(db: Session, project_id: str, action_id: str, token: str) -> dict:
    action = approved_action(db, project_id, action_id, token)
    if action.status != "pending":
        return public_delivery(action)
    expires = action.expires_at.replace(tzinfo=timezone.utc) if action.expires_at.tzinfo is None else action.expires_at
    if expires <= datetime.now(timezone.utc):
        action.status = "expired"
        db.commit()
        raise HTTPException(409, "승인 시간이 만료됐습니다. 전송문을 새로 준비해 주세요.")
    values = load_sender(db, project_id)
    if any(action.payload_json[k] != v for k, v in sender_snapshot(values).items()):
        raise HTTPException(409, "봇·수신자 설정이 변경됐습니다. 새 전송문으로 다시 확인해 주세요.")
    # Commit the claim BEFORE any network request. A crash or uncertain response
    # leaves it non-retryable, including across process restarts and double-clicks.
    claimed = db.execute(update(MessageDelivery).where(MessageDelivery.id == action.id,
        MessageDelivery.status == "pending").values(status="sending"))
    db.commit()
    db.refresh(action)
    if claimed.rowcount != 1:
        return public_delivery(action)
    connector = NaverWorksConnector(values)
    try:
        for part in action.payload_json["parts"]:
            connector.send_text(values["bot_id"], values["self_user_id"], part)
            action.completed_parts += 1
            db.commit()
        action.status = "sent"
        action.result_message = "네이버웍스 API가 발송 요청을 접수했습니다. 수신·읽음 여부는 확인하지 않았습니다."
    except NaverWorksError as exc:
        action.status = "unknown" if isinstance(exc, NaverWorksDeliveryError) and exc.uncertain else "failed"
        action.result_message = str(exc)
    except Exception:
        db.rollback()
        action = db.get(MessageDelivery, action_id)
        action.status = "unknown"
        action.result_message = "전송 결과를 확정할 수 없습니다. 메신저를 확인해 주세요. 자동 재전송하지 않습니다."
    finally:
        connector.close()
    db.commit()
    db.refresh(action)
    return public_delivery(action)


def cancel_delivery(db: Session, project_id: str, action_id: str, token: str) -> dict:
    action = approved_action(db, project_id, action_id, token)
    db.execute(update(MessageDelivery).where(MessageDelivery.id == action.id,
        MessageDelivery.status == "pending").values(status="cancelled"))
    db.commit()
    db.refresh(action)
    return public_delivery(action)
