"""The `resolve_redirects` hook in the worker: the wiring, not the helper.

`tests/extract/test_resolve.py` proves the resolver. This proves the worker
calls it — with the page's final URL — and calls it BEFORE
`self_referential_link_warning`, which would otherwise (correctly) flag every
stub as a link back to the page's own host. Removing the hook, or moving it
below that guard, leaves every other test green; these two go red.
"""
from __future__ import annotations

import time

import pytest

from src.browser.runner import FetchResult
from src.proxy.models import ProxyConfig
from src.queue import scrape_runner
from src.queue.envelope import ScrapeOk
from src.queue.scrape_runner import run_scrape
from src.settings import settings

# ABSOLUTE stubs on the page's own host, on purpose. `self_referential_link_warning`
# compares hosts, so a RELATIVE `/goto?url=` has no host to match and the guard
# is silent on it whatever we do (a recorded inversion of that guard). Absolute
# stubs are the shape on which the guard genuinely fires when the stubs are not
# resolved and stays silent when they are — which is what the ordering claim
# below needs in order to mean anything.
STUBS = [f"https://www.google.com/goto?url={t}" for t in ("AAA", "BBB", "CCC")]
HTML = (
    '<div id="rso">'
    + "".join(f'<div class="r"><a href="{s}"><h3>{i}</h3></a></div>' for i, s in enumerate(STUBS))
    + "</div>"
)
FINAL = "https://www.google.com/search?q=x"


class _Transport:
    """Stands in for the guard context manager `resolve_proxy(None)` returns."""

    exited = False

    async def __aexit__(self, *_exc):
        type(self).exited = True


GUARD = ProxyConfig(server="http://127.0.0.1:9")


class _Runner:
    async def resolve_proxy(self, proxy):
        # The real one returns (effective_proxy, cm, guard); on the direct path
        # the proxy IS the egress guard. The hook must hand exactly this proxy
        # to the resolver and unwind the cm when it is done.
        _Transport.exited = False
        return GUARD, _Transport(), None

    async def fetch(self, **_kw):
        return FetchResult(
            html=HTML, final_url=FINAL, status_code=200, screenshot_b64=None, ok=True, error=None,
        )


def _request(**extra):
    return {
        "url": "https://www.google.com/search?q=x", "device": "desktop", "proxy_type": "none",
        "extract": {"type": "css", "fields": {
            "links": {"selector": "div.r a", "attr": "href", "all": True}}},
        **extra,
    }


@pytest.mark.asyncio
async def test_the_hook_runs_with_the_final_url_and_before_the_self_link_guard(monkeypatch):
    seen = {}

    async def fake_resolve(data, fields, base_url, **kw):
        seen["fields"], seen["base_url"], seen["links_in"] = list(fields), base_url, list(data["links"])
        seen["proxy"], seen["unwound_before_resolve"] = kw.get("proxy"), _Transport.exited
        assert 0.5 <= kw["batch_timeout_s"] <= 12.0, kw["batch_timeout_s"]
        return {**data, "links": [f"https://dest.example/{i}" for i in range(len(data["links"]))]}, ["field 'links': resolved 3 of 3 redirect stubs"]

    monkeypatch.setattr(scrape_runner, "resolve_redirect_fields", fake_resolve)
    out = await run_scrape(_Runner(), "req_resolve", _request(resolve_redirects=["links"]), None)

    assert isinstance(out, ScrapeOk)
    assert seen == {
        "fields": ["links"], "base_url": FINAL, "links_in": STUBS,
        # The runner's guard, not a transport of the resolver's own — and it is
        # still open while the resolver runs, unwound only afterwards.
        "proxy": GUARD, "unwound_before_resolve": False,
    }
    assert _Transport.exited is True, "the guard the hook opened must be closed after"
    assert out.result.data["links"] == ["https://dest.example/0", "https://dest.example/1", "https://dest.example/2"]
    assert "field 'links': resolved 3 of 3 redirect stubs" in out.result.warnings
    # Ordering: the self-link guard saw the RESOLVED links, so it stayed silent.
    assert not any("self_referential" in w for w in out.result.warnings), out.result.warnings


class _CamoufoxShapedRunner(_Runner):
    """`CamoufoxRunner.resolve_proxy` returns TWO values and, on the direct
    path, no proxy at all: Camoufox guards its own requests by route
    interception, which httpx never sees."""

    async def resolve_proxy(self, proxy):
        return proxy, None


