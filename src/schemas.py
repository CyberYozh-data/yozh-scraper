from __future__ import annotations
import re
from typing import Any, Literal, Dict
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

# Single source of truth for extraction models. `FieldRule`/`ExtractRule`
# (and `PostProcess`) live in src.extract.models — the worker validates the
# same classes, so a preset's parsing_instructions flows through
# ScrapeRequest.extract without losing post_process/type.
# pylint: disable=unused-import
from src.extract.models import (  # noqa: F401  re-exported for API consumers
    ExtractRule,
    ExtractType,
    FieldRule,
    PostProcess,
)
from src.browser.fingerprint_profile import FingerprintProfile, claimed_os
from src.presets.models import ParserPlan, PresetMeta

# pylint: enable=unused-import

ProxyType = Literal["mobile_shared", "mobile", "res_static", "res_rotating", "dc_static", "prem_res_rotating"]
ScrapeProxyType = Literal["none", "mobile_shared", "mobile", "res_static", "res_rotating", "dc_static", "prem_res_rotating"]
SearchEngine = Literal["google", "bing", "yandex"]
WaitUntil = Literal["domcontentloaded", "load", "networkidle"]
Device = Literal["desktop", "mobile", "legacy_wap"]
OutputFormat = Literal["markdown", "fit_markdown", "raw_html", "html", "links", "screenshot"]
ContentFilter = Literal["none", "pruning", "llm"]
JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]
ElementScreenshotStatus = Literal[
    "element",
    "fallback_not_found",
    "fallback_invalid",
    "fallback_zero_size",
    "fallback_timeout",
    "not_requested",
    "no_screenshot",
]
BrowserEngine = Literal["chromium", "firefox", "webkit", "camoufox"]
SpoofOS = Literal["windows", "macos", "linux"]


class ProxyItem(BaseModel):
    id: str
    url: str
    status: str
    expired: bool
    host: str | None = None
    port: int | None = None
    access_type: str | None = None


class ProxyListResponse(BaseModel):
    proxy_type: ProxyType
    category: str
    configured: bool
    items: list[ProxyItem]


class CountryItem(BaseModel):
    code: str
    name: str


class CountriesResponse(BaseModel):
    countries: list[CountryItem]


class ProxyResolveResponse(BaseModel):
    proxy_url: str | None = Field(
        default=None,
        description="Resolved upstream proxy URL (e.g. socks5://… or http://… with embedded creds), or null for proxy_type=none / no proxy.",
    )


class Cookie(BaseModel):
    name: str
    value: str
    domain: str | None = None
    path: str | None = "/"
    expires: int | None = None
    httpOnly: bool | None = None
    secure: bool | None = None
    sameSite: Literal["Strict", "Lax", "None"] | None = None


class ProxyGeo(BaseModel):
    country_code: str | None = None
    region: str | None = Field(default=None, max_length=128)
    city: str | None = Field(default=None, max_length=128)

    @field_validator("country_code")
    @classmethod
    def _validate_country_code(cls, v: str | None) -> str | None:
        # country_code is templated raw into the proxy username (c-<iso>), so a
        # stray '-' would inject extra gateway targeting tokens. Constrain it to
        # an ISO-3166 alpha-2 code; blank means "no country" (kept for the legacy
        # `(country_code or "").strip()` behaviour downstream).
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not re.fullmatch(r"[A-Za-z]{2}", v):
            raise ValueError("country_code must be an ISO-3166 alpha-2 code")
        return v


class Viewport(BaseModel):
    """Browser viewport size in CSS pixels; window.screen is set to match.

    Keeping screen and innerWidth/Height equal avoids a fingerprint tell: a
    window larger than the reported screen is physically impossible and flags
    automation. Bounds are generous but reject absurd values.
    """

    width: int = Field(ge=320, le=7680)
    height: int = Field(ge=240, le=4320)


