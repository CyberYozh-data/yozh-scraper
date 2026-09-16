from __future__ import annotations

import logging

import pytest
from unittest.mock import Mock, AsyncMock, MagicMock, call, patch
import base64

from src.browser.runner import (
    PlaywrightRunner, FetchResult, DESKTOP, MOBILE, classify_fetch, _chrome_ua_metadata,
    strip_engine_owned_headers,
)
from src.proxy.models import ProxyConfig
from playwright.async_api import Error as PWError


class TestPlaywrightRunner:
    def test_runner_init(self):
        """Initializing runner"""
        runner = PlaywrightRunner(
            headless=True,
            block_assets=False,
            timeout_ms=30000,
        )

        assert runner.headless is True
        assert runner.block_assets is False
        assert runner.timeout_ms == 30000
        assert runner._browser is None
        assert runner._playwright is None

    @pytest.mark.asyncio
    async def test_runner_start(self):
        """Start browser"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        # Mock playwright
        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_playwright.stop = AsyncMock()

        # Call mock for async_playwright()
        mock_async_playwright_instance = MagicMock()
        mock_async_playwright_instance.start = AsyncMock(return_value=mock_playwright)
        mock_async_playwright_instance.__aenter__ = AsyncMock(return_value=mock_playwright)
        mock_async_playwright_instance.__aexit__ = AsyncMock()

        mock_async_playwright = MagicMock(return_value=mock_async_playwright_instance)

        with patch("src.browser.runner.async_playwright", mock_async_playwright):
            await runner.start()

            assert runner._browser == mock_browser
            assert runner._playwright == mock_playwright

            # Check that launch was called with expected args
            call_kwargs = mock_playwright.chromium.launch.call_args[1]
            assert call_kwargs["headless"] is True
            assert "args" in call_kwargs

            # Cleanup
            await runner.stop()

    @pytest.mark.asyncio
    async def test_stop_stops_the_driver_even_when_browser_close_raises(self):
        """A wedged / OOM-killed browser raises on close(). If that skips
        playwright.stop() the node driver process leaks — and for an ephemeral
        runner (never in state.runners) no lifecycle path can ever reclaim it."""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        mock_browser = AsyncMock()
        mock_browser.close = AsyncMock(side_effect=PWError("browser wedged"))
        mock_playwright = AsyncMock()
        mock_playwright.stop = AsyncMock()
        runner._browser = mock_browser
        runner._playwright = mock_playwright

        await runner.stop()  # must not raise: teardown is best-effort

        mock_playwright.stop.assert_awaited_once()  # the driver was reclaimed
        # Handles cleared regardless, so the next start() rebuilds from scratch.
        assert runner._browser is None
        assert runner._playwright is None

    @pytest.mark.asyncio
    async def test_runner_start_with_webrtc_block(self):
        """Start browser with WebRTC blocking enabled"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_playwright.stop = AsyncMock()

        mock_async_playwright_instance = MagicMock()
        mock_async_playwright_instance.start = AsyncMock(return_value=mock_playwright)
        mock_async_playwright = MagicMock(return_value=mock_async_playwright_instance)

        with patch("src.browser.runner.async_playwright", mock_async_playwright):
            with patch("src.browser.runner.settings.webrtc_block", True):
                await runner.start()

                call_kwargs = mock_playwright.chromium.launch.call_args[1]
                assert call_kwargs["headless"] is True
                # WebRTC flags present...
                assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in call_kwargs["args"]
                assert "--force-webrtc-ip-handling-policy" in call_kwargs["args"]
                # ...alongside the always-on anti-automation flag.
                assert "--disable-blink-features=AutomationControlled" in call_kwargs["args"]

                await runner.stop()

    @pytest.mark.asyncio
    async def test_runner_start_without_webrtc_block(self):
        """Start browser with WebRTC blocking disabled"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_playwright.stop = AsyncMock()

        mock_async_playwright_instance = MagicMock()
        mock_async_playwright_instance.start = AsyncMock(return_value=mock_playwright)
        mock_async_playwright = MagicMock(return_value=mock_async_playwright_instance)

        with patch("src.browser.runner.async_playwright", mock_async_playwright):
            with patch("src.browser.runner.settings.webrtc_block", False):
                await runner.start()

                call_kwargs = mock_playwright.chromium.launch.call_args[1]
                assert call_kwargs["headless"] is True
                # No WebRTC flags, but the anti-automation flag is always on.
                # Asserted by membership rather than exact list equality: the
                # WebRTC flags are what this test is about, and pinning the
                # whole list makes every unrelated Chromium flag a failure here
                # instead of in the test that owns it.
                args = call_kwargs["args"]
                assert "--disable-blink-features=AutomationControlled" in args
                assert not any("webrtc" in a for a in args)

                await runner.stop()

    @pytest.mark.asyncio
    async def test_runner_start_always_disables_automation_controlled(self):
        """Google (and other anti-bot SERPs) redirect a headless Chromium to a
        /sorry captcha unless --disable-blink-features=AutomationControlled is
        set; it must be present regardless of the WebRTC setting. Empirically
        this flips Google from a captcha to a full rendered SERP on a clean IP."""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_playwright.stop = AsyncMock()
        mock_async_playwright_instance = MagicMock()
        mock_async_playwright_instance.start = AsyncMock(return_value=mock_playwright)
        mock_async_playwright = MagicMock(return_value=mock_async_playwright_instance)

        with patch("src.browser.runner.async_playwright", mock_async_playwright):
            for webrtc in (True, False):
                with patch("src.browser.runner.settings.webrtc_block", webrtc):
                    await runner.start()
                    args = mock_playwright.chromium.launch.call_args[1]["args"]
                    assert "--disable-blink-features=AutomationControlled" in args
                    await runner.stop()

    @pytest.mark.asyncio
    async def test_runner_stop(self):
        """Browser stop"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        # Set mock
        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()
        runner._browser = mock_browser
        runner._playwright = mock_playwright

        await runner.stop()

        mock_browser.close.assert_called_once()
        mock_playwright.stop.assert_called_once()
        assert runner._browser is None
        assert runner._playwright is None

    @pytest.mark.asyncio
    async def test_runner_stop_nulls_handles_even_if_close_raises(self):
        """A wedged browser.close() must still leave the runner ready to rebuild.

        Without this guarantee an idle-shutdown that hits a broken Chromium
        would keep ._browser set, and the next idempotent start() would skip
        re-launching, so the next fetch() reuses a dead Browser handle.

        stop() no longer propagates the close failure: teardown is best-effort so
        that playwright.stop() always runs (see the driver-leak test above). Every
        caller already discarded this exception anyway.
        """
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        mock_browser = AsyncMock()
        mock_browser.close.side_effect = RuntimeError("wedged Chromium")
        mock_playwright = AsyncMock()
        runner._browser = mock_browser
        runner._playwright = mock_playwright

        await runner.stop()

        # Handles cleared before close() ran, so the next start() will rebuild.
        assert runner._browser is None
        assert runner._playwright is None
        assert runner.is_started() is False

    @pytest.mark.asyncio
    async def test_runner_start_idempotent(self):
        """Double start not create new browser"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        mock_browser = AsyncMock()
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_playwright.stop = AsyncMock()

        mock_async_playwright_instance = MagicMock()
        mock_async_playwright_instance.start = AsyncMock(return_value=mock_playwright)
        mock_async_playwright = MagicMock(return_value=mock_async_playwright_instance)

        with patch("src.browser.runner.async_playwright", mock_async_playwright):
            await runner.start()
            await runner.start()

            # start() must be call only one time (because have if self._browser is not None)
            assert mock_async_playwright_instance.start.call_count == 1

            await runner.stop()


async def _new_context_kwargs(
    engine: str, *, device: str = "desktop", headers: dict | None = None,
    version: str = "152.0.7977.82",
):
    """The kwargs `_new_context` hands Playwright, with the browser mocked out.

    Shared by every class below that asserts on the context Playwright is
    actually given: three of them grew a private copy of this setup, and the
    copies then disagreed on the Chrome major they pinned.
    """
    runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
    runner._engine = engine
    mock_browser = AsyncMock()
    mock_browser.version = version
    mock_browser.new_context = AsyncMock(return_value=AsyncMock())
    runner._browser = mock_browser
    await runner._new_context(
        device=device, proxy=None, headers=headers, render=True,
        viewport=None, proxy_geo=None,
    )
    return mock_browser.new_context.await_args.kwargs


class TestPerEngineUserAgent:
    """A UA is only safe to state when the transport agrees with it.

    Measured 2026-09-06 against a TLS mirror: our Firefox sent
    `Chrome/124.0.0.0 (Windows NT 10.0)` while its JA4 was
    `t13d1717h2_5b57614c22b0` (Firefox), its HTTP/2 settings hash was
    Firefox's, and it sent no Sec-CH-UA at all -- Firefox has no Client
    Hints. Three axes contradicting the header at once, which no real user
    produces. The desktop preset's Chrome UA is therefore stated only by the
    engine that can back it.
    """

    @pytest.mark.asyncio
    async def test_chromium_states_the_chrome_ua_aligned_to_its_build(self):
        kwargs = await _new_context_kwargs("chromium")
        ua = kwargs["user_agent"]
        assert "Chrome/152.0.0.0" in ua, ua
        assert "Windows NT 10.0" in ua, ua

    @pytest.mark.asyncio
    async def test_webkit_under_mobile_emulation_keeps_the_iphone_ua(self):
        """The mobile preset's UA is an iPhone Safari string, which WebKit CAN
        back -- same engine family, unlike the desktop Chrome string."""
        kwargs = await _new_context_kwargs("webkit", device="mobile")
        assert "iPhone" in (kwargs["user_agent"] or ""), kwargs["user_agent"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["firefox", "webkit"])
    async def test_other_engines_keep_their_own_true_ua(self, engine):
        """None means "browser default": Playwright leaves the engine's real UA,
        which is the only string its TLS and Client-Hints behaviour can back."""
        kwargs = await _new_context_kwargs(engine)
        assert kwargs["user_agent"] is None, (
            f"{engine} must not advertise the Chrome desktop UA: its JA4 and "
            "its absent/!=Chrome Client Hints contradict it on sight"
        )


class TestStripEngineOwnedHeaders:
    """The choke point both runners go through, tested on its own.

    It exists because the rule was written twice and the copies drifted, so the
    thing worth pinning is the function, not each caller's use of it.
    """

    @pytest.mark.parametrize(
        "spelling", ["User-Agent", "user-agent", "USER-AGENT", "uSeR-aGeNt"]
    )
    def test_every_spelling_is_removed(self, spelling):
        kept, dropped = strip_engine_owned_headers({spelling: "curl/8", "X-Keep": "yes"})
        assert kept == {"X-Keep": "yes"}
        assert dropped == ["headers['User-Agent']"]

    def test_the_reported_name_is_canonical_not_the_callers_spelling(self):
        """The warning text is a cross-repo string; it must not vary with how
        the caller happened to capitalise the header."""
        _, dropped = strip_engine_owned_headers({"uSeR-aGeNt": "curl/8"})
        assert dropped == ["headers['User-Agent']"]

    def test_the_client_hints_family_goes_with_it(self):
        """Four of the presets in scraper-tester send these NEXT TO the UA.
        Dropping only the UA left firefox stating an honest `Firefox/144`
        beside `sec-ch-ua: "Google Chrome"` -- two headers contradicting each
        other, which is worse than the contradiction being removed."""
        kept, dropped = strip_engine_owned_headers({
            "User-Agent": "Chrome/124", "sec-ch-ua": '"Google Chrome";v="124"',
            "sec-ch-ua-platform": '"Windows"', "sec-ch-ua-mobile": "?0",
            "X-Keep": "yes",
        })
        assert kept == {"X-Keep": "yes"}
        assert dropped == [
            "headers['Sec-CH-UA']", "headers['Sec-CH-UA-Mobile']",
            "headers['Sec-CH-UA-Platform']", "headers['User-Agent']",
        ]

    def test_absent_ua_is_reported_as_not_dropped(self):
        kept, dropped = strip_engine_owned_headers({"X-Keep": "yes"})
        assert kept == {"X-Keep": "yes"}
        assert dropped == []

    @pytest.mark.parametrize("empty", [None, {}])
    def test_no_headers_at_all(self, empty):
        assert strip_engine_owned_headers(empty) == ({}, [])

    def test_a_header_merely_containing_the_word_is_kept(self):
        """`not in` on a substring would eat these; the rule is an exact
        case-insensitive name match."""
        headers = {"X-User-Agent": "a", "User-Agent-Original": "b",
                   "X-Sec-CH-UA": "c"}
        kept, dropped = strip_engine_owned_headers(headers)
        assert kept == headers
        assert dropped == []

    def test_the_input_mapping_is_not_mutated(self):
        headers = {"User-Agent": "curl/8", "X-Keep": "yes"}
        strip_engine_owned_headers(headers)
        assert headers == {"User-Agent": "curl/8", "X-Keep": "yes"}


class TestCallerSuppliedUserAgentHeader:
    """A `User-Agent` in the caller's `headers` is not a UA the engine can back.

    Measured 2026-09-11 through `PlaywrightRunner.fetch` against a live header
    echo, one engine per row, caller sending
    `Nokia7610/2.0 (5.0509.0) SymbianOS/7.0s ...`:

      chromium  wire UA = the preset's Chrome/143  (context UA wins over
                `extra_http_headers`, so the caller's string never reaches the
                wire), Sec-CH-UA = `Google Chrome/143`, applied_user_agent =
                Chrome/143 -- coherent, but the caller was never told their
                header was discarded.
      firefox   wire UA = Nokia7610, Sec-CH-UA absent, applied_user_agent =
                `Firefox/144`.
      webkit    wire UA = Nokia7610, Sec-CH-UA absent, applied_user_agent =
                `Safari/26`.

    Firefox and WebKit pass `user_agent=None`, so nothing outranks the header
    and the caller's string DOES go out -- over Gecko/WebKit TLS, with no
    Client Hints. That is the shape PR #123 removed from our own presets,
    reachable again through the public API. Worse, `meta.applied_user_agent`
    then names a string that was NOT sent, and that field is the first thing
    read when a scrape comes back blocked.

    So the header is dropped on every Playwright engine and the drop is logged,
    which is what the Camoufox runner already does (`camoufox_runner.py`, "the
    fingerprint manages it"). On chromium this changes nothing on the wire; it
    only stops the silence. Every other caller header is untouched.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
    async def test_the_caller_ua_never_reaches_extra_http_headers(self, engine):
        kwargs = await _new_context_kwargs(
            engine, headers={"User-Agent": "Nokia7610/2.0 (5.0509.0) SymbianOS/7.0s"}
        )
        sent = kwargs.get("extra_http_headers") or {}
        assert not any(k.lower() == "user-agent" for k in sent), (
            f"{engine} would put the caller's UA on the wire: {sent!r}. On "
            "firefox/webkit that is measured to actually happen -- an "
            "unbackable UA over the wrong TLS with no Client Hints."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spelling", ["User-Agent", "user-agent", "USER-AGENT"])
    async def test_a_caller_ua_is_matched_case_insensitively(self, spelling):
        """HTTP header names are case-insensitive; `user-agent` must not slip
        past a comparison written against the capitalised spelling.

        One engine is enough: the filter sits above every engine branch. The
        spelling is the input that carries the signal."""
        engine = "firefox"
        kwargs = await _new_context_kwargs(engine, headers={spelling: "curl/8", "X-Keep": "yes"})
        sent = kwargs.get("extra_http_headers") or {}
        assert not any(k.lower() == "user-agent" for k in sent), sent
        assert sent.get("X-Keep") == "yes", "an unrelated header was collateral"

    @pytest.mark.asyncio
    async def test_every_other_caller_header_survives(self):
        kwargs = await _new_context_kwargs(
            "chromium",
            headers={"User-Agent": "curl/8", "X-Probe": "yes", "Accept-Language": "de-DE"},
        )
        sent = kwargs.get("extra_http_headers") or {}
        assert sent.get("X-Probe") == "yes"
        assert sent.get("Accept-Language") == "de-DE", (
            "Accept-Language is the header most likely to be caught by a "
            "sloppier filter, since the line below this one also touches it"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
    async def test_the_engine_ua_decision_is_unchanged_by_the_drop(self, engine):
        """Dropping the header must not become a back door into `user_agent`:
        chromium still states its own aligned Chrome string, firefox and webkit
        are still given None."""
        kwargs = await _new_context_kwargs(engine, headers={"User-Agent": "curl/8"})
        if engine == "chromium":
            assert "Chrome/152.0.0.0" in (kwargs["user_agent"] or "")
        else:
            assert kwargs["user_agent"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("headers,expected", [
        ({"User-Agent": "curl/8"}, ["headers['User-Agent']"]),
        ({"X-Probe": "yes"}, []),
        (None, []),
    ])
    async def test_the_drop_reaches_the_envelope_through_the_real_context(
        self, headers, expected, monkeypatch,
    ):
        """Drives the REAL `_new_context`, not a patched one.

        An earlier version of this test hand-set the value the production code
        was supposed to produce, so deleting that production line left the whole
        suite green -- the exact failure this repo has recorded twice. The
        wiring is what needs pinning, not the copy.
        """
        import src.browser.runner as mod
        monkeypatch.setattr(mod, "assert_navigable", AsyncMock())
        monkeypatch.setattr(mod, "assert_page_public", AsyncMock())
        monkeypatch.setattr(mod, "apply_page_masking", AsyncMock())

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html><body>ok</body></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._engine = "chromium"
        mock_browser = AsyncMock()
        mock_browser.version = "152.0.7977.82"
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        runner._browser = mock_browser
        runner.start = AsyncMock()

        result = await runner.fetch(
            url="https://example.com", device="desktop", proxy=None,
            headers=headers, wait_until="domcontentloaded",
            wait_for_selector=None, timeout_ms=None, screenshot=False,
        )
        assert result.ignored_request_fields == expected

    @pytest.mark.asyncio
    async def test_dropping_it_is_logged_so_the_caller_can_find_out(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.browser.runner"):
            await _new_context_kwargs("firefox", headers={"User-Agent": "curl/8"})
        assert any(
            "user-agent" in rec.message.lower() for rec in caplog.records
        ), "a silently discarded request field is how #123 stayed hidden"

    @pytest.mark.asyncio
    async def test_no_warning_when_the_caller_sent_no_ua(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.browser.runner"):
            await _new_context_kwargs("firefox", headers={"X-Probe": "yes"})
        assert not any("user-agent" in rec.message.lower() for rec in caplog.records)


class TestAppliedUserAgentReadback:
    """`meta.applied_user_agent` is documented as the UA that was actually sent
    and is the first field anyone reads when a scrape comes back blocked.
    Chromium echoes the override we stated; Firefox and WebKit are given none,
    so the runner reads the engine's own UA back off the page -- the same
    read-back CamoufoxRunner already does.
    """

    @staticmethod
    async def _fetch_with(engine: str, ua_from_page: str, evaluate_raises=None):
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._engine = engine
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html><body>ok</body></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()
        mock_page.evaluate = AsyncMock(
            side_effect=evaluate_raises if evaluate_raises else None,
            return_value=ua_from_page,
        )

        mock_context = AsyncMock()
        mock_context._applied_user_agent = None      # what firefox/webkit leave behind
        mock_context._applied_locale = "en-US"
        mock_context._applied_timezone = "America/New_York"
        mock_context._applied_accept_language = None
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            return await runner.fetch(
                url="https://example.com", device="desktop", proxy=None, headers=None,
                wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
                screenshot=False,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine,ua", [
        ("firefox", "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0"),
        ("webkit", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    ])
    async def test_the_engines_own_ua_reaches_the_envelope(self, engine, ua):
        result = await self._fetch_with(engine, ua)
        assert result.applied_user_agent == ua

    @pytest.mark.asyncio
    async def test_a_failed_readback_does_not_fail_the_fetch(self):
        """A restricted page must cost the field, never the scrape."""
        result = await self._fetch_with(
            "firefox", "unused", evaluate_raises=RuntimeError("execution context destroyed"),
        )
        assert result.ok is True
        assert result.applied_user_agent is None


class TestFetch:
    @pytest.mark.asyncio
    async def test_fetch_success_basic(self):
        """Base fetch is success"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        # Set _browser, for go around start()
        mock_browser = AsyncMock()
        runner._browser = mock_browser

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html><body>Test</body></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=None,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=None,
                screenshot=False,
            )

        assert result.ok is True
        assert result.html == "<html><body>Test</body></html>"
        assert result.final_url == "https://example.com"
        assert result.status_code == 200
        assert result.screenshot_b64 is None
        assert result.error is None

    @pytest.mark.asyncio
    async def test_fetch_http_403_is_not_ok_and_blocked(self):
        """A 403 still returns an HTML body (no Playwright error), but it's a
        proxy ban — the fetch must be not-ok + blocked so the queue rotates."""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 403
        mock_page.goto = AsyncMock(return_value=mock_response)
        # A generic edge error page (not a captcha phrase) — the old heuristic
        # missed this, so the fetch was wrongly reported ok.
        mock_page.content = AsyncMock(return_value="<html><body>Error Page</body></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=None,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=None,
                screenshot=False,
            )

        assert result.status_code == 403
        assert result.ok is False
        assert result.blocked is True
        assert result.error and "403" in result.error

    @pytest.mark.asyncio
    async def test_fetch_with_proxy(self):
        """Fetch with proxy"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        proxy = ProxyConfig(
            server="http://proxy.com:8080",
            username="user",
            password="pass",
        )

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_new_context = AsyncMock(return_value=mock_context)

        with patch.object(runner, "_new_context", mock_new_context):
            await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=proxy,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=None,
                screenshot=False,
            )

        # Check, _new_context was called with proxy
        mock_new_context.assert_called_once()
        call_kwargs = mock_new_context.call_args[1]
        assert call_kwargs["proxy"] == proxy

    @pytest.mark.asyncio
    async def test_fetch_with_headers(self):
        """Fetch with custom headers"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        headers = {"User-Agent": "Custom UA", "Accept-Language": "en-US"}

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_new_context = AsyncMock(return_value=mock_context)

        with patch.object(runner, "_new_context", mock_new_context):
            await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=None,
                headers=headers,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=None,
                screenshot=False,
            )

        # Check, headers send
        call_kwargs = mock_new_context.call_args[1]
        assert call_kwargs["headers"] == headers

    @pytest.mark.asyncio
    async def test_fetch_with_wait_for_selector(self):
        """Fetch with wait_for_selector"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.wait_for_selector = AsyncMock()
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=None,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=".content",
                timeout_ms=30000,
                screenshot=False,
            )

        mock_page.wait_for_selector.assert_called_once_with(".content", timeout=30000)

    @pytest.mark.asyncio
    async def test_fetch_with_screenshot(self):
        """Fetch with screenshot"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        screenshot_bytes = b"fake_png_data"

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.url = "https://example.com"
        mock_page.screenshot = AsyncMock(return_value=screenshot_bytes)
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=None,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=None,
                screenshot=True,
            )

        assert result.screenshot_b64 is not None
        assert result.screenshot_b64 == base64.b64encode(screenshot_bytes).decode("ascii")
        mock_page.screenshot.assert_called_once_with(full_page=True)

    @pytest.mark.asyncio
    async def test_fetch_mobile_device(self):
        """Fetch for mobile"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_new_context = AsyncMock(return_value=mock_context)

        with patch.object(runner, "_new_context", mock_new_context):
            await runner.fetch(
                url="https://example.com",
                device="mobile",
                proxy=None,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=None,
                screenshot=False,
            )

        # Check, that device send
        call_kwargs = mock_new_context.call_args[1]
        assert call_kwargs["device"] == "mobile"

    @pytest.mark.asyncio
    async def test_fetch_timeout_error(self):
        """Fetch with timeout"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=PWError("Timeout exceeded"))
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=None,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=1000,
                screenshot=False,
            )

        assert result.ok is False
        assert result.error is not None
        assert "PlaywrightError" in result.error
        assert "Timeout" in result.error

    @pytest.mark.asyncio
    async def test_fetch_captcha_detection(self):
        """Found captcha"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        captcha_html = "<html><body>Please verify you are a human</body></html>"

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value=captcha_html)
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=None,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=None,
                screenshot=False,
            )

        assert result.ok is False
        assert "Captcha/block" in result.error

    @pytest.mark.asyncio
    async def test_fetch_network_error(self):
        """Network error during fetch"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=PWError("net::ERR_CONNECTION_REFUSED"))
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://example.com",
                device="desktop",
                proxy=None,
                headers=None,
                wait_until="domcontentloaded",
                wait_for_selector=None,
                timeout_ms=None,
                screenshot=False,
            )

        assert result.ok is False
        assert "PlaywrightError" in result.error
        assert "ERR_CONNECTION_REFUSED" in result.error


class TestNewContext:
    @pytest.mark.asyncio
    async def test_new_context_desktop(self):
        """Created context for desktop"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        runner._browser = mock_browser

        context = await runner._new_context(
            device="desktop",
            proxy=None,
            headers=None,
        )

        assert context == mock_context
        call_kwargs = mock_browser.new_context.call_args[1]
        assert call_kwargs["user_agent"] == DESKTOP["user_agent"]
        assert call_kwargs["viewport"] == DESKTOP["viewport"]

    @pytest.mark.asyncio
    async def test_new_context_mobile(self):
        """Create context for mobile"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        runner._browser = mock_browser

        context = await runner._new_context(
            device="mobile",
            proxy=None,
            headers=None,
        )

        call_kwargs = mock_browser.new_context.call_args[1]
        assert call_kwargs["user_agent"] == MOBILE["user_agent"]
        assert call_kwargs["viewport"] == MOBILE["viewport"]

    @pytest.mark.asyncio
    async def test_new_context_with_proxy(self):
        """Create context with proxy"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        proxy = ProxyConfig(
            server="http://proxy.com:8080",
            username="user",
            password="pass",
        )

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        runner._browser = mock_browser

        await runner._new_context(
            device="desktop",
            proxy=proxy,
            headers=None,
        )

        call_kwargs = mock_browser.new_context.call_args[1]
        proxy_arg = call_kwargs["proxy"]
        assert proxy_arg["server"] == "http://proxy.com:8080"
        assert proxy_arg["username"] == "user"
        assert proxy_arg["password"] == "pass"

    @pytest.mark.asyncio
    async def test_new_context_with_headers(self):
        """Create context with headers"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        headers = {"Custom-Header": "Value"}

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        runner._browser = mock_browser

        await runner._new_context(
            device="desktop",
            proxy=None,
            headers=headers,
        )

        call_kwargs = mock_browser.new_context.call_args[1]
        assert call_kwargs["extra_http_headers"] == headers

    @pytest.mark.asyncio
    async def test_new_context_block_assets(self):
        """Blocking assets in context"""
        runner = PlaywrightRunner(headless=True, block_assets=True, timeout_ms=30000)

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_context.route = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        runner._browser = mock_browser

        await runner._new_context(
            device="desktop",
            proxy=None,
            headers=None,
        )

        # Two handlers now: the asset blocker, then the egress guard. The
        # guard is registered second on purpose — Playwright runs handlers
        # last-registered-first, so it decides before the blocker forwards.
        assert mock_context.route.await_count == 2


class TestCaptchaDetection:
    def test_looks_like_captcha_detection_captcha_keyword(self):
        """Detecting captcha by word"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        html = "<html><body>Please solve the CAPTCHA</body></html>"
        assert runner._looks_like_captcha_or_block(html) is True

    def test_looks_like_captcha_detection_unusual_traffic(self):
        """Detecting by unusual traffic"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        html = "<html><body>Unusual traffic from your network</body></html>"
        assert runner._looks_like_captcha_or_block(html) is True

    def test_looks_like_captcha_detection_verify_human(self):
        """Detecting by verify you are a human"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        html = "<html><body>Please verify you are a human</body></html>"
        assert runner._looks_like_captcha_or_block(html) is True

    def test_looks_like_captcha_detection_access_denied(self):
        """Detecting by access denied"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        html = "<html><body>Access Denied</body></html>"
        assert runner._looks_like_captcha_or_block(html) is True

    def test_looks_like_captcha_detection_clean_html(self):
        """Clear HTML without blocking sings"""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)

        html = "<html><body><h1>Welcome</h1><p>Content</p></body></html>"
        assert runner._looks_like_captcha_or_block(html) is False

    def test_large_content_page_with_trigger_phrase_not_flagged(self):
        """A real, fully-rendered results page is large; a trigger phrase that
        appears there is content (e.g. a SERP for a query *about* captchas /
        anti-detect tooling), not a block. Real block/challenge interstitials
        are tiny, so size separates them. Regression: searching "cyberyozh"
        returned 7 organic results yet was flagged blocked, driving 5 wasteful
        proxy-rotating retries (~140s) and a scary warning on a good page."""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        big_results = (
            "<html><body>"
            + "<div data-hveid='1'><h3>Result about captcha solving</h3>"
            "<span>How to bypass unusual traffic checks — verify you are human"
            "</span></div>" * 400  # well past the block-page size ceiling
            + "</body></html>"
        )
        assert len(big_results) > 20_000
        assert runner._looks_like_captcha_or_block(big_results) is False

    def test_sorry_redirect_final_url_is_block(self):
        """Google serves real blocks at /sorry/ with HTTP 200, so the redirect
        URL — not the status code — is the unambiguous block signal."""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        html = "<html><body>Some short page</body></html>"
        final_url = (
            "https://www.google.com/sorry/index?continue=https://www.google.com/"
            "search%3Fq%3Dcyberyozh"
        )
        assert runner._looks_like_captcha_or_block(html, final_url=final_url) is True

    def test_yandex_showcaptcha_redirect_is_block(self):
        """Yandex SmartCaptcha serves at /showcaptcha with HTTP 200; the redirect
        URL marks the block so the worker rotates off a captcha'd exit instead of
        treating the empty interstitial as a successful (zero-result) fetch."""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        # A real SmartCaptcha page is large enough (>20 KB) that the size ceiling
        # would otherwise treat it as content — the URL signal is what flags it.
        html = "<html><body>" + ("x" * 30000) + "</body></html>"
        final_url = "https://yandex.com/showcaptcha?cc=1&retpath=aHR0cHM6&t=2"
        assert runner._looks_like_captcha_or_block(html, final_url=final_url) is True

    def test_small_block_page_still_flagged(self):
        """The real /sorry interstitial (tiny, with trigger phrases) must keep
        being flagged so the worker rotates off the burned exit."""
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        html = (
            "<html><body>Our systems have detected unusual traffic from your "
            "computer network. <div data-sitekey='x'></div></body></html>"
        )
        assert runner._looks_like_captcha_or_block(html) is True


class TestFetchResultElementStatus:
    def test_default_is_none(self):
        result = FetchResult(
            html="",
            final_url=None,
            status_code=None,
            screenshot_b64=None,
            ok=True,
            error=None,
        )
        assert result.element_status is None

    def test_can_be_set_to_literal_value(self):
        result = FetchResult(
            html="",
            final_url=None,
            status_code=None,
            screenshot_b64=None,
            ok=True,
            error=None,
            element_status="not_requested",
        )
        assert result.element_status == "not_requested"


class TestCaptureScreenshotLegacyPaths:
    @pytest.mark.asyncio
    async def test_screenshot_false_returns_no_screenshot(self):
        from src.browser.runner import _capture_screenshot
        page = AsyncMock()
        png, status = await _capture_screenshot(
            page,
            screenshot=False,
            element_selector=None,
            effective_block_assets=False,
        )
        assert png is None
        assert status == "no_screenshot"
        page.screenshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_screenshot_true_no_selector_returns_not_requested(self):
        from src.browser.runner import _capture_screenshot
        page = AsyncMock()
        page.screenshot = AsyncMock(return_value=b"\x89PNG fake bytes")
        page.evaluate = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        png, status = await _capture_screenshot(
            page,
            screenshot=True,
            element_selector=None,
            effective_block_assets=True,  # block_assets on => skip scroll pass
        )
        assert png == b"\x89PNG fake bytes"
        assert status == "not_requested"
        page.screenshot.assert_called_once()
        # full_page=True is the load-bearing assertion
        assert page.screenshot.call_args.kwargs.get("full_page") is True


class TestCaptureScreenshotFallbackFailure:
    @pytest.mark.asyncio
    async def test_fallback_capture_failure_returns_no_screenshot(self):
        """Element capture fails (selector matches nothing) and then the
        full-page fallback ITSELF raises: the helper must still NOT raise — it
        returns (None, "no_screenshot"). This is the one failure branch the
        real-Chromium e2e suite cannot trigger."""
        from src.browser.runner import _capture_screenshot
        from playwright.async_api import Error as PWError

        page = AsyncMock()
        locator = MagicMock()
        locator.count = AsyncMock(return_value=0)  # -> fallback_not_found
        page.locator = MagicMock(return_value=locator)
        page.screenshot = AsyncMock(side_effect=PWError("boom"))

        png, status = await _capture_screenshot(
            page,
            screenshot=True,
            element_selector="#x",
            effective_block_assets=True,  # skip scroll pass -> straight to screenshot
        )
        assert png is None
        assert status == "no_screenshot"


class TestComputeElementClip:
    @pytest.mark.asyncio
    async def test_rejects_nan_geometry(self):
        """A NaN coordinate must be rejected (returns None), not slip past the
        `<= 0` guards into an invalid clip rect that would crash the driver."""
        from src.browser.runner import _compute_element_clip

        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value={
            "x": float("nan"), "y": 0.0, "w": 200.0, "h": 100.0,
            "docW": 1280.0, "docH": 800.0,
        })
        assert await _compute_element_clip(locator) is None

    @pytest.mark.asyncio
    async def test_rejects_infinite_geometry(self):
        from src.browser.runner import _compute_element_clip

        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value={
            "x": 0.0, "y": 0.0, "w": float("inf"), "h": 100.0,
            "docW": 1280.0, "docH": 800.0,
        })
        assert await _compute_element_clip(locator) is None

    @pytest.mark.asyncio
    async def test_valid_geometry_returns_clip(self):
        from src.browser.runner import _compute_element_clip

        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value={
            "x": 50.0, "y": 50.0, "w": 200.0, "h": 100.0,
            "docW": 1280.0, "docH": 800.0,
        })
        clip = await _compute_element_clip(locator)
        assert clip is not None
        assert clip["width"] > 0 and clip["height"] > 0


class TestFetchFailurePathHonesty:
    """H5: a failed fetch must report element_status='no_screenshot' (not None,
    which the schema documents as 'legacy response') so consumers can tell a
    real capture failure apart from a pre-field response."""

    @pytest.mark.asyncio
    async def test_goto_error_reports_no_screenshot_status(self):
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=PWError("Timeout 30000ms exceeded"))
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://example.com", device="desktop", proxy=None,
                headers=None, wait_until="networkidle", wait_for_selector=None,
                timeout_ms=None, screenshot=True, element_selector="#app",
            )

        assert result.ok is False
        assert result.element_status == "no_screenshot"


class TestFetchBlockedFlag:
    """Captcha/block detection must set a structured `blocked` flag so the
    queue layer can rotate the proxy and retry (a captcha means the IP is
    burned), instead of giving up after one attempt."""

    @pytest.mark.asyncio
    async def test_captcha_content_sets_blocked(self):
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(
            return_value="<html><body>Our systems have detected unusual "
                         "traffic from your computer network.</body></html>")
        mock_page.url = "https://www.google.com/sorry/index"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://www.google.com/search?q=x", device="desktop",
                proxy=None, headers=None, wait_until="domcontentloaded",
                wait_for_selector=None, timeout_ms=None, screenshot=False,
            )

        assert result.ok is False
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_clean_page_not_blocked(self):
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_page = AsyncMock()
        mock_response = Mock(request=Mock(redirected_from=None))
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.content = AsyncMock(return_value="<html><body>Hello</body></html>")
        mock_page.url = "https://example.com"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context):
            result = await runner.fetch(
                url="https://example.com", device="desktop", proxy=None,
                headers=None, wait_until="domcontentloaded", wait_for_selector=None,
                timeout_ms=None, screenshot=False,
            )

        assert result.ok is True
        assert result.blocked is False


class TestFetchResourceCleanup:
    """M4: context/page/SOCKS-bridge must not leak when page setup raises
    before the main fetch try/finally is entered."""

    @pytest.mark.asyncio
    async def test_new_page_failure_closes_context_and_bridge(self):
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(side_effect=PWError("crashed"))
        mock_context.close = AsyncMock()

        bridge_cm = AsyncMock()
        bridge_cm.__aexit__ = AsyncMock()

        with patch.object(runner, "_new_context", return_value=mock_context), \
                patch.object(runner, "resolve_proxy",
                             AsyncMock(return_value=(None, bridge_cm, None))):
            with pytest.raises(PWError):
                await runner.fetch(
                    url="https://example.com", device="desktop", proxy=None,
                    headers=None, wait_until="domcontentloaded",
                    wait_for_selector=None, timeout_ms=None, screenshot=False,
                )

        mock_context.close.assert_awaited_once()
        bridge_cm.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_context_failure_closes_bridge(self):
        runner = PlaywrightRunner(headless=True, block_assets=False, timeout_ms=30000)
        runner._browser = AsyncMock()

        bridge_cm = AsyncMock()
        bridge_cm.__aexit__ = AsyncMock()

        with patch.object(runner, "_new_context",
                          AsyncMock(side_effect=PWError("context boom"))), \
                patch.object(runner, "resolve_proxy",
                             AsyncMock(return_value=(None, bridge_cm, None))):
            with pytest.raises(PWError):
                await runner.fetch(
                    url="https://example.com", device="desktop", proxy=None,
                    headers=None, wait_until="domcontentloaded",
                    wait_for_selector=None, timeout_ms=None, screenshot=False,
                )

        bridge_cm.__aexit__.assert_awaited_once()


class TestBlockAssetsRoute:
    """M5: asset-blocking route handler must be an awaitable coroutine (not a
    fire-and-forget create_task) so abort/continue complete before teardown."""

    @pytest.mark.asyncio
    async def test_aborts_blocked_resource_types(self):
        from src.browser.runner import _block_assets_route

        for rtype in ("image", "media", "font"):
            route = AsyncMock()
            route.request = Mock(resource_type=rtype)
            await _block_assets_route(route)
            route.abort.assert_awaited_once()
            route.continue_.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_other_resource_types(self):
        from src.browser.runner import _block_assets_route

        route = AsyncMock()
        route.request = Mock(resource_type="document")
        await _block_assets_route(route)
        route.continue_.assert_awaited_once()
        route.abort.assert_not_called()


class TestClassifyFetch:
    """classify_fetch() maps (http status, captcha heuristic) -> (ok, blocked, error).

    A ban/transient HTTP status must mark the fetch NOT ok so the queue's retry
    loop reaches its rotation decision instead of short-circuiting on ok=True.
    """

    def test_plain_200_is_ok(self):
        assert classify_fetch(200, captcha_detected=False) == (True, False, None)

    def test_captcha_is_blocked(self):
        ok, blocked, error = classify_fetch(200, captcha_detected=True)
        assert ok is False
        assert blocked is True
        assert error and "captcha" in error.lower()

    def test_ban_status_403_is_blocked(self):
        ok, blocked, error = classify_fetch(403, captcha_detected=False)
        assert ok is False
        assert blocked is True
        assert error and "403" in error

    def test_rate_limit_429_is_blocked(self):
        ok, blocked, _ = classify_fetch(429, captcha_detected=False)
        assert ok is False and blocked is True

    def test_transient_503_is_not_ok_but_not_burned(self):
        ok, blocked, error = classify_fetch(503, captcha_detected=False)
        assert ok is False
        assert blocked is False
        assert error and "503" in error

    def test_genuine_404_stays_ok(self):
        # A 404 is a real target response, not a proxy ban — must not trigger rotation.
        assert classify_fetch(404, captcha_detected=False) == (True, False, None)

    def test_missing_status_with_no_captcha_is_ok(self):
        assert classify_fetch(None, captcha_detected=False) == (True, False, None)


class TestChromeUaMetadata:
    """Client-Hints metadata builder that drops the HeadlessChrome Sec-CH-UA tell."""

    def test_non_chrome_ua_returns_none(self):
        # iPhone Safari (the mobile preset UA) sends no Sec-CH-UA at all.
        assert _chrome_ua_metadata(MOBILE["user_agent"]) is None
        assert _chrome_ua_metadata("Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/125.0") is None

    def test_windows_chrome_desktop_preset(self):
        md = _chrome_ua_metadata(DESKTOP["user_agent"])
        assert md is not None
        assert md["platform"] == "Windows"
        assert md["mobile"] is False
        assert md["architecture"] == "x86" and md["bitness"] == "64"
        brands = {b["brand"]: b["version"] for b in md["brands"]}
        assert brands["Google Chrome"] == "124" and brands["Chromium"] == "124"
        # fullVersionList mirrors brands with a full X.0.0.0 version
        fv = {b["brand"]: b["version"] for b in md["fullVersionList"]}
        assert fv["Google Chrome"] == "124.0.0.0"

    def test_platform_mapping(self):
        mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        android = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
        linux = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        assert _chrome_ua_metadata(mac)["platform"] == "macOS"
        amd = _chrome_ua_metadata(android)
        assert amd["platform"] == "Android" and amd["mobile"] is True
        assert _chrome_ua_metadata(linux)["platform"] == "Linux"
