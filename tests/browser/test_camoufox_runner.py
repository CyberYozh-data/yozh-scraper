from __future__ import annotations

import pytest
from src.browser.camoufox_runner import CamoufoxRunner, build_camoufox_options


def test_build_options_maps_normalized_fields():
    opts = build_camoufox_options(
        proxy={"server": "http://h:1", "username": "u", "password": "p"},
        block_assets=True, webrtc_block=True,
        humanize=True, spoof_os="windows", block_webgl=True, addons=["ublock"],
    )
    assert opts["geoip"] is True
    assert opts["block_images"] is True
    # WEBRTC_BLOCK no longer maps to Camoufox's block_webrtc — it deletes the
    # constructor, which is its own tell. See
    # test_webrtc_block_keeps_the_api_native_and_stops_the_transport.
    assert opts["block_webrtc"] is False
    assert opts["humanize"] is True
    assert opts["os"] == "windows"
    assert opts["block_webgl"] is True
    assert opts["addons"] == ["ublock"]
    assert opts["proxy"]["server"] == "http://h:1"


def test_options_never_ask_for_a_persistent_context():
    """Pins what `cast(Browser, browser)` in fetch() assumes.

    camoufox's `__aenter__` returns a BrowserContext instead of a Browser when
    `persistent_context=True`, and only Browser.new_page takes the context
    options this runner passes (`no_viewport`). Nothing here sets that flag, so
    the cast holds — but that is an invariant of this builder, not a law, and
    without a test the next option added here would break the launch with a
    TypeError mid-scrape instead of failing at review.
    """
    for kwargs in (
        {"proxy": None, "block_assets": False, "webrtc_block": False},
        {"proxy": None, "block_assets": True, "webrtc_block": True,
         "viewport": {"width": 1920, "height": 1080}},
    ):
        assert "persistent_context" not in build_camoufox_options(**kwargs)


def test_build_options_defaults_are_sane():
    opts = build_camoufox_options(proxy=None, block_assets=False, webrtc_block=False)
    assert opts["geoip"] is True          # always geoip when proxying
    assert opts["block_images"] is False
    assert opts["humanize"] is False
    assert opts["block_webgl"] is False
    assert opts.get("os") is None
    assert opts.get("addons") is None
    assert "window" not in opts           # no viewport -> Camoufox picks its own


def test_build_options_viewport_maps_to_window_tuple():
    """A viewport is forwarded as Camoufox's window=(w, h)."""
    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=False,
        viewport={"width": 1920, "height": 1080},
    )
    assert opts["window"] == (1920, 1080)


def test_camoufox_options_pin_the_locale_for_the_requested_country():
    """Camoufox draws a language from the exit region's list when none is pinned,
    and that list includes minority languages: production runs announced bar-DE
    (Bavarian), it-DE and en-DE for Germany and es-US for the United States.
    en-DE is what made Amazon serve an English page to a preset whose price
    parser had been told to expect European formatting."""
    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=False,
        proxy_geo={"country_code": "DE"},
    )
    assert opts["locale"] == "de-DE"


def test_camoufox_options_omit_the_locale_for_an_unknown_country():
    """Unknown country: no pin, so Camoufox's own draw still applies rather than
    a guess of ours being asserted as fact."""
    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=False,
        proxy_geo={"country_code": "ZZ"},
    )
    assert "locale" not in opts


def test_camoufox_options_omit_the_locale_when_no_geo_is_given():
    """No proxy_geo at all (the common no-proxy path) must not KeyError or
    invent a locale — same as the unknown-country case."""
    opts = build_camoufox_options(proxy=None, block_assets=False, webrtc_block=False)
    assert "locale" not in opts