class PremProxyOptions(BaseModel):
    """Targeting options for the v2 premium rotating gateway (prem_res_rotating).

    country/region/city are read from proxy_geo. All fields optional; defaults
    reproduce a plain rotating RU-agnostic exit. Suffixes are resolved from the
    CyberYozh v2 /geo and /session-options endpoints at runtime, never hardcoded.
    """

    sub_user_id: str | None = Field(
        default=None,
        description="Which v2 sub-user to authenticate as. Defaults to the first/primary sub-user.",
    )
    ip_filter: Literal[
        "max-size-security", "max-speed-security",
        "quality-security", "speed-quality-security",
    ] = "max-size-security"
    zip: str | None = Field(
        default=None,
        max_length=64,
        description="Target by ZIP. Mutually exclusive with proxy_geo.region / proxy_geo.city.",
    )
    isp: str | None = Field(default=None, max_length=64)
    session_type: Literal["rotating", "sticky"] = "rotating"
    sticky_id: str | None = Field(
        default=None,
        description="Sticky-session id (reuse = same exit until ttl). Auto-generated (8 chars) for sticky if unset. Distinct from the top-level session_id.",
    )
    rotation_minutes: int | None = Field(default=None, ge=1, le=1440)
    protocol: Literal["http", "socks5"] = "http"

    @field_validator("sticky_id")
    @classmethod
    def _validate_sticky_id(cls, v: str | None) -> str | None:
        # sticky_id is templated raw into the username token "s-<id>", so a '-'
        # would inject extra gateway targeting tokens. Constrain to the same
        # alphabet gen_sticky_id uses; blank means "auto-generate downstream".
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", v):
            raise ValueError("sticky_id must be 1-64 alphanumeric characters")
        return v

    @model_validator(mode="after")
    def _validate_sticky(self) -> "PremProxyOptions":
        if self.session_type != "sticky":
            if self.rotation_minutes is not None:
                raise ValueError("rotation_minutes requires session_type='sticky'")
            if self.sticky_id is not None:
                raise ValueError("sticky_id requires session_type='sticky'")
        return self


class WarmupOptions(BaseModel):
    """Pre-navigation warmup: visit a page and dwell before the real fetch, in
    the same browser context (seeds cookies/session). Works on all engines.
    'homepage' visits the target's own origin; 'custom' visits an explicit URL."""

    type: Literal["homepage", "custom"] = "homepage"
    url: str | None = Field(
        default=None,
        description="Warmup URL for type='custom' (visited before the target). Ignored for 'homepage'.",
    )
    dwell_ms: int | None = Field(
        default=None, ge=0, le=60000,
        description="Override the server's WARMUP_DWELL_MS for this request.",
    )

    @model_validator(mode="after")
    def _validate_warmup(self) -> "WarmupOptions":
        if self.type == "custom":
            if not self.url:
                raise ValueError("warmup type='custom' requires a url")
            if not self.url.startswith(("http://", "https://")):
                raise ValueError("warmup url must start with http:// or https://")
        return self


class AppliedWarmup(BaseModel):
    """What the pre-navigation warmup ACTUALLY did (response side). Distinct from
    WarmupOptions (the request input): `url` is the URL actually visited (the
    resolved origin for type='homepage') and `dwell_ms` is the real dwell, which
    may be the server's configured WARMUP_DWELL_MS. It carries NO request-side
    bounds — this records reality, so it must always validate on read-back."""

    type: str
    url: str
    dwell_ms: int
    blocked: bool = False
    """The warmup navigated and landed on a challenge page.

    Reality, like the rest of this model: nothing failed, `url` still says what
    was visited. It means the exit was turned away BEFORE the request the
    caller asked for, so an empty result afterwards is the site refusing this
    exit rather than a selector that stopped matching."""


