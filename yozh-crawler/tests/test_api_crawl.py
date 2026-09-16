from __future__ import annotations

import json
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.crawl import router
from src.schemas import CrawlJobRecord, CrawlRequest, CrawlStats


def _make_record(job_id: str = "crawl_abc", status: str = "queued") -> CrawlJobRecord:
    return CrawlJobRecord(
        job_id=job_id,
        status=status,
        request=CrawlRequest(seed_url="https://example.com"),
        stats=CrawlStats(),
        pages=[],
    )


def _make_client(store=None, runner=None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/crawl")
    app.state.job_store = store or MagicMock()
    app.state.job_runner = runner or MagicMock()
    return TestClient(app, raise_server_exceptions=False)


# ─── POST /crawl ──────────────────────────────────────────────────────────────

def test_create_crawl_returns_job_id():
    store = MagicMock()
    runner = MagicMock()
    rec = _make_record()
    store.create = AsyncMock(return_value=rec)
    runner.submit = AsyncMock()

    client = _make_client(store, runner)
    resp = client.post("/crawl", json={"seed_url": "https://example.com"})

    assert resp.status_code == 200
    assert resp.json()["job_id"] == "crawl_abc"


def test_create_crawl_queue_full_returns_503():
    store = MagicMock()
    runner = MagicMock()
    store.create = AsyncMock(return_value=_make_record())
    runner.submit = AsyncMock(side_effect=RuntimeError("queue_full"))

    client = _make_client(store, runner)
    resp = client.post("/crawl", json={"seed_url": "https://example.com"})

    assert resp.status_code == 503


def test_create_crawl_invalid_url_returns_422():
    client = _make_client()
    resp = client.post("/crawl", json={"seed_url": "not-a-url"})
    assert resp.status_code == 422


# ─── GET /crawl/{job_id} ──────────────────────────────────────────────────────

def test_get_crawl_returns_record():
    store = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "running")

    client = _make_client(store)
    resp = client.get("/crawl/crawl_abc")

    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == "crawl_abc"
    assert data["status"] == "running"


def test_get_crawl_not_found_returns_404():
    store = MagicMock()
    store.get.return_value = None

    client = _make_client(store)
    resp = client.get("/crawl/does_not_exist")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "job_not_found"


# ─── GET /crawl/{job_id}/results ──────────────────────────────────────────────

def test_get_crawl_results_returns_record():
    store = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "running")

    client = _make_client(store)
    resp = client.get("/crawl/crawl_abc/results")

    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == "crawl_abc"
    assert data["status"] == "running"


def test_get_crawl_results_not_found_returns_404():
    store = MagicMock()
    store.get.return_value = None

    client = _make_client(store)
    resp = client.get("/crawl/does_not_exist/results")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "job_not_found"


def test_get_crawl_results_payload_matches_get_crawl():
    store = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "running")

    client = _make_client(store)
    record = client.get("/crawl/crawl_abc").json()
    alias = client.get("/crawl/crawl_abc/results").json()

    assert record == alias


# ─── DELETE /crawl/{job_id} ───────────────────────────────────────────────────

def test_cancel_crawl_soft_returns_cancelled_true():
    store = MagicMock()
    runner = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "running")
    runner.request_cancel = AsyncMock(return_value=True)

    client = _make_client(store, runner)
    resp = client.delete("/crawl/crawl_abc")

    assert resp.status_code == 200
    assert resp.json() == {"job_id": "crawl_abc", "cancelled": True, "hard": False}


def test_cancel_crawl_not_found_returns_404():
    store = MagicMock()
    store.get.return_value = None

    client = _make_client(store)
    resp = client.delete("/crawl/no_such_job")

    assert resp.status_code == 404


def test_cancel_crawl_hard_flag_forwarded():
    store = MagicMock()
    runner = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "running")
    runner.request_cancel = AsyncMock(return_value=True)

    client = _make_client(store, runner)
    resp = client.delete("/crawl/crawl_abc?hard=true")

    assert resp.status_code == 200
    assert resp.json()["hard"] is True
    runner.request_cancel.assert_called_once_with("crawl_abc", hard=True)


def test_cancel_already_done_returns_cancelled_false():
    store = MagicMock()
    runner = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "done")
    runner.request_cancel = AsyncMock(return_value=False)

    client = _make_client(store, runner)
    resp = client.delete("/crawl/crawl_abc")

    assert resp.status_code == 200
    assert resp.json()["cancelled"] is False


# ─── GET /crawl/{job_id}/events?slim=… ────────────────────────────────────────