@pytest.mark.asyncio
async def test_a_two_value_runner_gets_the_guard_opened_at_the_call_site(monkeypatch):
    """The first version unpacked three values and raised ValueError on every
    successful Camoufox parse that asked for resolution — turning a good scrape
    into ScrapeErr. Now: two or three values both work, and a runner that hands
    over no proxy gets the egress guard opened here, so the resolver still
    dials through it."""
    monkeypatch.setattr(settings, "egress_transport_guard", True)
    seen = {}

    async def fake_resolve(data, fields, base_url, **kw):
        seen["proxy"] = kw.get("proxy")
        return data, []

    monkeypatch.setattr(scrape_runner, "resolve_redirect_fields", fake_resolve)
    out = await run_scrape(_CamoufoxShapedRunner(), "req_cfx", _request(resolve_redirects=["links"]), None)
    assert isinstance(out, ScrapeOk), out
    assert isinstance(seen["proxy"], ProxyConfig)
    assert seen["proxy"].server.startswith("http://127.0.0.1:"), "the guard the hook opened"


@pytest.mark.asyncio
async def test_with_the_transport_guard_off_the_resolver_gets_no_proxy(monkeypatch):
    """Then the resolver's own per-stub check is what stands between a
    page-controlled URL and the network — its default, `assert_navigable`."""
    monkeypatch.setattr(settings, "egress_transport_guard", False)
    seen = {}

    async def fake_resolve(data, fields, base_url, **kw):
        seen["proxy"] = kw.get("proxy")
        return data, []

    monkeypatch.setattr(scrape_runner, "resolve_redirect_fields", fake_resolve)
    out = await run_scrape(_CamoufoxShapedRunner(), "req_noguard", _request(resolve_redirects=["links"]), None)
    assert isinstance(out, ScrapeOk), out
    assert seen == {"proxy": None}


@pytest.mark.asyncio
async def test_with_no_budget_left_resolution_is_skipped_and_said_so(monkeypatch):
    """A batch deadline longer than what remains of the attempt would be
    cancelled from outside and discard every partial. With (almost) nothing
    left the hook does not start at all, and says so, rather than starting
    something the ceiling is about to kill."""
    async def must_not_run(*_a, **_kw):
        raise AssertionError("resolver started with no budget")

    monkeypatch.setattr(scrape_runner, "resolve_redirect_fields", must_not_run)
    # `attempt_deadline` is the task deadline minus the packing slack, so aim
    # ~0.9 s past that slack: the (instant) fake fetch and parse fit, and what
    # is left for the batch is under the 0.5 s the hook insists on.
    slack = scrape_runner.attempt_slack_s(settings.page_task_timeout_s)
    out = await run_scrape(
        _Runner(), "req_nobudget", _request(resolve_redirects=["links"]), None,
        deadline=time.perf_counter() + slack + 0.9,
    )
    assert isinstance(out, ScrapeOk), out
    assert any("redirect resolution skipped" in w for w in out.result.warnings), out.result.warnings
    assert _Transport.exited is True, "the guard is unwound on the skip path too"


@pytest.mark.asyncio
async def test_a_preset_whose_links_stay_on_site_is_not_flagged(monkeypatch):
    """mobile.de: every listing is the site's own query-addressed page on one
    path -- the shape the guard reads as an unwrapped redirect. The preset
    says so through `preset_meta.links_stay_on_site` and the guard stays out."""
    async def must_not_run(*_a, **_kw):
        raise AssertionError("resolver called without resolve_redirects")

    monkeypatch.setattr(scrape_runner, "resolve_redirect_fields", must_not_run)
    meta = {"name": "mobile_de_search_chromium", "source": "mobile_de", "links_stay_on_site": True}
    out = await run_scrape(_Runner(), "req_onsite", _request(preset_meta=meta), None)

    assert isinstance(out, ScrapeOk)
    assert out.result.data["links"] == STUBS
    assert not [w for w in out.result.warnings if "self_referential" in w], out.result.warnings


@pytest.mark.asyncio
async def test_without_the_field_nothing_is_resolved_and_the_stubs_are_flagged(monkeypatch):
    async def must_not_run(*_a, **_kw):
        raise AssertionError("resolver called without resolve_redirects")

    monkeypatch.setattr(scrape_runner, "resolve_redirect_fields", must_not_run)
    out = await run_scrape(_Runner(), "req_noresolve", _request(), None)

    assert isinstance(out, ScrapeOk)
    assert out.result.data["links"] == STUBS
    # Unresolved absolute stubs on the page's host are exactly what that guard
    # exists to flag — which is also why resolution has to run before it.
    assert any("self_referential" in w for w in out.result.warnings), out.result.warnings
