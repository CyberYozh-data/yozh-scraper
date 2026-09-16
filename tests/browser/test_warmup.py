from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.browser.runner import warmup_origin, run_warmup


def _warmup_page(url: str = "https://example.com/"):
    """An AsyncMock page shaped like a real Playwright one: a string `url` and
    a `goto` that returns a Response with a terminated redirect chain. Without
    both, the warmup landing check sees an unreadable chain and refuses."""
    page = AsyncMock()
    page.url = url
    page.goto = AsyncMock(
        return_value=MagicMock(request=MagicMock(redirected_from=None))
    )
    return page


def test_warmup_origin_from_search_url():
    assert warmup_origin("https://yandex.ru/search/?text=x&lr=213") == "https://yandex.ru/"
    assert warmup_origin("http://example.com/a/b?q=1") == "http://example.com/"
    assert warmup_origin("not-a-url") is None


@pytest.mark.asyncio
async def test_run_warmup_visits_origin_then_dwells():
    page = _warmup_page()
    applied = await run_warmup(
        page, "https://yandex.ru/search/?text=x",
        {"type": "homepage"}, timeout_ms=40000, default_dwell_ms=2500,
    )
    page.goto.assert_awaited_once()
    assert page.goto.call_args.args[0] == "https://yandex.ru/"
    page.wait_for_timeout.assert_awaited_once_with(2500)
    # Reports what actually ran: the resolved origin, not the requested config.
    assert applied.applied == {"type": "homepage", "url": "https://yandex.ru/",
                               "dwell_ms": 2500, "blocked": False}
    assert applied.error is None


@pytest.mark.asyncio
async def test_run_warmup_noop_when_disabled_or_unknown_type():
    page = _warmup_page()
    for warmup in (None, {"type": "other"}):
        outcome = await run_warmup(page, "https://yandex.ru/s", warmup,
                                   timeout_ms=1, default_dwell_ms=1)
        # Nothing applied AND no error: a warmup nobody asked for is not a
        # failure, and conflating the two is what this contract exists to stop.
        assert outcome.applied is None
        assert outcome.error is None
    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_warmup_swallows_errors():
    page = _warmup_page()
    page.goto.side_effect = RuntimeError("boom")

    outcome = await run_warmup(page, "https://yandex.ru/s", {"type": "homepage"},
                               timeout_ms=1, default_dwell_ms=1)

    # Must not raise, and must not claim a failed warmup ran...
    assert outcome.applied is None
    # ...but it must SAY it failed, naming where. That is the whole difference
    # from the old `return None`, which made a warmup failing on every attempt
    # indistinguishable from one that was never configured.
    assert outcome.error is not None
    assert "https://yandex.ru/" in outcome.error and "boom" in outcome.error


@pytest.mark.asyncio
async def test_camoufox_fetch_runs_warmup(monkeypatch):
    import src.browser.camoufox_runner as cr

    page = _warmup_page()
    page.goto.return_value = AsyncMock(status=200)
    page.content.return_value = "<html>OrganicTitle</html>"
    page.url = "https://yandex.ru/search/?text=x"
    page.evaluate.return_value = {}

    browser = AsyncMock()
    browser.new_page.return_value = page

    class _CM:
        async def __aenter__(self): return browser
        async def __aexit__(self, *a): return None

    monkeypatch.setattr(cr, "AsyncCamoufox", lambda **k: _CM())

    runner = cr.CamoufoxRunner(timeout_ms=40000)
    await runner.fetch(
        url="https://yandex.ru/search/?text=x", device="desktop", proxy=None,
        headers=None, wait_until="domcontentloaded", wait_for_selector=None,
        timeout_ms=None, screenshot=False, warmup={"type": "homepage"},
    )
    # first goto is the warmup origin, second is the real url
    assert page.goto.await_args_list[0].args[0] == "https://yandex.ru/"
    assert page.goto.await_args_list[-1].args[0] == "https://yandex.ru/search/?text=x"


@pytest.mark.asyncio
async def test_scrape_runner_threads_warmup_to_fetch():
    from src.browser.runner import FetchResult
    from src.queue import scrape_runner

    runner = AsyncMock()
    runner.fetch.return_value = FetchResult(
        html="<html>ok</html>", final_url="u", status_code=200,
        screenshot_b64=None, ok=True, error=None,
    )
    req = {"url": "https://yandex.ru/search/?text=x", "warmup": {"type": "homepage"}}
    await scrape_runner.run_scrape(runner, "rid", req, None)
    assert runner.fetch.await_args.kwargs["warmup"] == {"type": "homepage"}


@pytest.mark.asyncio
async def test_run_warmup_custom_visits_given_url():
    page = _warmup_page()
    applied = await run_warmup(
        page, "https://yandex.ru/search/?text=x",
        {"type": "custom", "url": "https://warm.example/seed"},
        timeout_ms=40000, default_dwell_ms=2500,
    )
    page.goto.assert_awaited_once()
    assert page.goto.call_args.args[0] == "https://warm.example/seed"
    page.wait_for_timeout.assert_awaited_once_with(2500)
    assert applied.applied == {"type": "custom", "url": "https://warm.example/seed",
                               "dwell_ms": 2500, "blocked": False}
    assert applied.error is None