class MarkdownOptions(BaseModel):
    only_main_content: bool = Field(
        default=False,
        description=(
            "Strip navigation / header / footer / cookie / sidebar boilerplate "
            "before conversion. Off by default: a faithful full-page render is "
            "safer for callers that audit footer/cookie/legal elements."
        ),
    )
    content_filter: ContentFilter = Field(
        default="none",
        description=(
            "How to produce `fit_markdown` (the noise-filtered variant). "
            "'pruning' = cheap heuristic; 'llm' = model-based clean-up. "
            "'none' means no fit_markdown unless the `fit_markdown` format is "
            "requested, in which case 'pruning' is used."
        ),
    )
    filter_instruction: str | None = Field(
        default=None,
        description="Natural-language instruction for the 'llm' content filter.",
    )
    filter_model: str | None = Field(
        default=None,
        description="Model for the 'llm' content filter (defaults to the server's DEFAULT_LLM_MODEL).",
    )
    ignore_links: bool = False
    ignore_images: bool = False
    citations: bool = Field(
        default=False,
        description="Replace inline links with numbered ⟨n⟩ markers + a references section.",
    )
    body_width: int = Field(
        default=0,
        description="Hard wrap width; 0 disables wrapping (recommended for LLMs).",
    )


# `resolve_redirects` bounds, stated once. The schema enforces them on the way
# in; src/extract/resolve.py enforces them again on the stored page dict it is
# handed (those cross deploy boundaries — see src/queue/field_guards.py).
RESOLVE_REDIRECTS_MAX_FIELDS = 8
RESOLVE_REDIRECTS_MAX_LINKS = 20


