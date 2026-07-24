from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

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
from app.connectors.notion import NotionConnector
from app.db import get_db, init_db
from app.models import ConnectorCredential, Document, Project
from app.schemas import (
    AskRequest,
    AskResponse,
    DocumentOut,
    GmailSyncRequest,
    NotionImportRequest,
    ProjectCreate,
    ProjectOut,
)
from app.services.ingestion import IngestionInput, ingest_document
from app.services.parser import ParseError, parse_file_bytes
from app.services.rag import answer_question
from app.services.security import decrypt_json, encrypt_json

settings = get_settings()
oauth_states: dict[str, str] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def require_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return project


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text("utf-8"))


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "database": "postgresql+pgvector" if settings.is_postgres else "sqlite",
        "answer_mode": "generative" if settings.openai_api_key else "extractive",
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
        return answer_question(db, payload.project_id, payload.question, payload.top_k)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"답변 생성에 실패했습니다: {exc}") from exc


@app.post("/api/connectors/notion/import")
def import_notion(payload: NotionImportRequest, db: Session = Depends(get_db)):
    require_project(db, payload.project_id)
    connector = NotionConnector(payload.integration_token)
    aggregate = {"pages": 0, "attachments": 0, "unchanged": 0, "errors": []}
    for page_id in payload.page_ids:
        try:
            result = connector.sync_page(db, payload.project_id, page_id)
            aggregate["pages"] += result.pages
            aggregate["attachments"] += result.attachments
            aggregate["unchanged"] += result.unchanged
            aggregate["errors"].extend(result.errors or [])
        except Exception as exc:
            aggregate["errors"].append(f"{page_id}: {exc}")
    return aggregate


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


def _google_flow(state: str | None = None) -> Flow:
    if settings.google_redirect_uri.startswith("http://localhost"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    return Flow.from_client_config(
        _google_client_config(),
        scopes=GMAIL_SCOPES,
        state=state,
        redirect_uri=settings.google_redirect_uri,
    )


@app.get("/api/connectors/gmail/auth-url")
def gmail_auth_url(project_id: str, db: Session = Depends(get_db)):
    require_project(db, project_id)
    state = secrets.token_urlsafe(32)
    oauth_states[state] = project_id
    flow = _google_flow(state)
    authorization_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return {"authorization_url": authorization_url}


@app.get("/api/connectors/gmail/callback")
def gmail_callback(state: str, code: str, db: Session = Depends(get_db)):
    project_id = oauth_states.pop(state, None)
    if not project_id:
        raise HTTPException(status_code=400, detail="OAuth 상태가 만료되었거나 유효하지 않습니다.")
    require_project(db, project_id)
    if not settings.token_encryption_key:
        raise HTTPException(
            status_code=503, detail="TOKEN_ENCRYPTION_KEY를 .env에 설정해 주세요."
        )
    flow = _google_flow(state)
    flow.fetch_token(code=code)
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
    return RedirectResponse(url="/?gmail=connected")


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
