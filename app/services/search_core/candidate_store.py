from __future__ import annotations

import re

from sqlalchemy import Text, case, cast, func, literal_column, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Chunk, Document
from app.services.search_core.scoring import tokenize
from app.services.search_core.types import QueryPlan


settings = get_settings()
CandidateRow = tuple[Chunk, Document]


def _korean_prefix_query(terms: list[str]):
    """Supplement simple FTS for Korean particles; never prefix Latin IDs.

    Keep the original exact lexical pool first. Prefix matches only fill unused
    slots, so existing exact candidates and their tie order are not displaced.
    Only complete Hangul terms are allowed in the tsquery expression.
    """
    korean_terms = list(dict.fromkeys(
        term for term in terms if re.fullmatch(r"[가-힣]{2,}", term)
    ))
    if not korean_terms:
        return None
    expression = " | ".join(f"'{term}':*" for term in korean_terms)
    return func.to_tsquery(literal_column("'simple'"), expression)


def _load_all_project_rows(db: Session, project_id: str) -> list[CandidateRow]:
    return list(
        db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.project_id == project_id)
            .order_by(Document.id, Chunk.position, Chunk.id)
        ).all()
    )


def _postgres_candidate_rows(
    db: Session,
    project_id: str,
    plan: QueryPlan,
    query_embeddings: list[list[float]],
) -> list[CandidateRow]:
    rows_by_chunk: dict[str, CandidateRow] = {}
    limit = settings.search_candidate_limit

    for query_embedding in query_embeddings:
        distance = Chunk.embedding.cosine_distance(query_embedding)
        vector_rows = db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.project_id == project_id)
            .order_by(distance, Document.id, Chunk.position, Chunk.id)
            .limit(limit)
        ).all()
        for chunk, document in vector_rows:
            rows_by_chunk[chunk.id] = (chunk, document)

    searchable = func.concat_ws(
        " ",
        Document.title,
        cast(Document.metadata_json, Text),
        Chunk.text,
    )
    text_vector = func.to_tsvector(literal_column("'simple'"), searchable)
    for query in plan.search_queries:
        terms = list(dict.fromkeys([*tokenize(query), *plan.entity_terms]))[:12]
        if not terms:
            continue
        web_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
        text_query = func.websearch_to_tsquery(
            literal_column("'simple'"), web_query
        )
        rank = func.ts_rank_cd(text_vector, text_query)
        exact_match = text_vector.op("@@")(text_query)
        prefix_query = _korean_prefix_query(terms)
        matches = exact_match
        ordering = [rank.desc(), Document.id, Chunk.position, Chunk.id]
        if prefix_query is not None:
            matches = exact_match | text_vector.op("@@")(prefix_query)
            supplemental_rank = case(
                (exact_match, 0.0),
                else_=func.ts_rank_cd(text_vector, prefix_query),
            )
            ordering = [exact_match.desc(), rank.desc(), supplemental_rank.desc(),
                        Document.id, Chunk.position, Chunk.id]
        lexical_rows = db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Chunk.project_id == project_id,
                matches,
            )
            .order_by(*ordering)
            .limit(limit)
        ).all()
        for chunk, document in lexical_rows:
            rows_by_chunk[chunk.id] = (chunk, document)

    return sorted(
        rows_by_chunk.values(),
        key=lambda row: (row[1].id, row[0].position, row[0].id),
    )


def load_candidate_rows(
    db: Session,
    project_id: str,
    plan: QueryPlan,
    query_embeddings: list[list[float]],
) -> list[CandidateRow]:
    dialect = db.get_bind().dialect.name
    use_bounded_postgres_search = (
        dialect == "postgresql"
        and plan.retrieval_mode == "hybrid"
        and plan.cardinality == "one"
    )
    if use_bounded_postgres_search:
        rows = _postgres_candidate_rows(
            db, project_id, plan, query_embeddings
        )
        if rows:
            return rows
    return _load_all_project_rows(db, project_id)