def test_camoufox_never_pins_a_locale_camoufox_itself_would_reject():
    """Every _COUNTRY_MAP entry, run through camoufox's OWN validator.

    geo_profile.py is not guaranteed to only contain locales Camoufox's
    IANA-tag data recognizes: measured, exactly one of 250 entries --
    "XK" -> "sq-XK" (Kosovo, not a registered ISO region as far as the
    `language-tags` package is concerned) -- raises `InvalidLocale` when
    handed to Camoufox's own `handle_locales`. Before the guard in
    build_camoufox_options existed, that locale was pinned unconditionally,
    so `AsyncCamoufox(**opts)` raised before the browser ever started and the
    queue's retry loop repeated the same guaranteed failure on every retry,
    burning the attempt budget and the proxy session on something that could
    never succeed.

    This is the guard's regression pin, not a comment: for every country the
    pin build_camoufox_options actually produces must agree with what
    Camoufox's own validator would accept — present exactly when Camoufox
    accepts it, absent precisely when Camoufox would not.
    """
    from camoufox.exceptions import InvalidLocale
    from camoufox.locales import handle_locales

    from src.browser.geo_profile import _COUNTRY_MAP

    for country_code, profile in _COUNTRY_MAP.items():
        try:
            handle_locales(profile.locale, {})
            camoufox_accepts = True
        except InvalidLocale:
            camoufox_accepts = False

        opts = build_camoufox_options(
            proxy=None, block_assets=False, webrtc_block=False,
            proxy_geo={"country_code": country_code},
        )

        if camoufox_accepts:
            assert opts.get("locale") == profile.locale, country_code
        else:
            assert "locale" not in opts, (
                f"{country_code}: Camoufox rejects {profile.locale!r} but "
                "build_camoufox_options pinned it anyway"
            )


def test_runner_is_never_warm():
    r = CamoufoxRunner(timeout_ms=30000)
    assert r.is_started() is False


class _ACM:
    """Minimal async-context-manager wrapper around a mock browser."""
    def __init__(self, browser):
        self._browser = browser
    async def __aenter__(self):
        return self._browser
    async def __aexit__(self, *a):
        return False


def _mock_page(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    page = AsyncMock()
    page.goto = AsyncMock(return_value=Mock(status=200, request=Mock(redirected_from=None)))
    page.content = AsyncMock(return_value="<html><body>real results</body></html>")
    page.url = "https://ya.ru/"
    page.evaluate = AsyncMock(return_value={
        "ua": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
        "locale": "ru", "tz": "Asia/Almaty", "al": "ru,en;q=0.9",
    })
    page.screenshot = AsyncMock(return_value=b"PNGBYTES")
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)
    # Reachable from the page so a test can assert on how the page was opened,
    # not just on what happened to it afterwards.
    page.owning_browser = browser
    monkeypatch.setattr("src.browser.camoufox_runner.AsyncCamoufox", lambda **kw: _ACM(browser))
    return page


@pytest.mark.asyncio
async def test_fetch_reports_applied_fingerprint(monkeypatch):
    """Camoufox aligns locale/timezone to the exit IP via geoip; the runner must
    read them back so meta.applied_* isn't null (it was, vs the Playwright path)."""
    _mock_page(monkeypatch)
    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False,
    )
    assert res.applied_user_agent and "Firefox" in res.applied_user_agent
    assert res.applied_locale == "ru"
    assert res.applied_timezone == "Asia/Almaty"
    assert res.applied_accept_language == "ru,en;q=0.9"


@pytest.mark.asyncio
async def test_fetch_captures_screenshot_when_requested(monkeypatch):
    """screenshot=True must yield a base64 PNG on Camoufox (was always None)."""
    _mock_page(monkeypatch)
    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=True, block_assets=True,
    )
    assert res.screenshot_b64 is not None
    import base64
    assert base64.b64decode(res.screenshot_b64) == b"PNGBYTES"


@pytest.mark.asyncio
async def test_fetch_no_screenshot_when_not_requested(monkeypatch):
    _mock_page(monkeypatch)
    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False,
    )
    assert res.screenshot_b64 is None


@pytest.mark.asyncio
async def test_fetch_screenshot_full_page_status(monkeypatch):
    """Full-page screenshot (no selector) reports the same status as Playwright."""
    _mock_page(monkeypatch)
    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=True, block_assets=True,
    )
    assert res.element_status == "not_requested"


@pytest.mark.asyncio
async def test_fetch_meta_readback_failure_does_not_break_fetch(monkeypatch):
    """If the applied-meta page.evaluate raises, the fetch still succeeds with
    applied_* left None (best-effort)."""
    page = _mock_page(monkeypatch)
    from unittest.mock import AsyncMock
    page.evaluate = AsyncMock(side_effect=RuntimeError("evaluate blew up"))
    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False,
    )
    assert res.ok is True
    assert res.applied_user_agent is None
    assert res.applied_locale is None
    assert res.applied_timezone is None


