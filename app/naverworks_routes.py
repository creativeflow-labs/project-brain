"""Isolated first-stage routes. Local setup only; never expose without access control."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.naverworks import NaverWorksConnector, NaverWorksError, validate_period
from app.db import get_db
from app.models import ConnectorCredential, Project
from app.services.security import decrypt_json, encrypt_json
from app.services.naverworks_sync import message_inputs, ingest_messages

router = APIRouter(prefix="/api/connectors/naverworks", tags=["naverworks"])
_download_lock = threading.Lock()


class ConnectionInput(BaseModel):
    project_id: str
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: SecretStr
    service_account: str = Field(min_length=1, max_length=320)
    private_key: SecretStr


class DownloadInput(BaseModel):
    project_id: str
    channel_id: str = Field(min_length=1, max_length=200)
    start: datetime
    end: datetime


class SyncInput(DownloadInput):
    channel_name: str = Field(default="", max_length=120)


class ProjectInput(BaseModel):
    project_id: str


def same_origin(request: Request) -> None:
    # New sensitive setup routes must come from our page, not a cross-site form.
    if request.headers.get("x-project-brain-client") != "1":
        raise HTTPException(403, "피비 화면에서 요청해 주세요.")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "다른 출처의 연결 요청을 허용하지 않습니다.")


def credential(db: Session, project_id: str) -> ConnectorCredential | None:
    if not db.get(Project, project_id):
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    return db.scalar(select(ConnectorCredential).where(
        ConnectorCredential.project_id == project_id, ConnectorCredential.provider == "naverworks"))


def public_status(saved: ConnectorCredential | None) -> dict:
    state = json.loads(saved.cursor or "{}") if saved else {}
    return {"connected": bool(saved), "stage": "manual_sync", "ingestion_enabled": True,
            "authenticated_at": state.get("authenticated_at"),
            "last_download_at": state.get("last_download_at"),
            "last_row_count": state.get("last_row_count"),
            "last_sync_at": state.get("last_sync_at"), "last_sync_count": state.get("last_sync_count"),
            "channel_id": state.get("channel_id"), "channel_name": state.get("channel_name"),
            "message": ("메시지 검색 적재 완료" if state.get("last_sync_at") else "인증 저장됨 · 메시지 동기화 필요") if saved else "연결 전 · Lite와 관리자 설정 필요"}


@router.get("/status")
def status(project_id: str, db: Session = Depends(get_db)):
    return public_status(credential(db, project_id))


@router.post("/connect", dependencies=[Depends(same_origin)])
def connect(payload: ConnectionInput, db: Session = Depends(get_db)):
    saved = credential(db, payload.project_id)
    values = {"client_id": payload.client_id.strip(), "client_secret": payload.client_secret.get_secret_value(),
              "service_account": payload.service_account.strip(), "private_key": payload.private_key.get_secret_value()}
    connector = NaverWorksConnector(values)
    try:
        connector.authenticate()
    except NaverWorksError as exc:
        raise HTTPException(400, str(exc)) from None
    finally:
        connector.close()
    if saved is None:
        saved = ConnectorCredential(project_id=payload.project_id, provider="naverworks", encrypted_payload="")
        db.add(saved)
    saved.encrypted_payload = encrypt_json(values)
    state = json.loads(saved.cursor or "{}")
    state["authenticated_at"] = datetime.now(timezone.utc).isoformat()
    saved.cursor = json.dumps(state)
    db.commit()
    return public_status(saved)


@router.post("/download", dependencies=[Depends(same_origin)])
def download(payload: DownloadInput, db: Session = Depends(get_db)):
    saved = credential(db, payload.project_id)
    if saved is None:
        raise HTTPException(400, "먼저 네이버웍스 연결을 설정해 주세요.")
    try:
        validate_period(payload.start, payload.end)
    except NaverWorksError as exc:
        raise HTTPException(400, str(exc)) from None
    if not _download_lock.acquire(blocking=False):
        raise HTTPException(409, "다른 네이버웍스 다운로드가 진행 중입니다.")
    connector = None
    try:
        try:
            values = decrypt_json(saved.encrypted_payload)
        except Exception:
            raise HTTPException(400, "저장된 연결을 해독하지 못했습니다. 암호화 키 또는 연결 설정을 확인해 주세요.") from None
        connector = NaverWorksConnector(values)
        data, summary = connector.download(payload.start, payload.end, payload.channel_id)
        state = json.loads(saved.cursor or "{}")
        state.update(last_download_at=datetime.now(timezone.utc).isoformat(), last_row_count=summary["rows"])
        saved.cursor = json.dumps(state)
        db.commit()
        return Response(data, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="naverworks-messages.csv"',
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Message-Rows": str(summary["rows"]),
        })
    except NaverWorksError as exc:
        raise HTTPException(400, str(exc)) from None
    finally:
        if connector:
            connector.close()
        _download_lock.release()


@router.post("/disconnect", dependencies=[Depends(same_origin)])
def disconnect(payload: ProjectInput, db: Session = Depends(get_db)):
    # Only credentials are removed. Future ingested records require an explicit policy.
    saved = credential(db, payload.project_id)
    if saved:
        db.delete(saved)
        db.commit()
    return public_status(None)


@router.post("/sync", dependencies=[Depends(same_origin)])
def sync(payload: SyncInput, db: Session = Depends(get_db)):
    saved = credential(db, payload.project_id)
    if saved is None:
        raise HTTPException(400, "먼저 네이버웍스 연결을 설정해 주세요.")
    if not _download_lock.acquire(blocking=False):
        raise HTTPException(409, "다른 네이버웍스 조회 또는 동기화가 진행 중입니다.")
    connector = None
    try:
        try:
            values = decrypt_json(saved.encrypted_payload)
        except Exception:
            raise HTTPException(400, "저장된 연결을 해독하지 못했습니다. 연결 설정을 확인해 주세요.") from None
        channel_id = payload.channel_id.strip()
        connector = NaverWorksConnector(values)
        data, _ = connector.download(payload.start, payload.end, channel_id)
        # Validate every row before any document or embedding write.
        items = message_inputs(data, payload.project_id, channel_id, payload.start, payload.end, payload.channel_name)
        try:
            result = ingest_messages(db, items)
        except Exception:
            db.rollback()
            raise HTTPException(502, "메시지 검색 적재 중 오류가 발생했습니다. 일부가 저장됐을 수 있으며, 같은 기간으로 재시도해도 중복 적재하지 않습니다.") from None
        state = json.loads(saved.cursor or "{}")
        state.update(last_sync_at=datetime.now(timezone.utc).isoformat(),
                     last_sync_count=result["processed_messages"], channel_id=channel_id,
                     channel_name=payload.channel_name.strip())
        saved.cursor = json.dumps(state)
        db.commit()
        return result
    except NaverWorksError as exc:
        raise HTTPException(400, str(exc)) from None
    finally:
        if connector:
            connector.close()
        _download_lock.release()
