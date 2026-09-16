"""A warmup that failed must be distinguishable from one that never ran.

`applied_warmup=None` means both, and nothing reaches `warnings` — only a line
in the worker log. That conflation hid a real fault for a working day: the
google presets' warmup navigated into a host the proxy gateway refused, failed
on every attempt, and the response looked like an ordinary network error.
"""
from __future__ import annotations

import pytest

from src.browser.runner import run_warmup


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


class _Page:
    """Minimal page double: goto either works or raises."""

    def __init__(self, boom: Exception | None = None):
        self.boom = boom
        self.visited: list[str] = []
        # A real page exposes `url`; the warmup landing check reads it.
        self.url = "https://example.com/"

    async def goto(self, url, **_kw):
        self.visited.append(url)
        if self.boom is not None:
            raise self.boom
        self.url = url
        return None

    async def wait_for_timeout(self, _ms):
        return None


@pytest.mark.asyncio
async def test_a_successful_warmup_reports_what_it_did_and_no_error():
    page = _Page()

    outcome = await run_warmup(
        page, "https://example.com/p", {"type": "homepage"},
        timeout_ms=1000, default_dwell_ms=250,
    )

    assert outcome.applied == {
        "type": "homepage", "url": "https://example.com/", "dwell_ms": 250,
        "blocked": False,
    }
    assert outcome.error is None


@pytest.mark.asyncio
async def test_a_failed_warmup_reports_the_reason_instead_of_vanishing():
    """The whole point: a failure has to leave a trace the caller can surface."""
    page = _Page(boom=RuntimeError("NS_ERROR_CONNECTION_REFUSED"))

    outcome = await run_warmup(
        page, "https://example.com/p", {"type": "homepage"},
        timeout_ms=1000, default_dwell_ms=250,
    )

    assert outcome.applied is None, "a failed warmup did not run, so nothing was applied"
    assert outcome.error is not None
    assert "https://example.com/" in outcome.error, "the URL that failed must be named"
    assert "NS_ERROR_CONNECTION_REFUSED" in outcome.error


@pytest.mark.asyncio
async def test_no_warmup_configured_is_not_an_error():
    """Silence and failure must not look the same from the other side either."""
    page = _Page()

    outcome = await run_warmup(
        page, "https://example.com/p", None, timeout_ms=1000, default_dwell_ms=250,
    )

    assert outcome.applied is None
    assert outcome.error is None
    assert page.visited == []


@pytest.mark.asyncio
async def test_a_warmup_with_no_usable_url_is_not_an_error_either():
    page = _Page()

    outcome = await run_warmup(
        page, "https://example.com/p", {"type": "custom"},
        timeout_ms=1000, default_dwell_ms=250,
    )

    assert outcome.applied is None
    assert outcome.error is None
    assert page.visited == []


# --- The propagation chain -------------------------------------------------
# `run_warmup` reporting the failure is only useful if the reason survives all
# the way to a caller-visible warning. Review found the whole chain untested:
# three separate one-line mutations disabled the feature with the suite green.


@pytest.mark.asyncio
async def test_camoufox_carries_the_reason_onto_the_result(monkeypatch):
    from tests.browser.test_camoufox_runner import _mock_page
    from src.browser.camoufox_runner import CamoufoxRunner
    from src.browser.runner import WarmupOutcome

    _mock_page(monkeypatch)
    monkeypatch.setattr(
        "src.browser.camoufox_runner.run_warmup",
        lambda *a, **kw: _outcome(WarmupOutcome(error="https://ya.ru/: boom")),
    )

    res = await CamoufoxRunner(timeout_ms=1000).fetch(
        url="https://ya.ru/s", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=1000,
        screenshot=False, warmup={"type": "homepage"},
    )

    assert res.warmup_error == "https://ya.ru/: boom"
    assert res.applied_warmup is None


@pytest.mark.asyncio
async def test_playwright_carries_the_reason_onto_the_result(monkeypatch):
    from tests.browser.test_selector_timeout_classification import (
        SERP_URL, _page, _playwright_fetch,
    )
    from src.browser.runner import WarmupOutcome

    page = _page(content="<html>ok</html>", url=SERP_URL, selector_times_out=False)
    monkeypatch.setattr(
        "src.browser.runner.run_warmup",
        lambda *a, **kw: _outcome(WarmupOutcome(error="https://ya.ru/: refused")),
    )

    res = await _playwright_fetch(page, warmup={"type": "homepage"})

    assert res.warmup_error == "https://ya.ru/: refused"