@pytest.mark.asyncio
async def test_fetch_applies_extra_headers(monkeypatch):
    """User-supplied headers must reach the page (they were silently dropped)."""
    page = _mock_page(monkeypatch)
    await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None,
        headers={"X-Custom": "1", "Accept-Language": "de-DE"},
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False,
    )
    page.set_extra_http_headers.assert_awaited_once_with(
        {"X-Custom": "1", "Accept-Language": "de-DE"}
    )


@pytest.mark.asyncio
async def test_fetch_accept_language_override_is_echoed(monkeypatch):
    """An explicit Accept-Language wins on the wire, so applied_* must report
    it — navigator.languages keeps the fingerprint value (here ru,en;q=0.9)."""
    _mock_page(monkeypatch)
    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None,
        headers={"Accept-Language": "de-DE"},
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False,
    )
    assert res.applied_accept_language == "de-DE"


@pytest.mark.asyncio
async def test_fetch_drops_user_agent_header(monkeypatch, caplog):
    """Camoufox owns the UA (rotating fingerprint); a network-level override
    would desync it from navigator.userAgent, so it must be filtered out."""
    import logging
    page = _mock_page(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="src.browser.camoufox_runner"):
        await CamoufoxRunner(timeout_ms=30000).fetch(
            url="https://ya.ru/", device="desktop", proxy=None,
            headers={"User-Agent": "curl/8", "X-Keep": "yes"},
            wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
            screenshot=False,
        )
    page.set_extra_http_headers.assert_awaited_once_with({"X-Keep": "yes"})
    assert any("User-Agent" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_fetch_drops_the_client_hints_that_came_with_the_ua(monkeypatch):
    """Four of the header presets in scraper-tester send `sec-ch-ua*` next to
    the UA as one set. Dropping only the UA leaves Camoufox's own Firefox
    identity beside a Chrome Client-Hints claim -- a contradiction the
    fingerprint cannot back, and one it did not have before."""
    page = _mock_page(monkeypatch)
    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None,
        headers={
            "User-Agent": "Chrome/124", "sec-ch-ua": '"Google Chrome";v="124"',
            "sec-ch-ua-platform": '"Windows"', "X-Keep": "yes",
        },
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False,
    )
    page.set_extra_http_headers.assert_awaited_once_with({"X-Keep": "yes"})
    assert res.ignored_request_fields == [
        "headers['Sec-CH-UA']", "headers['Sec-CH-UA-Platform']",
        "headers['User-Agent']",
    ]


@pytest.mark.asyncio
async def test_the_drop_is_reported_on_a_failed_fetch_too(monkeypatch):
    """The path where a caller most needs the answer.

    A UA override is reached for on exactly the hard targets, which are also
    the ones that time out. Reporting the drop only on success meant the
    operator saw nothing on the runs they were trying to explain -- the same
    reasoning `applied_fingerprint` already carries on this error return.
    """
    from unittest.mock import AsyncMock

    from playwright.async_api import TimeoutError as PWTimeoutError

    page = _mock_page(monkeypatch)
    page.goto = AsyncMock(side_effect=PWTimeoutError("Timeout 30000ms exceeded"))

    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None,
        headers={"User-Agent": "curl/8"}, wait_until="domcontentloaded",
        wait_for_selector=None, timeout_ms=None, screenshot=False,
    )
    assert res.ok is False
    assert res.ignored_request_fields == ["headers['User-Agent']"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [None, {}, {"User-Agent": "curl/8"}])
async def test_fetch_no_applicable_headers_skips_header_call(monkeypatch, headers):
    page = _mock_page(monkeypatch)
    await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=headers,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False,
    )
    page.set_extra_http_headers.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_screenshot_failure_does_not_break_fetch(monkeypatch):
    """A screenshot failure must degrade to None, not fail the fetch."""
    page = _mock_page(monkeypatch)
    from unittest.mock import AsyncMock
    page.screenshot = AsyncMock(side_effect=RuntimeError("snap failed"))
    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=True, block_assets=True,
    )
    assert res.ok is True
    assert res.screenshot_b64 is None
    assert res.element_status == "no_screenshot"