class ScrapeRequest(BaseModel):
    url: HttpUrl
    render: bool = True
    wait_until: WaitUntil = "domcontentloaded"
    wait_for_selector: str | None = None
    timeout_ms: int | None = Field(
        default=None,
        description=(
            "Navigation budget in ms (default REQUEST_TIMEOUT_MS). Applied to each "
            "phase separately — the warmup navigation, the page load and "
            "wait_for_selector — so a request using all three can cost three times "
            "this. It is a ceiling, not a promise: an attempt that would not finish "
            "inside the server's per-task deadline is shortened, and `warnings` says "
            "so."
        ),
    )

    device: Device = Field(
        default="desktop",
        description=(
            "Which identity the browser presents. `desktop` and `mobile` are "
            "the ordinary ones. `legacy_wap` claims a 2004 feature phone and "
            "suppresses Client Hints, because some sites serve a "
            "JavaScript-free layout to legacy User-Agents -- Google's "
            "`/wml/search` returns real `/url?q=` links to it instead of the "
            "opaque `/goto?url=` stubs its JS results page ships. REQUIRES "
            "`render: false` (422 otherwise: rendering runs the JavaScript this "
            "identity claims not to have). Not supported on camoufox, which "
            "owns its own fingerprint, and cannot be pinned to a session, whose "
            "login runs JS. Read `meta.applied_user_agent` for what was sent."
        ),
    )
    viewport: Viewport | None = Field(
        default=None,
        description=(
            "Browser viewport size in CSS pixels; window.screen is set to match. "
            "Defaults to 1920x1080 for desktop, the mobile preset's size for "
            "device='mobile', and 176x208 for device='legacy_wap'. Applies to "
            "chromium and camoufox."
        ),
    )
    headers: Dict[str, str] | None = Field(
        default=None,
        description=(
            "Extra HTTP headers for the request. `User-Agent` is ignored on "
            "every engine: the engine states the only UA its transport can "
            "back, so a header that contradicts it is a bot tell rather than a "
            "disguise. Read `meta.applied_user_agent` for what was actually "
            "sent."
        ),
    )
    cookies: list[Cookie] | None = None

    proxy_type: ScrapeProxyType = "none"
    proxy_pool_id: str | None = None
    proxy_geo: ProxyGeo | None = None
    prem_proxy_options: PremProxyOptions | None = Field(
        default=None,
        description="Targeting for proxy_type='prem_res_rotating'. Ignored for other types.",
    )

    max_retries: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description=(
            "Max fetch attempts for this request when a failure looks like a "
            "proxy issue or a captcha/block (the proxy is rotated each attempt). "
            "1 means a single attempt with no retry. Ignored for direct "
            "(proxy_type=none) requests, which never retry. When unset, the "
            "server's MAX_RETRIES default is used."
        ),
    )

    session_id: str | None = Field(
        default=None,
        description=(
            "Use a previously authenticated server-side session. When set, "
            "the server pulls cookies + storage_state from the session and "
            "writes any new state back. Mutually exclusive with `cookies`. "
            "Must match the session's pinned device/proxy_type/proxy_pool_id/proxy_geo."
        ),
    )

    block_assets: bool | None = Field(
        default=None,
        description=(
            "Block images / fonts / media during page load for speed. "
            "If unset, falls back to the BLOCK_ASSETS env var. Turn off when "
            "capturing screenshots if images are needed."
        ),
    )
    raw_html: bool = Field(
        default=False,
        description="Include the full post-render HTML in the response.",
    )
    formats: list[OutputFormat] | None = Field(
        default=None,
        description=(
            "Requested output formats. When omitted, legacy behaviour applies "
            "(driven by the raw_html / screenshot booleans). When set, the "
            "effective outputs are the union of this list and those booleans, "
            "so existing callers are unaffected. 'markdown' / 'fit_markdown' "
            "are the new LLM-ready outputs."
        ),
    )
    markdown_options: MarkdownOptions | None = Field(
        default=None,
        description="Tuning for markdown / fit_markdown output. Ignored unless a markdown format is produced.",
    )
    extract: ExtractRule | None = Field(
        default=None,
        description=(
            "Optional structured extraction. When set, the response will "
            "include a 'data' object keyed by the names you chose, with the "
            "extracted values. Much cheaper than downloading raw_html and "
            "parsing yourself."
        ),
    )
    screenshot: bool = Field(
        default=False,
        description=(
            "Capture a full-page PNG screenshot (base64-encoded in the "
            "response). Triggers a scroll pass to load lazy images unless "
            "block_assets is on."
        ),
    )
    element_selector: str | None = Field(
        default=None,
        max_length=2048,
        description=(
            "Optional CSS selector. When set together with `screenshot=true`, "
            "the returned `screenshot_base64` captures only the matching "
            "element (plus 24px padding) instead of the full page. If the "
            "selector does not match, is invalid, or matches a zero-size "
            "element, the response falls back to the full-page screenshot "
            "and `element_screenshot_status` reports the reason. Ignored "
            "when `screenshot=false`."
        ),
    )
    stealth: bool = Field(
        default=True,
        description=(
            "Apply playwright-stealth patches (navigator.webdriver, WebGL / "
            "Canvas fingerprint, chrome runtime) to reduce bot detection."
        ),
    )
    browser_engine: BrowserEngine = Field(
        default="chromium",
        description=(
            "Rendering engine. 'camoufox' is an anti-detect Firefox used for "
            "engines that fingerprint Chromium (e.g. Yandex SmartCaptcha)."
        ),
    )
    headless: bool | None = Field(
        default=None,
        description=(
            "Launch mode for this request. Unset (default) uses the server's "
            "HEADLESS default; true forces headless; false forces headful "
            "(headful needs an X display — the container runs Xvfb). This is "
            "independent of `warmup`, which controls session warm-up, not how "
            "the browser launches. Asking for the non-default mode launches a "
            "throwaway browser for this request instead of using the warm one, "
            "so it costs ~1-2s extra and no idle RAM."
        ),
    )
    humanize: bool = Field(
        default=False,
        description="Camoufox only: human-like cursor movement. No-op on other engines.",
    )
    spoof_os: SpoofOS | None = Field(
        default=None,
        description="Camoufox only: spoof the OS fingerprint. No-op on other engines.",
    )
    fingerprint_profile: FingerprintProfile | None = Field(
        default=None,
        description=(
            "Camoufox only: how close the fingerprint sits to the machine this "
            "server runs on. No-op on other engines. Unset means 'auto', which "
            "follows CAMOUFOX_FINGERPRINT_PROFILE. A profile states the OS and "
            "the WebGL vendor pair, and nothing else. 'windows_on_host' claims "
            "Windows on the host's GPU vendor; 'host' claims the host OS "
            "honestly, which measured WORSE here than claiming Windows; "
            "'random' restores Camoufox's uniform windows/macos/linux draw with "
            "nothing pinned; 'windows'/'macos'/'linux' state the OS and leave "
            "the GPU to Camoufox, which is exactly what `spoof_os` does. Given "
            "together with `spoof_os` the two must claim the same OS."
        ),
    )
    block_webgl: bool = Field(
        default=False,
        description="Camoufox only: disable WebGL. No-op on other engines.",
    )
    addons: list[str] | None = Field(
        default=None,
        description="Camoufox only: Firefox addon ids/paths to load (e.g. uBlock Origin).",
    )
    warmup: WarmupOptions | None = Field(
        default=None,
        description="Optional pre-navigation warmup (visit origin + dwell). Off when unset.",
    )
    resolve_redirects: list[str] | None = Field(
        default=None,
        max_length=RESOLVE_REDIRECTS_MAX_FIELDS,
        description=(
            "Names of extracted fields whose values are the page's OWN redirect "
            "stubs and should be resolved to the destinations they hide, e.g. "
            "['links'] on a Google SERP, where every result href is an opaque "
            "`/goto?url=` token that only a GET on the stub (redirects not "
            "followed, destination in `Location`) can recover. Off when unset. "
            "Only values on the page's own host are ever requested — a value on "
            "another host is already a destination — and every request dials "
            "through the same egress guard the browser does, from this host "
            "directly, not through the scrape's proxy. A Location that is a block "
            "page, or is still on the page's own host, or is the same stub on "
            "another host, is refused; a stub that cannot be resolved stays as it "
            "was. `warnings` carries counts and HTTP statuses, never a URL. At "
            f"most {RESOLVE_REDIRECTS_MAX_LINKS} stubs per request are resolved."
        ),
    )
    preset_meta: PresetMeta | None = Field(
        default=None,
        description=(
            "Internal — populated by the preset materializer, not by direct "
            "callers. Echoes which preset/locale/version produced this "
            "request so the worker can surface it on the response meta."
        ),
    )
    parser_plan: ParserPlan | None = Field(
        default=None,
        description=(
            "Internal — populated by the preset materializer. Carries the "
            "self-heal / AI-extraction config the worker runs after render."
        ),
    )

    @model_validator(mode="after")
    def _validate_engine_device(self) -> "ScrapeRequest":
        if self.device == "legacy_wap" and self.render is not False:
            # `render` IS `java_script_enabled` on the context, so the default
            # ships a 2004 feature phone that executes modern JavaScript --
            # the contradiction #123 and #125 removed from the User-Agent, one
            # layer down, and this one the site can test for directly. The
            # layout this device exists for is the JS-free one, so there is
            # nothing to render either.
            raise ValueError(
                "device='legacy_wap' requires render=false: the identity "
                "claims a phone that predates JavaScript, and rendering would "
                "run it anyway -- a contradiction the page can measure. The "
                "no-JS layout it exists for needs no rendering."
            )
        if self.device == "legacy_wap" and self.browser_engine == "camoufox":
            raise ValueError(
                "device='legacy_wap' is not supported with "
                "browser_engine='camoufox': Camoufox owns its own fingerprint "
                "and would keep its Firefox identity while the request claimed "
                "a feature phone -- the contradiction this device exists to "
                "avoid. Use chromium, firefox or webkit."
            )
        if self.device == "mobile" and self.browser_engine in ("firefox", "camoufox"):
            raise ValueError(
                f"device='mobile' is not supported with browser_engine='{self.browser_engine}' "
                "(Playwright Firefox has no mobile emulation); use chromium or webkit."
            )
        return self

    @model_validator(mode="after")
    def _validate_camoufox_unsupported(self) -> "ScrapeRequest":
        """Refuse what the Camoufox runner would accept and silently drop.

        `cookies`, `storage_state` (via `session_id`) and `render` are documented
        as accepted-and-ignored there. Silently ignored is worse than
        unsupported: the request returns 200 with real HTML and nothing tells the
        caller their session never reached the browser, so a pinned scrape comes
        back logged OUT and reads as the site having changed. Same precedent as
        device='mobile' above — reject until the runner can honour it.
        """
        if self.browser_engine != "camoufox":
            return self
        unsupported = [
            name for name, used in (
                ("session_id", self.session_id is not None),
                ("cookies", bool(self.cookies)),
                ("render=false", self.render is False),
            ) if used
        ]
        if unsupported:
            raise ValueError(
                f"{', '.join(unsupported)} not supported with "
                "browser_engine='camoufox' (the Camoufox runner accepts and "
                "ignores them, which would silently return content you did not "
                "ask for); use chromium, or drop the option."
            )
        return self

    @model_validator(mode="after")
    def _validate_fingerprint_profile_conflict(self) -> "ScrapeRequest":
        """Two channels naming DIFFERENT operating systems must not both be set.

        Compared by the OS each one claims, not by the literal strings: a first
        draft compared the strings and so rejected `spoof_os='windows'` beside
        `fingerprint_profile='windows_on_host'`, which name the same OS and
        differ only in what else they pin. That turned every `spoof_os` call on
        every Camoufox preset — which state a profile — into a 400.

        A profile that claims no OS (`random`, and `auto` before the server
        default is applied) is not a disagreement: `fingerprint_profile` still
        wins at launch, and `meta.applied_fingerprint` reports what ran, so
        nothing is dropped without the caller being able to see it.
        """
        if self.spoof_os is None or self.fingerprint_profile is None:
            return self
        profile_os = claimed_os(self.fingerprint_profile)
        if profile_os is not None and profile_os != self.spoof_os:
            raise ValueError(
                f"spoof_os={self.spoof_os!r} conflicts with "
                f"fingerprint_profile={self.fingerprint_profile!r}, which claims "
                f"{profile_os!r}; set one of them, or name the same OS in both."
            )
        return self

    @model_validator(mode="after")
    def _validate_prem_zip(self) -> "ScrapeRequest":
        if (
            self.prem_proxy_options is not None
            and self.prem_proxy_options.zip
            and self.proxy_geo is not None
            and (self.proxy_geo.region or self.proxy_geo.city)
        ):
            raise ValueError(
                "prem_proxy_options.zip is mutually exclusive with proxy_geo.region/city"
            )
        return self


