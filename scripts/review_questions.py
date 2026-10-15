"""Run a private question list sequentially without grading model prose as truth.

Input and responses belong under ignored data/accuracy-review/, never in Git.
Uses the normal API, creating an ordinary new conversation and search traces.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--project", required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    if args.output.exists():
        raise SystemExit("Output exists; use a new run name to preserve evidence.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    thread_id = None
    results = []
    for index, question in enumerate(questions, start=1):
        started = time.perf_counter()
        request = urllib.request.Request(
            args.base.rstrip("/") + "/api/ask",
            data=json.dumps({"project_id": args.project, "question": question,
                             "thread_id": thread_id, "top_k": 6}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            thread_id = result["thread_id"]
            row = {"number": index, "question": question,
                   "seconds": round(time.perf_counter() - started, 3),
                   "response": result}
            print(f"{index}/{len(questions)} {row['seconds']}s "
                  f"{result['evidence_status']} citations={len(result['citations'])}", flush=True)
        except Exception as exc:
            row = {"number": index, "question": question,
                   "seconds": round(time.perf_counter() - started, 3),
                   "error": str(exc)}
            print(f"{index}/{len(questions)} ERROR {type(exc).__name__}", flush=True)
            results.append(row)
            args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            raise SystemExit("Stopped: context chain cannot be compared after an error.")
        results.append(row)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved private responses; human review required.", flush=True)


if __name__ == "__main__":
    main()
