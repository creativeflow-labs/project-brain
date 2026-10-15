"""Read-only retrieval replay with frozen plans/embeddings; no answer-model calls.

Keep --frozen and --output under ignored data/. Compare candidate order/scores
and evidence hashes, not variable model prose. This never updates user records.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from time import perf_counter

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import ConversationMessage, SearchTrace
from app.services.search_core import retrieval
from app.services.search_core.types import QueryPlan


def assert_same_results(reference, results):
    for before, after in zip(reference, results, strict=True):
        left, right = before["snapshot"], after["snapshot"]
        assert before["label"] == after["label"]
        assert left["evidence"] == right["evidence"], after["label"]
        assert left["matched_count"] == right["matched_count"], after["label"]
        for a, b in zip(left["candidates"], right["candidates"], strict=True):
            assert a[0] == b[0] and a[2] == b[2], after["label"]
            # BM25 iterates a token set: process hash seeds can change float
            # addition order by one ULP even for UNCHANGED retrieval code.
            assert math.isclose(a[1], b[1], rel_tol=0, abs_tol=1e-12), after["label"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output exists; preserve previous measurements.")
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        if args.frozen.exists():
            cases = json.loads(args.frozen.read_text())
        else:
            cases = []
            for label in ("김그륜", "sk 다이렉트", "ai 웨비나", "6월 영상 제목"):
                user = db.scalar(select(ConversationMessage).where(
                    ConversationMessage.role == "user",
                    ConversationMessage.content.ilike(f"%{label}%"),
                ).order_by(ConversationMessage.created_at.desc()).limit(1))
                if user is None:
                    raise ValueError(f"No saved question for {label}")
                # SearchTrace is flushed BEFORE its user message is inserted.
                trace = db.scalar(select(SearchTrace).where(
                    SearchTrace.thread_id == user.thread_id,
                    SearchTrace.created_at <= user.created_at,
                ).order_by(SearchTrace.created_at.desc()).limit(1))
                saved = trace.trace_json
                plan = QueryPlan(
                    original_question=user.content,
                    resolved_question=user.resolved_question or user.content,
                    search_queries=saved["search_queries"],
                    entity_terms=saved["entity_terms"],
                    cardinality=saved["cardinality"],
                    retrieval_mode=saved["retrieval_mode"],
                    record_type=saved.get("record_type"),
                )
                if saved.get("time_range"):
                    plan.time_start = datetime.fromisoformat(saved["time_range"]["start"])
                    plan.time_end = datetime.fromisoformat(saved["time_range"]["end"])
                    plan.time_field = saved["time_range"]["field"]
                cases.append({"label": label, "project_id": trace.project_id,
                    "plan": asdict(plan), "embeddings": retrieval.embedding_service.embed_queries(plan.search_queries)})
            args.frozen.write_text(json.dumps(cases, ensure_ascii=False, default=str))
        results = []
        for case in cases:
            values = dict(case["plan"])
            for key in ("time_start", "time_end"):
                if isinstance(values.get(key), str):
                    values[key] = datetime.fromisoformat(values[key])
            plan = QueryPlan(**values)
            retrieval.embedding_service.embed_queries = lambda queries: case["embeddings"]
            times = []
            snapshots = []
            for _ in range(args.repeat):
                started = perf_counter()
                candidates = retrieval.retrieve_candidates(db, case["project_id"], plan)
                retrieval_time = perf_counter() - started
                packets = retrieval.build_evidence_packets(db, case["project_id"], plan, candidates)
                times.append(round(retrieval_time, 4))
                snapshots.append({
                    "candidates": [(c.chunk.id, c.score, c.hierarchy_score) for c in candidates],
                    "evidence": [(p.document.id, hashlib.sha256(p.context_text.encode()).hexdigest()) for p in packets],
                    "matched_count": plan.collection_match_count,
                })
            if any(snapshot != snapshots[0] for snapshot in snapshots):
                raise AssertionError("Results changed between repeated frozen runs.")
            result = {"label": case["label"], "seconds": times,
                      "median_seconds": median(times), "snapshot": snapshots[0]}
            results.append(result)
            print(json.dumps({key: value for key, value in result.items() if key != "snapshot"}, ensure_ascii=False), flush=True)
        db.rollback()
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    if args.reference:
        reference = json.loads(args.reference.read_text())
        assert_same_results(reference, json.loads(args.output.read_text()))
        print("Candidate orders, hierarchy scores, evidence hashes match exactly; scores agree within 1e-12.")


if __name__ == "__main__":
    main()
