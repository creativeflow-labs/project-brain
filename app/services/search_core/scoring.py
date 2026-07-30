from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass


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


def tokenize(text: str) -> list[str]:
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


def vector_norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector)) or 1.0


def cosine_with_norms(
    left: list[float],
    right: list[float],
    left_norm: float,
    right_norm: float,
) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False)) / (
        left_norm * right_norm
    )


@dataclass(frozen=True)
class BM25Corpus:
    counts: list[Counter[str]]
    lengths: list[int]
    average_length: float

    @classmethod
    def from_texts(cls, texts: list[str]) -> "BM25Corpus":
        tokenized = [tokenize(text) for text in texts]
        lengths = [len(tokens) for tokens in tokenized]
        average_length = (sum(lengths) / len(lengths)) if lengths else 1.0
        return cls(
            counts=[Counter(tokens) for tokens in tokenized],
            lengths=lengths,
            average_length=average_length or 1.0,
        )

    def scores(self, query_tokens: list[str]) -> list[float]:
        if not query_tokens or not self.counts:
            return [0.0] * len(self.counts)

        unique_query_tokens = set(query_tokens)
        document_frequency: Counter[str] = Counter()
        for counter in self.counts:
            for token in unique_query_tokens:
                if token in counter:
                    document_frequency[token] += 1

        scores: list[float] = []
        document_count = len(self.counts)
        for length, counter in zip(self.lengths, self.counts, strict=True):
            score = 0.0
            for token in unique_query_tokens:
                frequency = counter.get(token, 0)
                if not frequency:
                    continue
                inverse_frequency = math.log(
                    1
                    + (document_count - document_frequency[token] + 0.5)
                    / (document_frequency[token] + 0.5)
                )
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * length / self.average_length
                )
                score += inverse_frequency * (frequency * 2.5) / denominator
            scores.append(score)

        maximum = max(scores, default=0.0)
        return [score / maximum if maximum else 0.0 for score in scores]
