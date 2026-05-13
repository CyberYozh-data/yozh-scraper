from __future__ import annotations

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
