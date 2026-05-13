from __future__ import annotations

import pytest

from src.session import ManagedSession, Session


def make_pool(**overrides) -> ManagedSession:
    defaults = dict(
        base_proxy_type="none",
        base_proxy_pool_id=None,
        base_proxy_geo=None,
        max_error_score=3.0,
        max_usage=5,
        blocked_codes=[401, 403, 429],
    )
    defaults.update(overrides)
    return ManagedSession(**defaults)


# ─── acquire ──────────────────────────────────────────────────────────────────

def test_acquire_creates_session_on_first_call():
    pool = make_pool()
    s = pool.acquire()
    assert isinstance(s, Session)
    assert s.usage_count == 1
    assert not s.retired


def test_acquire_returns_same_session_on_second_call():
    pool = make_pool()
    s1 = pool.acquire()
    s2 = pool.acquire()
    assert s1 is s2
    assert s2.usage_count == 2


def test_acquire_after_retire_creates_fresh_session():
    pool = make_pool()
    s1 = pool.acquire()
    s1.retired = True
    s2 = pool.acquire()
    assert s2 is not s1
    assert not s2.retired
    assert s2.usage_count == 1


# ─── release / retire ─────────────────────────────────────────────────────────

def test_release_ok_decrements_error_score():
    pool = make_pool()
    s = pool.acquire()
    s.error_score = 1.0
    pool.release(s, status_code=200, ok=True)
    assert s.error_score == 0.5


def test_release_error_increments_error_score():
    pool = make_pool()
    s = pool.acquire()
    pool.release(s, status_code=500, ok=False)
    assert s.error_score == 1.0


def test_release_ok_does_not_go_below_zero():
    pool = make_pool()
    s = pool.acquire()
    s.error_score = 0.0
    pool.release(s, status_code=200, ok=True)
    assert s.error_score == 0.0


def test_retire_on_blocked_code():
    pool = make_pool(blocked_codes=[403])
    s = pool.acquire()
    pool.release(s, status_code=403, ok=False)
    assert s.retired


def test_retire_on_max_error_score():
    pool = make_pool(max_error_score=2.0)
    s = pool.acquire()
    pool.release(s, status_code=500, ok=False)
    pool.release(s, status_code=500, ok=False)
    assert s.retired


def test_retire_on_max_usage():
    pool = make_pool(max_usage=2)
    s = pool.acquire()
    pool.acquire()  # usage_count → 2
    pool.release(s, status_code=200, ok=True)
    assert s.retired


def test_non_blocked_code_does_not_retire_immediately():
    pool = make_pool(max_error_score=10.0)
    s = pool.acquire()
    pool.release(s, status_code=500, ok=False)
    assert not s.retired


# ─── overrides ────────────────────────────────────────────────────────────────

def test_overrides_includes_proxy_type():
    pool = make_pool(base_proxy_type="mobile")
    s = pool.acquire()
    assert s.overrides()["proxy_type"] == "mobile"


def test_overrides_includes_pool_id_when_set():
    pool = make_pool(base_proxy_pool_id="pool_1")
    s = pool.acquire()
    assert s.overrides()["proxy_pool_id"] == "pool_1"


def test_overrides_excludes_pool_id_when_none():
    pool = make_pool(base_proxy_pool_id=None)
    s = pool.acquire()
    assert "proxy_pool_id" not in s.overrides()


def test_overrides_includes_geo_when_set():
    pool = make_pool(base_proxy_geo={"country_code": "US"})
    s = pool.acquire()
    assert s.overrides()["proxy_geo"] == {"country_code": "US"}


def test_overrides_excludes_geo_when_none():
    pool = make_pool(base_proxy_geo=None)
    s = pool.acquire()
    assert "proxy_geo" not in s.overrides()


# ─── stats ────────────────────────────────────────────────────────────────────

def test_stats_when_no_session():
    pool = make_pool()
    assert pool.stats() == {"active": False}


def test_stats_when_session_active():
    pool = make_pool()
    pool.acquire()
    s = pool.stats()
    assert s["active"] is True
    assert "id" in s
    assert s["usage_count"] == 1
