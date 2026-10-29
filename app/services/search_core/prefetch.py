from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore

from app.services.embeddings import embedding_service

# No unbounded task queue and no shared database sessions in worker threads.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pb-embedding")
_slots = BoundedSemaphore(2)


def start_prefetch(question: str) -> Future | None:
    if not _slots.acquire(blocking=False):
        return None
    try:
        future = _executor.submit(embedding_service.prefetch_query, question)
    except Exception:
        _slots.release()
        return None
    future.add_done_callback(lambda _: _slots.release())
    return future


def take_ready(future: Future | None, question: str, queries: list[str]) -> dict:
    """Never wait, never reuse another query, and fail back to normal retrieval."""
    if future is None:
        return {}
    if question not in queries or not future.done():
        future.cancel()
        return {}
    try:
        return {question: future.result()}
    except Exception:
        return {}
