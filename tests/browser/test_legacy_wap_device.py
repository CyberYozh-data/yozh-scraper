"""A device that predates Client Hints must send none of them.

`device='legacy_wap'` exists for one measured reason: Google serves a
JavaScript-free layout at `/wml/search` to a whitelist of legacy User-Agents,
and that layout ships real `/url?q=` links instead of the opaque
`/goto?url=CAES…` stubs the JS results page returns.

The identity has to be coherent or it is worse than the honest Chrome UA it
replaces. Measured on the wire, chromium, same request three ways:

    no override at all      UA=Nokia7610  Sec-CH-UA="HeadlessChrome";v="143"
                                          Sec-CH-UA-Platform="Windows"
    override, empty brands  UA=Nokia7610  Sec-CH-UA absent
                                          Sec-CH-UA-Platform=""      <- no client does this
    override, no metadata   UA=Nokia7610  both headers ABSENT        <- what ships

and end to end through a residential exit: HTTP 200, 14 result blocks, 22
`/url?q=` links, `applied_user_agent` reporting the Nokia string that was
actually sent.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.browser.runner import LEGACY_WAP, PlaywrightRunner, apply_page_masking
from src.schemas import ScrapeRequest
from src.sessions.models import SessionCreateRequest


async def _context_kwargs(engine: str, device: str):
    runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
    runner._engine = engine
    browser = AsyncMock()
    browser.version = "152.0.7977.82"
    browser.new_context = AsyncMock(return_value=AsyncMock())
    runner._browser = browser
    await runner._new_context(device=device, proxy=None, headers=None, render=False,
                              viewport=None, proxy_geo=None)
    return browser.new_context.await_args.kwargs


class TestTheIdentityIsStatedByEveryEngine:
    """Unlike the Chrome desktop UA, this one is not engine-specific.

    It carries no `Chrome/` token to align to the bundle, and with Client Hints
    suppressed there is nothing left to contradict it — Firefox and WebKit emit
    none natively, Chromium is made to emit none. TLS still says what the
    engine is, which `LEGACY_WAP`'s docstring states out loud.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
    async def test_the_nokia_ua_is_stated_verbatim(self, engine):
        kwargs = await _context_kwargs(engine, "legacy_wap")
        assert kwargs["user_agent"] == LEGACY_WAP["user_agent"]
        assert "Nokia7610" in kwargs["user_agent"]

    @pytest.mark.asyncio
    async def test_it_is_not_version_aligned_like_the_chrome_string(self):
        """`_align_ua_to_engine` rewrites a `Chrome/` token to the real bundle
        major. There is none here, and rewriting would break the whitelist
        match that is the entire point."""
        kwargs = await _context_kwargs("chromium", "legacy_wap")
        assert "Chrome/" not in kwargs["user_agent"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
    async def test_the_other_devices_are_unchanged(self, engine):
        """Adding a device must not move the two that ship."""
        kwargs = await _context_kwargs(engine, "desktop")
        if engine == "chromium":
            assert "Chrome/152.0.0.0" in (kwargs["user_agent"] or "")
        else:
            assert kwargs["user_agent"] is None

    @pytest.mark.asyncio
    async def test_playwright_firefox_would_reject_mobile_emulation(self):
        """Firefox has no mobile emulation, and this device is measured working
        on Firefox — so it must not ask for any."""
        kwargs = await _context_kwargs("firefox", "legacy_wap")
        assert not kwargs.get("is_mobile")
        assert not kwargs.get("has_touch")


class TestClientHintsAreSuppressedNotSkipped:
    @staticmethod
    def _context(suppress):
        ctx = MagicMock()
        ctx._applied_user_agent = LEGACY_WAP["user_agent"]
        ctx._suppress_client_hints = suppress
        ctx.new_cdp_session = AsyncMock()
        return ctx

    @pytest.mark.asyncio
    async def test_the_override_is_sent_without_metadata(self):
        """Absent, not empty. Sending empty brands still emits a bare
        `Sec-CH-UA-Platform: ""`, which no real client does."""
        ctx = self._context(True)
        await apply_page_masking(ctx, MagicMock(), engine="chromium", stealth=False)
        sent = ctx.new_cdp_session.return_value.send.await_args
        assert sent.args[0] == "Emulation.setUserAgentOverride"
        payload = sent.args[1]
        assert payload["userAgent"] == LEGACY_WAP["user_agent"]
        assert "userAgentMetadata" not in payload, (
            "metadata present means Chromium still advertises Client Hints"
        )

    @pytest.mark.asyncio
    async def test_a_truthy_double_does_not_switch_this_on(self):
        """`MagicMock` auto-creates any attribute and the result is truthy, so
        a loose check turned this branch on for every mock-based test in the
        suite — including two that assert the ordinary Chrome path."""
        ctx = MagicMock()
        ctx._applied_user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
        )
        ctx.new_cdp_session = AsyncMock()
        await apply_page_masking(ctx, MagicMock(), engine="chromium", stealth=False)
        payload = ctx.new_cdp_session.return_value.send.await_args.args[1]
        assert "userAgentMetadata" in payload, (
            "an ordinary Chrome context must keep its aligned Client Hints"
        )


