from __future__ import annotations

import time

from src.frontier import Frontier, Request


def _req(url: str, depth: int = 0, next_attempt_at: float = 0.0) -> Request:
    return Request(url=url, parent_url=None, depth=depth, next_attempt_at=next_attempt_at)


def test_push_pop_single_domain_fifo():
    f = Frontier()
    f.push(_req("https://a.com/1"))
    f.push(_req("https://a.com/2"))
    f.push(_req("https://a.com/3"))
    assert [f.pop().url for _ in range(3)] == [
        "https://a.com/1",
        "https://a.com/2",
        "https://a.com/3",
    ]
    assert f.pop() is None


def test_round_robin_across_domains():
    f = Frontier()
    f.push(_req("https://a.com/1"))
    f.push(_req("https://a.com/2"))
    f.push(_req("https://b.com/1"))
    f.push(_req("https://b.com/2"))
    # Expected order: a, b, a, b — round-robin
    popped = [f.pop().url for _ in range(4)]
    assert popped == [
        "https://a.com/1",
        "https://b.com/1",
        "https://a.com/2",
        "https://b.com/2",
    ]


def test_future_attempt_deferred():
    f = Frontier()
    future = time.monotonic() + 60.0
    f.push(_req("https://a.com/1", next_attempt_at=future))
    f.push(_req("https://b.com/1"))
    # Only b is ready right now
    r = f.pop()
    assert r is not None and r.url == "https://b.com/1"
    # a is still deferred
    assert f.pop() is None
    # But it's still counted in the frontier
    assert len(f) == 1


def test_len_and_domains():
    f = Frontier()
    f.push(_req("https://a.com/1"))
    f.push(_req("https://a.com/2"))
    f.push(_req("https://b.com/1"))
    assert len(f) == 3
    assert f.domains() == {"a.com": 2, "b.com": 1}
