from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import re
import time
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Optional, Literal

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Error as PWError,
    TimeoutError as PWTimeoutError,
)
from playwright_stealth import Stealth

from src.browser.launch_env import browser_env
from src.browser.page_io import read_content_settling_navigation
from src.browser.teardown import TEARDOWN_TIMEOUT_S as _TEARDOWN_TIMEOUT_S, close_quietly
from src.proxy.models import ProxyConfig
from src.proxy.socks_bridge import open_socks_to_http_bridge
from src.security.egress_guard import open_egress_guard
from src.browser.geo_profile import resolve_profile
from src.browser.fingerprint_profile import chromium_webgl_identity
from src.schemas import ElementScreenshotStatus
from src.security.egress import (
    EGRESS_BLOCKED_ERROR,
    EgressBlocked,
    assert_landing_public,
    assert_navigable,
    assert_page_public,
    make_route_guard,
)
from src.settings import settings


log = logging.getLogger(__name__)
# playwright_stealth's default WebGL spoof reports a macOS renderer ("Intel Iris
# OpenGL Engine"), which contradicts our Windows UA/navigator.platform and is a
# cross-check that advanced anti-bots (CreepJS/Cloudflare) flag. Override it with
# a real Windows Chrome ANGLE/Direct3D renderer so the GPU identity matches the
# rest of the fingerprint. Desktop preset is Windows; the value suits it.
#
# Which GPU it names follows the host rather than being fixed here, so this
# machine's AMD silicon stops being reported as an Intel card while Camoufox on
# the same box reports AMD.
def _build_stealth(webgl_vendor: str, webgl_renderer: str) -> Stealth:
    return Stealth(
        webgl_vendor_override=webgl_vendor,
        webgl_renderer_override=webgl_renderer,
        # These evasions patch navigator getters in JS, whose `.toString()`
        # returns non-native source — an integrity tell that anti-bots (e.g.
        # ServicePipe) probe directly. Disable them and supply the same values
        # natively instead:
        #   - user_agent / platform: set via CDP Emulation.setUserAgentOverride
        #     below, which the engine applies at the protocol level (native
        #     getter preserved).
        #   - vendor / plugins / mimeTypes: headless Chromium already reports the
        #     real Chrome values ("Google Inc." + the PDF viewer plugins).
        #   - languages: Playwright's context `locale` sets navigator.languages
        #     natively.
        navigator_user_agent=False,
        navigator_platform=False,
        navigator_vendor=False,
        navigator_plugins=False,
        navigator_languages=False,
        # Clear the matching *_override values too: with the evasions off they
        # are unused, and leaving them set makes playwright_stealth warn on every
        # start ("override provided but evasion disabled") — misleading noise.
        navigator_platform_override=None,
        navigator_languages_override=None,
    )


@lru_cache(maxsize=1)
def stealth_config() -> Stealth:
    """The Stealth object every Chromium page is patched with.

    Built on first use rather than at import so the GPU claim reaches the log:
    `setup_logging` runs from inside `create_app` / the worker's startup, long
    after this module is imported, so a line emitted at import time is dropped
    on the floor. Cached because `host_facts()` cannot change under a running
    process, and because rebuilding it per page would re-read /proc.
    """
    vendor, renderer = chromium_webgl_identity()
    log.info("chromium webgl claim: vendor=%s renderer=%s", vendor, renderer)
    return _build_stealth(vendor, renderer)


# WebRTC leak-protection init script, kept as a standalone .js asset so it reads
# and edits as JavaScript (syntax highlighting, no Python-string escaping of the
# regex/backslashes). Loaded once at import; see the file for the full rationale.
_WEBRTC_STEALTH_JS = (Path(__file__).resolve().parent / "webrtc_stealth.js").read_text(
    encoding="utf-8"
)


# Desktop default viewport (and matching window.screen). 1920x1080 @ DPR 1 is
# the single most common real desktop resolution, so it blends in. Shared with
# the Camoufox path so both engines default to the same size.
DEFAULT_DESKTOP_VIEWPORT = {"width": 1920, "height": 1080}

DESKTOP = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "viewport": DEFAULT_DESKTOP_VIEWPORT,
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "color_scheme": "light",
}
MOBILE = {
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "viewport": {"width": 390, "height": 844},
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "color_scheme": "light",
    "is_mobile": True,
    "has_touch": True,
}
# A 2004-era feature phone. It exists for one measured reason: Google serves a
# JavaScript-free WML/XHTML-Mobile layout at `/wml/search` to a short WHITELIST
# of legacy User-Agents, and that layout ships real `/url?q=` links instead of
# the opaque `/goto?url=CAES…` stubs the JS SERP now returns. Measured
# 2026-09-11, same endpoint and query, only the UA varying: the six strings
# below return 11-12 result blocks; `NokiaN70 (SymbianOS/8.1)` and
# `NokiaN97 (SymbianOS/9.4)`, which are NOT on the list, return an "Update your
# browser" page with zero results, as does a current Chrome UA.
#
# Stated plainly, because the per-engine UA rule is otherwise absolute: NO
# engine we ship can truly back this claim. Our TLS is Chromium's or Gecko's,
# not a Symbian phone's. It is offered anyway because the endpoint keys on the
# UA and not on the handshake (measured: plain `curl`, with no browser TLS at
# all, gets the same page), and because the alternative -- a caller smuggling
# the UA through `headers` -- was worse: it worked only on the engines that
# also had no Client Hints to contradict it, and `meta.applied_user_agent`
# reported a string that was never sent.
#
# What makes it coherent rather than merely spoofed is that Client Hints are
# SUPPRESSED for it (see `apply_page_masking`): a phone of this era sends no
# `Sec-CH-UA` at all. Measured on the wire: without that suppression the
# request goes out as `Nokia7610…` carrying `Sec-CH-UA: "HeadlessChrome";v="143"`
# and `Sec-CH-UA-Platform: "Windows"` -- two headers contradicting the UA and
# each other, which is a louder tell than the honest Chrome UA it replaced.
LEGACY_WAP = {
    # One of the six, fixed rather than rotated: the whitelist is what matters,
    # and a stable string keeps a reproduction reproducible.
    "user_agent": "Nokia7610/2.0 (5.0509.0) SymbianOS/7.0s Series60/2.1 "
                  "Profile/MIDP-2.0 Configuration/CLDC-1.0",
    "viewport": {"width": 176, "height": 208},
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "color_scheme": "light",
    # Deliberately NOT `is_mobile`/`has_touch`. Playwright Firefox has no mobile
    # emulation and refuses the context outright, and Firefox is one of the
    # engines this device is measured working on. What `is_mobile` would buy
    # here is viewport-meta handling and `Sec-CH-UA-Mobile` -- the first is
    # irrelevant to a layout we fetch with `render: false`, and the second is
    # suppressed anyway.
    # Read by `apply_page_masking`; the UA alone cannot say it, because a
    # non-Chrome UA is also how an iPhone preset looks and that one keeps its
    # Client Hints skipped-but-present behaviour.
    "suppress_client_hints": True,
}


_DEVICE_PRESETS = {"desktop": DESKTOP, "mobile": MOBILE, "legacy_wap": LEGACY_WAP}


def _chrome_ua_metadata(user_agent: str) -> dict | None:
    """Build coherent User-Agent Client Hints from a Chrome UA string.

    Playwright's UA override changes the UA *string* but leaves the browser's
    real ``userAgentMetadata`` — so on headless Chromium the ``Sec-CH-UA``
    header leaks ``"HeadlessChrome"`` and the real bundle version (verified on
    the wire), contradicting the spoofed UA. Feeding this metadata to
    ``Emulation.setUserAgentOverride`` aligns the brands/version with the UA and
    drops the ``HeadlessChrome`` tell. Returns None for non-Chrome UAs (e.g. the
    mobile Safari preset), which do not send Sec-CH-UA at all.
    """
    match = re.search(r"Chrome/(\d+)", user_agent)
    if not match:
        return None
    major = match.group(1)
    if "Windows" in user_agent:
        platform = "Windows"
    elif "Macintosh" in user_agent or "Mac OS X" in user_agent:
        platform = "macOS"
    elif "Android" in user_agent:
        platform = "Android"
    else:
        platform = "Linux"
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not.A/Brand", "version": "24"},
    ]
    return {
        "brands": brands,
        "fullVersionList": [
            {"brand": b["brand"], "version": f"{b['version']}.0.0.0"} for b in brands
        ],
        "platform": platform,
        "platformVersion": "",
        "architecture": "x86",
        "bitness": "64",
        "model": "",
        "mobile": "Mobile" in user_agent or "Android" in user_agent,
    }