@pytest.mark.asyncio
async def test_the_queue_turns_the_reason_into_a_caller_visible_warning():
    """The last link. A reason nobody can read is the bug we started from.

    Asserted on a fetch that SUCCEEDED, because that is the case nobody goes
    looking for: the scrape returns data, the response looks healthy, and a
    warmup that failed on every attempt leaves no trace at all.
    """
    from src.browser.runner import FetchResult
    from src.queue.envelope import ScrapeOk
    from src.queue.scrape_runner import run_scrape

    class _Runner:
        async def resolve_proxy(self, proxy):
            return proxy, None

        async def fetch(self, **_kw):
            return FetchResult(
                html="<html><body>real content</body></html>",
                final_url="https://ya.ru/s", status_code=200,
                screenshot_b64=None, ok=True, error=None,
                warmup_error="https://ya.ru/: PlaywrightError: refused",
            )

    out = await run_scrape(
        _Runner(), "req_warmup_warn",
        {"url": "https://ya.ru/s", "device": "desktop", "proxy_type": "none"},
        None,
    )

    assert isinstance(out, ScrapeOk)
    assert out.result.meta.fetch_ok is True, "the scrape itself worked"
    assert any("warmup_failed" in w for w in out.result.warnings), out.result.warnings
    assert any("https://ya.ru/" in w for w in out.result.warnings)


async def _outcome(value):
    return value


# --- The block state travels the same chain --------------------------------
# The lesson recorded above was re-learned on this very feature: a parallel
# `FetchResult.warmup_blocked` field was carried on the success return and
# dropped on both Camoufox error arms — and the most likely sequel to a blocked
# warmup is a FAILED fetch, so the one path that mattered was the broken one.
# The flag now rides inside `applied_warmup`, which every return already
# carries, and these tests pin that on both engines including the failure arm.

_BLOCKED = {"type": "homepage", "url": "https://ya.ru/", "dwell_ms": 0,
            "blocked": True}


@pytest.mark.asyncio
async def test_camoufox_carries_the_block_on_a_SUCCESSFUL_fetch(monkeypatch):
    from tests.browser.test_camoufox_runner import _mock_page
    from src.browser.camoufox_runner import CamoufoxRunner
    from src.browser.runner import WarmupOutcome

    _mock_page(monkeypatch)
    monkeypatch.setattr(
        "src.browser.camoufox_runner.run_warmup",
        lambda *a, **kw: _outcome(WarmupOutcome(applied=dict(_BLOCKED), blocked=True)),
    )
    res = await CamoufoxRunner(timeout_ms=1000).fetch(
        url="https://ya.ru/s", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=1000,
        screenshot=False, warmup={"type": "homepage"},
    )
    assert res.ok is True
    assert (res.applied_warmup or {}).get("blocked") is True


@pytest.mark.asyncio
async def test_camoufox_carries_the_block_when_the_fetch_then_FAILS(monkeypatch):
    """The composite case, and the one that was broken.

    A turned-away exit usually goes on to fail the real navigation too, so the
    error arm is where the caller most needs to be told that the refusal came
    first — otherwise a plain timeout is all they see.
    """
    from unittest.mock import AsyncMock
    from playwright.async_api import TimeoutError as PWTimeoutError
    from tests.browser.test_camoufox_runner import _mock_page
    from src.browser.camoufox_runner import CamoufoxRunner
    from src.browser.runner import WarmupOutcome

    page = _mock_page(monkeypatch)
    page.goto = AsyncMock(side_effect=PWTimeoutError("Timeout 1000ms exceeded"))
    monkeypatch.setattr(
        "src.browser.camoufox_runner.run_warmup",
        lambda *a, **kw: _outcome(WarmupOutcome(applied=dict(_BLOCKED), blocked=True)),
    )
    res = await CamoufoxRunner(timeout_ms=1000).fetch(
        url="https://ya.ru/s", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=1000,
        screenshot=False, warmup={"type": "homepage"},
    )
    assert res.ok is False
    assert (res.applied_warmup or {}).get("blocked") is True, (
        "a failed fetch is exactly when the earlier refusal explains the failure"
    )


@pytest.mark.asyncio
async def test_playwright_carries_the_block_onto_the_result(monkeypatch):
    from tests.browser.test_selector_timeout_classification import (
        SERP_URL, _page, _playwright_fetch,
    )
    from src.browser.runner import WarmupOutcome

    page = _page(content="<html>ok</html>", url=SERP_URL, selector_times_out=False)
    monkeypatch.setattr(
        "src.browser.runner.run_warmup",
        lambda *a, **kw: _outcome(WarmupOutcome(applied=dict(_BLOCKED), blocked=True)),
    )
    res = await _playwright_fetch(page, warmup={"type": "homepage"})
    assert (res.applied_warmup or {}).get("blocked") is True
