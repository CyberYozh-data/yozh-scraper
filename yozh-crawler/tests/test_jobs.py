from __future__ import annotations

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
