from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.fetcher import ScraperClient, ScraperError


def _resp(status: int, body) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    if isinstance(body, dict):
        r.json.return_value = body
        r.text = str(body)
    else:
        r.text = body
        r.json.return_value = {}
    return r


@pytest.fixture
def client():
    return ScraperClient(base_url="http://scraper", poll_interval_ms=1, timeout_ms=5_000)


# ─── happy path ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_returns_first_result(client, mocker):
    result_payload = {"meta": {"status_code": 200}, "raw_html": "<html/>"}

    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {"job_id": "j1"})))
    mocker.patch.object(
        client._client,
        "get",
        AsyncMock(side_effect=[
            _resp(200, {"status": "done"}),
            _resp(200, {"results": [result_payload]}),
        ]),
    )

    result = await client.fetch("https://example.com", {})
    assert result == result_payload


@pytest.mark.asyncio
async def test_fetch_polls_until_done(client, mocker):
    """Client must keep polling while status is running/queued."""
    result_payload = {"meta": {"status_code": 200}, "raw_html": "<html/>"}

    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {"job_id": "j1"})))
    mocker.patch.object(
        client._client,
        "get",
        AsyncMock(side_effect=[
            _resp(200, {"status": "queued"}),
            _resp(200, {"status": "running"}),
            _resp(200, {"status": "done"}),
            _resp(200, {"results": [result_payload]}),
        ]),
    )

    result = await client.fetch("https://example.com", {})
    assert result == result_payload


# ─── submit errors ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_4xx_on_submit_raises_not_retryable(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(422, "bad")))

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert not exc.value.retryable
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_fetch_5xx_on_submit_raises_retryable(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(503, "down")))

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert exc.value.retryable


@pytest.mark.asyncio
async def test_fetch_no_job_id_in_response_raises(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {})))

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert not exc.value.retryable


@pytest.mark.asyncio
async def test_fetch_network_error_on_submit_raises_retryable(client, mocker):
    mocker.patch.object(
        client._client, "post",
        AsyncMock(side_effect=httpx.ConnectError("refused")),
    )

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert exc.value.retryable
    assert exc.value.status_code is None


# ─── poll errors ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_job_failed_raises_retryable(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {"job_id": "j1"})))
    mocker.patch.object(
        client._client, "get",
        AsyncMock(return_value=_resp(200, {"status": "failed", "error": "proxy error"})),
    )

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert exc.value.retryable
    assert "proxy error" in exc.value.message


@pytest.mark.asyncio
async def test_fetch_unexpected_status_raises_not_retryable(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {"job_id": "j1"})))
    mocker.patch.object(
        client._client, "get",
        AsyncMock(return_value=_resp(200, {"status": "unknown_status"})),
    )

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert not exc.value.retryable


@pytest.mark.asyncio
async def test_fetch_poll_non_200_raises_retryable(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {"job_id": "j1"})))
    mocker.patch.object(
        client._client, "get",
        AsyncMock(return_value=_resp(500, "oops")),
    )

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert exc.value.retryable


@pytest.mark.asyncio
async def test_fetch_network_error_on_poll_raises_retryable(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {"job_id": "j1"})))
    mocker.patch.object(
        client._client, "get",
        AsyncMock(side_effect=httpx.ConnectError("connection lost")),
    )

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert exc.value.retryable


# ─── results errors ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_empty_results_raises(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {"job_id": "j1"})))
    mocker.patch.object(
        client._client, "get",
        AsyncMock(side_effect=[
            _resp(200, {"status": "done"}),
            _resp(200, {"results": []}),
        ]),
    )

    with pytest.raises(ScraperError):
        await client.fetch("https://example.com", {})


@pytest.mark.asyncio
async def test_fetch_results_non_200_raises_retryable(client, mocker):
    mocker.patch.object(client._client, "post", AsyncMock(return_value=_resp(200, {"job_id": "j1"})))
    mocker.patch.object(
        client._client, "get",
        AsyncMock(side_effect=[
            _resp(200, {"status": "done"}),
            _resp(503, "unavailable"),
        ]),
    )

    with pytest.raises(ScraperError) as exc:
        await client.fetch("https://example.com", {})

    assert exc.value.retryable


# ─── health ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_returns_true_on_200(client, mocker):
    mocker.patch.object(client._client, "get", AsyncMock(return_value=_resp(200, {})))
    assert await client.health() is True


@pytest.mark.asyncio
async def test_health_returns_false_on_non_200(client, mocker):
    mocker.patch.object(client._client, "get", AsyncMock(return_value=_resp(503, "down")))
    assert await client.health() is False


@pytest.mark.asyncio
async def test_health_returns_false_on_network_error(client, mocker):
    mocker.patch.object(
        client._client, "get",
        AsyncMock(side_effect=httpx.ConnectError("refused")),
    )
    assert await client.health() is False