def _heavy_page_event() -> dict:
    """SSE page event that mirrors a real scrape — large base64 PNG + HTML."""
    return {
        "type": "page",
        "page": {
            "url": "https://example.com/",
            "parent_url": None,
            "depth": 0,
            "fetched_at": 0.0,
            "took_ms": 100,
            "status_code": 200,
            "scrape_response": {
                "request_id": "r1",
                "screenshot_base64": "A" * 100_000,
                "raw_html": "<html>" + ("x" * 50_000) + "</html>",
                "cleaned_html": "<body>" + ("y" * 50_000) + "</body>",
                "status_code": 200,
            },
            "error": None,
        },
    }


def _parse_sse_text(text: str) -> list[dict]:
    """Parse SSE text into a list of {type, data: dict} dicts."""
    events: list[dict] = []
    event_type: str | None = None
    data_lines: list[str] = []
    for line in text.splitlines():
        if line == "":
            if event_type is not None and data_lines:
                events.append(
                    {"type": event_type, "data": json.loads("\n".join(data_lines))}
                )
            event_type = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    return events


def _events_stream(events: list[dict]) -> AsyncIterator[dict]:
    """An async generator yielding the given events then closing the stream."""
    async def _gen() -> AsyncIterator[dict]:
        for ev in events:
            yield ev

    return _gen()


def test_stream_events_default_keeps_full_payload_for_legacy_consumers():
    """Default (no ?slim query) must pass the full payload through — the
    scraper-tester UI depends on screenshot_base64 + raw_html arriving via
    SSE."""
    store = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "running")
    events = [_heavy_page_event(), {"type": "done", "status": "done"}]
    store.subscribe = lambda _job_id: _events_stream(events)

    client = _make_client(store)
    resp = client.get("/crawl/crawl_abc/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    parsed = _parse_sse_text(resp.text)
    page_events = [e for e in parsed if e["type"] == "page"]
    assert len(page_events) == 1
    sr = page_events[0]["data"]["page"]["scrape_response"]
    # Heavy fields survive — backward compatible with scraper-tester UI.
    assert sr["screenshot_base64"] == "A" * 100_000
    assert sr["raw_html"].startswith("<html>")
    assert sr["cleaned_html"].startswith("<body>")


def test_stream_events_slim_strips_heavy_payload_fields():
    """`?slim=true` drops screenshot_base64 / raw_html / cleaned_html so
    consumers that only need page metadata (like yozh-law-checker's worker)
    don't get multi-MB SSE frames."""
    store = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "running")
    events = [_heavy_page_event(), {"type": "done", "status": "done"}]
    store.subscribe = lambda _job_id: _events_stream(events)

    client = _make_client(store)
    resp = client.get("/crawl/crawl_abc/events?slim=true")
    assert resp.status_code == 200

    parsed = _parse_sse_text(resp.text)
    page_events = [e for e in parsed if e["type"] == "page"]
    assert len(page_events) == 1
    sr = page_events[0]["data"]["page"]["scrape_response"]
    assert "screenshot_base64" not in sr
    assert "raw_html" not in sr
    assert "cleaned_html" not in sr
    # Lightweight metadata survives.
    assert sr["request_id"] == "r1"
    assert page_events[0]["data"]["page"]["url"] == "https://example.com/"


def test_stream_events_slim_passes_non_page_events_through():
    """stats / done events are already small; slim must not corrupt them."""
    store = MagicMock()
    store.get.return_value = _make_record("crawl_abc", "running")
    events = [
        {"type": "stats", "stats": {"visited": 5, "queued": 0}},
        {"type": "done", "status": "done"},
    ]
    store.subscribe = lambda _job_id: _events_stream(events)

    client = _make_client(store)
    resp = client.get("/crawl/crawl_abc/events?slim=true")
    assert resp.status_code == 200

    parsed = _parse_sse_text(resp.text)
    by_type = {e["type"]: e["data"] for e in parsed}
    assert by_type["stats"] == {"type": "stats", "stats": {"visited": 5, "queued": 0}}
    assert by_type["done"] == {"type": "done", "status": "done"}


def test_stream_events_not_found_returns_404():
    store = MagicMock()
    store.get.return_value = None

    client = _make_client(store)
    resp = client.get("/crawl/does_not_exist/events")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "job_not_found"



def test_create_crawl_malformed_scope_regex_returns_422():
    """Audit H-15 at the HTTP boundary."""
    client = _make_client(MagicMock(), MagicMock())
    resp = client.post("/crawl", json={"seed_url": "https://example.com", "scope": {"include_patterns": ["("]}})
    assert resp.status_code == 422
    assert "invalid regex" in resp.text
