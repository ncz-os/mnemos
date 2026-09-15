"""Bound CPU work across event loops, including cancelled async callers."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import Callable, TypeVar

T = TypeVar("T")


class BoundedExecutor:
    """Non-blocking admission with permits owned by underlying futures.

    ``capacity`` includes running and queued work. Cancelling an asyncio
    wrapper cannot release a running task's permit early. No asyncio lock
    is bound at import time, so callers may use different event loops.
    """

    def __init__(self, *, workers: int = 1, capacity: int = 4, name: str = "mnemos-cpu"):
        if workers < 1 or capacity < workers:
            raise ValueError("capacity must be >= workers >= 1")
        self._permits = BoundedSemaphore(capacity)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)

    def submit(self, function: Callable[..., T], *args) -> Future[T] | None:
        if not self._permits.acquire(blocking=False):
            return None
        try:
            future = self._pool.submit(function, *args)
        except BaseException:
            self._permits.release()
            raise
        future.add_done_callback(lambda _: self._permits.release())
        return future

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
