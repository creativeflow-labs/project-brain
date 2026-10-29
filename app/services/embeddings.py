from __future__ import annotations

import hashlib
import math
import re
from collections import OrderedDict
from threading import Lock

from app.config import get_settings
from app.services.openai_client import get_openai_client

settings = get_settings()


class EmbeddingService:
    def __init__(self) -> None:
        self.client = get_openai_client()
        self._query_cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._query_cache_lock = Lock()
        self._query_cache_size = 512

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

    def prefetch_query(self, text: str) -> list[float]:
        """Bounded speculative work; do not cache until a plan actually uses it."""
        with self._query_cache_lock:
            cached = self._query_cache.get(text)
            if cached is not None:
                return list(cached)
        if not self.client:
            return self._local_embedding(text)
        response = self.client.with_options(timeout=3.0, max_retries=0).embeddings.create(
            model=settings.openai_embedding_model,
            input=[text],
            dimensions=settings.embedding_dimensions,
        )
        return response.data[0].embedding

    def embed_queries(
        self, texts: list[str], *, prepared: dict[str, list[float]] | None = None
    ) -> list[list[float]]:
        if not texts:
            return []

        cached: dict[str, list[float]] = {}
        missing: list[str] = []
        with self._query_cache_lock:
            for text in dict.fromkeys(texts):
                vector = self._query_cache.get(text)
                if vector is None and prepared and text in prepared:
                    candidate = prepared[text]
                    if len(candidate) == settings.embedding_dimensions and all(
                        math.isfinite(value) for value in candidate
                    ):
                        vector = tuple(candidate)
                        self._query_cache[text] = vector
                if vector is None:
                    missing.append(text)
                    continue
                self._query_cache.move_to_end(text)
                cached[text] = list(vector)
            while len(self._query_cache) > self._query_cache_size:
                self._query_cache.popitem(last=False)

        if missing:
            embedded = self.embed_many(missing)
            with self._query_cache_lock:
                for text, vector in zip(missing, embedded, strict=True):
                    frozen = tuple(vector)
                    self._query_cache[text] = frozen
                    self._query_cache.move_to_end(text)
                    cached[text] = list(frozen)
                while len(self._query_cache) > self._query_cache_size:
                    self._query_cache.popitem(last=False)

        return [cached[text] for text in texts]

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
