from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.parser import ParsedDocument


@dataclass
class ChunkDraft:
    text: str
    locator: str | None
    token_estimate: int


def estimate_tokens(text: str) -> int:
    # 한국어·영어 혼합 텍스트를 위한 보수적 근사치입니다.
    return max(1, len(text) // 2)


def chunk_document(
    parsed: ParsedDocument, target_chars: int = 1200, overlap_chars: int = 180
) -> list[ChunkDraft]:
    chunks: list[ChunkDraft] = []
    for section in parsed.sections:
        cleaned = re.sub(r"\n{3,}", "\n\n", section.text).strip()
        if not cleaned:
            continue
        if len(cleaned) <= target_chars:
            chunks.append(
                ChunkDraft(
                    text=cleaned,
                    locator=section.locator,
                    token_estimate=estimate_tokens(cleaned),
                )
            )
            continue

        start = 0
        while start < len(cleaned):
            end = min(start + target_chars, len(cleaned))
            if end < len(cleaned):
                candidates = [
                    cleaned.rfind("\n", start, end),
                    cleaned.rfind(". ", start, end),
                    cleaned.rfind("다. ", start, end),
                ]
                boundary = max(candidates)
                if boundary > start + target_chars // 2:
                    end = boundary + 1
            piece = cleaned[start:end].strip()
            if piece:
                chunks.append(
                    ChunkDraft(
                        text=piece,
                        locator=section.locator,
                        token_estimate=estimate_tokens(piece),
                    )
                )
            if end >= len(cleaned):
                break
            start = max(end - overlap_chars, start + 1)
    return chunks