def test_a_forced_window_carries_the_screen_constraint_it_needs():
    """Camoufox does NOT derive a screen >= window from `window` alone.

    That was assumed rather than checked, and it is false: with `window` set to
    1920x1080 and the screen left to Camoufox, measured screens came back
    1680x1050 and even 960x540 — a window larger than the monitor it is
    supposedly on, which is physically impossible and therefore a tell. The
    constraint has to be stated, so a window is never emitted without a screen
    floor that matches it.
    """
    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=True,
        viewport={"width": 1920, "height": 1080},
    )

    assert opts["window"] == (1920, 1080)
    screen = opts["screen"]
    assert screen.min_width == 1920 and screen.min_height == 1080, (
        "the randomised screen must not be allowed below the forced window"
    )


def test_no_viewport_means_no_window_and_no_screen_floor():
    """Camoufox generates its own coherent pair when we do not interfere."""
    opts = build_camoufox_options(proxy=None, block_assets=False, webrtc_block=True)

    assert "window" not in opts
    assert "screen" not in opts


def test_webrtc_block_keeps_the_api_native_and_stops_the_transport():
    """Deleting RTCPeerConnection is itself the tell we are hiding from.

    Camoufox's `block_webrtc` removes the constructor outright: measured in the
    production option set, `typeof RTCPeerConnection === "undefined"`. Real
    Firefox always has it, so on the one engine we reserve for the hardest
    targets we were announcing that something had been done to the browser.

    The Gecko pref restricts ICE to the proxy instead, which is the documented
    contract for the same guarantee and leaves the API where a real browser has
    it. (Neither configuration emitted a UDP packet in this container — the
    change buys the missing tell, not a leak fix.)
    """
    opts = build_camoufox_options(proxy=None, block_assets=False, webrtc_block=True)

    assert opts["block_webrtc"] is False, "the constructor must stay where a real Firefox has it"
    # Nothing is added in its place: the shipped camoufox.cfg already sets
    # ice.proxy_only_if_behind_proxy, and an extra ice.proxy_only would collapse
    # the no-proxy candidate set to zero — a stronger tell than the one removed.
    assert "firefox_user_prefs" not in opts


def test_webrtc_off_leaves_both_alone():
    opts = build_camoufox_options(proxy=None, block_assets=False, webrtc_block=False)

    assert opts["block_webrtc"] is False
    assert "firefox_user_prefs" not in opts


def test_a_viewport_too_large_to_floor_forces_no_window_either():
    """A window without a guaranteed screen floor is the original defect.

    Camoufox cannot generate a screen floor above roughly 2560x1440 — it raises
    outright at 2880x1800 and 3840x2160, and silently returns screens BELOW the
    floor higher still. Forcing the window anyway would put us back to a window
    larger than its own monitor, so above the cap the geometry is left to
    Camoufox entirely.
    """
    for width, height in ((2880, 1800), (3840, 2160), (7680, 4320)):
        opts = build_camoufox_options(
            proxy=None, block_assets=False, webrtc_block=True,
            viewport={"width": width, "height": height},
        )
        assert "window" not in opts, (width, height)
        assert "screen" not in opts, (width, height)


def test_the_floor_is_derived_from_the_request_not_a_constant():
    """A second size, because the only one under test used to equal the default
    desktop viewport — so a hardcoded 1920x1080 floor passed everything."""
    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=True,
        viewport={"width": 1366, "height": 768},
    )

    assert opts["window"] == (1366, 768)
    assert opts["screen"].min_width == 1366
    assert opts["screen"].min_height == 768


@pytest.mark.e2e
def test_the_screen_floor_cap_is_still_what_camoufox_can_serve():
    """Re-measure the corpus boundary the cap encodes.

    `_MAX_SERVEABLE_SCREEN_FLOOR` is a constant because probing it costs a
    fingerprint generation. This asserts the constant still matches reality, so
    a camoufox or browserforge bump that moves the boundary fails here instead
    of in production.
    """
    from browserforge.fingerprints import Screen
    from camoufox.utils import launch_options

    from src.browser.camoufox_runner import _MAX_SERVEABLE_SCREEN_FLOOR as cap

    launch_options(headless=True, geoip=False, window=cap,
                   screen=Screen(min_width=cap[0], min_height=cap[1]))

    with pytest.raises(ValueError):
        launch_options(headless=True, geoip=False, window=(2880, 1800),
                       screen=Screen(min_width=2880, min_height=1800))


