from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.connectors.gmail import (
    GMAIL_SCOPES,
    GmailConnector,
    credentials_from_dict,
    credentials_to_dict,
)
from app.connectors.notion import NotionApiError, NotionConnector
from app.db import get_db, init_db
from app.models import ConnectorCredential, Document, Project
from app.schemas import (
    AskRequest,
    AskResponse,
    DocumentOut,
    GmailSyncRequest,
    NotionConnectRequest,
    NotionImportRequest,
    NotionSyncRequest,
    ProjectCreate,
    ProjectOut,
)
from app.services.ingestion import IngestionInput, ingest_document
from app.services.notion_sync import (
    NotionSyncBusyError,
    connect_notion,
    get_notion_status,
    sync_due_notion_connections,
    sync_notion_project,
)
from app.services.parser import ParseError, parse_file_bytes
from app.services.rag import answer_question
from app.services.security import decrypt_json, encrypt_json
from app.version import APP_VERSION

settings = get_settings()
logger = logging.getLogger(__name__)

GMAIL_OAUTH_STATE_TTL_SECONDS = 600


async def notion_scheduler_loop() -> None:
    while True:
        await asyncio.sleep(settings.notion_scheduler_poll_seconds)
        await asyncio.to_thread(sync_due_notion_connections)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    scheduler_task = asyncio.create_task(notion_scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=settings.app_name, version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return project


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse(
        (Path(__file__).parent / "static" / "index.html").read_text("utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "database": "postgresql+pgvector" if settings.is_postgres else "sqlite",
        "answer_mode": "generative" if settings.openai_api_key else "extractive",
        "version": APP_VERSION,
    }


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.scalars(select(Project).order_by(Project.created_at.desc())).all()


@app.post("/api/projects", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(name=payload.name.strip(), description=payload.description.strip())
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@app.get("/api/projects/{project_id}/documents", response_model=list[DocumentOut])
def list_documents(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return db.scalars(
        select(Document)
        .where(Document.project_id == project_id)
        .order_by(Document.created_at.desc())
    ).all()


@app.delete("/api/documents/{document_id}", status_code=204)
def delete_document(document_id: str, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    stored_path_value = (document.metadata_json or {}).get("stored_path")
    if document.source_type == "upload" and stored_path_value:
        stored_path = Path(stored_path_value).resolve()
        upload_root = settings.upload_dir.resolve()
        if stored_path.is_relative_to(upload_root) and stored_path.is_file():
            stored_path.unlink()
    db.delete(document)
    db.commit()
    return None


@app.post("/api/documents/upload", response_model=DocumentOut)
async def upload_document(
    project_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    require_project(db, project_id)
    filename = Path(file.filename or "upload").name
    content = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail=f"파일은 {settings.max_upload_mb}MB 이하만 가능합니다."
        )
    try:
        parsed = parse_file_bytes(content, filename, file.content_type)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    digest = hashlib.sha256(content).hexdigest()
    project_dir = settings.upload_dir / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    stored_path = project_dir / f"{digest[:16]}-{filename}"
    if not stored_path.exists():
        stored_path.write_bytes(content)

    document, _ = ingest_document(
        db,
        IngestionInput(
            project_id=project_id,
            source_type="upload",
            external_id=digest,
            title=parsed.title,
            text=parsed.text,
            filename=filename,
            mime_type=parsed.mime_type,
            metadata={"stored_path": str(stored_path), "size": len(content)},
            warnings=parsed.warnings,
            sections=parsed.sections,
        ),
    )
    return document


@app.post("/api/ask", response_model=AskResponse)
def ask(payload: AskRequest, db: Session = Depends(get_db)):
    require_project(db, payload.project_id)
    try:
        return answer_question(
            db,
            payload.project_id,
            payload.question,
            payload.top_k,
            payload.thread_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"답변 생성에 실패했습니다: {exc}") from exc


@app.post("/api/connectors/notion/import")
def import_notion(payload: NotionImportRequest, db: Session = Depends(get_db)):
    require_project(db, payload.project_id)
    connector = NotionConnector(payload.integration_token)
    aggregate = {"pages": 0, "attachments": 0, "unchanged": 0, "errors": []}
    for page_id in payload.page_ids:
        try:
            page = connector.fetch_page(page_id)
            result = connector.sync_page(db, payload.project_id, page)
            aggregate["pages"] += result.pages
            aggregate["attachments"] += result.attachments
            aggregate["unchanged"] += result.unchanged
            aggregate["errors"].extend(result.warnings)
        except Exception as exc:
            db.rollback()
            aggregate["errors"].append(f"{page_id}: {exc}")
    connector.close()
    return aggregate


@app.post("/api/connectors/notion/connect")
def notion_connect(payload: NotionConnectRequest, db: Session = Depends(get_db)):
    require_project(db, payload.project_id)
    try:
        return connect_notion(
            db,
            payload.project_id,
            payload.access_token,
            payload.auth_mode,
            payload.auto_sync_enabled,
            payload.sync_interval_minutes,
        )
    except NotionApiError as exc:
        status_code = 401 if exc.status_code in {401, 403} else 502
        raise HTTPException(
            status_code=status_code, detail=f"Notion 토큰 확인 실패: {exc}"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Notion 연결 저장 실패: {exc}"
        ) from exc


@app.get("/api/connectors/notion/status")
def notion_status(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    return get_notion_status(db, project_id)


@app.post("/api/connectors/notion/sync")
def notion_sync(payload: NotionSyncRequest, db: Session = Depends(get_db)):
    require_project(db, payload.project_id)
    try:
        return sync_notion_project(db, payload.project_id, payload.force)
    except NotionSyncBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotionApiError as exc:
        status_code = 401 if exc.status_code in {401, 403} else 502
        raise HTTPException(
            status_code=status_code, detail=f"Notion 동기화 실패: {exc}"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Notion 동기화 실패: {exc}"
        ) from exc


def _google_client_config() -> dict:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_CLIENT_ID와 GOOGLE_CLIENT_SECRET을 .env에 설정해 주세요.",
        )
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def _google_flow(
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    if settings.google_redirect_uri.startswith("http://localhost"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    return Flow.from_client_config(
        _google_client_config(),
        scopes=GMAIL_SCOPES,
        state=state,
        redirect_uri=settings.google_redirect_uri,
        code_verifier=code_verifier,
        autogenerate_code_verifier=False,
    )


@app.get("/api/connectors/gmail/auth-url")
def gmail_auth_url(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    code_verifier = secrets.token_urlsafe(64)
    state = encrypt_json(
        {
            "provider": "gmail",
            "project_id": project_id,
            "code_verifier": code_verifier,
        }
    )
    flow = _google_flow(state=state, code_verifier=code_verifier)
    authorization_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return {"authorization_url": authorization_url}


@app.get("/api/connectors/gmail/callback")
def gmail_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error or not code:
        return RedirectResponse(
            url="/?gmail=error&reason=access_denied", status_code=303
        )

    try:
        oauth_payload = decrypt_json(
            state, ttl_seconds=GMAIL_OAUTH_STATE_TTL_SECONDS
        )
    except (InvalidToken, ValueError, TypeError):
        return RedirectResponse(
            url="/?gmail=error&reason=invalid_state", status_code=303
        )

    project_id = oauth_payload.get("project_id")
    code_verifier = oauth_payload.get("code_verifier")
    provider = oauth_payload.get("provider")
    if (
        provider != "gmail"
        or not isinstance(project_id, str)
        or not isinstance(code_verifier, str)
    ):
        return RedirectResponse(
            url="/?gmail=error&reason=invalid_state", status_code=303
        )

    require_project(db, project_id)
    flow = _google_flow(state=state, code_verifier=code_verifier)
    try:
        flow.fetch_token(code=code)
    except Exception:
        logger.exception("Gmail OAuth 토큰 교환에 실패했습니다.")
        return RedirectResponse(
            url="/?gmail=error&reason=token_exchange", status_code=303
        )

    encrypted = encrypt_json(credentials_to_dict(flow.credentials))
    credential = db.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.project_id == project_id,
            ConnectorCredential.provider == "gmail",
        )
    )
    if credential:
        credential.encrypted_payload = encrypted
    else:
        db.add(
            ConnectorCredential(
                project_id=project_id, provider="gmail", encrypted_payload=encrypted
            )
        )
    db.commit()
    return RedirectResponse(url="/?gmail=connected", status_code=303)


@app.post("/api/connectors/gmail/sync")
def gmail_sync(payload: GmailSyncRequest, db: Session = Depends(get_db)):
    require_project(db, payload.project_id)
    credential = db.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.project_id == payload.project_id,
            ConnectorCredential.provider == "gmail",
        )
    )
    if not credential:
        raise HTTPException(status_code=409, detail="Gmail을 먼저 연결해 주세요.")
    try:
        credentials = credentials_from_dict(decrypt_json(credential.encrypted_payload))
        result = GmailConnector(credentials).sync(
            db, payload.project_id, payload.query, payload.max_results
        )
        credential.encrypted_payload = encrypt_json(credentials_to_dict(credentials))
        db.commit()
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gmail 동기화 실패: {exc}") from exc
