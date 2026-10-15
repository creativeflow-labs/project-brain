"""Make a human-readable, private before/after report from normal API runs."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    if len(before) != len(after):
        raise SystemExit("Runs are incomplete or have different lengths.")
    lines = ["# PB 질문지 변경 전후 비교", "",
             "한 번씩 실행한 실제 API 응답입니다. 사실 정확도 자동 합격 판정이 아닙니다.",
             "모델 변동·캐시·동시 진단 부하가 있어 성능 개선률로 해석하지 않습니다.", "",
             "| 번호 | 이전 상태 | 이후 상태 | 이전/이후 시간(초) | 이전/이후 인용 수 |", "|---|---|---|---|---|"]
    for old, new in zip(before, after, strict=True):
        if old["question"] != new["question"]:
            raise SystemExit("Question order differs.")
        a, b = old["response"], new["response"]
        refs = [int(x) for x in re.findall(r"\[(\d+)\]", b["answer"])]
        if any(x < 1 or x > len(b["citations"]) for x in refs):
            raise SystemExit(f"Broken citation numbering in question {new['number']}")
        lines.append(f"| {new['number']} | {a['evidence_status']} | {b['evidence_status']} | "
                     f"{old['seconds']} / {new['seconds']} | {len(a['citations'])} / {len(b['citations'])} |")
    for old, new in zip(before, after, strict=True):
        lines.extend(["", f"## {new['number']}. {new['question']}", "", "### 변경 전", "",
                      old["response"]["answer"], "", "### 변경 후", "", new["response"]["answer"],
                      "", "### 변경 후 근거", ""])
        lines.extend(f"- [{i}] {citation['title']}" for i, citation in enumerate(new["response"]["citations"], 1))
    if args.output.exists():
        raise SystemExit("Output exists; preserve previous report.")
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Compared {len(after)} ordered questions; citation numbers are in range. Human review required.")


if __name__ == "__main__":
    main()