class ScrapeMeta(BaseModel):
    url: str
    final_url: str | None = None
    status_code: int | None = None
    device: Device
    proxy_type: ScrapeProxyType
    proxy_pool_id: str | None = None
    retries: int = 0
    fetch_ok: bool = Field(
        default=True,
        description=(
            "Whether the underlying page fetch succeeded. False means the "
            "render failed or was blocked (captcha/timeout) — the response "
            "still carries any partial data, but screenshots/markdown may be "
            "degraded or absent. Defaults True so legacy responses are "
            "unaffected."
        ),
    )
    applied_user_agent: str | None = None
    applied_locale: str | None = None
    applied_timezone: str | None = None
    applied_accept_language: str | None = None
    applied_fingerprint: Dict[str, Any] | None = Field(
        default=None,
        description=(
            "Camoufox only: the fingerprint profile that ran and what it "
            "pinned — keys `profile`, `os`, `webgl_vendor`, `webgl_renderer`. "
            "A profile that degraded — an unknown name, or a GPU vendor "
            "Camoufox's table has no row for — reports what it fell back to. "
            "Null on other engines and on responses that predate profiles."
        ),
    )
    applied_preset: PresetMeta | None = None
    applied_prem_targeting: str | None = Field(
        default=None,
        description=(
            "For proxy_type='prem_res_rotating': the resolved CyberYozh v2 username "
            "targeting suffix (country/region/city/zip/isp/session/ttl/filter tokens, "
            "e.g. 'c-us-filter-iqs-s-ab12cd34'). The account login is stripped, so no "
            "credentials are exposed. Null for other proxy types. On a request that "
            "rotated and failed, this names the LAST exit tried, which is not "
            "necessarily the one that fetched the page being reported."
        ),
    )
    applied_warmup: AppliedWarmup | None = Field(
        default=None,
        description=(
            "What the pre-navigation warmup actually did, if it ran: type, the URL "
            "actually visited (the resolved origin for type='homepage'), and the "
            "dwell. Null when no warmup was requested, it had no usable URL, or it "
            "failed (warmup is non-fatal) — so this reflects what was applied, not "
            "merely what was requested."
        ),
    )


