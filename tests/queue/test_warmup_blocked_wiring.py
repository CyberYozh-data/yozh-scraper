"""A warmup that was turned away reaches the caller, not just the log.

`run_warmup` learns it; this proves `run_scrape` says it. Deleting the one
line that appends the warning leaves every other test green — the warmup hop
went unchecked for exactly that long.
"""
from __future__ import annotations

import pytest

from src.browser.runner import FetchResult
from src.queue.envelope import ScrapeOk
from src.queue.scrape_runner import run_scrape

HTML = "<html><body><h1>Widget</h1></body></html>"


class _Runner:
    warmup_blocked = True

    async def resolve_proxy(self, proxy):
        return None, None, None

    async def fetch(self, **_kw):
        return FetchResult(
            html=HTML, final_url="https://x.example/a", status_code=200,
            screenshot_b64=None, ok=True, error=None,
            applied_warmup={"type": "homepage", "url": "https://x.example/",
                            "dwell_ms": 0, "blocked": self.warmup_blocked},
        )


class _Clean(_Runner):
    warmup_blocked = False


def _request():
    return {"url": "https://x.example/a", "device": "desktop", "proxy_type": "none",
            "extract": {"type": "css", "fields": {"t": {"selector": "h1"}}}}


@pytest.mark.asyncio
async def test_a_turned_away_warmup_is_reported():
    out = await run_scrape(_Runner(), "req_wb", _request(), None)
    assert isinstance(out, ScrapeOk)
    assert (
        "warmup_blocked: the warmup navigation landed on a challenge page"
        in out.result.warnings
    ), out.result.warnings


@pytest.mark.asyncio
async def test_an_ordinary_warmup_says_nothing():
    out = await run_scrape(_Clean(), "req_ok", _request(), None)
    assert not any("warmup_blocked" in w for w in out.result.warnings), out.result.warnings


@pytest.mark.asyncio
async def test_the_text_trips_no_cross_repo_classifier():
    """yozh-law-checker publishes a scan as blocked on "captcha",
    "block detected", "anti-bot" or "antibot" anywhere in a warning, and
    re-crawls a whole site on "timeout" or "goto". A warmup notice is advisory
    and must do neither — and the warmup URL, which a caller chooses, stays
    out of the text for the same reason `egress_warnings` withholds hostnames.
    """
    out = await run_scrape(_Runner(), "req_markers", _request(), None)
    note = next(w for w in out.result.warnings if w.startswith("warmup_blocked"))
    for marker in ("captcha", "block detected", "anti-bot", "antibot",
                   "timeout", "goto", "serp_unavailable"):
        assert marker not in note.lower(), marker
    assert "x.example" not in note, "no caller-chosen URL in a scanned string"