def _navigator_platform_for_ua(user_agent: str) -> str:
    """The `navigator.platform` string matching a UA's OS.

    Set natively via CDP (Emulation.setUserAgentOverride) instead of a stealth JS
    patch, so `navigator.platform`'s getter stays `[native code]`.
    """
    # Order matters: an iPhone UA carries "like Mac OS X", so the mobile checks
    # must run before the desktop-Mac check or they'd be misread as MacIntel.
    if "Windows" in user_agent:
        return "Win32"
    if "iPhone" in user_agent:
        return "iPhone"
    if "Android" in user_agent:
        return "Linux armv8l"
    if "Macintosh" in user_agent or "Mac OS X" in user_agent:
        return "MacIntel"
    return "Linux x86_64"


async def apply_page_masking(
    context: BrowserContext, page: Any, *, engine: str, stealth: bool
) -> None:
    """Everything a freshly opened page needs before a target sees it.

    Extracted so there is ONE of it. The login-replay path had its own copy that
    was a bare `Stealth()` — playwright-stealth's macOS defaults under our
    Windows UA, the navigator JS patches this repo deliberately disables because
    their toString leaks, and no Client-Hints alignment at all. That was the
    credential submit: the request where being flagged costs an account rather
    than a retry, masked worse than every ordinary scrape around it.
    """
    if stealth and engine == "chromium":
        await stealth_config().apply_stealth_async(page)
    elif stealth:
        log.debug("stealth requested but skipped: not supported for engine=%r", engine)

    # Align Client Hints with the spoofed UA: Playwright's UA override leaves
    # the real headless metadata, so Sec-CH-UA otherwise leaks "HeadlessChrome"
    # + the real version. Chromium only; per-page CDP. Non-fatal: any failure
    # (incl. building the metadata) is swallowed.
    if engine != "chromium":
        return
    try:
        ua = getattr(context, "_applied_user_agent", None)
        # `is True`, not truthiness: a MagicMock context auto-creates any
        # attribute asked of it and the result is truthy, so a loose check
        # turned this branch on for every mock-based test in the suite -- and
        # would turn it on for any future double just as silently.
        if getattr(context, "_suppress_client_hints", False) is True and isinstance(ua, str):
            # A device that predates Client Hints must send NONE, and "none" is
            # not the same as "the ones we failed to align". Measured on the
            # wire with a Nokia UA: skipping the override entirely leaves
            # `Sec-CH-UA: "HeadlessChrome";v="143"` + `Sec-CH-UA-Platform:
            # "Windows"`, while sending the override WITHOUT `userAgentMetadata`
            # removes both headers and empties `navigator.userAgentData.brands`.
            # Sending empty brands instead still emits a bare
            # `Sec-CH-UA-Platform: ""`, which no real client does.
            cdp = await context.new_cdp_session(page)
            await cdp.send("Emulation.setUserAgentOverride", {
                "userAgent": ua, "platform": "",
            })
            return
        ua_metadata = _chrome_ua_metadata(ua) if isinstance(ua, str) else None
        if ua_metadata is None:
            # The one no-op that actually happens in production, and it used to
            # be the only silent one: the mobile preset is an iPhone Safari UA,
            # `_chrome_ua_metadata` returns None for it, and the whole override
            # below is skipped. Measured on the wire for that pin: Sec-CH-UA
            # still says "HeadlessChrome" (real iOS Safari sends none at all)
            # and navigator.platform stays Linux x86_64 under an iPhone UA.
            # Pre-existing on both paths and not fixed here — but no longer
            # invisible, because the except clause below argues that exact case
            # is worth surfacing.
            log.warning(
                "Client-Hints override skipped: no Chrome metadata for ua=%r "
                "(Sec-CH-UA may leak HeadlessChrome and navigator.platform will "
                "not match)", ua,
            )
        else:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Emulation.setUserAgentOverride", {
                "userAgent": ua,
                # Native navigator.platform (replaces the disabled stealth
                # navigator_platform JS patch that leaked its source via
                # toString).
                "platform": _navigator_platform_for_ua(ua),
                "userAgentMetadata": ua_metadata,
            })
    except Exception as exc:  # pylint: disable=broad-except
        # Anti-bot-relevant: a silent no-op re-leaks "HeadlessChrome" in
        # Sec-CH-UA and gets scrapes blocked, so surface it.
        log.warning("Client-Hints override failed (Sec-CH-UA may leak HeadlessChrome): %s", exc)


_CHROME_UA_VERSION_RE = re.compile(r"Chrome/\d+(?:\.\d+)*")


# Headers that state WHAT THE BROWSER IS. The engine owns them, because only
# the engine can back them: the UA is cross-checked against the TLS handshake,
# and the Client-Hints family against `navigator.userAgentData` and against the
# CDP metadata we set from the UA we actually stated.
#
# Dropping the UA alone is not enough, and briefly made things worse: four of
# the header presets `scraper-tester/public/app.js` shipped until this change
# sent `sec-ch-ua`, `sec-ch-ua-platform` and `sec-ch-ua-mobile` next to the UA
# as one set. Remove only the UA and firefox goes out with its honest
# `Firefox/144` beside `sec-ch-ua: "Google Chrome";v="124"` -- two headers now
# contradicting each other, where before they at least agreed. The whole set is
# engine-owned or none of it is.
#
# Canonical spellings, matched case-insensitively; the canonical name is what
# `ignored_request_fields` reports, so the warning text does not vary with how
# the caller happened to capitalise.
_ENGINE_OWNED_HEADERS: dict[str, str] = {
    "user-agent": "User-Agent",
    "sec-ch-ua": "Sec-CH-UA",
    "sec-ch-ua-arch": "Sec-CH-UA-Arch",
    "sec-ch-ua-bitness": "Sec-CH-UA-Bitness",
    "sec-ch-ua-full-version": "Sec-CH-UA-Full-Version",
    "sec-ch-ua-full-version-list": "Sec-CH-UA-Full-Version-List",
    "sec-ch-ua-mobile": "Sec-CH-UA-Mobile",
    "sec-ch-ua-model": "Sec-CH-UA-Model",
    "sec-ch-ua-platform": "Sec-CH-UA-Platform",
    "sec-ch-ua-platform-version": "Sec-CH-UA-Platform-Version",
    "sec-ch-ua-wow64": "Sec-CH-UA-Wow64",
}


def strip_engine_owned_headers(
    headers: dict[str, str] | None,
) -> tuple[dict[str, str], list[str]]:
    """Caller headers minus the ones only the engine can back, and their names.

    Both runners enforce this rule and they had drifted apart once already,
    which is what made it reachable through the public API on firefox/webkit --
    so it lives in one function rather than in a copy per runner. Each caller
    keeps its own log line: they legitimately differ in what they can name.

    Returns the NAMES, never the values. Echoing a User-Agent we did not send is
    the bug being closed, and the names are ours rather than the target's, so
    naming them hands nobody a lever into the substring classifier that reads
    `warnings` (see `egress_warnings`, which withholds hostnames for the
    opposite reason).
    """
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in (headers or {}).items():
        canonical = _ENGINE_OWNED_HEADERS.get(key.lower())
        if canonical is None:
            kept[key] = value
        else:
            dropped.append(f"headers[{canonical!r}]")
    return kept, sorted(set(dropped))