class ScrapeResponse(BaseModel):
    request_id: str
    took_ms: int
    meta: ScrapeMeta
    data: Dict[str, Any] | None = None
    raw_html: str | None = None
    markdown: str | None = Field(
        default=None,
        description="LLM-ready Markdown of the page. Populated when the 'markdown' format is requested.",
    )
    fit_markdown: str | None = Field(
        default=None,
        description="Noise-filtered Markdown (pruning or LLM). Populated when the 'fit_markdown' format is requested.",
    )
    markdown_references: str | None = Field(
        default=None,
        description="References section for ⟨n⟩ citation markers, when markdown_options.citations is on.",
    )
    links: list[str] | None = Field(
        default=None,
        description="Absolute hyperlinks found on the page. Populated when the 'links' format is requested.",
    )
    html: str | None = Field(
        default=None,
        description="Cleaned HTML (boilerplate-stripped per markdown_options). Populated when the 'html' format is requested.",
    )
    screenshot_base64: str | None = None
    element_screenshot_status: ElementScreenshotStatus | None = Field(
        default=None,
        description=(
            "Diagnostic field for the element-screenshot mode. None on "
            "responses produced before the field existed; legacy clients "
            "that never read it are unaffected. `element` means "
            "screenshot_base64 is the element crop. Any `fallback_*` value "
            "means full-page was returned because the element capture "
            "failed for the named reason. `not_requested` means no selector "
            "was passed. `no_screenshot` means screenshot=false or capture "
            "failed entirely (in that case screenshot_base64 is also None)."
        ),
    )
    warnings: list[str] = Field(default_factory=list)
    error: str | None = Field(
        default=None,
        description=(
            "Why the page produced no result (proxy configuration error, "
            "worker timeout, session failure, cancelled job slots). None on "
            "success and on degraded-but-structured fetches (those report via "
            "meta.fetch_ok and warnings). Also duplicated into warnings for "
            "older clients."
        ),
    )