class TestTheContractRefusesWhatItCannotHonour:
    def test_camoufox_is_refused(self):
        """Camoufox owns its fingerprint: it would keep its Firefox identity
        while the request claimed a feature phone — the contradiction this
        device exists to avoid."""
        with pytest.raises(ValidationError, match="legacy_wap"):
            ScrapeRequest(url="https://x.example/", device="legacy_wap",
                          browser_engine="camoufox")

    @pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
    def test_the_engines_that_can_are_accepted(self, engine):
        req = ScrapeRequest(url="https://x.example/", device="legacy_wap",
                            browser_engine=engine, render=False)
        assert req.device == "legacy_wap"

    def test_it_is_not_the_default(self):
        assert ScrapeRequest(url="https://x.example/").device == "desktop"


class TestTheIdentityIsRefusedWhereItCannotHold:
    """Two pairings the schema used to accept.

    `render` defaults to true, and `render` IS `java_script_enabled` on the
    context (`runner.py`), so a feature phone from 2004 arrived executing
    modern JavaScript -- the same shape of contradiction #123/#125 removed from
    the User-Agent, one layer down. A session is worse than a scrape: its device
    pins the LOGIN as well, so the credentials would go out under that identity.
    """

    def test_js_rendering_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            ScrapeRequest(url="https://example.com/", device="legacy_wap")
        # The sentence, not the field name: a narrowed Literal would mention
        # `render` too and would be a different mechanism entirely.
        assert "requires render=false" in str(exc.value)

    def test_with_rendering_off_it_is_accepted(self):
        req = ScrapeRequest(url="https://example.com/", device="legacy_wap", render=False)
        assert req.device == "legacy_wap"

    def test_the_other_devices_still_render_by_default(self):
        assert ScrapeRequest(url="https://example.com/", device="mobile").render is True

    def test_a_session_cannot_pin_it(self):
        with pytest.raises(ValidationError) as exc:
            SessionCreateRequest(device="legacy_wap", ttl_seconds=3600)
        # Likewise: dropping the value from the Literal would also say
        # "legacy_wap", and would take it away from scrapes as well.
        assert "cannot be pinned to a session" in str(exc.value)

    def test_a_session_still_takes_the_ordinary_devices(self):
        assert SessionCreateRequest(device="mobile", ttl_seconds=3600).device == "mobile"


class TestTheSuppressionFlagIsActuallySet:
    """The read side was pinned; the WRITE was not.

    `apply_page_masking` reads `_suppress_client_hints` with a default of False,
    so neutering the line that stashes it leaves Client Hints leaking and the
    whole suite green -- measured, 2576 passed. That is the failure #125 already
    paid for: a flag stashed on an object and read with a default fails OPEN, so
    the test has to start from the device, not from the flag.
    """

    @staticmethod
    def _runner_with_fake_browser():
        # An AsyncMock context, so the awaited calls in `_new_context` work and
        # an attribute that is never ASSIGNED reads back as a mock child rather
        # than as True/False -- which is what makes the identity checks below
        # fail when the write is removed.
        runner = PlaywrightRunner.__new__(PlaywrightRunner)
        runner._engine = "chromium"
        runner.block_assets = False
        runner._browser = MagicMock()
        runner._browser.version = "143.0.0.0"
        runner._browser.new_context = AsyncMock(return_value=AsyncMock())
        return runner

    @pytest.mark.asyncio
    async def test_render_false_is_what_turns_javascript_off(self):
        """The premise the schema rule rests on, asserted rather than assumed:
        `render` is not advice, it is the context's `java_script_enabled`."""
        runner = self._runner_with_fake_browser()
        await runner._new_context(device="legacy_wap", proxy=None, headers=None, render=False)
        kwargs = runner._browser.new_context.await_args.kwargs
        assert kwargs["java_script_enabled"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("device,expected", [("legacy_wap", True), ("desktop", False), ("mobile", False)])
    async def test_the_context_carries_the_device_decision(self, device, expected):
        runner = self._runner_with_fake_browser()
        context = await runner._new_context(device=device, proxy=None, headers=None, render=False)
        assert getattr(context, "_suppress_client_hints", None) is expected