def _align_ua_to_engine(user_agent: str, browser_version: str) -> str:
    """Rewrite the UA's Chrome version to match the real engine build.

    The bundled Chromium (e.g. Playwright 1.57 ships Chrome-for-Testing 143)
    drifts from a hardcoded UA string; advertising an old major while exposing a
    newer engine's JS/CSS feature set is a bot tell. Chrome's own UA reduction
    pins everything but the major to zero, so we mirror that: ``Chrome/<major>.0.0.0``.
    Returns the UA unchanged if it carries no ``Chrome/`` token (non-Chrome UA)
    or the version can't be parsed.
    """
    if not isinstance(browser_version, str):
        return user_agent
    major = browser_version.split(".", 1)[0]
    if not major.isdigit() or not _CHROME_UA_VERSION_RE.search(user_agent):
        return user_agent
    return _CHROME_UA_VERSION_RE.sub(f"Chrome/{major}.0.0.0", user_agent)


# Element-screenshot timing/padding constants. Not in Settings — per-deploy
# variation has no realistic use case.
_ELEMENT_SCROLL_TIMEOUT_MS = 3000
_ELEMENT_NETWORKIDLE_TIMEOUT_MS = 2000
_ELEMENT_PADDING_PX = 24

# Resource types aborted when asset blocking is enabled.
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})


async def _block_assets_route(route) -> None:
    """Abort heavy asset requests, continue everything else.

    Must be awaited by Playwright (registered as a coroutine handler, not a
    fire-and-forget ``create_task``) so abort/continue settle before teardown
    and their errors are not swallowed.
    """
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


# Captcha / block interstitials are tiny (Google's /sorry page is ~3 KB); a
# rendered results or content page is far larger (a Google SERP is ~90 KB+).
# Above this ceiling we treat trigger phrases as page content, not a block.
_MAX_BLOCK_PAGE_BYTES = 20_000

# HTTP statuses that mean "this exit IP is burned" — the body (if any) is a
# block/error page, not target content, so the fetch is not ok and the queue
# should rotate to a fresh proxy. Mirrored by the queue's retry policy.
HTTP_BAN_STATUSES = frozenset({401, 403, 407, 429})
# Transient upstream/proxy failures: also not ok and worth a fresh exit, but
# the IP isn't necessarily burned (so not flagged as a hard "block").
HTTP_TRANSIENT_STATUSES = frozenset(
    {500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 413}
)


SELECTOR_MISS_PREFIX = "selector_not_found: "
"""Error prefix for "the page loaded but never grew the requested selector".

Shared with the queue, which rotates the exit on it: a missing SERP selector
usually means an interstitial replaced the results.
"""




def classify_fetch(
    status_code: int | None, *, captcha_detected: bool
) -> tuple[bool, bool, str | None]:
    """Decide (ok, blocked, error) from the HTTP status + captcha heuristic.

    A navigation that returns a ban/transient HTTP status still yields an HTML
    body, so Playwright reports no error — but the response is not genuine
    content. Marking it ``ok=False`` lets the queue's retry loop reach its
    proxy-rotation decision instead of short-circuiting on ``ok``. A genuine
    target status (e.g. 404) stays ``ok`` so we don't waste proxies retrying it.
    """
    if captcha_detected:
        return False, True, "Captcha/block detected by heuristic"
    if status_code in HTTP_BAN_STATUSES:
        return False, True, f"HTTP {status_code} (proxy ban / rate limit)"
    if status_code in HTTP_TRANSIENT_STATUSES:
        return False, False, f"HTTP {status_code} (transient upstream failure)"
    return True, False, None


def redirected_to_block(requested_url: str, current_url: str | None) -> bool:
    """Did this navigation END somewhere only a block serves?

    Answerable the instant `page.goto` returns, which is the point: measured on
    yandex_search, five blocked attempts out of six already had
    `page.url == https://yandex.ru/showcaptcha` at that moment, and then spent
    the full 45s selector deadline discovering what the URL had already said.
    The one good attempt spent 0.0s there.

    Deliberately narrower than `looks_like_captcha_or_block`, in two ways:

    - The URL alone. That function's other arm reads the body, which is not safe
      here — right after `goto` the DOM is still settling, and the size ceiling
      that keeps trigger phrases from firing on real content has not been
      reached yet.
    - Only when the URL CHANGED. `/sorry/` is an ordinary path (an apology page,
      a returns policy); a caller who navigated there on purpose has not been
      blocked, and cutting their selector wait short would hand back a page that
      had not finished rendering.

    This never changes a verdict — the full classification still runs on the
    complete html afterwards. It only declines to wait for a selector that a
    block page will never grow.
    """
    if not current_url or current_url == requested_url:
        return False
    path = urlsplit(current_url).path.lower().rstrip("/") or "/"
    # Matched on the PARSED PATH, exactly — not as a substring of the whole URL
    # the way `looks_like_captcha_or_block` does it. Two reasons, both real:
    #
    # `/showcaptchafast` CONTAINS `/showcaptcha` but is not a block. It is
    # Yandex's transparent browser check, which self-resolves in a few seconds
    # and redirects to the real SERP — see the fix that put
    # `wait_for_selector: li.serp-item` on yandex_search precisely so the wait
    # outlives it. Skipping that wait on a substring match turns an 18-result
    # SERP into a hard `blocked` and burns three exits chasing it. Reproduced
    # against a real browser before this was narrowed.
    #
    # And the query string is excluded entirely, because Google's own block URL
    # embeds the page it blocked: /sorry/index?continue=<original>. A substring
    # test over the full URL would fire on whatever that original happened to
    # contain.
    return path == "/showcaptcha" or path == "/sorry" or path.startswith("/sorry/")


def looks_like_captcha_or_block(html: str, *, final_url: str | None = None) -> bool:
    """Heuristic for detecting captcha / block pages.

    Real captcha / challenge interstitials are *tiny* compared to a rendered
    content page, so size is the primary discriminator: above
    ``_MAX_BLOCK_PAGE_BYTES`` we treat the page as genuine content and ignore
    trigger phrases in it. Otherwise a real results page that merely *mentions*
    a phrase — e.g. a Google SERP for a query about captchas / anti-detect
    tooling — gets misread as a block, which fails the fetch and drives a
    wasteful chain of proxy-rotating retries.

    ``final_url`` is the one size-independent signal: Google serves real
    blocks from ``/sorry/`` (HTTP 200) and Yandex SmartCaptcha from
    ``/showcaptcha`` (HTTP 200), so the redirect target (not the status code)
    is what unambiguously marks the block. Without the Yandex marker a
    SmartCaptcha page is read as a successful empty fetch, so the queue never
    rotates past a captcha'd exit and the SERP parse returns zero results.

    Extracted to module level so the Camoufox runner can reuse it without
    duplication (the heuristic body must not be copied verbatim).
    """
    if final_url:
        final_lower = final_url.lower()
        if "/sorry/" in final_lower or "/showcaptcha" in final_lower:
            return True
    # Past the block-page size ceiling we're looking at real content; any
    # trigger phrase there (result snippets, help text, footers) is a false
    # positive, so don't inspect the body.
    if len(html) > _MAX_BLOCK_PAGE_BYTES:
        return False
    html_lower = html.lower()
    strong_signals = (
        "unusual traffic",
        "verify you are a human",
        "verify you are human",
        "access denied",
        "temporarily blocked",
        "are you a robot",
        "enable javascript and cookies to continue",
        "/sorry/",
        "cf-chl-",  # cloudflare challenge marker
        "data-sitekey",  # reCAPTCHA / Turnstile attribute
        # Amazon's throttle page, whose entire visible text this is. It arrives
        # with HTTP 503 sometimes and HTTP 200 sometimes, from the same URL —
        # and the 200 variant was being accepted as a successful fetch, so the
        # retry loop stopped rotating on exactly the response that needed a
        # fresh exit. Measured at ~2.3 KB against 2-3.7 MB for a real page, so
        # the size ceiling above already keeps it clear of genuine content.
        # Matched as the whole phrase: "sorry" and "something went wrong" are
        # ordinary words apart, and either alone would flag a small error page
        # a caller deliberately scraped.
        "sorry! something went wrong",
        # Akamai Bot Manager's behavioural interstitial, served at HTTP 200
        # from the requested URL with no redirect, so neither the status code
        # nor `final_url` says anything. Measured 2026-09-11 on
        # `suchen.mobile.de/fahrzeuge/details.html`: 2.5-2.7 KB, no `<title>`,
        # zero `data-testid` nodes, an obfuscated sensor script and this
        # container. 6 of 6 runs of the shipped `mobile_de_ad_*` presets landed
        # on it and were reported as `selector_not_found` -- a recipe problem,
        # which it is not: the exit was refused. Matched on Akamai's own
        # element id rather than on the visible text, which is localised.
        "sec-if-cpt-container",
        "scf-akamai-protected-by",
    )
    if any(signal in html_lower for signal in strong_signals):
        return True
    # The bare word "captcha" is the weakest signal — only trust it on a
    # block-sized page (the size ceiling above already bounds us here).
    if "captcha" in html_lower:
        return True
    return False


