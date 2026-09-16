from __future__ import annotations

import asyncio
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from src.app import create_app
from src.queue.store import get_job_store, pack_response
from src.queue.tasks import make_error_payload
from src.schemas import ScrapeRequest, ScrapeResponse, ScrapeMeta


@pytest.fixture
def client():
    """TestClient for API tests"""
    app = create_app()
    with TestClient(app) as client:
        yield client


def _run(coro):
    """Run a coroutine against the current _store using a fresh event loop.

    TestClient uses anyio internally, so the event loop it runs the lifespan on
    is NOT the loop returned by asyncio.get_event_loop() in a sync test body.
    asyncio.run() creates a fresh loop; fakeredis and the _store singleton don't
    depend on a specific loop, so this is safe.
    """
    return asyncio.run(coro)


def _make_result_payload(url: str = "https://example.com", request_id: str = "req_1") -> bytes:
    """Build a packed ScrapeResponse payload for writing into the store."""
    resp = ScrapeResponse(
        request_id=request_id,
        took_ms=100,
        meta=ScrapeMeta(
            url=url,
            device="desktop",
            proxy_type="none",
            retries=0,
        ),
    )
    return pack_response(resp)


class TestScrapePageEndpoint:
    def test_scrape_page_minimal(self, client, mocker):
        """Minimal request /scrape/page"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/page",
            json={"url": "https://example.com"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data

    def test_scrape_page_queue_full_returns_503(self, client, mocker):
        """Submit is rejected with 503 when the stream depth exceeds QUEUE_MAXSIZE."""
        mocker.patch("src.scrape_service.is_inmemory_broker", return_value=False)
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        store = get_job_store()
        mocker.patch.object(store, "queue_depth", new=AsyncMock(return_value=10**9))

        response = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})

        assert response.status_code == 503
        assert response.json()["detail"] == "queue_full"

    def test_scrape_page_returns_job_id(self, client, mocker):
        """POST /scrape/page returns a non-empty job_id"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/page",
            json={"url": "https://example.com"},
        )

        data = response.json()
        assert isinstance(data["job_id"], str)
        assert len(data["job_id"]) > 0

    def test_scrape_page_with_proxy(self, client, mocker):
        """POST /scrape/page with proxy_type"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/page",
            json={
                "url": "https://example.com",
                "proxy_type": "mobile",
            },
        )

        assert response.status_code == 200

    def test_scrape_page_with_extract(self, client, mocker):
        """POST /scrape/page with extract rules"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/page",
            json={
                "url": "https://example.com",
                "extract": {
                    "type": "css",
                    "fields": {
                        "title": {
                            "selector": "h1",
                        }
                    },
                },
            },
        )

        assert response.status_code == 200

    def test_scrape_page_with_all_options(self, client, mocker):
        """POST /scrape/page with all params"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/page",
            json={
                "url": "https://example.com",
                "render": True,
                "wait_until": "networkidle",
                "device": "mobile",
                "proxy_type": "res_rotating",
                "raw_html": True,
                "screenshot": True,
            },
        )

        assert response.status_code == 200

    def test_scrape_page_with_proxy_geo(self, client, mocker):
        """POST /scrape/page with proxy_geo"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/page",
            json={
                "url": "https://example.com",
                "proxy_type": "res_rotating",
                "proxy_geo": {"country_code": "GB", "city": "London"},
            },
        )

        assert response.status_code == 200

    def test_scrape_page_validation_error(self, client, mocker):
        """POST /scrape/page with not valid URL"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/page",
            json={"url": "not-a-url"},
        )

        assert response.status_code == 422


class TestScrapePagesEndpoint:
    def test_scrape_pages_single(self, client, mocker):
        """POST /scrape/pages with single page"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/pages",
            json={
                "pages": [
                    {"url": "https://example.com"},
                ]
            },
        )

        assert response.status_code == 200

    def test_scrape_pages_multiple(self, client, mocker):
        """POST /scrape/pages with a few pages"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/pages",
            json={
                "pages": [
                    {"url": "https://example.com"},
                    {"url": "https://example.org"},
                    {"url": "https://example.net"},
                ]
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data

    def test_scrape_pages_returns_job_id(self, client, mocker):
        """POST /scrape/pages returns a non-empty job_id"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/pages",
            json={
                "pages": [
                    {"url": "https://example.com"},
                ]
            },
        )

        data = response.json()
        assert isinstance(data["job_id"], str)
        assert len(data["job_id"]) > 0

    def test_scrape_pages_empty_list(self, client, mocker):
        """POST /scrape/pages with empty list"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        response = client.post(
            "/api/v1/scrape/pages",
            json={"pages": []},
        )

        # Empty list is valid
        assert response.status_code == 200


class TestScrapeStatusEndpoint:
    def test_scrape_status_queued(self, client, mocker):
        """GET /scrape/{job_id} for queued job"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        # Submit a job so it exists in the store (status=queued)
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]

        response = client.get(f"/api/v1/scrape/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["done"] == 0
        assert data["total"] == 1

    def test_scrape_status_running(self, client, mocker):
        """GET /scrape/{job_id} for running job"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        # Drive the job to "running" via mark_running
        store = get_job_store()
        _run(store.mark_running(job_id))

        response = client.get(f"/api/v1/scrape/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["done"] == 0
        assert data["total"] == 1

    def test_scrape_status_done(self, client, mocker):
        """GET /scrape/{job_id} for done job"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        # Write a slot to finalize the job as done
        store = get_job_store()
        _run(store.write_slot(job_id, 0, _make_result_payload()))

        response = client.get(f"/api/v1/scrape/{job_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "done"
        assert data["done"] == 1
        assert data["total"] == 1

    def test_scrape_status_failed(self, client, mocker):
        """GET /scrape/{job_id} reflects error when an error slot is written"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        # Write an error slot — the job will be done with a warning, not "failed" status
        store = get_job_store()
        page = {"url": "https://example.com"}
        _run(store.write_slot(job_id, 0, pack_response(make_error_payload(page, "Worker error"))))

        response = client.get(f"/api/v1/scrape/{job_id}")

        assert response.status_code == 200
        data = response.json()
        # The job finalized as "done" (error payloads still fill slots)
        assert data["status"] == "done"

    def test_scrape_status_not_found(self, client, mocker):
        """GET /scrape/{job_id} for not exists task"""
        response = client.get("/api/v1/scrape/non_existent")

        assert response.status_code == 404
        data = response.json()
        assert "job_not_found" in data["detail"]

    def test_scrape_status_response_format(self, client, mocker):
        """GET /scrape/{job_id} return valid format"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]

        response = client.get(f"/api/v1/scrape/{job_id}")

        data = response.json()
        assert "job_id" in data
        assert "status" in data
        assert "done" in data
        assert "total" in data
        assert "error" in data


class TestScrapeResultsEndpoint:
    def test_scrape_results_success(self, client, mocker):
        """GET /scrape/{job_id}/results for finished job"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        # Write a slot to complete the job
        store = get_job_store()
        _run(store.write_slot(job_id, 0, _make_result_payload()))

        response = client.get(f"/api/v1/scrape/{job_id}/results")

        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert "results" in data
        assert len(data["results"]) == 1

    def test_scrape_results_not_found(self, client, mocker):
        """GET /scrape/{job_id}/results for not exists job"""
        response = client.get("/api/v1/scrape/non_existent/results")

        assert response.status_code == 404

    def test_scrape_results_not_done_queued(self, client, mocker):
        """GET /scrape/{job_id}/results for queued job"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]

        response = client.get(f"/api/v1/scrape/{job_id}/results")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"

    def test_scrape_results_not_done_running(self, client, mocker):
        """GET /scrape/{job_id}/results for running job"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        store = get_job_store()
        _run(store.mark_running(job_id))

        response = client.get(f"/api/v1/scrape/{job_id}/results")

        assert response.status_code == 200

    def test_scrape_results_no_slots_returns_200_null(self, client, mocker):
        """GET /results for a job with no slots filled yet returns 200 with null
        results (matches the old queued-job contract). Note: the "failed" job
        status is unreachable in the taskiq design — every page converges to a
        slot, so jobs finalize as done/cancelled, never failed."""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        # Do NOT write a slot — no results filled, job still queued

        response = client.get(f"/api/v1/scrape/{job_id}/results")

        assert response.status_code == 200
        data = response.json()
        assert data["results"] is None

    def test_scrape_results_partial_returns_null_slots(self, client, mocker):
        """GET /results for a running batch returns 200 with null for unfinished
        slots, index-aligned (the tester polls this for incremental progress)."""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post(
            "/api/v1/scrape/pages",
            json={"pages": [{"url": "https://a.example"}, {"url": "https://b.example"}]},
        )
        job_id = resp.json()["job_id"]
        store = get_job_store()
        _run(store.write_slot(job_id, 1, _make_result_payload("https://b.example", "req_b")))

        response = client.get(f"/api/v1/scrape/{job_id}/results")

        assert response.status_code == 200
        data = response.json()
        assert data["results"][0] is None  # in-flight slot stays null
        assert data["results"][1]["meta"]["url"] == "https://b.example"

    def test_scrape_results_response_format(self, client, mocker):
        """GET /scrape/{job_id}/results returns correct format"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        store = get_job_store()
        _run(store.write_slot(job_id, 0, _make_result_payload()))

        response = client.get(f"/api/v1/scrape/{job_id}/results")

        data = response.json()
        assert data["job_id"] == job_id
        assert isinstance(data["results"], list)
        # The result has meta.url from the payload
        assert data["results"][0]["meta"]["url"] == "https://example.com"

    def test_scrape_results_multiple_pages(self, client, mocker):
        """GET /scrape/{job_id}/results with multiple results"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post(
            "/api/v1/scrape/pages",
            json={"pages": [
                {"url": "https://example.com"},
                {"url": "https://example.org"},
            ]},
        )
        job_id = resp.json()["job_id"]
        store = get_job_store()

        async def _fill():
            await store.write_slot(job_id, 0, _make_result_payload("https://example.com", "req_1"))
            await store.write_slot(job_id, 1, _make_result_payload("https://example.org", "req_2"))

        _run(_fill())

        response = client.get(f"/api/v1/scrape/{job_id}/results")

        data = response.json()
        assert len(data["results"]) == 2


class TestIntegrationFlows:
    def test_full_flow_page(self, client, mocker):
        """Full flow: page -> status -> results"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())

        # 1. Submit
        response = client.post(
            "/api/v1/scrape/page",
            json={"url": "https://example.com"},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]

        # 2. Status (queued initially)
        response = client.get(f"/api/v1/scrape/{job_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "queued"

        # 3. Simulate worker completing the job
        store = get_job_store()
        _run(store.write_slot(job_id, 0, _make_result_payload()))

        # 4. Results
        response = client.get(f"/api/v1/scrape/{job_id}/results")
        assert response.status_code == 200
        assert len(response.json()["results"]) == 1

    def test_full_flow_pages(self, client, mocker):
        """Full flow: pages -> status -> results"""
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())

        # Submit
        response = client.post(
            "/api/v1/scrape/pages",
            json={
                "pages": [
                    {"url": "https://example.com"},
                    {"url": "https://example.org"},
                ]
            },
        )
        job_id = response.json()["job_id"]

        # Simulate both pages completing
        store = get_job_store()

        async def _fill():
            await store.write_slot(job_id, 0, _make_result_payload("https://example.com", "req_1"))
            await store.write_slot(job_id, 1, _make_result_payload("https://example.org", "req_2"))

        _run(_fill())

        # Results
        response = client.get(f"/api/v1/scrape/{job_id}/results")
        assert response.status_code == 200
        assert len(response.json()["results"]) == 2


class TestScrapeCancelEndpoint:
    def test_cancel_running_job_returns_cancelled_true(self, client, mocker):
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        store = get_job_store()
        _run(store.mark_running(job_id))

        response = client.delete(f"/api/v1/scrape/{job_id}")
        assert response.status_code == 200
        data = response.json()
        assert data == {"job_id": job_id, "cancelled": True}

    def test_cancel_terminal_job_returns_cancelled_false(self, client, mocker):
        mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
        resp = client.post("/api/v1/scrape/page", json={"url": "https://example.com"})
        job_id = resp.json()["job_id"]
        # Complete the job (terminal state)
        store = get_job_store()
        _run(store.write_slot(job_id, 0, _make_result_payload()))

        response = client.delete(f"/api/v1/scrape/{job_id}")
        assert response.status_code == 200
        assert response.json() == {"job_id": job_id, "cancelled": False}

    def test_cancel_unknown_job_returns_404(self, client, mocker):
        response = client.delete("/api/v1/scrape/job_missing")
        assert response.status_code == 404
        assert response.json()["detail"] == "job_not_found"


class TestDegradedSlotsReachTheCaller:
    """What a caller sees when a stored result will not decode.

    The store degrades such a slot to None so the readable pages survive. That
    is only half the fix: rendered naively, `results: null` on a one-page job is
    indistinguishable from "still running", and the sibling law-checker reads a
    null results list as "no content" without logging anything — which is how a
    paid-for scrape turns into a wrong published verdict.
    """

    def test_an_unreadable_slot_is_named_and_the_job_does_not_look_pending(self, client):
        import gzip

        store = get_job_store()
        job_id = _run(store.create([ScrapeRequest(url="https://example.com")]))
        _run(store.client.hset(f"job:{job_id}:results", "0", gzip.compress(b"not json")))

        body = client.get(f"/api/v1/scrape/{job_id}/results").json()

        assert body["unreadable_slots"] == [0]
        # The slot is FILLED — it just would not decode — so the envelope must
        # not normalize to "nothing yet", or a poller waits for a result that
        # is never coming.
        assert body["results"] == [None]

    def test_a_healthy_job_never_grows_the_field(self, client):
        store = get_job_store()
        job_id = _run(store.create([ScrapeRequest(url="https://example.com")]))
        payload = ScrapeResponse(
            request_id="req_ok", took_ms=1,
            meta=ScrapeMeta(url="https://example.com", device="desktop", proxy_type="none"),
        )
        _run(store.write_slot(job_id, 0, pack_response(payload)))

        body = client.get(f"/api/v1/scrape/{job_id}/results").json()

        assert body["unreadable_slots"] is None, "the additive field stays null on a healthy job"
        assert body["results"][0]["request_id"] == "req_ok"

    def test_such_a_job_can_still_be_cancelled(self, client):
        """The harm being fixed: it used to answer 404 and sit until the TTL."""
        store = get_job_store()
        job_id = _run(store.create([ScrapeRequest(url="https://example.com")]))
        _run(store.client.hset(f"job:{job_id}:pages", "0", b"{not json"))

        response = client.delete(f"/api/v1/scrape/{job_id}")

        assert response.status_code == 200
        assert response.json()["cancelled"] is True
        assert _run(store.get_meta(job_id)).cancelled is True


class TestEveryGetFullCallerSurvivesAnUnreadableSpec:
    """One exception, four call sites — two of them were missed the first time.

    `get_full` backs the results endpoint, cancel, run_and_wait and the
    enqueue-failure convergence. Introducing JobPagesUnreadable without wiring
    all four turned a crash on one endpoint into a crash on another: /search
    polls through run_and_wait for page_task_timeout_s + 60, so it straddles a
    deploy by construction, and the blob it reads back there is a ScrapeRequest.
    """

    def test_run_and_wait_returns_nothing_instead_of_raising(self, client, mocker, caplog):
        """/search reaches get_full through here, and caught nothing."""
        from src.queue.store import JobPagesUnreadable
        from src.scrape_service import scrape_service

        store = get_job_store()
        mocker.patch.object(
            type(store), "get_full",
            side_effect=JobPagesUnreadable("req_x", 0),
        )

        with caplog.at_level("WARNING"):
            results = _run(scrape_service.run_and_wait(
                [ScrapeRequest(url="https://example.com")], timeout_s=0.01,
            ))

        assert results == [], "an unreadable spec must not become a 500 on /search"
        assert any(
            "page specs unreadable" in r.getMessage() for r in caplog.records
        ), caplog.text

    def test_the_results_endpoint_logs_before_it_answers_500(self, client, caplog):
        """A 500 that says nothing leaves ops blind to a corrupt spec.

        One such job means Redis holds specs this build cannot parse, i.e. every
        in-flight job across that deploy is affected — the loudest thing in the
        system, and it used to emit nothing at any level.
        """
        store = get_job_store()
        job_id = _run(store.create([ScrapeRequest(url="https://example.com")]))
        _run(store.client.hset(f"job:{job_id}:pages", "0", b"{not json"))

        with caplog.at_level("WARNING"):
            response = client.get(f"/api/v1/scrape/{job_id}/results")

        assert response.status_code == 500, "not 404: the job exists and is cancellable"
        assert response.json()["detail"] == "job_spec_unreadable"
        assert any(job_id in r.getMessage() for r in caplog.records), caplog.text

    def test_a_failed_enqueue_still_converges_the_job(self, client, mocker):
        """_cancel_unfilled runs inside the enqueue-failure handler.

        Raising there would replace the original failure with this one and leave
        the partial job non-terminal — losing both the reason and the
        convergence that handler exists to provide.
        """
        from src.scrape_service import scrape_service

        store = get_job_store()
        job_id = _run(store.create([ScrapeRequest(url="https://example.com")]))
        _run(store.client.hset(f"job:{job_id}:pages", "0", b"{not json"))

        _run(scrape_service._cancel_unfilled(job_id))

        assert _run(store.get_meta(job_id)).cancelled is True


def test_the_results_echo_is_masked_while_the_store_keeps_the_real_values(client, mocker):
    """Audit 2026-09-03, H-09: anyone with the job id used to read the caller's
    `Authorization` and cookies out of `pages`."""
    mocker.patch("src.scrape_service.scrape_page_task.kiq", new=AsyncMock())
    resp = client.post(
        "/api/v1/scrape/page",
        json={
            "url": "https://example.com",
            "headers": {"Authorization": "Bearer S3CRET", "Private-Token": "S3CRET-2"},
            "cookies": [{"name": "sid", "value": "COOKIE-S3CRET"}],
        },
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    results = client.get(f"/api/v1/scrape/{job_id}/results")
    echoed = results.json()["pages"][0]
    assert list(echoed["headers"]) == ["Authorization", "Private-Token"]
    assert echoed["cookies"][0]["name"] == "sid"
    assert "S3CRET" not in results.text

    stored = _run(get_job_store().get_full(job_id)).pages[0]
    assert stored.headers["Authorization"] == "Bearer S3CRET", "the worker still gets the real header"
    assert stored.cookies[0].value == "COOKIE-S3CRET"
