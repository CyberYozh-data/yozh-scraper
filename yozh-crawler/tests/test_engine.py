from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.engine import CrawlEngine
from src.fetcher import ScraperError
from src.limiter import DomainRateLimiter
from src.schemas import CrawlPageRecord, CrawlRequest, CrawlScope, CrawlStats
from src.settings import Settings


_test_settings = Settings.model_construct(
    max_retries=2,
    retry_http_codes=[429, 503],
    retry_backoff_base=0.01,
    retry_backoff_max=0.01,
    session_max_error_score=3.0,
    session_max_usage=50,
    session_blocked_codes=[403],
)


def _scrape(status: int = 200, raw_html: str = "<html><body></body></html>", final_url: str | None = None) -> dict:
    return {"meta": {"status_code": status, "final_url": final_url}, "raw_html": raw_html}


def _make_engine(
    seed_url: str = "https://example.com",
    scope_kwargs: dict | None = None,
    fetch_return: dict | None = None,
    fetch_side_effect=None,
) -> tuple[CrawlEngine, list[CrawlPageRecord], list[dict], list[CrawlStats]]:
    pages: list[CrawlPageRecord] = []
    events: list[dict] = []
    stats: list[CrawlStats] = []

    request = CrawlRequest(
        seed_url=seed_url,
        scope=CrawlScope(**(scope_kwargs or {})),
    )

    scraper = MagicMock()
    if fetch_side_effect is not None:
        scraper.fetch = AsyncMock(side_effect=fetch_side_effect)
    else:
        scraper.fetch = AsyncMock(return_value=fetch_return or _scrape())

    engine = CrawlEngine(
        job_id="test",
        request=request,
        scraper=scraper,
        limiter=DomainRateLimiter(default_rps=1000.0),
        settings=_test_settings,
        on_event=events.append,
        on_page=pages.append,
        on_stats=stats.append,
    )
    return engine, pages, events, stats


# ─── basic crawl ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_www_alias_pages_share_seed_rate_limit_bucket():
    # www-canonical site behind a bare seed: www pages must inherit the
    # seed's per_domain_rps bucket (and semaphore), not a fresh default-rps
    # one — the alias is one physical server.
    seed_html = '<html><body><a href="https://www.example.com/a">a</a></body></html>'
    engine, pages, _, _ = _make_engine(
        scope_kwargs={"max_pages": 5, "per_domain_rps": 250.0},
        fetch_return=_scrape(raw_html=seed_html),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert len(pages) == 2  # seed + the www-alias page
    snap = engine._limiter.snapshot()
    assert "www.example.com" not in snap
    assert snap["example.com"]["rps"] == 250.0


@pytest.mark.asyncio
async def test_www_homepage_twin_is_not_fetched_twice():
    # Bare seed on a www-canonical site: the homepage self-link resolves to
    # https://www.example.com/ — the same page as the seed, not a second one.
    seed_html = '<html><body><a href="https://www.example.com/">home</a></body></html>'
    engine, pages, _, _ = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_return=_scrape(raw_html=seed_html),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)
    assert len(pages) == 1


@pytest.mark.asyncio
async def test_single_page_no_links():
    engine, pages, events, stats = _make_engine(scope_kwargs={"max_pages": 5})
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert len(pages) == 1
    assert pages[0].url == "https://example.com/"
    assert pages[0].status_code == 200
    assert pages[0].error is None


@pytest.mark.asyncio
async def test_stats_visited_after_crawl():
    engine, pages, events, stats = _make_engine(scope_kwargs={"max_pages": 5})
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].visited == 1


@pytest.mark.asyncio
async def test_page_depth_is_zero_for_seed():
    engine, pages, events, stats = _make_engine(scope_kwargs={"max_pages": 1})
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert pages[0].depth == 0
    assert pages[0].parent_url is None


# ─── link following ───────────────────────────────────────────────────────────

