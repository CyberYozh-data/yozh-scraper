from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class Request:
    url: str
    parent_url: str | None
    depth: int
    attempts: int = 0
    next_attempt_at: float = 0.0


class Frontier:
    """In-memory queue with per-domain round-robin fairness.

    Each domain gets its own FIFO sub-queue. pop() cycles domains so a single
    slow host can't starve others. next_attempt_at defers retries — if every
    head is in the future, pop() returns None and the caller sleeps.
    """

    def __init__(self) -> None:
        self._queues: dict[str, deque[Request]] = {}
        self._domain_order: deque[str] = deque()
        self._size = 0

    @staticmethod
    def _domain(url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    def push(self, r: Request) -> None:
        d = self._domain(r.url)
        q = self._queues.get(d)
        if q is None:
            q = deque()
            self._queues[d] = q
            self._domain_order.append(d)
        q.append(r)
        self._size += 1

    def pop(self) -> Request | None:
        if not self._domain_order:
            return None
        now = time.monotonic()

        # Try at most one full cycle to find a ready request
        for _ in range(len(self._domain_order)):
            d = self._domain_order[0]
            q = self._queues[d]
            r = q[0] if q else None
            if r is not None and r.next_attempt_at <= now:
                q.popleft()
                if q:
                    # rotate: move domain to the back for fairness
                    self._domain_order.rotate(-1)
                else:
                    self._domain_order.popleft()
                    del self._queues[d]
                self._size -= 1
                return r
            # not ready — rotate and keep looking
            self._domain_order.rotate(-1)
        return None

    def __len__(self) -> int:
        return self._size

    def domains(self) -> dict[str, int]:
        return {d: len(q) for d, q in self._queues.items()}
