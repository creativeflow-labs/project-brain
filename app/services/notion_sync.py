from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.connectors.notion import (
    NOTION_METADATA_SCHEMA_VERSION,
    NotionApiError,
    NotionConnector,
)
from app.db import SessionLocal
from app.models import ConnectorCredential, Document
from app.services.security import decrypt_json, encrypt_json

settings = get_settings()
_project_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class NotionSyncBusyError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _state_from_credential(credential: ConnectorCredential | None) -> dict:
    if not credential or not credential.cursor:
        return {}
    try:
        value = json.loads(credential.cursor)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _save_state(db: Session, credential: ConnectorCredential, state: dict) -> None:
    credential.cursor = json.dumps(state, ensure_ascii=False)
    db.commit()


def _notion_credential(db: Session, project_id: str) -> ConnectorCredential | None:
    return db.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.project_id == project_id,
            ConnectorCredential.provider == "notion",
        )
    )


def _project_lock(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _project_locks.setdefault(project_id, threading.Lock())


def connect_notion(
    db: Session,
    project_id: str,
    token: str,
    auth_mode: str,
    auto_sync_enabled: bool,
    sync_interval_minutes: int,
) -> dict:
    connector = NotionConnector(token)
    try:
        identity = connector.validate_token()
    finally:
        connector.close()

    credential = _notion_credential(db, project_id)
    encrypted = encrypt_json({"access_token": token, "auth_mode": auth_mode})
    state = _state_from_credential(credential)
    state.update(
        {
            "connected": True,
            "auth_mode": auth_mode,
            "connection_name": identity.get("name", "Notion 연결"),
            "connection_id": identity.get("id", ""),
            "auto_sync_enabled": auto_sync_enabled,
            "sync_interval_minutes": sync_interval_minutes,
            "status": "connected",
            "connected_at": state.get("connected_at") or _iso_now(),
            "last_error": "",
        }
    )
    if credential:
        credential.encrypted_payload = encrypted
    else:
        credential = ConnectorCredential(
            project_id=project_id,
            provider="notion",
            encrypted_payload=encrypted,
        )
        db.add(credential)
        db.flush()
    _save_state(db, credential, state)
    return public_notion_status(credential)


def public_notion_status(credential: ConnectorCredential | None) -> dict:
    if not credential:
        return {
            "connected": False,
            "status": "not_connected",
            "auto_sync_enabled": False,
            "sync_interval_minutes": 60,
        }
    state = _state_from_credential(credential)
    allowed = {
        "connected",
        "auth_mode",
        "connection_name",
        "auto_sync_enabled",
        "sync_interval_minutes",
        "status",
        "connected_at",
        "last_attempt_at",
        "last_success_at",
        "last_error",
        "discovered_pages",
        "processed_pages",
        "changed_pages",
        "changed_attachments",
        "unchanged",
        "removed",
        "error_count",
        "warning_count",
        "discovery_incomplete",
        "coverage_mode",
        "duration_seconds",
    }
    return {key: value for key, value in state.items() if key in allowed} | {
        "connected": True
    }


def get_notion_status(db: Session, project_id: str) -> dict:
    return public_notion_status(_notion_credential(db, project_id))


def _remove_notion_page(db: Session, project_id: str, page_id: str) -> int:
    removed = 0
    page_documents = db.scalars(
        select(Document).where(
            Document.project_id == project_id,
            Document.source_type == "notion",
            Document.external_id == page_id,
        )
    ).all()
    attachment_documents = db.scalars(
        select(Document).where(
            Document.project_id == project_id,
            Document.source_type == "notion_attachment",
            Document.metadata_json["parent_page_id"].as_string() == page_id,
        )
    ).all()
    for document in [*page_documents, *attachment_documents]:
        db.delete(document)
        removed += 1
    if removed:
        db.commit()
    return removed


def sync_notion_project(db: Session, project_id: str, force: bool = False) -> dict:
    credential = _notion_credential(db, project_id)
    if not credential:
        raise RuntimeError("Notion을 먼저 연결해 주세요.")

    lock = _project_lock(project_id)
    if not lock.acquire(blocking=False):
        raise NotionSyncBusyError("이 프로젝트의 Notion 동기화가 이미 진행 중입니다.")

    started = _utcnow()
    state = _state_from_credential(credential)
    state.update(
        {
            "status": "running",
            "last_attempt_at": started.isoformat(),
            "last_error": "",
        }
    )
    _save_state(db, credential, state)

    connector: NotionConnector | None = None
    try:
        secret = decrypt_json(credential.encrypted_payload)
        connector = NotionConnector(secret["access_token"])
        discovery = connector.discover_pages(settings.notion_max_pages_per_sync)

        known_documents = db.scalars(
            select(Document).where(
                Document.project_id == project_id,
                Document.source_type == "notion",
            )
        ).all()
        known_by_id = {document.external_id: document for document in known_documents}
        queue = deque(discovery.pages)
        queued = set(discovery.pages)
        for page_id in known_by_id:
            if page_id not in queued:
                queue.append(page_id)
                queued.add(page_id)

        processed: set[str] = set()
        errors: list[str] = []
        warnings = list(discovery.warnings)
        changed_pages = 0
        changed_attachments = 0
        unchanged = 0
        removed = 0

        while queue and len(processed) < settings.notion_max_pages_per_sync:
            page_id = queue.popleft()
            if page_id in processed:
                continue
            processed.add(page_id)
            discovered_page = discovery.pages.get(page_id)
            existing = known_by_id.get(page_id)
            discovered_edited = (discovered_page or {}).get("last_edited_time")
            existing_metadata = existing.metadata_json if existing else {}

            if (
                not force
                and existing
                and discovered_edited
                and existing_metadata.get("last_edited_time") == discovered_edited
                and existing_metadata.get("metadata_schema_version", 0)
                >= NOTION_METADATA_SCHEMA_VERSION
            ):
                unchanged += 1
                for child_id in existing_metadata.get("child_page_ids", []):
                    if child_id not in queued:
                        queue.append(child_id)
                        queued.add(child_id)
                continue

            try:
                page = connector.fetch_page(page_id)
                if discovered_page:
                    for key in (
                        "_project_brain_collection_id",
                        "_project_brain_collection_title",
                    ):
                        if discovered_page.get(key):
                            page[key] = discovered_page[key]
                if page.get("archived") or page.get("in_trash"):
                    removed += _remove_notion_page(db, project_id, page_id)
                    continue
                result = connector.sync_page(db, project_id, page)
                changed_pages += result.pages
                changed_attachments += result.attachments
                unchanged += result.unchanged
                removed += result.removed
                warnings.extend(result.warnings)
                for child_id in result.child_page_ids:
                    if child_id not in queued:
                        queue.append(child_id)
                        queued.add(child_id)
            except NotionApiError as exc:
                db.rollback()
                if exc.status_code == 404 and existing:
                    removed += _remove_notion_page(db, project_id, page_id)
                else:
                    errors.append(f"{page_id[:8]}: {exc}")
            except Exception as exc:
                db.rollback()
                errors.append(f"{page_id[:8]}: {exc}")

        capped = bool(queue)
        discovery_incomplete = discovery.incomplete or capped
        duration = round((_utcnow() - started).total_seconds(), 2)
        status = "partial" if errors else "success"
        state.update(
            {
                "status": status,
                "last_success_at": _iso_now(),
                "last_error": errors[0] if errors else "",
                "discovered_pages": len(discovery.pages),
                "processed_pages": len(processed),
                "changed_pages": changed_pages,
                "changed_attachments": changed_attachments,
                "unchanged": unchanged,
                "removed": removed,
                "error_count": len(errors),
                "warning_count": len(warnings),
                "discovery_incomplete": discovery_incomplete,
                "coverage_mode": "best_effort_account"
                if state.get("auth_mode") == "personal_access_token"
                else "authorized_scope",
                "duration_seconds": duration,
            }
        )
        _save_state(db, credential, state)
        return public_notion_status(credential) | {
            "errors": errors[:20],
            "warnings": warnings[:20],
        }
    except Exception as exc:
        db.rollback()
        credential = _notion_credential(db, project_id)
        if credential:
            failed_state = _state_from_credential(credential)
            failed_state.update(
                {
                    "status": "failed",
                    "last_error": str(exc),
                    "duration_seconds": round(
                        (_utcnow() - started).total_seconds(), 2
                    ),
                }
            )
            _save_state(db, credential, failed_state)
        raise
    finally:
        if connector:
            connector.close()
        lock.release()


def sync_due_notion_connections() -> None:
    db = SessionLocal()
    try:
        credentials = db.scalars(
            select(ConnectorCredential).where(
                ConnectorCredential.provider == "notion"
            )
        ).all()
        now = _utcnow()
        due_project_ids: list[str] = []
        for credential in credentials:
            state = _state_from_credential(credential)
            if not state.get("auto_sync_enabled"):
                continue
            interval = int(state.get("sync_interval_minutes", 60))
            reference_value = state.get("last_attempt_at") or state.get("connected_at")
            if not reference_value:
                due_project_ids.append(credential.project_id)
                continue
            try:
                reference = datetime.fromisoformat(reference_value)
            except ValueError:
                due_project_ids.append(credential.project_id)
                continue
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            if now - reference >= timedelta(minutes=interval):
                due_project_ids.append(credential.project_id)
    finally:
        db.close()

    for project_id in due_project_ids:
        project_db = SessionLocal()
        try:
            sync_notion_project(project_db, project_id)
        except NotionSyncBusyError:
            pass
        except Exception:
            pass
        finally:
            project_db.close()
