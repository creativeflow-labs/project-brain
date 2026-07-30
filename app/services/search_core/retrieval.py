from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Chunk, Document
from app.services.embeddings import embedding_service
from app.services.record_metadata import infer_record_type
from app.services.search_core.types import EvidencePacket, QueryPlan, SearchCandidate

settings = get_settings()

KOREAN_SUFFIXES = (
    "에서는",
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "처럼",
    "인가요",
    "이야",
    "이에요",
    "예요",
    "의",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "와",
    "과",
    "에",
    "도",
    "로",
)
STOP_WORDS = {
    "그리고",
    "그러나",
    "대한",
    "관련",
    "알려줘",
    "무엇",
    "누구",
    "얼마",
    "그럼",
    "후속",
    "질문",
    "the",
    "and",
    "for",
    "from",
}

DATE_KEYS = {
    "published_at": (
        "published_at",
        "recorded_upload_at",
        "recorded_at",
        "published_time",
        "upload_time",
        "uploaded_at",
        "date",
    ),
    "received_at": ("received_at", "date"),
    "sent_at": ("sent_at", "date"),
    "updated_at": ("updated_at", "last_edited_time", "modified_time"),
    "occurred_at": (
        "occurred_at",
        "event_time",
        "date",
        "published_at",
        "sent_at",
        "received_at",
    ),
}


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[가-힣]+|[a-zA-Z0-9][a-zA-Z0-9._-]*", text.lower()):
        if len(raw) < 2 or raw in STOP_WORDS:
            continue
        tokens.append(raw)
        if re.fullmatch(r"[가-힣]+", raw):
            for suffix in KOREAN_SUFFIXES:
                if raw.endswith(suffix) and len(raw) - len(suffix) >= 2:
                    tokens.append(raw[: -len(suffix)])
                    break
    return tokens


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right)) or 1.0
    return dot / (left_norm * right_norm)


def _bm25(
    query_tokens: list[str],
    document_tokens: list[list[str]],
) -> list[float]:
    if not query_tokens or not document_tokens:
        return [0.0] * len(document_tokens)
    counts = [Counter(tokens) for tokens in document_tokens]
    average_length = (
        sum(len(tokens) for tokens in document_tokens) / len(document_tokens)
    ) or 1.0
    document_frequency = Counter(
        token for counter in counts for token in set(counter) if token in query_tokens
    )
    scores: list[float] = []
    for tokens, counter in zip(document_tokens, counts, strict=True):
        score = 0.0
        for token in set(query_tokens):
            frequency = counter.get(token, 0)
            if not frequency:
                continue
            inverse_frequency = math.log(
                1
                + (len(document_tokens) - document_frequency[token] + 0.5)
                / (document_frequency[token] + 0.5)
            )
            denominator = frequency + 1.5 * (
                1 - 0.75 + 0.75 * len(tokens) / average_length
            )
            score += inverse_frequency * (frequency * 2.5) / denominator
        scores.append(score)
    maximum = max(scores, default=0.0)
    return [score / maximum if maximum else 0.0 for score in scores]


def _row_text(chunk: Chunk, document: Document) -> str:
    metadata = json.dumps(document.metadata_json or {}, ensure_ascii=False)
    return f"{document.title}\n{metadata}\n{chunk.text}"