def warmup_origin(target_url: str) -> str | None:
    """scheme://host/ of the target, or None if not a usable absolute URL."""
    from urllib.parse import urlparse
    p = urlparse(target_url)
    if not p.scheme or not p.hostname:
        return None
    return f"{p.scheme}://{p.hostname}/"


@dataclass(frozen=True)
class WarmupOutcome:
    """What the warmup did, and why it did not.

    Three states, not two. `applied` is the descriptor of a warmup that ran;
    `error` names one that was configured and failed; `blocked` marks one that
    ran fine and landed on a challenge page -- nothing raised, so `error` is
    None and `applied` is truthful, but the exit was already turned away.
    `applied["blocked"]` carries the same flag onward, because that dict
    reaches the caller on every return path. Both `applied` and `error` None
    means none was configured — a distinction
    the old `dict | None` return could not make, so a warmup failing on every
    single attempt was indistinguishable from one nobody asked for. That cost a
    working day on the google presets, whose homepage warmup navigated into a
    host the proxy gateway refused: the response carried no trace of it.
    """

    applied: dict | None = None
    error: str | None = None
    # The warmup NAVIGATED, and landed on a block page. Distinct from `error`:
    # nothing raised, `applied` legitimately describes what ran, and the fetch
    # that follows may still succeed -- but the exit was already refused once
    # before the real navigation, which is the cheapest place to learn it.
    blocked: bool = False


async def run_warmup(
    page, target_url, warmup, *, timeout_ms, default_dwell_ms, resolve: bool = True
) -> WarmupOutcome:
    """Optional pre-navigation warmup. Non-fatal: any failure is reported and
    swallowed so the real navigation still runs.

    type='homepage' visits the target's own origin; type='custom' visits
    warmup['url'] verbatim. Both then dwell before the caller's real navigation.

    Returns a `WarmupOutcome`: `.applied` describes what actually ran ({type,
    url, dwell_ms}, with `url` the URL actually visited), `.error` names a
    configured warmup that failed. A warmup that was never configured — or has
    no usable URL to visit — is neither, and must not read as a failure.
    """
    if not warmup:
        return WarmupOutcome()
    wtype = warmup.get("type", "homepage")
    if wtype == "homepage":
        warm_url = warmup_origin(target_url)
    elif wtype == "custom":
        warm_url = warmup.get("url") or None
    else:
        return WarmupOutcome()
    if not warm_url:
        return WarmupOutcome()
    dwell = warmup.get("dwell_ms")
    dwell = default_dwell_ms if dwell is None else dwell
    # `type='custom'` is a second caller-supplied URL and was completely
    # unvalidated. Refused non-fatally like any other warmup failure, but with
    # the constant message — the requested host must not travel into
    # `warnings`, which is caller-visible.
    try:
        await assert_navigable(str(warm_url), resolve=resolve)
    except EgressBlocked:
        return WarmupOutcome(error=EGRESS_BLOCKED_ERROR)
    try:
        response = await page.goto(
            str(warm_url), wait_until="domcontentloaded", timeout=timeout_ms
        )
        # The warmup navigation redirects like any other, and the route guard
        # is blind to those hops. Non-fatal either way — but without this the
        # warmup was the one navigation with a pre-flight and no landing check.
        await assert_landing_public(response, page.url, resolve=resolve)
        # The warmup is a navigation like any other and can be refused like any
        # other -- a homepage warmup landing on `/sorry/` or `/showcaptcha` is
        # the exit being turned away BEFORE the request we care about. This hop
        # had a pre-flight and a landing check for egress and no block check at
        # all, so it reported success and the refusal reached the caller only
        # as an empty result page later.
        # Read AFTER the dwell, not at goto-return. Eight shipped presets dwell
        # 10 s, and a challenge reached by JS or meta-refresh rather than an
        # HTTP redirect arrives inside that window -- invisible to a snapshot
        # taken at `domcontentloaded`. The main navigation re-reads `page.url`
        # after settling for exactly this reason. Reading late cannot add a
        # false positive: `redirected_to_block` still compares against
        # `warm_url`, and a late in-app push to `/en/` matches neither path.
        await page.wait_for_timeout(dwell)
        landed_on_block = redirected_to_block(str(warm_url), page.url)
        if landed_on_block:
            # The caller learns this from `applied_warmup`; the log is where an
            # operator correlates it with the exit that was in use, which the
            # response deliberately never names.
            log.warning(
                "warmup landed on a block page: %s -> %s", warm_url, page.url
            )
        return WarmupOutcome(
            applied={
                "type": wtype, "url": str(warm_url), "dwell_ms": dwell,
                # Rides on `applied` rather than on a field of its own: every
                # FetchResult return in both runners already carries
                # `applied_warmup`, so a third state cannot be dropped on the
                # error paths the way a parallel field was -- twice.
                "blocked": landed_on_block,
            },
            blocked=landed_on_block,
        )
    except Exception as exc:  # pylint: disable=broad-except
        log.warning("warmup failed (non-fatal) for %s: %s", warm_url, exc)
        # Named with the URL, because "warmup failed" without it does not say
        # WHERE — and the google case was diagnosable only from the where.
        return WarmupOutcome(error=f"{warm_url}: {type(exc).__name__}: {exc}")


async def _fullpage_screenshot(page, *, effective_block_assets: bool) -> bytes:
    """Scroll-for-lazy-load (when assets aren't blocked) then full-page snap.

    Identical to the inline block that lived in `PlaywrightRunner.fetch` before
    the Phase D1 refactor.
    """
    if not effective_block_assets:
        try:
            await page.evaluate(
                """
                async () => {
                  const el = document.scrollingElement
                    || document.documentElement
                    || document.body;
                  if (!el) return;
                  const step = Math.max(200, window.innerHeight * 0.8);
                  let guard = 0;
                  while (
                    el.scrollTop + window.innerHeight < el.scrollHeight
                    && guard++ < 200
                  ) {
                    el.scrollBy(0, step);
                    await new Promise(r => setTimeout(r, 120));
                  }
                  await new Promise(r => setTimeout(r, 200));
                  window.scrollTo(0, 0);
                }
                """
            )
        except PWError:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except PWError:
            pass
    return await page.screenshot(full_page=True)


