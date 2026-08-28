from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chunk, Document
from app.services.chunker import chunk_document
from app.services.embeddings import embedding_service
from app.services.parser import ParsedDocument, ParsedSection


@dataclass
class IngestionInput:
    project_id: str
    source_type: str
    external_id: str
    title: str
    text: str
    mime_type: str = "text/plain"
    filename: str | None = None
    source_url: str | None = None
    metadata: dict | None = None
    warnings: list[str] | None = None
    sections: list[ParsedSection] | None = None


def build_chunk_search_text(title: str, metadata: dict, chunk_text: str) -> str:
    """PostgreSQL 전문검색에 사용할 문서·청크 통합 문자열을 만듭니다."""

    metadata_text = json.dumps(
        metadata,
        ensure_ascii=True,
        default=str,
    )
    return f"{title}\n{metadata_text}\n{chunk_text}"


def ingest_document(db: Session, item: IngestionInput) -> tuple[Document, bool]:
    content_hash = hashlib.sha256(item.text.encode("utf-8")).hexdigest()
    metadata = item.metadata or {}
    existing = db.scalar(
        select(Document).where(
            Document.project_id == item.project_id,
            Document.source_type == item.source_type,
            Document.external_id == item.external_id,
        )
    )
    if existing and existing.content_hash == content_hash:
        existing.title = item.title
        existing.filename = item.filename
        existing.mime_type = item.mime_type
        existing.source_url = item.source_url
        existing.metadata_json = metadata
        existing.warnings_json = item.warnings or []
        for chunk in existing.chunks:
            chunk.search_text = build_chunk_search_text(
                item.title, metadata, chunk.text
            )
        db.commit()
        db.refresh(existing)
        return existing, False

    if existing:
        document = existing
        document.title = item.title
        document.filename = item.filename
        document.mime_type = item.mime_type
        document.source_url = item.source_url
        document.content_hash = content_hash
        document.metadata_json = metadata
        document.warnings_json = item.warnings or []
        document.chunks.clear()
        db.flush()
    else:
        document = Document(
            project_id=item.project_id,
            source_type=item.source_type,
            external_id=item.external_id,
            title=item.title,
            filename=item.filename,
            mime_type=item.mime_type,
            source_url=item.source_url,
            content_hash=content_hash,
            metadata_json=metadata,
            warnings_json=item.warnings or [],
        )
        db.add(document)
        db.flush()

    parsed = ParsedDocument(
        title=item.title,
        text=item.text,
        mime_type=item.mime_type,
        sections=item.sections or [ParsedSection(text=item.text, locator="본문")],
        warnings=item.warnings or [],
    )
    drafts = chunk_document(parsed)
    embeddings = embedding_service.embed_many([draft.text for draft in drafts])
    for position, (draft, embedding) in enumerate(zip(drafts, embeddings, strict=True)):
        db.add(
            Chunk(
                document_id=document.id,
                project_id=item.project_id,
                position=position,
                text=draft.text,
                search_text=build_chunk_search_text(
                    item.title, metadata, draft.text
                ),
                locator=draft.locator,
                token_estimate=draft.token_estimate,
                embedding=embedding,
            )
        )
    db.commit()
    db.refresh(document)
    return document, True
