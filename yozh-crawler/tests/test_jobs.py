from __future__ import annotations

import asyncio

import pytest

from src.jobs import JobStore
from src.schemas import CrawlRequest


@pytest.mark.asyncio
async def test_store_create_produces_queued_job():
    store = JobStore()
    rec = await store.create(CrawlRequest(seed_url="https://example.com"))
    assert rec.status == "queued"
    assert rec.job_id.startswith("crawl_")
    assert store.get(rec.job_id) is rec


@pytest.mark.asyncio
async def test_set_status_transitions():
    store = JobStore()
    rec = await store.create(CrawlRequest(seed_url="https://example.com"))
    await store.set_status(rec.job_id, "running")
    assert store.get(rec.job_id).status == "running"
    await store.set_status(rec.job_id, "cancelled")
    assert store.get(rec.job_id).status == "cancelled"


@pytest.mark.asyncio
async def test_subscribe_on_already_terminal_job_closes_immediately():
    """Regression: late subscribers to a finished job must not hang —
    subscribe() yields replay + final event and returns."""
    store = JobStore()
    rec = await store.create(CrawlRequest(seed_url="https://example.com"))
    await store.set_status(rec.job_id, "cancelled")
    store.publish_final(rec.job_id, {"type": "cancelled", "stats": rec.stats.model_dump(mode="json")})

    events = []
    async for ev in store.subscribe(rec.job_id):
        events.append(ev)
    # Must have yielded at least the terminal event and returned
    assert any(e.get("type") == "cancelled" for e in events)


@pytest.mark.asyncio
async def test_subscribe_unknown_job_returns_empty_stream():
    store = JobStore()
    events = []
    async for ev in store.subscribe("crawl_unknown"):
        events.append(ev)
    assert events == []


# ─── slim page-event transform ────────────────────────────────────────────────


def testto_slim_page_event_strips_heavy_scrape_response_fields():
    """SSE consumers that only need page metadata can request a slim variant
    that drops the multi-MB fields (screenshot_base64, raw_html, cleaned_html)
    — the full payload still reaches get_results."""
    from src.jobs import to_slim_page_event

    event = {
        "type": "page",
        "page": {
            "url": "https://example.com/",
            "took_ms": 1234,
            "status_code": 200,
            "scrape_response": {
                "request_id": "r1",
                "screenshot_base64": "A" * 100_000,
                "raw_html": "<html>" + ("x" * 50_000) + "</html>",
                "cleaned_html": "<body>" + ("y" * 50_000) + "</body>",
                "status_code": 200,
            },
        },
    }
    slim = to_slim_page_event(event)
    sr = slim["page"]["scrape_response"]
    assert "screenshot_base64" not in sr
    assert "raw_html" not in sr
    assert "cleaned_html" not in sr
    # Lightweight metadata survives
    assert sr["request_id"] == "r1"
    assert sr["status_code"] == 200
    # Top-level page metadata survives
    assert slim["page"]["url"] == "https://example.com/"
    assert slim["page"]["took_ms"] == 1234


def testto_slim_page_event_is_a_copy_not_a_mutation():
    """The original event must not be mutated — other subscribers might
    still need the full payload."""
    from src.jobs import to_slim_page_event

    event = {
        "type": "page",
        "page": {
            "url": "https://example.com/",
            "scrape_response": {
                "screenshot_base64": "A" * 10,
                "raw_html": "<html/>",
            },
        },
    }
    _ = to_slim_page_event(event)
    assert event["page"]["scrape_response"]["screenshot_base64"] == "A" * 10
    assert event["page"]["scrape_response"]["raw_html"] == "<html/>"


def testto_slim_page_event_handles_event_without_scrape_response():
    """Some pages fail to scrape and have no scrape_response — must not crash."""
    from src.jobs import to_slim_page_event

    event = {
        "type": "page",
        "page": {"url": "https://example.com/", "scrape_response": None, "error": "timeout"},
    }
    slim = to_slim_page_event(event)
    assert slim["page"]["scrape_response"] is None
    assert slim["page"]["error"] == "timeout"


def testto_slim_page_event_passes_through_non_page_events_unchanged():
    """stats / done / cancelled events are already small; the helper must
    leave them untouched (and not assume a `page` key exists)."""
    from src.jobs import to_slim_page_event

    for ev in [
        {"type": "stats", "stats": {"visited": 3}},
        {"type": "done", "status": "done"},
        {"type": "cancelled"},
    ]:
        assert to_slim_page_event(ev) == ev


@pytest.mark.asyncio
async def test_engine_init_failure_fails_the_job_and_the_worker_lives(monkeypatch):
    """Audit H-15: the engine's constructor used to run outside the job's try."""
    from unittest.mock import MagicMock

    from src import jobs as jobs_mod
    from src.limiter import DomainRateLimiter
    from src.settings import Settings

    class _Engine:
        cancelled = False

        def __init__(self, *, request, **_kw):
            if "bad" in str(request.seed_url):
                raise RuntimeError("scope exploded")

        async def run(self):
            return None

    monkeypatch.setattr(jobs_mod, "CrawlEngine", _Engine)
    store = JobStore()
    settings = Settings(workers=1)
    runner = jobs_mod.JobRunner(store, MagicMock(), DomainRateLimiter(default_rps=1000.0), settings)
    bad = await store.create(CrawlRequest(seed_url="https://bad.example"))
    good = await store.create(CrawlRequest(seed_url="https://good.example"))
    await runner.start()
    try:
        await runner.submit(bad.job_id)
        await runner.submit(good.job_id)
        for _ in range(50):
            if store.get(good.job_id).status == "done":
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
    assert store.get(bad.job_id).status == "failed"
    assert "scope exploded" in (store.get(bad.job_id).error or "")
    assert store.get(good.job_id).status == "done", "the worker survived the bad job"