async def _compute_element_clip(locator) -> dict | None:
    """Document-relative clip rectangle for an element, with 24px padding.

    Uses getBoundingClientRect + window.scrollX/Y because `locator.bounding_box()`
    returns viewport-relative coordinates that are wrong for `position: fixed`
    or sticky-parent elements after scroll. Clamps to the document's scrollable
    bounds so Chromium does not reject the clip rectangle.

    Returns None when the element has zero size or the evaluate fails.
    """
    try:
        geom = await locator.evaluate(
            """el => {
                const r = el.getBoundingClientRect();
                return {
                    x: r.left + window.scrollX,
                    y: r.top + window.scrollY,
                    w: r.width,
                    h: r.height,
                    docW: document.documentElement.scrollWidth,
                    docH: document.documentElement.scrollHeight,
                };
            }"""
        )
    except PWError:
        return None
    if not geom:
        return None
    # Defense-in-depth: a non-numeric or non-finite coordinate (NaN/Infinity)
    # would slip past the `<= 0` guards (NaN comparisons are always False) and
    # later crash the Playwright driver when serialized into the clip rect as
    # invalid JSON. Real Chromium clamps getBoundingClientRect to finite floats,
    # so this is unreachable in practice — but it keeps the helper's "never
    # raises" contract intact for any pathological page. Must run before the
    # `<= 0` comparison, which would itself raise on a non-numeric value.
    if not all(
        isinstance(geom.get(k), (int, float)) and math.isfinite(geom[k])
        for k in ("x", "y", "w", "h", "docW", "docH")
    ):
        return None
    if geom["w"] <= 0 or geom["h"] <= 0:
        return None
    padding = _ELEMENT_PADDING_PX
    x = max(0.0, geom["x"] - padding)
    y = max(0.0, geom["y"] - padding)
    right = min(geom["docW"], geom["x"] + geom["w"] + padding)
    bottom = min(geom["docH"], geom["y"] + geom["h"] + padding)
    width = right - x
    height = bottom - y
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


async def _capture_screenshot(
    page,
    *,
    screenshot: bool,
    element_selector: str | None,
    effective_block_assets: bool,
) -> tuple[bytes | None, ElementScreenshotStatus]:
    """Return (png_bytes, status) for the requested screenshot mode.

    Never raises — every documented failure resolves into a returned status.
    Status strings match `src.schemas.ElementScreenshotStatus`.
    """
    if not screenshot:
        return (None, "no_screenshot")

    if not element_selector:
        try:
            png = await _fullpage_screenshot(
                page, effective_block_assets=effective_block_assets,
            )
        except PWError:
            return (None, "no_screenshot")
        return (png, "not_requested")

    # Element branch.
    _elem_start = time.monotonic_ns()
    fallback_status: ElementScreenshotStatus
    try:
        element_locator = page.locator(element_selector)
        count = await element_locator.count()
    except PWError:
        fallback_status = "fallback_invalid"
    else:
        if count == 0:
            fallback_status = "fallback_not_found"
        else:
            locator = element_locator.first
            try:
                await locator.scroll_into_view_if_needed(
                    timeout=_ELEMENT_SCROLL_TIMEOUT_MS,
                )
            except PWError:
                # A visibility/scroll timeout raises playwright TimeoutError,
                # a subclass of PWError; both land here.
                fallback_status = "fallback_timeout"
            else:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=_ELEMENT_NETWORKIDLE_TIMEOUT_MS,
                    )
                except PWError:
                    pass
                clip = await _compute_element_clip(locator)
                if clip is None:
                    fallback_status = "fallback_zero_size"
                else:
                    try:
                        png = await page.screenshot(full_page=True, clip=clip)
                    except PWError:
                        fallback_status = "fallback_zero_size"
                    else:
                        log.info(
                            "element_capture: selector=%r status=%s elapsed_ms=%d",
                            element_selector[:200], "element",
                            (time.monotonic_ns() - _elem_start) // 1_000_000,
                        )
                        return (png, "element")

    # Fallback path: full-page (existing behaviour). If the fallback capture
    # itself raises, return no_screenshot so the response is internally
    # consistent.
    try:
        png = await _fullpage_screenshot(
            page, effective_block_assets=effective_block_assets,
        )
    except PWError:
        log.warning(
            "element_capture: fallback screenshot also failed for selector=%r status=%s",
            element_selector[:200], fallback_status,
        )
        return (None, "no_screenshot")
    log.info(
        "element_capture: selector=%r status=%s elapsed_ms=%d",
        element_selector[:200], fallback_status,
        (time.monotonic_ns() - _elem_start) // 1_000_000,
    )
    return (png, fallback_status)


@dataclass
class FetchResult:
    html: str
    final_url: str | None
    status_code: int | None
    screenshot_b64: str | None
    ok: bool  # True if success
    error: str | None  # Error description, if set not ok
    # Browser fingerprint that was actually applied to the page.
    applied_user_agent: str | None = None
    applied_locale: str | None = None
    applied_timezone: str | None = None
    applied_accept_language: str | None = None
    # What the pre-navigation warmup actually did ({type, url, dwell_ms}), or
    # None if no warmup ran. Distinct from the requested warmup config.
    applied_warmup: dict | None = None
    # Set only when a warmup was CONFIGURED and failed. `applied_warmup=None`
    # cannot say that on its own — it also means "none configured" — and the
    # queue turns this into a caller-visible warning.
    warmup_error: str | None = None
    # ResolvedFingerprint.as_meta() for this fetch; None on the Playwright path,
    # which has no profiles.
    applied_fingerprint: dict | None = None
    storage_state: dict | None = None
    element_status: ElementScreenshotStatus | None = None
    # True when the page looked like a captcha / anti-bot block. Distinct from a
    # network/proxy error: the request succeeded but the IP is burned, so the
    # queue layer should rotate the proxy and retry.
    blocked: bool = False
    # Targets the transport guard refused during this fetch (host:port), so a
    # denied redirect hop or sub-resource is explainable. Without it the caller
    # sees only a raw transport error and reads it as a proxy fault.
    egress_denied: list[str] = field(default_factory=list)
    # Request fields the engine could not honour and dropped. `warnings` is the
    # only place a caller ever learns this: the scraper-tester UI shipped four
    # User-Agent header presets until this change removed them, so "I picked
    # chrome_win and nothing happened" is a real operator experience rather
    # than a hypothetical one, and the header is still sendable by hand. A
    # worker log the caller cannot read is not an answer -- the same argument
    # `field_guards` makes for a substituted field.
    ignored_request_fields: list[str] = field(default_factory=list)