def test_geoip_follows_the_setting(monkeypatch):
    """`geoip=True` was hardcoded, with no way to turn it off.

    It stays on by default: aligning locale and timezone to the exit is a
    fingerprint benefit, and it also gates camoufox's WebRTC exit-IP spoof. The
    cost is smaller than it first appears — camoufox caches the lookup per
    process under the proxy URL, and the prem gateway hands out a constant
    username, so it resolves once per worker (238ms cold, 65ms warm, one cache
    entry) rather than once per request.

    It is a setting because the exception is expensive: an unreachable proxy
    raises instead of caching, so a broken exit pays the lookup every time and
    then fails to launch.
    """
    from src.browser import camoufox_runner as mod

    monkeypatch.setattr(mod.settings, "camoufox_geoip", False)
    opts = build_camoufox_options(proxy=None, block_assets=False, webrtc_block=False)
    assert opts["geoip"] is False

    monkeypatch.setattr(mod.settings, "camoufox_geoip", True)
    opts = build_camoufox_options(proxy=None, block_assets=False, webrtc_block=False)
    assert opts["geoip"] is True


@pytest.mark.asyncio
async def test_a_forced_window_renders_at_the_window_not_playwrights_default(monkeypatch):
    """Playwright's default viewport silently overrode the window we forced.

    `new_page()` applies a 1280x720 viewport of its own, so a request for
    1920x1080 set the OS window to 1920x1080 and then rendered inside 1280x720.
    That is 640px of HORIZONTAL browser chrome, which no browser has — Firefox's
    chrome is vertical only — and it also means a caller asking for 1920x1080 was
    served 1280x720 without being told.

    Measured `no_viewport=True` five times: inner 1920x1029 under outer
    1920x1080, i.e. 0 horizontal and 51 vertical, which is the real Firefox
    shape. `new_page(viewport=...)` was measured too and is NOT the fix — it
    inverted the geometry in 3 of 5 launches.
    """
    page = _mock_page(monkeypatch)

    await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False, viewport={"width": 1920, "height": 1080},
    )

    page.owning_browser.new_page.assert_awaited_once_with(no_viewport=True)


@pytest.mark.asyncio
async def test_no_viewport_holds_even_where_no_window_is_forced(monkeypatch):
    """Outside the serveable range it is still the better of two bad options.

    An earlier draft made this conditional on a window having been forced, on
    the theory that rendering at Camoufox's own size was "the same silent
    substitution". Measuring it disproved that: out of range the requested size
    is ignored either way, but WITHOUT no_viewport the page also renders at
    1280x720 inside whatever window Camoufox picked — 160-416px of horizontal
    chrome in 5 of 5 launches. With it, 0 of 5.

    What it does not fix out of range is Camoufox's own window-versus-screen
    draw, which stayed broken in both arms. That is a separate lottery this
    argument does not touch.
    """
    page = _mock_page(monkeypatch)

    await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False, viewport={"width": 3840, "height": 2160},
    )

    page.owning_browser.new_page.assert_awaited_once_with(no_viewport=True)


@pytest.mark.asyncio
async def test_the_default_request_gets_the_window_and_renders_in_it(monkeypatch):
    """The commonest Camoufox request passes no viewport at all.

    It is also the one whose rendered size this change moves — 1280x720 to the
    default desktop 1920x1080 — so it should not be reachable only through the
    tests that pass an explicit viewport.
    """
    page = _mock_page(monkeypatch)

    await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False,
    )

    page.owning_browser.new_page.assert_awaited_once_with(no_viewport=True)


def test_a_window_too_narrow_for_firefox_is_not_forced_either():
    """Firefox will not lay out content below 500px however small the window.

    Measured with the window forced: at 320 and at 400, innerWidth came back 500
    — an inner viewport wider than its own window, which is exactly the inverted
    geometry `new_page(viewport=...)` was rejected for. `Viewport` allows width
    >= 320, so a caller can ask for this.
    """
    for width, height in ((320, 240), (400, 300), (499, 400)):
        opts = build_camoufox_options(
            proxy=None, block_assets=False, webrtc_block=True,
            viewport={"width": width, "height": height},
        )
        assert "window" not in opts, (width, height)
        assert "screen" not in opts, (width, height)