class BatchScrapeRequest(BaseModel):
    pages: list[ScrapeRequest]
    session_id: str | None = Field(
        default=None,
        description=(
            "Apply this session_id to every page in `pages`. Rejected (422) if "
            "any page already has a different session_id."
        ),
    )


class JobCreateResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    done: int
    total: int
    error: str | None = None


class JobResultsResponse(BaseModel):
    job_id: str
    status: JobStatus
    pages: list[ScrapeRequest]
    total: int
    done: int = 0
    error: str | None = None
    results: list[ScrapeResponse | None] | None = None
    unreadable_slots: list[int] | None = Field(
        default=None,
        description=(
            "Indexes whose stored result could not be decoded. Null on every "
            "healthy job — the field is always present, it just carries no "
            "indexes unless something failed to decode. "
            "Such a slot comes back null, which otherwise reads exactly like a "
            "page that has not finished: on a single-page job that made a "
            "finished response look like one still running, and a caller "
            "polling for a non-null result would wait for something that is "
            "never coming."
        ),
    )


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Search query.")
    engine: SearchEngine = Field(
        default="google",
        description="SERP engine: google (default), bing, or yandex.",
    )
    locale: str | None = Field(
        default=None,
        description="Engine locale key (us/uk/de/fr/ru/jp); preset default if unset.",
    )
    limit: int = Field(default=10, ge=1, le=50, description="Max results to return.")
    scrape: bool = Field(
        default=False,
        description="Also scrape each result page and attach the response.",
    )
    scrape_options: Dict[str, Any] | None = Field(
        default=None,
        description=(
            "Forwarded to each result's ScrapeRequest when scrape=true — any "
            "ScrapeRequest field except url, e.g. {\"raw_html\": true} or "
            "{\"formats\": [\"markdown\"]} where supported."
        ),
    )
    # Proxy override for the SERP fetch (and per-result scrapes when scrape=true).
    # When unset, the SERP keeps its engine's own preset's proxy (google ->
    # google_search_chromium; see api/search.py ENGINES). Useful to route the
    # Google fetch through a residential/mobile pool that isn't blocked.
    proxy_type: ScrapeProxyType | None = Field(
        default=None,
        description="Override the SERP preset's proxy_type; preset default if unset.",
    )
    proxy_pool_id: str | None = Field(
        default=None, description="Pin a proxy pool id (with proxy_type)."
    )
    proxy_geo: ProxyGeo | None = Field(
        default=None, description="Pin proxy geo (country_code/region/city)."
    )
    prem_proxy_options: PremProxyOptions | None = Field(
        default=None,
        description="Targeting for proxy_type='prem_res_rotating' on the SERP fetch.",
    )
    browser_engine: BrowserEngine | None = Field(
        default=None,
        description=(
            "Rendering engine override. None (default) means inherit the preset's engine. "
            "'camoufox' is an anti-detect Firefox used for engines that fingerprint Chromium "
            "(e.g. Yandex SmartCaptcha)."
        ),
    )
    max_retries: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description=(
            "Override the SERP preset's max fetch attempts on a proxy/captcha "
            "failure (1-10; 1 = no retry); preset/server default if unset."
        ),
    )
    humanize: bool = Field(
        default=False,
        description="Camoufox only: human-like cursor movement. No-op on other engines.",
    )
    spoof_os: SpoofOS | None = Field(
        default=None,
        description="Camoufox only: spoof the OS fingerprint. No-op on other engines.",
    )
    fingerprint_profile: FingerprintProfile | None = Field(
        default=None,
        description=(
            "Camoufox only: how close the fingerprint sits to the machine this "
            "server runs on. No-op on other engines. Unset means 'auto', which "
            "follows CAMOUFOX_FINGERPRINT_PROFILE. A profile states the OS and "
            "the WebGL vendor pair, and nothing else. 'windows_on_host' claims "
            "Windows on the host's GPU vendor; 'host' claims the host OS "
            "honestly, which measured WORSE here than claiming Windows; "
            "'random' restores Camoufox's uniform windows/macos/linux draw with "
            "nothing pinned; 'windows'/'macos'/'linux' state the OS and leave "
            "the GPU to Camoufox, which is exactly what `spoof_os` does. Given "
            "together with `spoof_os` the two must claim the same OS."
        ),
    )
    block_webgl: bool = Field(
        default=False,
        description="Camoufox only: disable WebGL. No-op on other engines.",
    )
    addons: list[str] | None = Field(
        default=None,
        description="Camoufox only: Firefox addon ids/paths to load (e.g. uBlock Origin).",
    )
    warmup: WarmupOptions | None = Field(
        default=None,
        description="Optional pre-navigation warmup for the SERP fetch. Off when unset.",
    )


class SearchResult(BaseModel):
    url: str
    title: str | None = None
    snippet: str | None = None
    scrape: ScrapeResponse | None = Field(
        default=None, description="Per-result scrape response when scrape=true."
    )


class SearchResponse(BaseModel):
    query: str
    count: int
    results: list[SearchResult]
    took_ms: int = 0  # wall-clock time of the search (SERP + optional scrapes)
    warnings: list[str] = Field(default_factory=list)
