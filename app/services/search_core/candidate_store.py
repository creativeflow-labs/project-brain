from __future__ import annotations

import re

from sqlalchemy import Text, cast, func, literal_column, select, union_all
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Chunk, Document
from app.services.search_core.scoring import tokenize
from app.services.search_core.types import QueryPlan


settings = get_settings()
CandidateRow = tuple[Chunk, Document]


def _korean_prefix_query(terms: list[str]):
    """Supplement simple FTS for Korean particles; never prefix Latin IDs.

    Called only when the original lexical query has zero matches, so ordinary
    lookups retain their original query cost, candidates and ordering.
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


def _query_terms(plan: QueryPlan) -> list[list[str]]:
    return [
        terms for query in plan.search_queries
        if (terms := list(dict.fromkeys([*tokenize(query), *plan.entity_terms]))[:12])
    ]


def _batched_postgres_lexical_rows(
    db: Session, project_id: str, query_terms: list[list[str]],
) -> list[CandidateRow]:
    """Reuse the exact same FTS corpus within ONE request, never across requests.

    Each query keeps its own top-k, tie-breaks and zero-result-only Korean
    fallback. MATERIALIZED avoids rebuilding metadata/body vectors per query.
    No persistent index, cache, schema or search-text changes are involved.
    """
    searchable = func.concat_ws(
        " ", Document.title, cast(Document.metadata_json, Text), Chunk.text,
    )
    corpus = (
        select(
            Chunk.id.label("chunk_id"), Document.id.label("document_id"),
            Chunk.position.label("position"),
            func.to_tsvector(literal_column("'simple'"), searchable).label("vector"),
        )
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.project_id == project_id)
        .cte("lexical_corpus")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )

    def ranked(query):
        return (
            select(corpus.c.chunk_id)
            .where(corpus.c.vector.op("@@")(query))
            .order_by(
                func.ts_rank_cd(corpus.c.vector, query).desc(),
                corpus.c.document_id, corpus.c.position, corpus.c.chunk_id,
            )
            .limit(settings.search_candidate_limit)
        )

    selections = []
    for index, terms in enumerate(query_terms):
        web_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
        primary = ranked(func.websearch_to_tsquery(
            literal_column("'simple'"), web_query,
        )).cte(f"lexical_primary_{index}")
        selections.append(select(primary.c.chunk_id))
        prefix_query = _korean_prefix_query(terms)
        if prefix_query is not None:
            fallback = (
                ranked(prefix_query)
                .where(~select(primary.c.chunk_id).exists())
                .cte(f"lexical_fallback_{index}")
            )
            selections.append(select(fallback.c.chunk_id))
    matches = union_all(*selections).subquery("lexical_matches")
    return list(db.execute(
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.project_id == project_id,
               Chunk.id.in_(select(matches.c.chunk_id)))
        .order_by(Document.id, Chunk.position, Chunk.id)
    ).all())


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

    query_terms = _query_terms(plan)
    if len(query_terms) > 1:
        for chunk, document in _batched_postgres_lexical_rows(db, project_id, query_terms):
            rows_by_chunk[chunk.id] = (chunk, document)
        return sorted(rows_by_chunk.values(),
                      key=lambda row: (row[1].id, row[0].position, row[0].id))

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
        lexical_rows = db.execute(
            select(Chunk, Document)
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Chunk.project_id == project_id,
                text_vector.op("@@")(text_query),
            )
            .order_by(rank.desc(), Document.id, Chunk.position, Chunk.id)
            .limit(limit)
        ).all()
        if not lexical_rows:
            prefix_query = _korean_prefix_query(terms)
            if prefix_query is not None:
                lexical_rows = db.execute(
                    select(Chunk, Document)
                    .join(Document, Chunk.document_id == Document.id)
                    .where(Chunk.project_id == project_id,
                           text_vector.op("@@")(prefix_query))
                    .order_by(func.ts_rank_cd(text_vector, prefix_query).desc(),
                              Document.id, Chunk.position, Chunk.id)
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