class PlaywrightRunner:
    def __init__(self, headless: bool, block_assets: bool, timeout_ms: int, engine: str = "chromium"):
        self.headless = headless
        self.block_assets = block_assets
        self.timeout_ms = timeout_ms
        self._engine = engine
        self._playwright: Any = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            # Headful needs an X display. The container entrypoint starts Xvfb
            # and exports DISPLAY; fail fast with a readable message instead of
            # Playwright's opaque launch crash when it is missing.
            if not self.headless and not os.environ.get("DISPLAY"):
                raise RuntimeError(
                    "headful launch requires an X display but DISPLAY is unset "
                    "(is Xvfb running? see scripts/docker-entrypoint.sh)"
                )
            self._playwright = await async_playwright().start()
            # Optional WebRTC leak protection. When Chromium initialises
            # WebRTC it issues STUN requests over UDP directly to the remote
            # server, bypassing SOCKS5/HTTP proxies (which typically carry
            # TCP only). These flags force WebRTC to only use the proxied
            # path. Disable via WEBRTC_BLOCK=false if the scraped site
            # actually needs working WebRTC (video chat, RTCPeerConnection).
            #
            # Separately: hide the headless-automation signal. Without this flag
            # Google (and other anti-bot SERPs) redirect a headless Chromium
            # straight to a /sorry captcha even from a clean residential IP; with
            # it, the same browser renders a full SERP. Verified empirically.
            launch_args: list[str] = []
            if self._engine == "chromium":
                launch_args.append("--disable-blink-features=AutomationControlled")
                # This container has no GPU (/dev/dri is absent), and since
                # Chrome M136 a headful Chrome no longer falls back to software
                # WebGL on its own. The deployed config is exactly that — real
                # Google Chrome via CHROME_CHANNEL, headful under Xvfb — so
                # every page was handed a NULL WebGL context. That is a louder
                # tell than software rendering: real desktop Chrome effectively
                # always has one. Measured on bot.sannysoft.com as "Canvas has
                # no webgl context", identical with stealth on and off, because
                # playwright-stealth patches getParameter on the prototype and
                # with no context its patch never runs.
                #
                # On the headless paths Chrome still falls back on its own, but
                # the flag is not inert there either: without it each context
                # logs a deprecation warning, and Chromium calls the automatic
                # fallback deprecated rather than permanent. One flag for every
                # mode is therefore also the forward-compatible choice.
                #
                # WHAT IT COSTS. "unsafe" is not decoration: the flag re-enables
                # SwiftShader's Subzero JIT, which compiles shaders from the
                # scraped page to native code inside the GPU process — and this
                # service renders caller-supplied URLs in a GPU process that runs
                # --no-sandbox (Playwright's default; the container is root and
                # cannot sandbox) with no egress guard on /scrape. Chromium
                # removed the automatic fallback for exactly this reason.
                # Accepted here because the alternative is a fingerprint every
                # anti-bot reads instantly, and because the exposure is not new
                # to this flag — the headless and bundled-Chromium paths already
                # ran SwiftShader by automatic fallback. SOFTWARE_WEBGL=false
                # takes it back without a code change if that trade goes bad.
                if settings.software_webgl:
                    launch_args.append("--enable-unsafe-swiftshader")
                if settings.webrtc_block:
                    launch_args.extend([
                        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--force-webrtc-ip-handling-policy",
                    ])
            browser_type = getattr(self._playwright, self._engine)
            # Not this process's environment: the browser renders
            # caller-supplied URLs under --no-sandbox and has no business
            # holding the service token, the LLM keys or the proxy passwords.
            launch_kwargs: dict[str, object] = {
                "headless": self.headless,
                "env": browser_env(),
            }
            # Drive a real Google Chrome (channel="chrome") instead of the bundled
            # Chromium when configured — real branding/codecs and a populated
            # navigator.plugins, and a newer engine. Chromium-only; the bundled
            # Firefox/WebKit/Camoufox have no such channel.
            if self._engine == "chromium" and settings.chrome_channel:
                launch_kwargs["channel"] = settings.chrome_channel
            # No launch-level proxy on any engine. Playwright honours a
            # per-context proxy (set in _new_context) for Firefox/WebKit without
            # a launch placeholder, and a no-proxy context goes direct. The old
            # {"server": "per-context"} placeholder leaked into no-proxy contexts
            # as a literal host and broke them (NS_ERROR_UNKNOWN_PROXY_HOST).
            if launch_args:
                launch_kwargs["args"] = launch_args
            # Logged BEFORE the launch, not after: a launch that raises (missing
            # Chrome channel, dead Xvfb, OOM) is the other half of the silence
            # this line exists for, and Playwright's exception names none of
            # these values. The NULL-WebGL bug rendered every page wrong for an
            # unknown number of days without a single line in the log, because a
            # launch said nothing about itself. Safe to log in full: the proxy is
            # per-context by design (see below), so no credential passes here.
            log.info(
                "launching browser: engine=%s channel=%s headless=%s args=%s",
                self._engine, launch_kwargs.get("channel"), self.headless, launch_args,
            )
            self._browser = await browser_type.launch(**launch_kwargs)

    async def stop(self) -> None:
        # Must only be called from the worker's own coroutine (between jobs):
        # the lock is held across browser.close(), so an outside watchdog
        # invoking stop() while a fetch is in flight would deadlock against
        # the start() call that fetch performs internally.
        async with self._lock:
            browser, self._browser = self._browser, None
            playwright, self._playwright = self._playwright, None
            # Always null the handles first so that even if close() raises
            # (wedged Chromium, broken WS pipe) the next start() will rebuild
            # the runner from scratch instead of reusing a dead Browser.
            if browser:
                # Best-effort: a wedged / OOM-killed browser raises here, and
                # letting that propagate would skip playwright.stop() below and
                # leak the node driver process. The browser is already being
                # discarded, so a failed close has nothing left to salvage.
                try:
                    await browser.close()
                except Exception:  # pylint: disable=broad-except
                    log.warning("browser close failed, stopping driver anyway", exc_info=True)
            if playwright:
                await playwright.stop()

    def is_started(self) -> bool:
        return self._browser is not None

    async def resolve_proxy(self, proxy: Optional[ProxyConfig]):
        """Translate a possibly-authenticated SOCKS5 proxy into a Playwright-safe one.

        Chromium does not support SOCKS5 with username/password. When the caller
        hands us an authenticated socks5:// URL we spawn a local HTTP-to-SOCKS5
        bridge and hand Playwright the local HTTP URL instead. Callers must
        ``await bridge_cm.__aexit__(None, None, None)`` in their finally clause
        when the returned bridge is not None.

        On the DIRECT path (no upstream proxy — which is the default,
        `proxy_type="none"`) it also inserts the local egress guard into the
        proxy slot. That is the only layer that PREVENTS a refused request
        instead of refusing its content afterwards: measured against real
        Chromium, a proxy here is handed both hops of a redirect and is handed
        a hostname rather than an address the browser already resolved, so it
        closes both the redirect blind spot and the TOCTOU window.

        Bound to the caller's existing `bridge_cm` slot, so no new lifecycle
        appears at any call site.

        Returns:
            (effective_proxy, bridge_cm, guard) — `guard` is None unless the
            transport guard is in play; `bridge_cm` is None when neither is.
        """
        if proxy is None:
            if not settings.egress_transport_guard:
                return None, None, None
            guard_cm = open_egress_guard(resolve=True)
            guard = await guard_cm.__aenter__()
            return ProxyConfig(server=guard.url), guard_cm, guard
        needs_bridge = (
            proxy.server.lower().startswith("socks5://")
            and (proxy.username or proxy.password)
        )
        if not needs_bridge:
            return proxy, None, None

        from urllib.parse import urlparse, quote
        parsed = urlparse(proxy.server)
        auth = ""
        if proxy.username:
            auth = quote(proxy.username, safe="")
            if proxy.password:
                auth += ":" + quote(proxy.password, safe="")
            auth += "@"
        socks_url = f"socks5://{auth}{parsed.hostname}:{parsed.port}"
        bridge_cm = open_socks_to_http_bridge(socks_url)
        local_url = await bridge_cm.__aenter__()
        log.info("routing SOCKS5 proxy %s via local bridge %s", proxy.server, local_url)
        return ProxyConfig(server=local_url, username=None, password=None), bridge_cm, None

    async def _new_context(
        self,
        device: str,
        proxy: Optional[ProxyConfig],
        headers: dict[str, str] | None,
        block_assets: bool | None = None,
        proxy_geo: dict[str, str] | None = None,
        render: bool = True,
        storage_state: dict | None = None,
        viewport: dict[str, int] | None = None,
        resolve_dns: bool | None = None,
    ) -> BrowserContext:
        assert self._browser is not None
        preset = _DEVICE_PRESETS.get(device, DESKTOP)
        # A caller-supplied viewport overrides the device preset. window.screen
        # is set to the same size (below) so screen and innerWidth stay
        # consistent — a window larger than the screen is a fingerprint tell.
        effective_viewport = viewport or preset["viewport"]

        proxy_arg = None
        if proxy is not None:
            proxy_arg = {"server": proxy.server}
            if proxy.username:
                proxy_arg["username"] = proxy.username
            if proxy.password:
                proxy_arg["password"] = proxy.password

        # Align locale / timezone / Accept-Language with the proxy's geo so
        # the browser fingerprint matches the exit IP. Explicit Accept-Language
        # supplied via `headers` wins over the geo-derived default.
        locale = preset.get("locale", "en-US")
        timezone_id = preset.get("timezone_id", "America/New_York")
        accept_language: str | None = None

        if proxy_geo:
            profile = resolve_profile(proxy_geo.get("country_code"), proxy_geo.get("city"))
            if profile is not None:
                locale = profile.locale
                timezone_id = profile.timezone_id
                accept_language = profile.accept_language
                log.info(
                    "geo profile applied: country=%s city=%s -> locale=%s tz=%s",
                    proxy_geo.get("country_code"),
                    proxy_geo.get("city"),
                    locale,
                    timezone_id,
                )

        # Measured 2026-09-11 through `fetch()` against a live header echo,
        # caller sending `Nokia7610/2.0 ... SymbianOS/7.0s`:
        #   chromium  wire UA stayed the preset's Chrome/143 -- the context UA
        #             outranks `extra_http_headers`, so the header was already
        #             a no-op, just a silent one.
        #   firefox   wire UA WAS `Nokia7610`, Sec-CH-UA absent, while
        #             `applied_user_agent` reported `Firefox/144`.
        #   webkit    same, reporting `Safari/26` -- a string never sent, in the
        #             field read first when a scrape comes back blocked.
        effective_headers, dropped_headers = strip_engine_owned_headers(headers)
        if dropped_headers:
            log.warning(
                "%s: dropping %s (the engine states the only identity its "
                "transport can back); see meta.applied_user_agent for what was "
                "sent", self._engine, ", ".join(dropped_headers),
            )
        if accept_language and not any(k.lower() == "accept-language" for k in effective_headers):
            effective_headers["Accept-Language"] = accept_language

        # Advertise the real engine major in the UA (and, via _chrome_ua_metadata
        # below, in Sec-CH-UA) so we don't claim an old Chrome while exposing a
        # newer engine's features.
        #
        # A UA is only safe to state when the TRANSPORT agrees with it. The
        # desktop preset's UA is a Chrome string, and a server reads far more
        # than the header: measured 2026-09-06 against a TLS mirror, our
        # Firefox sent `Chrome/124 (Windows)` while its JA4 was
        # `t13d1717h2_5b57614c22b0` (Firefox), its HTTP/2 settings hash was
        # Firefox's, and it sent no Sec-CH-UA at all — because Firefox has no
        # Client Hints. Three axes contradicting the header at once; no real
        # user produces that. WebKit had the same shape with Safari's TLS.
        # So the Chrome UA is stated only by the engine that can back it, and
        # Firefox/WebKit keep their own true UA (Camoufox owns its own).
        effective_ua = preset["user_agent"] if self._engine == "chromium" else None
        if preset.get("suppress_client_hints"):
            # The one identity every engine states verbatim. It carries no
            # `Chrome/` token to align, and with Client Hints suppressed there
            # is nothing left for the engine to contradict it with -- Firefox
            # and WebKit emit no Client Hints at all, and Chromium is made to
            # emit none. TLS still says what the engine is; the docstring on
            # LEGACY_WAP says so out loud.
            effective_ua = preset["user_agent"]
        elif self._engine == "chromium":
            effective_ua = _align_ua_to_engine(effective_ua, self._browser.version)
        elif preset.get("is_mobile") and self._engine == "webkit":
            # An iPhone Safari UA on WebKit is coherent — same engine family.
            effective_ua = preset["user_agent"]

        context = await self._browser.new_context(
            user_agent=effective_ua,
            viewport=effective_viewport,
            screen=effective_viewport,
            locale=locale,
            timezone_id=timezone_id,
            color_scheme=preset.get("color_scheme", "light"),
            is_mobile=preset.get("is_mobile", False),
            has_touch=preset.get("has_touch", False),
            java_script_enabled=render,
            proxy=proxy_arg,
            extra_http_headers=effective_headers or None,
            storage_state=storage_state,
        )
        # Stash applied fingerprint on the context so fetch() can surface it
        # in FetchResult without needing to re-derive the values.
        context._applied_user_agent = effective_ua  # type: ignore[attr-defined]
        context._suppress_client_hints = bool(  # type: ignore[attr-defined]
            preset.get("suppress_client_hints")
        )
        context._applied_locale = locale  # type: ignore[attr-defined]
        context._applied_timezone = timezone_id  # type: ignore[attr-defined]
        context._applied_accept_language = (  # type: ignore[attr-defined]
            effective_headers.get("Accept-Language")
            or effective_headers.get("accept-language")
        )

        # WebRTC leak protection, applied at the JS level because the Chromium
        # launch flags aren't always effective in headless mode. The script is
        # kept in webrtc_stealth.js (loaded as _WEBRTC_STEALTH_JS); see that file
        # for why the API is preserved native-looking rather than deleted.
        if settings.webrtc_block:
            await context.add_init_script(_WEBRTC_STEALTH_JS)

        effective_block_assets = self.block_assets if block_assets is None else block_assets
        if effective_block_assets:
            await context.route("**/*", _block_assets_route)
        # Registered AFTER the asset blocker and unconditionally: Playwright
        # runs route handlers last-registered-first, so this decides before the
        # blocker forwards anything, and it must not inherit `block_assets`.
        # It yields with `fallback()`, so the blocker still gets its turn.
        #
        # Cost, stated because it is not free: "enabling routing disables http
        # cache" (Playwright's own API docs). Only new behaviour when
        # `block_assets=false` — the default already registered a route — so
        # the affected case is screenshot-with-images scrapes, which is also
        # where the re-fetched bytes are paid for through the proxy.
        # Explicit, never `proxy is None`: this parameter receives
        # `effective_proxy`, so once the transport guard occupies that slot the
        # derived answer inverts and the route guard silently stops resolving.
        # The caller knows whether an UPSTREAM proxy is in play; this does not.
        await context.route(
            "**/*",
            make_route_guard(
                resolve=(proxy is None) if resolve_dns is None else resolve_dns
            ),
        )
        return context

    def _looks_like_captcha_or_block(
        self, html: str, *, final_url: str | None = None
    ) -> bool:
        """Delegate to the module-level function; kept for backward compat."""
        return looks_like_captcha_or_block(html, final_url=final_url)

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
        # Camoufox-only premium options — accepted here so the call shape in
        # scrape_runner.py is identical for both runners; ignored by Chromium/Firefox/WebKit.
        humanize: bool = False,
        spoof_os: str | None = None,
        fingerprint_profile: str | None = None,
        block_webgl: bool = False,
        addons: list[str] | None = None,
        warmup: dict | None = None,
    ) -> FetchResult:
        # Derived here rather than round-tripped through the context: `headers`
        # is right there, and a stash the reader defaults to "nothing dropped"
        # fails open -- losing the write would silently restore the old silence
        # with a green suite, which is how this class of bug survives here.
        _, ignored_request_fields = strip_engine_owned_headers(headers)

        # Before `start()`, so a refused target costs neither a browser launch
        # nor a proxy lease AT THIS BOUNDARY — `EphemeralPlaywrightRunner` and
        # the queue layer both do their own setup before calling in, so this is
        # not the end-to-end claim. `resolve` is off on the proxied path: the
        # upstream proxy does its own DNS, so a local answer describes a
        # different network than the one that will be dialled.
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

        await self.start()
        assert self._browser is not None

        if storage_state and cookies:
            log.warning("both storage_state and cookies provided — ignoring cookies")
            cookies = None

        effective_block_assets = self.block_assets if block_assets is None else block_assets

        effective_proxy, bridge_cm, egress_guard = await self.resolve_proxy(proxy)

        context = None
        page = None

        async def _teardown() -> None:
            """Close everything this fetch opened. See src/browser/teardown.py
            for why each close is independent, bounded and logged."""
            if page is not None:
                await close_quietly(
                    "page", page.close, owner=url, timeout=_TEARDOWN_TIMEOUT_S
                )
            if context is not None:
                await close_quietly(
                    "context", context.close, owner=url, timeout=_TEARDOWN_TIMEOUT_S
                )
            if bridge_cm is not None:
                await close_quietly(
                    "socks bridge",
                    lambda: bridge_cm.__aexit__(None, None, None),
                    owner=url,
                    timeout=_TEARDOWN_TIMEOUT_S,
                )

        # Acquire context/page outside the main fetch try; a failure here would
        # otherwise leak the context and SOCKS bridge (the fetch finally never
        # runs because its try is never entered).
        try:
            context = await self._new_context(
                device=device, proxy=effective_proxy, headers=headers,
                block_assets=block_assets, proxy_geo=proxy_geo, render=render,
                storage_state=storage_state, viewport=viewport,
                resolve_dns=proxy is None,
            )

            if cookies:
                # Playwright requires either url or both domain+path. If the caller
                # omitted domain, default it to the URL's hostname and path="/".
                from urllib.parse import urlparse  # local import to avoid top-level pollution
                default_domain = urlparse(url).hostname or ""
                prepared_cookies = []
                for cookie in cookies:
                    cookie_dict = dict(cookie)
                    if not cookie_dict.get("domain") and not cookie_dict.get("url"):
                        cookie_dict["domain"] = default_domain
                        cookie_dict.setdefault("path", "/")
                    prepared_cookies.append(cookie_dict)
                try:
                    await context.add_cookies(prepared_cookies)
                except Exception as exc:  # pylint: disable=broad-except
                    log.warning("failed to add cookies: %s", exc)

            page = await context.new_page()
            await apply_page_masking(
                context, page, engine=self._engine, stealth=stealth,
            )

            # Firefox and WebKit keep their own UA (we state no override for
            # them), so the stash is None and `meta.applied_user_agent` --
            # documented as the UA that was actually sent, and the first field
            # anyone reads when a scrape comes back blocked -- would report
            # nothing. Read the real one off the page, the way CamoufoxRunner
            # already does for the same reason. INSIDE this try on purpose: it
            # is an await, and everything between the acquire guard and the
            # fetch try below is unguarded, so a cancellation there (the queue
            # runs each scrape under asyncio.wait_for) would escape with the
            # page, context, SOCKS bridge and egress guard all unclosed.
            # `except Exception` keeps an evaluate failure non-fatal while
            # still letting BaseException reach the teardown below.
            if getattr(context, "_applied_user_agent", None) is None:
                try:
                    context._applied_user_agent = await page.evaluate(  # type: ignore[attr-defined]
                        "() => navigator.userAgent"
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    # Warning, not debug: this field is what someone reads
                    # first when a scrape comes back blocked, and debug is off
                    # in production, so a silent null would be unexplainable.
                    log.warning(
                        "user-agent readback failed for engine=%s: %s",
                        self._engine, exc,
                    )
        except BaseException:
            await _teardown()
            raise

        applied = {
            "user_agent": getattr(context, "_applied_user_agent", None),
            "locale": getattr(context, "_applied_locale", None),
            "timezone": getattr(context, "_applied_timezone", None),
            "accept_language": getattr(context, "_applied_accept_language", None),
        }

        # Late-bound: set inside the try below, read by _with_applied when each
        # FetchResult is wrapped after the fetch completes.
        applied_warmup: dict | None = None
        warmup_error: str | None = None

        def _with_applied(result: FetchResult) -> FetchResult:
            result.applied_user_agent = applied["user_agent"]
            result.applied_locale = applied["locale"]
            result.applied_timezone = applied["timezone"]
            result.applied_accept_language = applied["accept_language"]
            result.applied_warmup = applied_warmup
            result.warmup_error = warmup_error
            # Read here rather than at each return: the guard accumulates
            # refusals for the whole fetch, so the list is only final once the
            # fetch is over — including on the error paths, where a refused
            # target is usually the REASON for the error.
            if egress_guard is not None:
                result.egress_denied = list(egress_guard.denied)
            result.ignored_request_fields = list(ignored_request_fields)
            return result

        try:
            effective_timeout_ms = timeout_ms or self.timeout_ms
            warmup_outcome = await run_warmup(
                page, url, warmup,
                timeout_ms=effective_timeout_ms,
                default_dwell_ms=settings.warmup_dwell_ms,
                resolve=proxy is None,
            )
            applied_warmup = warmup_outcome.applied
            warmup_error = warmup_outcome.error
            resp = await page.goto(url, wait_until=wait_until, timeout=effective_timeout_ms)
            # The route guard is blind to redirect hops (Playwright does not
            # invoke it for them), so a public URL that 302s into the internal
            # network reaches this line having already fetched. Walking the
            # chain here refuses to hand the body back — blind, not full-read.
            await assert_landing_public(resp, page.url, resolve=proxy is None)
            selector_missing = False
            # See redirected_to_block: once the navigation has landed on a block
            # endpoint, the selector cannot appear, and waiting for it burns a
            # full deadline per attempt for nothing.
            if wait_for_selector and not redirected_to_block(url, page.url):
                try:
                    await page.wait_for_selector(
                        wait_for_selector, timeout=effective_timeout_ms
                    )
                except PWTimeoutError as exc:
                    # Don't abandon the page. The selector is most often absent
                    # because the target served an interstitial instead of the
                    # content — a case the block heuristic below already
                    # recognises — and jumping to the except handler discards
                    # the html/url it needs, turning a captcha into a bare
                    # timeout the queue can't act on.
                    #
                    # Only the deadline is recovered: a malformed selector
                    # raises a plain PWError here, and reporting that as
                    # "not found" would both misdiagnose it and spend the whole
                    # rotation budget on a request that can never succeed.
                    selector_missing = True
                    log.warning(
                        "selector %r did not appear before the deadline for %s: %s",
                        wait_for_selector, url, exc,
                    )

            html = await read_content_settling_navigation(page)
            final_url = page.url
            # AFTER the read, not before. The landing check above is a snapshot
            # taken at `goto`; `read_content_settling_navigation` deliberately
            # waits out an in-flight navigation and returns the content of
            # wherever the page ENDED UP. Checked before the read, a public
            # page that meta-refreshes to a public hop which 302s internal
            # still returned the internal body with ok=True — measured.
            # `final_url` is the URL this html actually belongs to, so that is
            # what has to be public.
            await assert_page_public(page, resolve=proxy is None)
            status_code = resp.status if resp is not None else None
            captcha_detected = self._looks_like_captcha_or_block(
                html, final_url=final_url
            )
            fetch_ok, fetch_blocked, fetch_error = classify_fetch(
                status_code, captcha_detected=captcha_detected
            )
            if selector_missing:
                # A page that never grew the requested selector is incomplete,
                # so it never counts as ok. Keep whatever reason the classifier
                # already found — it explains why the selector never appeared —
                # and only name the selector when nothing else did.
                fetch_ok = False
                fetch_error = fetch_error or f"{SELECTOR_MISS_PREFIX}{wait_for_selector}"

            screenshot_b64 = None
            png, element_status = await _capture_screenshot(
                page,
                screenshot=screenshot,
                element_selector=element_selector,
                effective_block_assets=effective_block_assets,
            )
            if png is not None:
                # The capture is its own window in which the page can navigate,
                # and an image of an internal page is the same disclosure as
                # its html.
                await assert_page_public(page, resolve=proxy is None)
                screenshot_b64 = base64.b64encode(png).decode("ascii")

            new_storage_state = None
            if storage_state is not None:
                try:
                    new_storage_state = await context.storage_state()
                except PWError as exc:
                    log.warning("failed to capture storage_state: %s", exc)

            return _with_applied(FetchResult(
                html=html,
                final_url=final_url,
                status_code=status_code,
                screenshot_b64=screenshot_b64,
                ok=fetch_ok,
                error=fetch_error,
                storage_state=new_storage_state,
                element_status=element_status,
                blocked=fetch_blocked,
            ))

        # Before the broad arms: they stringify the exception into `error`,
        # which would put the attacker's host back into a caller-visible
        # message and hand `looks_like_proxy_failure` needles to match on.
        except EgressBlocked:
            return _with_applied(FetchResult(
                html="",
                final_url=None,
                status_code=None,
                screenshot_b64=None,
                ok=False,
                error=EGRESS_BLOCKED_ERROR,
                element_status="no_screenshot",
            ))
        except PWError as e:
            return _with_applied(FetchResult(
                html="",
                final_url=None,
                status_code=None,
                screenshot_b64=None,
                ok=False,
                error=f"PlaywrightError: {str(e)}",
                element_status="no_screenshot",
            ))
        except Exception as e:
            return _with_applied(FetchResult(
                html="",
                final_url=None,
                status_code=None,
                screenshot_b64=None,
                ok=False,
                error=f"UnexpectedError: {str(e)}",
                element_status="no_screenshot",
            ))
        finally:
            await _teardown()
