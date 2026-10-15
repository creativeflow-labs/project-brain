"""Replay frozen baseline lexical plans, without model calls or DB writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal
from app.models import SearchTrace
from app.services.search_core.candidate_store import _postgres_candidate_rows
from app.services.search_core.types import QueryPlan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        for row in json.loads(args.input.read_text(encoding="utf-8")):
            trace = db.get(SearchTrace, row["response"]["trace_id"])
            saved = trace.trace_json
            if saved["retrieval_mode"] != "hybrid" or saved["cardinality"] != "one":
                continue
            plan = QueryPlan(original_question=row["question"],
                             resolved_question=row["response"]["resolved_question"],
                             search_queries=saved["search_queries"],
                             entity_terms=saved["entity_terms"])
            # Deliberately omit vector queries to compare only the changed pool.
            rows = _postgres_candidate_rows(db, trace.project_id, plan, [])
            results.append({"number": row["number"],
                            "chunk_ids": [chunk.id for chunk, _ in rows],
                            "document_ids": sorted({doc.id for _, doc in rows})})
        db.rollback()
    if args.output.exists():
        raise SystemExit("Output already exists; preserve previous comparison.")
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Read-only lexical replay: {len(results)} frozen plans; no model calls.")


if __name__ == "__main__":
    main()