def _seed_only(html: str):
    """A fetch stub that serves `html` for the seed and an empty page elsewhere,
    plus the list of URLs it was asked for."""
    fetched: list[str] = []

    async def fetch(url, opts):
        fetched.append(url)
        return _scrape(raw_html=html if url.rstrip("/") == "https://example.com" else "<html/>")

    return fetch, fetched


async def _crawl_seed(html: str) -> tuple[list[str], list[CrawlStats]]:
    fetch, fetched = _seed_only(html)
    engine, _pages, _events, stats = _make_engine(
        scope_kwargs={"max_pages": 10, "max_depth": 1}, fetch_side_effect=fetch,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)
    return fetched, stats


@pytest.mark.asyncio
async def test_a_bad_href_costs_one_link_not_the_worker():
    """Audit H-16: an IPv6-literal href used to end the worker that parsed it."""
    fetched, _ = await _crawl_seed(
        '<html><body><a href="/ok1">a</a><a href="http://[::1]/">b</a>'
        '<a href="http://[2001:db8::1]/">c</a><a href="/ok2">d</a></body></html>'
    )
    assert any(u.endswith("/ok1") for u in fetched) and any(u.endswith("/ok2") for u in fetched), fetched
    assert not any("[" in u for u in fetched), "the IPv6 hosts are out of scope, not fetched"


@pytest.mark.asyncio
async def test_a_dedup_failure_costs_one_link_not_the_worker(monkeypatch):
    """Pins the widened try independently of the IPv6 fix: whatever the dedup
    step raises, the page's other links are followed and the drop is counted."""
    from src import dedup as dedup_mod
    original = dedup_mod.DedupSet.add

    def exploding_add(self, url):
        if "boom" in url:
            raise RuntimeError("dedup exploded")
        return original(self, url)

    monkeypatch.setattr(dedup_mod.DedupSet, "add", exploding_add)
    fetched, stats = await _crawl_seed(
        '<html><body><a href="/ok1">a</a><a href="/boom">b</a><a href="/ok2">c</a></body></html>'
    )
    assert any(u.endswith("/ok1") for u in fetched) and any(u.endswith("/ok2") for u in fetched), fetched
    assert not any("boom" in u for u in fetched)
    assert stats[-1].links_dropped == 1


@pytest.mark.asyncio
async def test_follows_links_within_scope():
    """Seed returns a child link; child gets fetched at depth 1."""
    html_with_link = '<html><body><a href="/child">x</a></body></html>'
    call_count = 0

    async def fetch(url, opts):
        nonlocal call_count
        call_count += 1
        return _scrape(raw_html=html_with_link if "child" not in url else "<html/>")

    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 10, "max_depth": 1},
        fetch_side_effect=fetch,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert call_count == 2
    urls = [p.url for p in pages]
    assert any("/child" in u for u in urls)


@pytest.mark.asyncio
async def test_dedup_prevents_revisit():
    """Same link appearing multiple times → fetched only once."""
    html = '<a href="/p">a</a><a href="/p">b</a><a href="/p">c</a>'
    fetch_count = 0

    async def fetch(url, opts):
        nonlocal fetch_count
        fetch_count += 1
        return _scrape(raw_html=html)

    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 20, "max_depth": 1},
        fetch_side_effect=fetch,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert fetch_count == 2  # seed + /p


@pytest.mark.asyncio
async def test_dedup_counts_in_stats():
    html = '<a href="/p">a</a><a href="/p">b</a>'
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 20, "max_depth": 1},
        fetch_return=_scrape(raw_html=html),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].dedup_skipped >= 1


# ─── max_pages / max_depth caps ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_max_pages_cap():
    """max_pages=1 means exactly 1 page even if seed has links."""
    html = "".join(f'<a href="/p{i}">x</a>' for i in range(20))
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 1},
        fetch_return=_scrape(raw_html=html),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].visited == 1


@pytest.mark.asyncio
async def test_max_depth_zero_crawls_only_seed():
    html = '<a href="/child">x</a>'
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_depth": 0, "max_pages": 100},
        fetch_return=_scrape(raw_html=html),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].visited == 1
    assert stats[-1].out_of_scope >= 1


