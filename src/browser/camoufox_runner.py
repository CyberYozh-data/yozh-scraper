"""Per-request Camoufox runner.

Camoufox (anti-detect Firefox fork) is the only engine that beats Yandex
SmartCaptcha. Its fingerprint rotates per launch, so we launch a fresh
instance per request and tear it down — strongest anti-detect posture and
zero idle RAM. Mirrors PlaywrightRunner.fetch's result shape so the queue
layer stays engine-agnostic.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Literal, Optional, cast

from browserforge.fingerprints import Screen
from camoufox.addons import DefaultAddons
from camoufox.async_api import AsyncCamoufox
from camoufox.exceptions import InvalidLocale
from camoufox.locales import handle_locales
from playwright.async_api import Browser, TimeoutError as PWTimeoutError

from src.browser.launch_env import browser_env
from src.browser.runner import (
    DEFAULT_DESKTOP_VIEWPORT,
    FetchResult,
    SELECTOR_MISS_PREFIX,
    _capture_screenshot,
    classify_fetch,
    looks_like_captcha_or_block,
    redirected_to_block,
    run_warmup,
    strip_engine_owned_headers,
)
from src.browser.fingerprint_profile import ResolvedFingerprint, resolve_fingerprint
from src.browser.geo_profile import resolve_profile
from src.browser.page_io import read_content_settling_navigation
from src.security.egress import (
    EGRESS_BLOCKED_ERROR,
    EgressBlocked,
    assert_landing_public,
    assert_navigable,
    assert_page_public,
    make_route_guard,
)
from src.schemas import ElementScreenshotStatus
from src.proxy.models import ProxyConfig
from src.settings import settings

log = logging.getLogger(__name__)


# The largest screen floor Camoufox will actually generate. Above it,
# `launch_options` raises "No headers based on this input can be generated"
# — measured 8/8 at 2880x1800 and 3840x2160, 8/8 fine at 2560x1440 — because
# Camoufox narrows the browserforge corpus further with its own OS/browser
# constraints. Higher still (7680x4320) it stops raising and silently returns
# screens BELOW the floor, which is the impossible geometry all over again.
# A constant rather than a probe because generating a fingerprint to find out
# costs a launch; `test_the_screen_floor_cap_is_still_what_camoufox_can_serve`
# re-measures it, so a camoufox bump that moves the boundary goes red.
_MAX_SERVEABLE_SCREEN_FLOOR = (2560, 1440)

# Firefox will not lay out content narrower than this, whatever the window says.
# Measured with the window forced and `no_viewport=True`: at windows of 320 and
# 400, innerWidth came back 500 both times — an inner viewport WIDER than its own
# window, which is the inverted geometry `new_page(viewport=...)` was rejected
# for. From 500 up it holds (500, 600, 800, 1024, 1280 all gave inner <= outer).
# `Viewport` allows width >= 320, so this range is reachable from the API.
_MIN_SERVEABLE_WINDOW_WIDTH = 500


def _window_is_serveable(viewport: dict[str, int]) -> bool:
    """Whether forcing this window leaves the geometry coherent.

    Bounded at both ends, for different reasons: above the cap Camoufox cannot
    generate a screen floor to match, below the minimum Firefox cannot lay out
    the content. Outside either bound the window is left to Camoufox, which at
    least keeps the window it picked and the content it renders in agreement.
    """
    return (
        _MIN_SERVEABLE_WINDOW_WIDTH
        <= viewport["width"]
        <= _MAX_SERVEABLE_SCREEN_FLOOR[0]
        and viewport["height"] <= _MAX_SERVEABLE_SCREEN_FLOOR[1]
    )


def _camoufox_accepts_locale(locale: str) -> bool:
    """Whether Camoufox's own locale validator accepts this tag.

    geo_profile.py's table is not guaranteed to only contain locales
    Camoufox's IANA-tag data recognizes: measured, `handle_locales("sq-XK",
    {})` (Kosovo) raises `InvalidLocale` -- "XK" is not a registered ISO
    region as far as the `language-tags` package is concerned. Passed
    straight through to `AsyncCamoufox(**opts)`, that raised before the
    browser ever started, so the queue's retry loop repeated the same
    guaranteed failure on every attempt and burned the proxy session for
    nothing recoverable. Checked here, once, so a bad pin degrades to "no
    pin" (Camoufox's own draw applies) instead of an unrecoverable launch
    failure -- the same fail-open posture already used when `resolve_profile`
    itself returns nothing for an unknown country.
    """
    try:
        handle_locales(locale, {})
    except InvalidLocale:
        return False
    return True


def build_camoufox_options(
    *,
    proxy: dict | None,
    block_assets: bool,
    webrtc_block: bool,
    headless: bool | None = None,
    humanize: bool = False,
    spoof_os: str | None = None,
    block_webgl: bool = False,
    addons: list | None = None,
    viewport: dict[str, int] | None = None,
    fingerprint: ResolvedFingerprint | None = None,
    proxy_geo: dict[str, str] | None = None,
    geoip: bool | str | None = None,
) -> dict:
    """Build the keyword-argument dict passed to AsyncCamoufox().

    Only non-default / non-None values that Camoufox actually accepts are
    included. ``geoip`` follows CAMOUFOX_GEOIP (on by default) so the browser
    timezone/WebRTC-IP align with the exit IP when a proxy is provided; see
    that setting for what the alignment costs.

    ``locale`` is pinned separately from ``geoip``, using the same
    ``geo_profile.resolve_profile`` table the Chromium path reads (see
    ``PlaywrightRunner._new_context`` in runner.py). Left to itself, Camoufox
    resolves the exit region to a language via its own selector, which can
    pick a minority language for the region: measured across the twenty-preset
    audit, 11 of 45 Camoufox runs announced an implausible language for a
    correct country -- en-DE, bar-DE (Bavarian), fr-DE, fr-GB, de-GB, pl-GB,
    es-US -- against 0 of 46 for Chromium, which already pins its locale from
    this same table. (it-DE was drawn the same way in a separate live
    observation; `grep -c it-DE` over the audit record is 0, so it is not one
    of those 11 and the total stands without it.) Two of those draws
    corrupted live data:
    en-DE made Amazon serve an English page to a preset whose price parser
    expected European formatting (a 100x parsing error), and es-US made
    Walmart serve a Spanish page to a US-locale preset. When the country is
    unknown (or no geo was given), ``locale`` is left out entirely so
    Camoufox's own draw still applies rather than asserting a guess as fact.
    ``geoip`` keeps supplying the timezone and the WebRTC value either way —
    only the language moves out of Camoufox's hands.

    ``fingerprint`` is a resolved profile (see ``fingerprint_profile.py``). Its
    OS wins over ``spoof_os``, which the request layer already refuses to let
    disagree with it. Each key is emitted only when the profile actually pinned
    something: a ``webgl_config`` of None is not the same as not passing one, and
    the ``random`` profile has to reproduce the pre-profile option dict exactly.

    ``viewport`` is forwarded as Camoufox's ``window=(w, h)`` outer size, and
    the screen floor is stated ALONGSIDE it. Camoufox does not derive one from
    the window: measured over eight launches with `window=(1920, 1080)` and the
    screen left alone, it randomised screens of 1680x1050 and 960x540 — a window
    larger than its own monitor, which cannot happen on real hardware and is
    therefore a tell. With both stated, `inner <= outer == window <= screen`
    held 8/8.

    The rendered viewport follows the window: ``CamoufoxRunner.fetch`` opens the
    page with ``no_viewport=True``, because Playwright's own 1280x720 viewport
    otherwise overrides it — measured inner 1280x720 inside an outer 1920x1080,
    i.e. 640px of horizontal browser chrome, which no browser has (Firefox's
    chrome is vertical only). With ``no_viewport`` it is inner 1920x1029 under
    outer 1920x1080, 5 of 5. ``new_page(viewport=...)`` is NOT the fix: it
    produced an outer window SMALLER than the inner viewport in 3 of 5 launches,
    trading one impossible geometry for another.

    ``no_viewport`` is unconditional, including outside the serveable range where
    no window is forced. Measured there (viewport 3840x2160, so no window and no
    screen floor), 5 launches each: without it, horizontal chrome of 160-416px in
    5 of 5; with it, 0 of 5. What it does NOT fix out of range is Camoufox's own
    window-versus-screen draw — outer exceeded screen in 4 of 5 without and 5 of
    5 with, because ``no_viewport`` only makes the content follow the window and
    touches neither the window nor the screen. Out of range the requested size is
    not honoured either way; the difference is whether the browser also contradicts
    itself about its own chrome.
    """
    opts: dict = {
        "headless": settings.headless if headless is None else headless,
        # A PARAMETER now, not a direct settings read: when the transport
        # guard occupies the proxy slot, `True` would make camoufox resolve
        # the exit IP through our per-fetch listener. See `direct_public_ip`.
        "geoip": settings.camoufox_geoip if geoip is None else geoip,
        "block_images": bool(block_assets),
        # NOT block_webrtc: that deletes RTCPeerConnection, and a Firefox
        # without the constructor is itself the anomaly we are hiding from —
        # measured `typeof RTCPeerConnection === "undefined"` in production.
        #
        # Nothing replaces it, deliberately. Camoufox's own camoufox.cfg already
        # ships `ice.proxy_only_if_behind_proxy` and `default_address_only`, and
        # its comment calls the former "the pref that actually prevents the
        # real-IP leak". Measured against a local STUN listener: with a proxy the
        # stock build emits 0 candidates and 0 UDP, so an added `ice.proxy_only`
        # buys nothing; WITHOUT a proxy the stock build produces the real-Firefox
        # shape (two mDNS-obfuscated host candidates, leaking nothing) and that
        # pref collapses it to zero — a peer connection that exists but gathers
        # nothing is a stronger tell than a missing constructor, because it
        # survives a typeof check and only shows up when someone actually
        # gathers. The no-proxy path is real here (a lapsed prem subscription
        # sends scans out direct).
        #
        # Setting this False is also what lets `geoip` spoof the WebRTC exit IP:
        # camoufox's `webrtc:ipv4` branch runs only when block_webrtc is off.
        "block_webrtc": False,
        "humanize": bool(humanize),
        "block_webgl": bool(block_webgl),
    }
    if proxy is not None:
        opts["proxy"] = proxy
    if proxy_geo:
        country_code = proxy_geo.get("country_code")
        profile = resolve_profile(country_code, proxy_geo.get("city"))
        if profile is None:
            # No table entry for this country: stay silent-by-default like
            # before, but a warning names it, because this is the same shape
            # as the bug this function exists to fix -- a caller-visible
            # locale draw nobody asked for -- just triggered by a gap in the
            # table (e.g. "UK" instead of the ISO "GB") instead of by
            # skipping the pin outright. The audit that found this defect
            # needed 45 runs of `applied_locale` to notice it; this log line
            # is what would have shortened that.
            log.warning(
                "camoufox: no geo profile for country_code=%r; leaving "
                "locale to Camoufox's own draw", country_code,
            )
        elif _camoufox_accepts_locale(profile.locale):
            opts["locale"] = profile.locale
            log.info(
                "camoufox: pinned locale=%s for country_code=%r (geoip "
                "still supplies timezone/WebRTC)", profile.locale, country_code,
            )
            # Residue camoufox itself leaves behind, not something to work
            # around here: handle_locales() only overwrites config's
            # 'locale:script' key when the PINNED language has an IANA
            # Suppress-Script (most do, e.g. de/fr -> Latn, ru -> Cyrl). For
            # 13 of 250 _COUNTRY_MAP entries it doesn't (measured via
            # camoufox.locale.handle_locale: AZ, ME, RS, CN, HK, KG, MN, MO,
            # TJ, TM, TW, UZ, VU), so whatever script geoip's OWN random
            # region->language draw picked survives underneath our pin --
            # navigator.language matches this locale, but
            # Intl.resolvedOptions().locale can carry a leftover script,
            # a navigator/Intl disagreement that `applied_locale` (a bare
            # navigator.language readback) cannot see. Not fixable from this
            # side without reaching into camoufox's own config dict after
            # the fact; out of scope for this task.
        else:
            log.warning(
                "camoufox: geo_profile locale %r for country_code=%r is not "
                "a locale Camoufox's own validator accepts; leaving the pin "
                "to Camoufox's own draw", profile.locale, country_code,
            )
    if fingerprint is not None and fingerprint.spoof_os is not None:
        opts["os"] = fingerprint.spoof_os
    elif spoof_os is not None:
        opts["os"] = spoof_os
    if fingerprint is not None and fingerprint.webgl_config is not None:
        opts["webgl_config"] = fingerprint.webgl_config
    if addons is not None:
        opts["addons"] = addons
    # Camoufox calls `add_default_addons()` unconditionally inside
    # `launch_options()`, so uBlock Origin ships on every launch whether or not
    # `addons` is passed; `exclude_addons` is the only way out. See
    # CAMOUFOX_DISABLE_UBO in settings for what uBO does to the pages we scrape
    # and why the default is nonetheless to keep it.
    if settings.camoufox_disable_ubo:
        opts["exclude_addons"] = [DefaultAddons.UBO]
    if viewport is not None and _window_is_serveable(viewport):
        opts["window"] = (viewport["width"], viewport["height"])
        # Stated, not assumed: see the docstring. Without this floor the
        # randomised screen can land below the window we just forced.
        opts["screen"] = Screen(
            min_width=viewport["width"], min_height=viewport["height"]
        )
    elif viewport is not None:
        log.info(
            "camoufox: viewport %dx%d is outside the range a coherent window can "
            "be forced in (%d..%dx%d); leaving the window to Camoufox, so the "
            "requested size is not honoured but the window and the rendered "
            "content still agree",
            viewport["width"], viewport["height"],
            _MIN_SERVEABLE_WINDOW_WIDTH, *_MAX_SERVEABLE_SCREEN_FLOOR,
        )
    # Camoufox defaults this to the whole of os.environ and merges its own
    # CAMOU_CONFIG_* chunks UNDER what we pass, so handing it a narrowed copy
    # keeps the fingerprint config intact and leaves the service token, the
    # LLM keys and the proxy passwords out of the Firefox process.
    opts["env"] = browser_env()
    return opts


class CamoufoxRunner:
    """Per-request Camoufox browser runner.

    Unlike PlaywrightRunner (which keeps a warm browser across requests),
    CamoufoxRunner launches a fresh browser for every fetch and tears it down
    immediately after. This means is_started() is always False; start() and
    stop() are no-ops so the worker can treat runners uniformly.
    """

    def __init__(self, timeout_ms: int, headless: bool | None = None) -> None:
        self._timeout_ms = timeout_ms
        # Per-request launch mode; None = the server's HEADLESS default.
        self._headless = settings.headless if headless is None else headless

    def is_started(self) -> bool:
        """Always False — Camoufox launches per request, nothing stays warm."""
        return False

    async def start(self) -> None:
        """No-op: Camoufox has nothing to pre-warm."""
        return None

    async def stop(self) -> None:
        """No-op: the browser is already torn down after each fetch."""
        return None

    async def resolve_proxy(
        self, proxy: Optional[ProxyConfig]
    ) -> tuple[Optional[ProxyConfig], None]:
        """Camoufox accepts plain HTTP/SOCKS proxies natively; no bridge needed."""
        return proxy, None

    async def fetch(
        self,
        url: str,
        device: str,
        proxy: Optional[ProxyConfig],
        headers: dict[str, str] | None,
        wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"],
        wait_for_selector: str | None,
        timeout_ms: int | None,
        screenshot: bool,
        element_selector: str | None = None,
        stealth: bool = True,
        block_assets: bool | None = None,
        proxy_geo: dict[str, str] | None = None,
        render: bool = True,
        cookies: list[dict[str, Any]] | None = None,
        storage_state: dict | None = None,
        viewport: dict[str, int] | None = None,
        # Camoufox premium options
        humanize: bool = False,
        spoof_os: str | None = None,
        fingerprint_profile: str | None = None,
        block_webgl: bool = False,
        addons: list | None = None,
        warmup: dict | None = None,
    ) -> FetchResult:
        """Fetch a URL using a fresh Camoufox instance.

        screenshot / element_selector are honoured: a Camoufox page is a
        Playwright (Firefox) page, so the shared `_capture_screenshot` helper
        applies (full-page or element crop). meta.applied_* is read back from
        the page so the locale/timezone Camoufox aligned via geoip are surfaced.

        headers are applied via page.set_extra_http_headers(), except
        User-Agent, which Camoufox owns as part of its fingerprint. An
        explicit Accept-Language overrides the geoip-derived one on the
        wire (parity with the Playwright path, where the user's value
        wins), but navigator.languages keeps the fingerprint value — a
        detectable mismatch, so override it only deliberately.

        Rejected upstream rather than ignored here (ScrapeRequest validator):
          - cookies, storage_state (via session_id), render=false. The runner
            would drop them silently and return a 200 with content the caller
            did not ask for; a session-pinned scrape came back logged OUT.

        Accepted-and-ignored (harmless, Camoufox owns the equivalent):
          - stealth / device: Camoufox manages its own fingerprint internally.

        ``proxy_geo`` pins the announced locale via the same geo_profile
        table the Chromium path uses (see build_camoufox_options); timezone
        and the WebRTC IP still come from Camoufox's own ``geoip`` alignment,
        not from ``proxy_geo``.
        """
        # Identity headers are fingerprint-owned and filtered out; an explicit
        # Accept-Language wins on the wire and is echoed via applied_* (see
        # docstring). Computed before the pre-flight below so its refusal --
        # and every other early return -- can report it: a caller who set a
        # header needs the answer most on the paths that failed.
        safe_headers, ignored_request_fields = strip_engine_owned_headers(headers)
        if ignored_request_fields:
            log.warning(
                "camoufox: dropping %s for %s (the fingerprint manages them)",
                ", ".join(ignored_request_fields), url,
            )

        # Before any launch: Camoufox costs a fresh browser per request, and a
        # target we will refuse should not pay for one.
        try:
            await assert_navigable(url, resolve=proxy is None)
        except EgressBlocked:
            return FetchResult(
                html="",
                final_url=None,
                status_code=None,
                screenshot_b64=None,
                ok=False,
                error=EGRESS_BLOCKED_ERROR,
                element_status="no_screenshot",
                ignored_request_fields=list(ignored_request_fields),
            )

        proxy_dict: dict | None = None
        if proxy is not None:
            proxy_dict = {"server": proxy.server}
            if proxy.username:
                proxy_dict["username"] = proxy.username
            if proxy.password:
                proxy_dict["password"] = proxy.password

        effective_block_assets = (
            settings.block_assets if block_assets is None else block_assets
        )
        effective_timeout_ms = timeout_ms or self._timeout_ms

        # Camoufox is desktop-only (mobile is rejected upstream), so default to
        # the shared desktop viewport when unset. What the viewport does to the
        # geometry — and what it does NOT do — is in build_camoufox_options.
        # Resolved here rather than in the queue layer so every caller of the
        # runner — probes and tests included — defaults the same way the queue
        # does. `spoof_os` feeds the same resolution because a bare OS name is
        # defined to mean exactly what that field already meant, which keeps the
        # two from drifting into separate code paths.
        fingerprint = resolve_fingerprint(fingerprint_profile or spoof_os)

        # The transport guard is deliberately NOT wired into this engine.
        #
        # Camoufox resolves the exit IP THROUGH whatever proxy it is handed
        # (`camoufox/utils.py`: `public_ip(Proxy(**proxy).as_string())`) and
        # caches that with `@lru_cache(maxsize=None)` keyed on the proxy
        # string. The guard's URL carries a fresh port per fetch, so every
        # Camoufox fetch would spend an external round trip to ipify and leak
        # one unbounded cache entry. Pre-resolving the IP ourselves removes
        # the per-fetch key but adds a hard network dependency BEFORE the
        # browser launches, turning an ipify outage into a failed scrape — and
        # this engine's fingerprint has burned an exit pool before, so a
        # change of this size does not ride along with a security fix.
        #
        # Camoufox keeps the layers that need no proxy slot: the context route
        # guard, the redirect-chain check and the read-time re-check. It
        # therefore still fires ONE internal GET on a redirect into the
        # network, where Chromium now fires none.
        opts = build_camoufox_options(
            proxy=proxy_dict,
            block_assets=effective_block_assets,
            webrtc_block=settings.webrtc_block,
            headless=self._headless,
            humanize=humanize,
            fingerprint=fingerprint,
            block_webgl=block_webgl,
            addons=addons,
            viewport=viewport or DEFAULT_DESKTOP_VIEWPORT,
            proxy_geo=proxy_geo,
        )

        al_override = next(
            (v for k, v in safe_headers.items() if k.lower() == "accept-language"),
            None,
        )

        applied_warmup: dict | None = None
        warmup_error: str | None = None

        try:
            async with AsyncCamoufox(**opts) as browser:
                # camoufox annotates __aenter__ as `Browser | BrowserContext`;
                # the BrowserContext half is its persistent_context mode, which
                # build_camoufox_options never asks for, so `new_page` here is
                # Browser's — the one that takes context options. Stated as a
                # cast, not an isinstance guard: the guard version rejected the
                # AsyncMock doubles in 19 tests, i.e. it invented a runtime
                # failure mode to satisfy a type checker.
                browser = cast(Browser, browser)
                # Unconditional: without it Playwright applies its own 1280x720
                # viewport, so the window says one size while the page renders
                # another — horizontal browser chrome, which no browser has.
                # That holds outside the serveable range too, where no window is
                # forced: measured there, 160-416px of horizontal chrome in 5 of
                # 5 without, 0 of 5 with. See build_camoufox_options.
                page = await browser.new_page(no_viewport=True)
                # Registered on the CONTEXT, not the page: a popup or
                # `target=_blank` window the target opens is a different page
                # in the same context and would otherwise get no guard at all.
                await page.context.route("**/*", make_route_guard(resolve=proxy is None))
                if safe_headers:
                    await page.set_extra_http_headers(safe_headers)
                warmup_outcome = await run_warmup(
                    page, url, warmup,
                    timeout_ms=effective_timeout_ms,
                    default_dwell_ms=settings.warmup_dwell_ms,
                    resolve=proxy is None,
                )
                applied_warmup = warmup_outcome.applied
                warmup_error = warmup_outcome.error
                resp = await page.goto(
                    url, wait_until=wait_until, timeout=effective_timeout_ms
                )
                # Route handlers are not invoked for redirect hops, so the
                # chain is checked after the fact. See PlaywrightRunner.fetch.
                await assert_landing_public(resp, page.url, resolve=proxy is None)
                selector_missing = False
                # A selector a block page will never grow is not worth its
                # deadline: measured on yandex_search, five blocked attempts of
                # six already had page.url == /showcaptcha the moment goto
                # returned, then spent the full 45s discovering it. This only
                # declines to WAIT — the classifier below still decides.
                if wait_for_selector and not redirected_to_block(url, page.url):
                    try:
                        await page.wait_for_selector(
                            wait_for_selector, timeout=effective_timeout_ms
                        )
                    except PWTimeoutError as exc:
                        # See PlaywrightRunner.fetch: the selector is usually
                        # missing because an interstitial replaced the content,
                        # so keep the page and let the classifier name it.
                        # Only the deadline — a malformed selector raises a
                        # plain PWError and must stay a hard error.
                        selector_missing = True
                        log.warning(
                            "selector %r did not appear before the deadline "
                            "for %s: %s", wait_for_selector, url, exc,
                        )
                html = await read_content_settling_navigation(page)
                final_url = page.url
                # See PlaywrightRunner.fetch: after the read, because the read
                # itself waits out an in-flight navigation and returns the
                # content of wherever the page ended up.
                await assert_page_public(page, resolve=proxy is None)
                status_code = resp.status if resp is not None else None
                captcha_detected = looks_like_captcha_or_block(html, final_url=final_url)
                fetch_ok, fetch_blocked, fetch_error = classify_fetch(
                    status_code, captcha_detected=captcha_detected
                )
                if selector_missing:
                    fetch_ok = False
                    fetch_error = fetch_error or f"{SELECTOR_MISS_PREFIX}{wait_for_selector}"

                # Screenshot: a Camoufox page is a Playwright (Firefox) page, so
                # reuse the runner's capture helper. It never raises; the extra
                # guard only covers a non-Playwright mock or driver hiccup so a
                # capture failure can't turn a good fetch into an error.
                screenshot_b64 = None
                # parity with PlaywrightRunner on capture failure
                element_status: ElementScreenshotStatus = "no_screenshot"
                try:
                    png, element_status = await _capture_screenshot(
                        page,
                        screenshot=screenshot,
                        element_selector=element_selector,
                        effective_block_assets=effective_block_assets,
                    )
                    if png is not None:
                        screenshot_b64 = base64.b64encode(png).decode("ascii")
                except Exception as exc:  # pylint: disable=broad-except
                    log.warning("Camoufox screenshot capture failed: %s", exc)

                # OUTSIDE the try above, deliberately: that arm catches
                # `Exception` and would swallow an `EgressBlocked`, turning a
                # refusal into a silently-returned screenshot. `_fullpage_
                # screenshot` scrolls up to 200 times at 120 ms, so the capture
                # is a ~24 s window in which the page can navigate — the same
                # window PlaywrightRunner already re-checks after its capture.
                if screenshot_b64 is not None:
                    await assert_page_public(page, resolve=proxy is None)

                # Read back the fingerprint Camoufox actually applied. geoip=True
                # aligns locale/timezone to the exit IP internally, but nothing
                # surfaced it before, so meta.applied_* came back null. Best
                # effort: a restricted/blocked page must not fail the fetch.
                applied: dict = {}
                try:
                    applied = await page.evaluate(
                        "() => ({"
                        "ua: navigator.userAgent,"
                        "locale: navigator.language,"
                        "tz: Intl.DateTimeFormat().resolvedOptions().timeZone,"
                        "al: (navigator.languages || []).join(',')"
                        "})"
                    ) or {}
                except Exception as exc:  # pylint: disable=broad-except
                    log.debug("Camoufox applied-meta readback failed: %s", exc)

                return FetchResult(
                    html=html,
                    final_url=final_url,
                    status_code=status_code,
                    screenshot_b64=screenshot_b64,
                    ok=fetch_ok,
                    error=fetch_error,
                    blocked=fetch_blocked,
                    element_status=element_status,
                    applied_user_agent=applied.get("ua"),
                    applied_locale=applied.get("locale"),
                    applied_timezone=applied.get("tz"),
                    applied_accept_language=al_override or applied.get("al"),
                    applied_warmup=applied_warmup,
                    warmup_error=warmup_error,
                    applied_fingerprint=fingerprint.as_meta(),
                    ignored_request_fields=list(ignored_request_fields),
                )
        # Before the broad arm: it stringifies the exception into `error`,
        # which would put the refused host into a caller-visible message.
        except EgressBlocked:
            return FetchResult(
                html="",
                final_url=None,
                status_code=None,
                screenshot_b64=None,
                ok=False,
                error=EGRESS_BLOCKED_ERROR,
                element_status="no_screenshot",
                applied_warmup=applied_warmup,
                warmup_error=warmup_error,
                ignored_request_fields=list(ignored_request_fields),
            )
        except Exception as exc:  # pylint: disable=broad-except
            error_type = type(exc).__name__
            log.warning("CamoufoxRunner.fetch failed for %s: %s: %s", url, error_type, exc)
            return FetchResult(
                html="",
                final_url=None,
                status_code=None,
                screenshot_b64=None,
                ok=False,
                error=f"{error_type}: {exc}",
                element_status="no_screenshot",
                applied_warmup=applied_warmup,
                warmup_error=warmup_error,
                # Reported on failure too: a refused launch is exactly when the
                # profile is the prime suspect, and the error string names
                # browserforge internals rather than the profile that chose them.
                applied_fingerprint=fingerprint.as_meta(),
                ignored_request_fields=list(ignored_request_fields),
            )
