from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    rps: float
    next_ready_at: float = 0.0
    throttled_until: float = 0.0
    throttle_factor: float = 1.0  # 1.0 means no throttle; 0.5 means rps halved


class DomainRateLimiter:
    """Global per-domain token bucket with round-robin fairness across jobs.

    Each domain has a single bucket. Concurrent acquire() calls across jobs
    serialize fairly on an asyncio.Lock and each gets a time slot 1/rps seconds
    apart. Adaptive throttle halves effective rps on on_429() for 60 seconds.
    """

    def __init__(self, default_rps: float = 1.0, throttle_cooldown_s: float = 60.0) -> None:
        self._default_rps = max(0.01, float(default_rps))
        self._cooldown = throttle_cooldown_s
        self._buckets: dict[str, _Bucket] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _bucket(self, domain: str) -> _Bucket:
        b = self._buckets.get(domain)
        if b is None:
            b = _Bucket(rps=self._default_rps)
            self._buckets[domain] = b
        return b

    def _lock(self, domain: str) -> asyncio.Lock:
        lk = self._locks.get(domain)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[domain] = lk
        return lk

    def set_rps(self, domain: str, rps: float) -> None:
        b = self._bucket(domain)
        b.rps = max(0.01, float(rps))

    async def acquire(self, domain: str, *, job_id: str = "") -> None:
        """Block until the next token for this domain is available."""
        lk = self._lock(domain)
        async with lk:
            b = self._bucket(domain)
            now = time.monotonic()
            effective_rps = b.rps * (b.throttle_factor if now < b.throttled_until else 1.0)
            interval = 1.0 / max(0.01, effective_rps)
            # Jitter ±20% so RPS is averaged but not clock-regular
            interval *= 0.8 + random.random() * 0.4

            wait_until = max(b.next_ready_at, now)
            wait = wait_until - now
            if wait > 0:
                await asyncio.sleep(wait)
            # Schedule next ready time
            b.next_ready_at = time.monotonic() + interval

    def on_429(self, domain: str) -> None:
        """Halve effective RPS for this domain for the cooldown window."""
        b = self._bucket(domain)
        b.throttle_factor = max(0.1, b.throttle_factor * 0.5)
        b.throttled_until = time.monotonic() + self._cooldown

    def on_success(self, domain: str) -> None:
        """Gradually recover throttle when cooldown has elapsed."""
        b = self._bucket(domain)
        if time.monotonic() >= b.throttled_until and b.throttle_factor < 1.0:
            b.throttle_factor = min(1.0, b.throttle_factor * 1.5)

    def snapshot(self) -> dict[str, dict[str, float]]:
        now = time.monotonic()
        return {
            d: {
                "rps": b.rps,
                "throttle_factor": b.throttle_factor if now < b.throttled_until else 1.0,
            }
            for d, b in self._buckets.items()
        }


# module-level singleton — one per crawler process
domain_limiter = DomainRateLimiter()