# ─── scope filtering ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_out_of_scope_links_not_followed():
    html = '<a href="https://other.com/page">external</a>'
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"mode": "same-domain", "max_pages": 10},
        fetch_return=_scrape(raw_html=html),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].visited == 1
    assert stats[-1].out_of_scope >= 1


# ─── failure handling ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_non_retryable_error_records_failed_page():
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_side_effect=ScraperError(status_code=422, message="bad url", retryable=False),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert any(p.error is not None for p in pages)
    assert stats[-1].failed >= 1


@pytest.mark.asyncio
async def test_non_retryable_error_emits_page_error_event():
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_side_effect=ScraperError(status_code=422, message="bad", retryable=False),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    error_events = [e for e in events if e.get("type") == "page_error"]
    assert error_events
    assert error_events[-1]["will_retry"] is False


@pytest.mark.asyncio
async def test_retryable_error_retries_and_eventually_succeeds():
    """After 2 failures the third call succeeds; page must be recorded as ok."""
    calls = 0

    async def flaky(url, opts):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ScraperError(status_code=503, message="down", retryable=True)
        return _scrape(200)

    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_side_effect=flaky,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert calls == 3
    assert stats[-1].retries_total == 2
    assert any(p.status_code == 200 for p in pages)


@pytest.mark.asyncio
async def test_retryable_error_beyond_max_retries_records_failure():
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_side_effect=ScraperError(status_code=503, message="always down", retryable=True),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].failed >= 1


@pytest.mark.asyncio
async def test_retry_emits_will_retry_event():
    calls = 0

    async def flaky(url, opts):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ScraperError(status_code=503, message="retry", retryable=True)
        return _scrape(200)

    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_side_effect=flaky,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    retry_events = [e for e in events if e.get("type") == "page_error" and e.get("will_retry")]
    assert retry_events


# ─── enable_scraping flag ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enable_scraping_false_discards_response():
    request = CrawlRequest(
        seed_url="https://example.com",
        enable_scraping=False,
        scope=CrawlScope(max_pages=1),
    )
    pages: list[CrawlPageRecord] = []
    scraper = MagicMock()
    scraper.fetch = AsyncMock(return_value=_scrape(200, "<html/>"))

    engine = CrawlEngine(
        job_id="t",
        request=request,
        scraper=scraper,
        limiter=DomainRateLimiter(default_rps=1000.0),
        settings=_test_settings,
        on_event=lambda e: None,
        on_page=pages.append,
        on_stats=lambda s: None,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert pages[0].scrape_response is None


@pytest.mark.asyncio
async def test_enable_scraping_true_keeps_response():
    from src.schemas import ScrapeOptions
    request = CrawlRequest(
        seed_url="https://example.com",
        enable_scraping=True,
        scrape_options=ScrapeOptions(),
        scope=CrawlScope(max_pages=1),
    )
    pages: list[CrawlPageRecord] = []
    scraper = MagicMock()
    scraper.fetch = AsyncMock(return_value=_scrape(200, "<html/>"))

    engine = CrawlEngine(
        job_id="t",
        request=request,
        scraper=scraper,
        limiter=DomainRateLimiter(default_rps=1000.0),
        settings=_test_settings,
        on_event=lambda e: None,
        on_page=pages.append,
        on_stats=lambda s: None,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert pages[0].scrape_response is not None


# ─── cancel ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_soft_stops_engine():
    """Soft cancel: engine finishes in-flight page then exits."""
    fetch_started = asyncio.Event()

    async def slow_fetch(url, opts):
        fetch_started.set()
        await asyncio.sleep(0.05)
        return _scrape()

    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 100, "max_depth": 5},
        fetch_side_effect=slow_fetch,
    )

    task = asyncio.create_task(engine.run())
    await fetch_started.wait()
    engine.cancel(hard=False)
    await asyncio.wait_for(task, timeout=3.0)

    assert engine.cancelled