def _normalise_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _normalise_datetime(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return _normalise_datetime(parsedate_to_datetime(raw))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return _normalise_datetime(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _text_datetimes(text: str, fallback_year: int) -> list[datetime]:
    values: list[datetime] = []
    patterns = (
        r"(?P<year>20\d{2})[-./]\s*(?P<month>1[0-2]|0?[1-9])[-./]\s*(?P<day>3[01]|[12]\d|0?[1-9])",
        r"(?P<year>20\d{2})\s*년\s*(?P<month>1[0-2]|0?[1-9])\s*월(?:\s*(?P<day>3[01]|[12]\d|0?[1-9])\s*일)?",
        r"(?<!\d)(?P<month>1[0-2]|0?[1-9])\s*월(?:\s*(?P<day>3[01]|[12]\d|0?[1-9])\s*일)?",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            groups = match.groupdict()
            try:
                values.append(
                    datetime(
                        int(groups.get("year") or fallback_year),
                        int(groups["month"]),
                        int(groups.get("day") or 1),
                        tzinfo=timezone.utc,
                    )
                )
            except ValueError:
                continue
    for match in re.finditer(
        r"(?<!\d)(?P<year>\d{2})[.-]\s*"
        r"(?P<month>1[0-2]|0?[1-9])[.-]\s*"
        r"(?P<day>3[01]|[12]\d|0?[1-9])(?!\d)",
        text,
    ):
        try:
            values.append(
                datetime(
                    2000 + int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                    tzinfo=timezone.utc,
                )
            )
        except ValueError:
            continue
    return values


def _scoped_document_ids(
    rows: list[tuple[Chunk, Document]], plan: QueryPlan
) -> set[str]:
    if plan.retrieval_mode != "scoped" or not plan.record_type:
        return {document.id for _, document in rows}
    document_text: defaultdict[str, list[str]] = defaultdict(list)
    documents: dict[str, Document] = {}
    for chunk, document in rows:
        documents[document.id] = document
        document_text[document.id].append(chunk.text)
    matching_ids = {
        document_id
        for document_id, document in documents.items()
        if document.source_type == "notion"
        and infer_record_type(
            document.title,
            "\n".join(document_text[document_id]),
            document.metadata_json or {},
        )
        == plan.record_type
    }
    grouped: defaultdict[str, set[str]] = defaultdict(set)
    unscoped_ids: set[str] = set()
    for document_id in matching_ids:
        metadata = documents[document_id].metadata_json or {}
        collection_id = metadata.get("collection_id")
        if collection_id:
            grouped[str(collection_id)].add(document_id)
        else:
            unscoped_ids.add(document_id)
    if not grouped or len(matching_ids) == 0:
        return matching_ids
    scoped_coverage = (len(matching_ids) - len(unscoped_ids)) / len(matching_ids)
    if scoped_coverage < 0.8:
        return matching_ids

    def group_score(item: tuple[str, set[str]]) -> tuple[int, int]:
        _, document_ids = item
        searchable_parts: list[str] = []
        for document_id in document_ids:
            searchable_parts.extend(
                [
                    str(
                        (documents[document_id].metadata_json or {}).get(
                            "collection_title", ""
                        )
                    ),
                    documents[document_id].title,
                    *document_text[document_id][:2],
                ]
            )
        searchable = "\n".join(searchable_parts)
        lowered = searchable.lower()
        entity_hits = sum(
            1 for term in plan.entity_terms if term and term.lower() in lowered
        )
        return entity_hits, len(document_ids)

    _, selected_ids = max(grouped.items(), key=group_score)
    return selected_ids


def _row_matches_time_range(chunk: Chunk, document: Document, plan: QueryPlan) -> bool:
    if plan.time_start is None or plan.time_end is None:
        return True
    metadata = document.metadata_json or {}
    keys = DATE_KEYS.get(plan.time_field or "occurred_at", DATE_KEYS["occurred_at"])
    values = [
        parsed
        for key in keys
        if metadata.get(key) is not None
        for parsed in [_parse_datetime(metadata[key])]
        if parsed is not None
    ]
    values.extend(
        _text_datetimes(
            f"{document.title}\n{chunk.text}",
            plan.time_start.year,
        )
    )
    start = _normalise_datetime(plan.time_start)
    end = _normalise_datetime(plan.time_end)
    return any(start <= _normalise_datetime(value) <= end for value in values)


def retrieve_candidates(
    db: Session, project_id: str, plan: QueryPlan
) -> list[SearchCandidate]:
    rows = db.execute(
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.project_id == project_id)
    ).all()
    scoped_ids = _scoped_document_ids(rows, plan)
    rows = [(chunk, document) for chunk, document in rows if document.id in scoped_ids]
    if plan.time_start is not None and plan.time_end is not None:
        rows = [
            (chunk, document)
            for chunk, document in rows
            if _row_matches_time_range(chunk, document, plan)
        ]
    if not rows:
        return []

    texts = [_row_text(chunk, document) for chunk, document in rows]
    tokenized = [_tokens(text) for text in texts]
    embeddings = embedding_service.embed_many(plan.search_queries)
    reciprocal_scores: defaultdict[int, float] = defaultdict(float)
    vector_best: defaultdict[int, float] = defaultdict(float)
    lexical_best: defaultdict[int, float] = defaultdict(float)
    exact_best: defaultdict[int, float] = defaultdict(float)

    for query, query_embedding in zip(plan.search_queries, embeddings, strict=True):
        query_tokens = _tokens(query)
        lexical_scores = _bm25(query_tokens, tokenized)
        vector_scores: list[float] = []
        exact_scores: list[float] = []
        normalized_query = re.sub(r"\s+", " ", query.lower()).strip()
        for index, ((chunk, document), text) in enumerate(zip(rows, texts, strict=True)):
            vector = list(chunk.embedding or [])
            vector_score = (
                (_cosine(query_embedding, vector) + 1.0) / 2.0 if vector else 0.0
            )
            lowered = text.lower()
            title = document.title.lower()
            entity_hits = sum(
                1 for term in plan.entity_terms if term and term in lowered
            )
            exact_score = min(
                1.0,
                (0.55 if normalized_query and normalized_query in lowered else 0.0)
                + (0.18 if any(term in title for term in plan.entity_terms) else 0.0)
                + min(0.36, entity_hits * 0.12),
            )
            vector_scores.append(vector_score)
            exact_scores.append(exact_score)
            vector_best[index] = max(vector_best[index], vector_score)
            lexical_best[index] = max(lexical_best[index], lexical_scores[index])
            exact_best[index] = max(exact_best[index], exact_score)

        for weight, scores in (
            (1.0, vector_scores),
            (1.15, lexical_scores),
            (1.25, exact_scores),
        ):
            ranked_indices = sorted(
                range(len(scores)), key=lambda index: scores[index], reverse=True
            )[: settings.search_candidate_limit]
            for rank, index in enumerate(ranked_indices, start=1):
                if scores[index] > 0:
                    reciprocal_scores[index] += weight / (60 + rank)

    combined: list[tuple[int, float]] = []
    maximum_rrf = max(reciprocal_scores.values(), default=1.0)
    for index in range(len(rows)):
        rrf = reciprocal_scores[index] / maximum_rrf
        score = (
            rrf * 0.55
            + lexical_best[index] * 0.18
            + vector_best[index] * 0.12
            + exact_best[index] * 0.15
        )
        combined.append((index, min(score, 1.0)))

    combined.sort(key=lambda item: item[1], reverse=True)
    candidates: list[SearchCandidate] = []
    selected_combined = combined[: settings.search_rerank_limit]
    if plan.cardinality == "all":
        document_limit = (
            settings.search_compose_evidence_limit
            if plan.task_mode == "compose" and plan.scope_mode == "collection"
            else settings.search_scoped_record_limit
            if plan.retrieval_mode == "scoped"
            else settings.search_collection_evidence_limit
        )
        selected_combined = []
        selected_document_ids: set[str] = set()
        for index, score in combined:
            document_id = rows[index][1].id
            if document_id in selected_document_ids:
                continue
            selected_combined.append((index, score))
            selected_document_ids.add(document_id)
            if len(selected_document_ids) >= document_limit:
                break
        selected_indices = {index for index, _ in selected_combined}
        per_document = Counter(rows[index][1].id for index in selected_indices)
        for index, score in combined:
            document_id = rows[index][1].id
            if index in selected_indices or document_id not in selected_document_ids:
                continue
            if per_document[document_id] >= 5:
                continue
            selected_combined.append((index, score))
            selected_indices.add(index)
            per_document[document_id] += 1
            if len(selected_combined) >= settings.search_candidate_limit:
                break
    for index, score in selected_combined:
        chunk, document = rows[index]
        candidates.append(
            SearchCandidate(
                chunk=chunk,
                document=document,
                score=score,
                vector_score=vector_best[index],
                lexical_score=lexical_best[index],
                exact_score=exact_best[index],
            )
        )
    return candidates


def _expanded_document_text(
    chunks: list[Chunk], anchors: list[Chunk], source_type: str
) -> str:
    ordered = sorted(chunks, key=lambda item: item.position)
    total_chars = sum(len(chunk.text) for chunk in ordered)
    if total_chars <= settings.search_parent_max_chars:
        selected = ordered
    else:
        radius = 2 if source_type == "notion" else 1
        positions = {
            position
            for anchor in anchors
            for position in range(anchor.position - radius, anchor.position + radius + 1)
        }
        selected = [chunk for chunk in ordered if chunk.position in positions]
    text = "\n".join(chunk.text.strip() for chunk in selected if chunk.text.strip())
    if source_type == "gmail":
        text = re.split(
            (
                r"\nOn .{0,300}wrote:\s*\n"
                r"|\n-{2,}\s*Original Message\s*-{2,}\s*\n"
                r"|\n보낸 사람:\s*.{0,300}\n보낸 날짜:"
            ),
            text,
            maxsplit=1,
            flags=re.IGNORECASE | re.DOTALL,
        )[0].strip()
    return text


def build_evidence_packets(
    db: Session,
    project_id: str,
    plan: QueryPlan,
    candidates: list[SearchCandidate],
) -> list[EvidencePacket]:
    if not candidates:
        return []
    by_document: defaultdict[str, list[SearchCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_document[candidate.document.id].append(candidate)

    related_documents: dict[str, tuple[Document, float]] = {}
    thread_scores: dict[str, float] = {}
    for matches in by_document.values():
        document = matches[0].document
        thread_id = (document.metadata_json or {}).get("thread_id")
        if document.source_type == "gmail" and thread_id:
            thread_scores[thread_id] = max(
                thread_scores.get(thread_id, 0.0),
                max(match.score for match in matches),
            )
    if thread_scores:
        gmail_documents = db.scalars(
            select(Document).where(
                Document.project_id == project_id,
                Document.source_type == "gmail",
            )
        ).all()
        for document in gmail_documents:
            thread_id = (document.metadata_json or {}).get("thread_id")
            if thread_id in thread_scores and document.id not in by_document:
                related_documents[document.id] = (
                    document,
                    thread_scores[thread_id] * 0.88,
                )

    document_ids = [*by_document, *related_documents]
    all_chunks = db.scalars(
        select(Chunk)
        .where(Chunk.project_id == project_id, Chunk.document_id.in_(document_ids))
        .order_by(Chunk.document_id, Chunk.position)
    ).all()
    chunks_by_document: defaultdict[str, list[Chunk]] = defaultdict(list)
    for chunk in all_chunks:
        chunks_by_document[chunk.document_id].append(chunk)

    packets: list[EvidencePacket] = []
    for document_id, matches in by_document.items():
        matches.sort(key=lambda item: item.score, reverse=True)
        document = matches[0].document
        anchors = [match.chunk for match in matches[:5]]
        context = _expanded_document_text(
            chunks_by_document[document_id], anchors, document.source_type
        )
        metadata = document.metadata_json or {}
        header_parts = [f"문서 제목: {document.title}"]
        for key in ("from", "to", "date", "thread_id", "last_edited_time"):
            if metadata.get(key):
                header_parts.append(f"{key}: {metadata[key]}")
        context_text = "\n".join([*header_parts, "", context]).strip()
        packets.append(
            EvidencePacket(
                document=document,
                anchor_chunks=anchors,
                context_text=context_text,
                score=matches[0].score,
                matched_queries=[
                    query
                    for query in plan.search_queries
                    if set(_tokens(query)) & set(_tokens(context_text))
                ],
            )
        )

    for document_id, (document, related_score) in related_documents.items():
        anchors = chunks_by_document[document_id][:5]
        context = _expanded_document_text(
            chunks_by_document[document_id], anchors, document.source_type
        )
        metadata = document.metadata_json or {}
        context_text = "\n".join(
            [
                f"문서 제목: {document.title}",
                *[
                    f"{key}: {metadata[key]}"
                    for key in ("from", "to", "date", "thread_id")
                    if metadata.get(key)
                ],
                "",
                context,
            ]
        ).strip()
        packets.append(
            EvidencePacket(
                document=document,
                anchor_chunks=anchors,
                context_text=context_text,
                score=related_score,
                matched_queries=["Gmail 동일 스레드 문맥"],
            )
        )

    packets.sort(key=lambda packet: packet.score, reverse=True)
    limit = (
        settings.search_compose_evidence_limit
        if plan.task_mode == "compose" and plan.scope_mode == "collection"
        else settings.search_scoped_record_limit
        if plan.retrieval_mode == "scoped"
        else settings.search_collection_evidence_limit
        if plan.cardinality == "all"
        else settings.search_evidence_limit
    )
    selected = packets[:limit]
    if packets:
        best_score = packets[0].score
        selected_ids = {packet.document.id for packet in selected}
        for source_type in {packet.document.source_type for packet in packets}:
            source_best = next(
                packet
                for packet in packets
                if packet.document.source_type == source_type
            )
            if (
                source_best.document.id not in selected_ids
                and source_best.score >= best_score * 0.35
            ):
                if len(selected) >= limit:
                    selected[-1] = source_best
                else:
                    selected.append(source_best)
                selected_ids.add(source_best.document.id)
    selected.sort(key=lambda packet: packet.score, reverse=True)
    return selected
