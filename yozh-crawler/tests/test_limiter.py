from __future__ import annotations

import asyncio
import time

import pytest

from src.limiter import DomainRateLimiter


# ─── basic acquire ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acquire_does_not_raise():
    limiter = DomainRateLimiter(default_rps=1000.0)
    await limiter.acquire("example.com")


@pytest.mark.asyncio
async def test_acquire_two_calls_serialized():
    """Two concurrent acquire() on the same domain must not overlap in time."""
    limiter = DomainRateLimiter(default_rps=1000.0)
    timestamps: list[float] = []

    async def worker():
        await limiter.acquire("example.com")
        timestamps.append(time.monotonic())

    await asyncio.gather(worker(), worker())
    assert len(timestamps) == 2


@pytest.mark.asyncio
async def test_acquire_different_domains_do_not_block_each_other():
    """Concurrent acquire() on different domains should both complete quickly."""
    limiter = DomainRateLimiter(default_rps=1000.0)
    start = time.monotonic()
    await asyncio.gather(
        limiter.acquire("a.com"),
        limiter.acquire("b.com"),
    )
    elapsed = time.monotonic() - start
    # Each domain's first acquire is instant; should finish well under 1s
    assert elapsed < 0.5


# ─── set_rps ──────────────────────────────────────────────────────────────────

def test_set_rps_updates_bucket():
    limiter = DomainRateLimiter(default_rps=1.0)
    limiter.set_rps("example.com", 5.0)
    assert limiter._bucket("example.com").rps == 5.0


def test_set_rps_clamps_to_minimum():
    limiter = DomainRateLimiter()
    limiter.set_rps("example.com", 0.0)
    assert limiter._bucket("example.com").rps >= 0.01


# ─── on_429 throttle ──────────────────────────────────────────────────────────

def test_on_429_halves_throttle_factor():
    limiter = DomainRateLimiter()
    limiter.on_429("example.com")
    b = limiter._bucket("example.com")
    assert b.throttle_factor == 0.5
    assert b.throttled_until > time.monotonic()


def test_on_429_accumulates():
    limiter = DomainRateLimiter()
    limiter.on_429("example.com")
    limiter.on_429("example.com")
    assert limiter._bucket("example.com").throttle_factor == 0.25


def test_on_429_floor_at_0_1():
    limiter = DomainRateLimiter()
    for _ in range(30):
        limiter.on_429("example.com")
    assert limiter._bucket("example.com").throttle_factor >= 0.1


def test_on_429_sets_throttled_until_in_future():
    limiter = DomainRateLimiter(throttle_cooldown_s=60.0)
    limiter.on_429("example.com")
    b = limiter._bucket("example.com")
    assert b.throttled_until > time.monotonic() + 50


# ─── on_success recovery ──────────────────────────────────────────────────────

def test_on_success_recovers_throttle_after_cooldown_elapsed():
    limiter = DomainRateLimiter(throttle_cooldown_s=0.0)
    limiter.on_429("example.com")
    b = limiter._bucket("example.com")
    b.throttled_until = time.monotonic() - 1.0  # force cooldown elapsed
    b.throttle_factor = 0.5
    limiter.on_success("example.com")
    assert b.throttle_factor > 0.5


def test_on_success_no_op_during_active_cooldown():
    limiter = DomainRateLimiter(throttle_cooldown_s=60.0)
    limiter.on_429("example.com")
    b = limiter._bucket("example.com")
    factor_before = b.throttle_factor
    limiter.on_success("example.com")
    assert b.throttle_factor == factor_before


def test_on_success_no_op_when_not_throttled():
    limiter = DomainRateLimiter()
    limiter.on_success("example.com")  # no prior throttle — must not raise
    b = limiter._bucket("example.com")
    assert b.throttle_factor == 1.0


# ─── snapshot ─────────────────────────────────────────────────────────────────

def test_snapshot_empty_when_no_domains_touched():
    limiter = DomainRateLimiter()
    assert limiter.snapshot() == {}


def test_snapshot_shows_throttle_factor_when_active():
    limiter = DomainRateLimiter()
    limiter.on_429("example.com")
    snap = limiter.snapshot()
    assert "example.com" in snap
    assert snap["example.com"]["throttle_factor"] == 0.5


def test_snapshot_shows_rps():
    limiter = DomainRateLimiter(default_rps=3.0)
    limiter.set_rps("example.com", 3.0)
    snap = limiter.snapshot()
    assert snap["example.com"]["rps"] == 3.0