def test_the_serveable_range_is_inclusive_at_both_ends():
    """Both bounds are measured values, so off-by-one costs a real request."""
    for width, height in ((500, 400), (2560, 1440)):
        opts = build_camoufox_options(
            proxy=None, block_assets=False, webrtc_block=True,
            viewport={"width": width, "height": height},
        )
        assert opts["window"] == (width, height)
        assert opts["screen"].min_width == width


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [(1920, 1080), (1601, 1001)])
async def test_the_geometry_invariant_still_holds_in_a_real_browser(requested):
    """`inner <= outer <= screen` with equal widths is a property of the
    camoufox+playwright pair, not of our code.

    Every unit test here asserts which kwarg we pass; none of them can tell
    whether the browser still honours it. A camoufox or playwright bump could
    restore the 1280x720 override and leave the whole suite green, which is the
    same gap `test_the_screen_floor_cap_is_still_what_camoufox_can_serve`
    exists to close for the cap constant.

    Sizes. 1920x1080 is the shipped desktop default, so it has to be here.
    1601x1001 is here because 1920x1080 cannot prove the kwargs did anything:
    Camoufox's own monitor corpus contains 1920x1080, and a launch that
    ignored every kwarg we passed still opens a window filling that monitor,
    satisfying both the floor and the size. Measured with the `window`/`screen`
    kwargs dropped: at 1920x1080 the assertions below survived that mutation
    1 launch in 30; at 1601x1001, which is nobody's monitor, 0 of 15 (they
    came back on 1680x1050 and 960x540 screens). The odd size is the
    discriminator; the default is the thing that ships.

    The size is compared against the spoofed monitor's WORK AREA in both
    dimensions, not against the request. `screen` floors at the request, so
    Camoufox may pick a monitor of exactly 1920x1080, whose availHeight is
    1040 or 1053 -- a taskbar. No browser opens a 1080-tall window there, and
    neither does ours: of 45 forced launches at the default size, the 4 that
    drew a 1920x1080 monitor are exactly the 4 whose window came back short,
    each equal to that draw's availHeight to the pixel.

    KNOWN, RARE, NOT OURS: 1 of those 45 launches came back on a 1280x720
    screen with a 1280x708 window -- Camoufox honoured neither the window nor
    the floor. It fails the floor assertion, which is the right outcome; a
    single red run whose reported screen is far BELOW the floor is that draw,
    not a regression. Re-run before believing it.
    """
    import os as _os

    # In the service image the entrypoint always starts Xvfb, so this skip is
    # for a bare-host run only. A green `-m e2e` outside a container with no
    # X server means the body never ran.
    if not _os.environ.get("DISPLAY"):
        pytest.skip("headful launch needs an X display (Xvfb); DISPLAY is unset")

    from camoufox.async_api import AsyncCamoufox

    width, height = requested
    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=True, headless=False,
        viewport={"width": width, "height": height},
    )
    async with AsyncCamoufox(**opts) as browser:
        page = await browser.new_page(no_viewport=True)
        await page.goto("https://example.com/", wait_until="domcontentloaded", timeout=60000)
        seen = await page.evaluate(
            "() => ({inner: [innerWidth, innerHeight],"
            " outer: [outerWidth, outerHeight],"
            " screen: [screen.width, screen.height],"
            " avail: [screen.availWidth, screen.availHeight]})"
        )

    inner, outer, screen, avail = seen["inner"], seen["outer"], seen["screen"], seen["avail"]
    # No browser has horizontal chrome; the vertical difference is the toolbar.
    assert inner[0] == outer[0], f"{inner[0] - outer[0]}px of horizontal chrome"
    assert inner[1] <= outer[1]
    assert avail[0] <= screen[0] and avail[1] <= screen[1], (
        f"work area {avail} larger than the monitor {screen}"
    )
    assert outer[0] <= screen[0] and outer[1] <= screen[1], "window larger than its monitor"
    assert screen[0] >= width and screen[1] >= height, (
        f"the screen floor did not survive: asked at least {width}x{height}, got {screen}"
    )
    served = [min(width, avail[0]), min(height, avail[1])]
    assert outer == served, (
        f"the forced window did not survive: asked {width}x{height}, "
        f"work area {avail}, got {outer}"
    )