@pytest.mark.asyncio
async def test_cancel_hard_stops_engine_immediately():
    """Hard cancel: cancels internal tasks; engine exits quickly."""
    async def blocking_fetch(url, opts):
        await asyncio.sleep(60)  # would block forever without hard cancel
        return _scrape()

    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 100},
        fetch_side_effect=blocking_fetch,
    )

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)
    engine.cancel(hard=True)
    await asyncio.wait_for(task, timeout=2.0)

    assert engine.cancelled


# ─── HIGH-23: a completed job whose page was blocked/failed is NOT a success ────

_AUTO = object()


def _blocked(status: int, raw_html: str = "<html><body></body></html>", *, error=_AUTO) -> dict:
    """A scraper ScrapeResponse for a page the scraper marked as not-genuine
    content (captcha/ban/transient): job status=done, but meta.fetch_ok=False.

    `error=None` models the load-bearing case: a soft block served at HTTP 200
    with a structured body and NO top-level error — only meta.fetch_ok flags it.
    """
    resp: dict = {
        "meta": {"status_code": status, "fetch_ok": False, "final_url": None},
        "raw_html": raw_html,
    }
    err = f"HTTP {status}" if error is _AUTO else error
    if err is not None:
        resp["error"] = err
    return resp


@pytest.mark.asyncio
async def test_fetch_ok_false_transient_is_retried_then_failed_not_visited():
    # meta.fetch_ok=False with a retryable status (503) must go through the
    # retry path and end as a failed page — never counted as a visited success.
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_return=_blocked(503),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].visited == 0
    assert stats[-1].failed == 1
    assert stats[-1].retries_total == 2  # max_retries; 503 is in retry_http_codes
    assert any(p.error is not None for p in pages)
    assert all(p.scrape_response is None for p in pages)


@pytest.mark.asyncio
async def test_fetch_ok_false_captcha_at_200_is_failed_and_links_not_walked():
    # A captcha/interstitial served at HTTP 200 (fetch_ok=False) must be a
    # failure, and its body must NOT feed link extraction — otherwise the
    # crawler walks the block page's chrome as if it were real content.
    calls = 0

    async def fetch(url, opts):
        nonlocal calls
        calls += 1
        return _blocked(200, raw_html='<a href="https://example.com/deep">x</a>')

    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_side_effect=fetch,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].visited == 0
    assert stats[-1].failed == 1
    assert stats[-1].retries_total == 0  # 200 not in retry_http_codes → not retried
    assert calls == 1  # the /deep link from the block body was never fetched
    assert any(p.error is not None for p in pages)


@pytest.mark.asyncio
async def test_fetch_ok_false_without_top_level_error_is_still_a_failure():
    # The load-bearing HIGH-23 case: a soft block at HTTP 200 with a structured
    # body and NO top-level error — only meta.fetch_ok=False marks it. This
    # isolates the `not fetch_ok` clause of the gate (error is absent here).
    calls = 0

    async def fetch(url, opts):
        nonlocal calls
        calls += 1
        return _blocked(200, raw_html='<a href="https://example.com/deep">x</a>', error=None)

    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_side_effect=fetch,
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].visited == 0
    assert stats[-1].failed == 1
    assert calls == 1  # block body's link never walked
    assert any(p.error is not None for p in pages)


@pytest.mark.asyncio
async def test_genuine_404_stays_a_visit_not_a_failure():
    # A real target 404 (fetch_ok defaults True, no error) is a definitive
    # server response, not a block — it must stay a visited page so the gate
    # can't start silently dropping 404s.
    engine, pages, events, stats = _make_engine(
        scope_kwargs={"max_pages": 5},
        fetch_return=_scrape(404),
    )
    await asyncio.wait_for(engine.run(), timeout=5.0)

    assert stats[-1].visited == 1
    assert stats[-1].failed == 0
    assert any(p.status_code == 404 and p.error is None for p in pages)
