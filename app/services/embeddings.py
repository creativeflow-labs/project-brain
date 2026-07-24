from __future__ import annotations

import hashlib
import math
import re

from openai import OpenAI

from app.config import get_settings

settings = get_settings()


class EmbeddingService:
    def __init__(self) -> None:
        self.client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.client:
            response = self.client.embeddings.create(
                model=settings.openai_embedding_model,
                input=texts,
                dimensions=settings.embedding_dimensions,
            )
            return [item.embedding for item in response.data]
        return [self._local_embedding(text) for text in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def _local_embedding(self, text: str) -> list[float]:
        vector = [0.0] * settings.embedding_dimensions
        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        tokens = re.findall(r"[가-힣]+|[a-z0-9]+", normalized)
        features = tokens + [normalized[i : i + 3] for i in range(max(0, len(normalized) - 2))]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % settings.embedding_dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


embedding_service = EmbeddingService()