def test_a_profile_pins_the_os_and_the_gpu():
    """The two knobs a profile states; Camoufox keeps the rest.

    A third — navigator.hardwareConcurrency — was measured and removed: it made
    yandex_search worse (1 retry to 8 on a Windows claim over 20 runs) and it
    hid nothing, because Camoufox already spoofs that value.
    """
    from src.browser.fingerprint_profile import ResolvedFingerprint

    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=True,
        fingerprint=ResolvedFingerprint(
            profile="windows_on_host",
            spoof_os="windows",
            webgl_config=(
                "Google Inc. (AMD)",
                "ANGLE (AMD, Radeon R9 200 Series Direct3D11 vs_5_0 ps_5_0), or similar",
            ),
        ),
    )

    assert opts["os"] == "windows"
    assert opts["webgl_config"] == (
        "Google Inc. (AMD)",
        "ANGLE (AMD, Radeon R9 200 Series Direct3D11 vs_5_0 ps_5_0), or similar",
    )
    assert "config" not in opts, "navigator properties are Camoufox's to generate"


def test_a_profile_that_pins_nothing_adds_no_keys():
    """`random` must leave the option dict exactly as it was before profiles.

    Handing Camoufox an empty `config` or a `webgl_config` of None is not the
    same as not passing them, and `random` is the benchmark's control arm — a
    control that quietly passes extra kwargs measures something else.
    """
    from src.browser.fingerprint_profile import ResolvedFingerprint

    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=True,
        fingerprint=ResolvedFingerprint(profile="random"),
    )

    assert "os" not in opts
    assert "webgl_config" not in opts
    assert "config" not in opts


def test_a_profile_pinning_only_the_os_leaves_the_hardware_keys_out():
    """A bare OS profile is `spoof_os`, so it must emit exactly what that did."""
    from src.browser.fingerprint_profile import ResolvedFingerprint

    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=True,
        fingerprint=ResolvedFingerprint(profile="macos", spoof_os="macos"),
    )

    assert opts["os"] == "macos"
    assert "webgl_config" not in opts
    assert "config" not in opts


def test_an_explicit_spoof_os_still_works_without_a_profile():
    """`spoof_os` is the field that shipped; it must not need the new one."""
    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=True, spoof_os="macos",
    )

    assert opts["os"] == "macos"
    assert "webgl_config" not in opts


@pytest.mark.asyncio
async def test_the_fetch_resolves_the_profile_it_was_given(monkeypatch):
    """Resolution lives in the runner so probes and tests default like the queue."""
    seen: dict = {}
    _mock_page(monkeypatch)
    real = build_camoufox_options

    def _spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr("src.browser.camoufox_runner.build_camoufox_options", _spy)

    await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False, fingerprint_profile="linux",
    )

    assert seen["fingerprint"].profile == "linux"
    assert seen["fingerprint"].spoof_os == "linux"


@pytest.mark.asyncio
async def test_the_fetch_forwards_proxy_geo_to_the_locale_pin(monkeypatch):
    """The entire production delta for the locale-pin fix is one forwarded
    kwarg at this call site (fetch -> build_camoufox_options). Every test
    that exercises the pin by calling build_camoufox_options directly would
    stay green even if that forwarding were deleted -- confirmed by deleting
    it and re-running this file: the direct-call locale tests still passed,
    and so did the rest of tests/browser. This is the one test that goes red
    on that deletion.
    """
    seen: dict = {}
    built: dict = {}
    _mock_page(monkeypatch)
    real = build_camoufox_options

    def _spy(**kwargs):
        seen.update(kwargs)
        result = real(**kwargs)
        built.update(result)
        return result

    monkeypatch.setattr("src.browser.camoufox_runner.build_camoufox_options", _spy)

    await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False, proxy_geo={"country_code": "DE"},
    )

    assert seen["proxy_geo"] == {"country_code": "DE"}
    assert built["locale"] == "de-DE"


