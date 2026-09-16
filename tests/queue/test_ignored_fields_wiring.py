"""The worker tells the caller which request field the engine dropped.

`tests/queue/test_scrape_runner.py::TestIgnoredFieldWarnings` proves the
translator. This proves `run_scrape` calls it — deleting the one line that
extends `warnings` leaves every other test in the suite green, which is how a
"now the caller is told" claim quietly stops being true.
"""
from __future__ import annotations

import pytest

from src.browser.runner import FetchResult
from src.queue.envelope import ScrapeOk
from src.queue.scrape_runner import run_scrape

HTML = '<div id="rso"><div class="r"><a href="https://dest.example/1"><h3>one</h3></a></div></div>'
FINAL = "https://www.google.com/search?q=x"


class _Runner:
    """Fetches successfully, having dropped an engine-owned header."""

    ignored: list[str] = ["headers['User-Agent']"]

    async def resolve_proxy(self, proxy):
        return None, None, None

    async def fetch(self, **_kw):
        return FetchResult(
            html=HTML, final_url=FINAL, status_code=200, screenshot_b64=None,
            ok=True, error=None, ignored_request_fields=list(self.ignored),
        )


class _CleanRunner(_Runner):
    ignored: list[str] = []


def _request():
    return {
        "url": FINAL, "device": "desktop", "proxy_type": "none",
        "headers": {"User-Agent": "curl/8"},
        "extract": {"type": "css", "fields": {
            "links": {"selector": "div.r a", "attr": "href", "all": True}}},
    }


@pytest.mark.asyncio
async def test_the_drop_reaches_the_callers_warnings():
    out = await run_scrape(_Runner(), "req_ignored", _request(), None)
    assert isinstance(out, ScrapeOk)
    assert (
        "ignored_request_field: headers['User-Agent'] (the engine states its own)"
        in out.result.warnings
    ), out.result.warnings


@pytest.mark.asyncio
async def test_a_scrape_that_dropped_nothing_says_nothing():
    """The warning must not become background noise on every scrape: 27 of the
    28 builtin presets send no headers at all."""
    out = await run_scrape(_CleanRunner(), "req_clean", _request(), None)
    assert isinstance(out, ScrapeOk)
    assert not any("ignored_request_field" in w for w in out.result.warnings), out.result.warnings


@pytest.mark.asyncio
async def test_the_data_is_untouched_by_the_notice():
    """Additive: a warning explains the response, it does not change it."""
    out = await run_scrape(_Runner(), "req_data", _request(), None)
    assert out.result.data["links"] == ["https://dest.example/1"]
