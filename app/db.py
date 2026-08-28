from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    if settings.is_postgres:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    chunk_columns = {column["name"] for column in inspect(engine).get_columns("chunks")}
    if "search_text" not in chunk_columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE chunks ADD COLUMN search_text "
                    "TEXT NOT NULL DEFAULT ''"
                )
            )
    if settings.is_postgres:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE chunks AS chunk "
                    "SET search_text = concat_ws(' ', document.title, "
                    "document.metadata_json::text, chunk.text) "
                    "FROM documents AS document "
                    "WHERE document.id = chunk.document_id "
                    "AND (chunk.search_text IS NULL OR chunk.search_text = '')"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_search_text_fts "
                    "ON chunks USING gin "
                    "(to_tsvector('simple', search_text))"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw "
                    "ON chunks USING hnsw (embedding vector_cosine_ops)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_documents_project_source "
                    "ON documents (project_id, source_type)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_documents_gmail_thread_id "
                    "ON documents ((metadata_json ->> 'thread_id')) "
                    "WHERE source_type = 'gmail'"
                )
            )
    else:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE chunks SET search_text = "
                    "COALESCE((SELECT document.title || ' ' || "
                    "document.metadata_json || ' ' || chunks.text "
                    "FROM documents AS document "
                    "WHERE document.id = chunks.document_id), chunks.text) "
                    "WHERE search_text IS NULL OR search_text = ''"
                )
            )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