@pytest.mark.asyncio
async def test_a_lone_spoof_os_routes_through_the_same_resolution(monkeypatch):
    """One code path, so the old field cannot drift from the new one."""
    seen: dict = {}
    _mock_page(monkeypatch)
    real = build_camoufox_options

    def _spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr("src.browser.camoufox_runner.build_camoufox_options", _spy)

    await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False, spoof_os="windows",
    )

    assert seen["fingerprint"].profile == "windows"
    assert seen["fingerprint"].webgl_config is None


@pytest.mark.asyncio
async def test_the_applied_fingerprint_comes_back_on_the_result(monkeypatch):
    """Otherwise the only record of which profile ran is a worker log.

    A profile that degraded — an unknown name, a GPU vendor the table has no row
    for — is indistinguishable from one that applied, and by the time anyone
    looks the response is somewhere else entirely.
    """
    _mock_page(monkeypatch)

    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False, fingerprint_profile="windows",
    )

    assert res.applied_fingerprint == {
        "profile": "windows",
        "os": "windows",
        "webgl_vendor": None,
        "webgl_renderer": None,
    }


@pytest.mark.asyncio
async def test_a_launch_failure_still_reports_the_profile_it_tried(monkeypatch):
    """A refused launch is exactly when the profile is the prime suspect."""
    def _boom(**_kwargs):
        raise RuntimeError("launch refused")

    monkeypatch.setattr("src.browser.camoufox_runner.AsyncCamoufox", _boom)

    res = await CamoufoxRunner(timeout_ms=30000).fetch(
        url="https://ya.ru/", device="desktop", proxy=None, headers=None,
        wait_until="domcontentloaded", wait_for_selector=None, timeout_ms=None,
        screenshot=False, fingerprint_profile="linux",
    )

    assert res.ok is False
    assert res.applied_fingerprint["profile"] == "linux"


def test_the_launch_hands_firefox_a_narrowed_environment(monkeypatch):
    """Camoufox defaults this to the whole of os.environ, so without it the
    Firefox process holds the service token, the LLM keys and the proxy
    passwords. Its own CAMOU_CONFIG chunks are merged UNDER what we pass and
    survive: measured live, 6 processes, chunk present, no canary, forced
    window and spoofed UA unchanged.
    """
    monkeypatch.setenv("SERVICE_TOKEN", "s3cret")
    monkeypatch.setenv("DISPLAY", ":99")

    opts = build_camoufox_options(proxy=None, block_assets=False, webrtc_block=True)

    assert "SERVICE_TOKEN" not in opts["env"]
    assert opts["env"]["DISPLAY"] == ":99"

def test_ublock_is_shipped_by_default_and_excludable_by_setting(monkeypatch):
    """Pins the CAMOUFOX_DISABLE_UBO branch, which review found untested.

    Replacing the branch with `if False:` left tests/browser and
    tests/queue green, so the switch proved nothing -- and it is the only lever
    against a class of failure that recurs without a commit, since uBO is
    downloaded at first browser LAUNCH (not at image build) and its filter
    lists rewrite the pages we scrape. Camoufox calls `add_default_addons()`
    unconditionally inside its own `launch_options()`, so `exclude_addons` is
    the only way out; asserted on the option dict that reaches
    `AsyncCamoufox(**opts)`.
    """
    from src.settings import settings

    base = dict(proxy=None, block_assets=False, webrtc_block=True)

    monkeypatch.setattr(settings, "camoufox_disable_ubo", False)
    assert "exclude_addons" not in build_camoufox_options(**base)

    monkeypatch.setattr(settings, "camoufox_disable_ubo", True)
    excluded = build_camoufox_options(**base)["exclude_addons"]
    assert [getattr(a, "name", a) for a in excluded] == ["UBO"]


def test_excluding_ublock_does_not_drop_a_caller_supplied_addon(monkeypatch):
    """The two lists are independent: one names what to ADD, one what to DROP."""
    from src.settings import settings

    monkeypatch.setattr(settings, "camoufox_disable_ubo", True)
    opts = build_camoufox_options(
        proxy=None, block_assets=False, webrtc_block=True, addons=["/tmp/some-addon"]
    )
    assert opts["addons"] == ["/tmp/some-addon"]
    assert "exclude_addons" in opts
