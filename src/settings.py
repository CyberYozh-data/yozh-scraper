from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import Field, SecretStr

from src.observability.log_context import install_record_factory
from src.observability.log_format import JsonFormatter, TextFormatter
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # =====================
    # Service
    # =====================
    host: str = Field(
        default="0.0.0.0",
        alias="HOST",
        description="Host address to bind the HTTP server to.",
    )

    port: int = Field(
        default=8000,
        alias="PORT",
        description="Port number for the HTTP server.",
    )

    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL",
        description="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )

    # =====================
    # Playwright
    # =====================
    headless: bool = Field(
        default=True,
        alias="HEADLESS",
        description=(
            "Default browser launch mode. A request's `headless` field "
            "overrides it per scrape; a request asking for the non-default "
            "mode gets a throwaway browser, so the warm pool never holds both "
            "modes. Xvfb runs regardless, so headful requests work even when "
            "this is true."
        ),
    )
    chrome_channel: str | None = Field(
        default=None,
        alias="CHROME_CHANNEL",
        description=(
            "Chromium engine channel. None (default) uses Playwright's bundled "
            "Chromium. Set to 'chrome' to drive a real Google Chrome installed in "
            "the image (better anti-bot: real branding/codecs + a populated "
            "navigator.plugins, and a newer engine than the bundled build) — "
            "requires `playwright install chrome` in the Dockerfile. The UA major "
            "auto-tracks whatever engine actually launches."
        ),
    )
    block_assets: bool = Field(
        default=True,
        alias="BLOCK_ASSETS",
        description="Block images, fonts and media to speed up scraping.",
    )
    request_timeout_ms: int = Field(
        default=30_000,
        alias="REQUEST_TIMEOUT_MS",
        description="Default page request timeout in milliseconds.",
    )
    warmup_dwell_ms: int = Field(
        default=2500,
        alias="WARMUP_DWELL_MS",
        description="Default dwell (ms) on the origin during a 'homepage' warmup before the real navigation.",
    )
    max_retries: int = Field(
        default=3,
        alias="MAX_RETRIES",
        description=(
            "Maximum number of fetch attempts per request when a failure looks "
            "like a proxy issue. Only applies when a proxy is used; direct "
            "connections never retry."
        ),
    )
    camoufox_geoip: bool = Field(
        default=True,
        alias="CAMOUFOX_GEOIP",
        description=(
            "Let Camoufox resolve the exit IP and align locale/timezone to it. "
            "On by default: the alignment is a fingerprint benefit and it is "
            "cheaper than it first looks. camoufox caches the lookup per process "
            "under the proxy URL, and the prem gateway issues a CONSTANT username "
            "(targeting only, no per-lease session token), so it resolves once "
            "per worker and once per distinct targeting — measured 238ms cold, "
            "65ms warm, one cache entry. A proxy that cannot be reached is the "
            "exception: that raises instead of caching, so a broken exit pays "
            "~280ms on every request and then fails to launch. Turning this off "
            "gives up more than the lookup — camoufox only spoofs webrtc:ipv4 "
            "when geoip resolved an address, so it is a fingerprint defence too."
        ),
    )
    camoufox_disable_ubo: bool = Field(
        default=False,
        alias="CAMOUFOX_DISABLE_UBO",
        description=(
            "Launch Camoufox WITHOUT uBlock Origin. Off by default, because uBO "
            "is one of the things that makes Camoufox look like a real user's "
            "browser and removing it is a fingerprint cost. It exists because "
            "uBO is not inert on the pages we scrape: it ships default-enabled "
            "filters that REWRITE them. Measured 2026-09-04 on a live Bing SERP "
            "-- `bing.com##+js(href-sanitizer, a[href*=\"bing.com/ck/a\"], ?u "
            "/^a1(.*)$/ -base64)` in uBO's Privacy list rewrote all 10 organic "
            "anchors from Bing's `ck/a?...u=a1<base64>` wrapper to the decoded "
            "destination, so `bing_search_camoufox`'s titles/links selector, "
            "which keys on `u=a1`, matched 0 of 10 with uBO on and 10 of 10 "
            "with it off. uBO also rewrites Google's instrumentation "
            "(`www.google.*##+js(set, rwt, noopFunc)`, `||google.com/gen_204?`), "
            "which is why this switch is worth having. (That sentence used to "
            "add 'while google_search_camoufox is blocked 48/48' -- reversed on "
            "2026-09-18: camoufox is now the twin that answers Google and chromium "
            "is the refused one.) Note uBO is downloaded at first browser "
            "LAUNCH, not at image build, so its filter lists change under a "
            "container recreate with nothing in this repo recording it."
        ),
    )
    camoufox_fingerprint_profile: str = Field(
        default="windows_on_host",
        alias="CAMOUFOX_FINGERPRINT_PROFILE",
        description=(
            "What `fingerprint_profile='auto'` resolves to — i.e. what a request "
            "that says nothing gets. Camoufox otherwise picks its OS uniformly "
            "from windows/macos/linux per launch and draws the GPU and thread "
            "count from its own corpus, so two runs in three claim an OS this "
            "machine is not, on hardware belonging to a third. Ships as "
            "'windows_on_host': the most common desktop OS, wearing this host's "
            "GPU vendor. Measured on yandex_search, 20 runs per arm: a Windows "
            "claim took 1 and 8 retries where a Linux one took 15 and 23, so "
            "'host' — claiming this Linux host honestly — is the worst option "
            "rather than the safest. 'random' restores the old uniform draw. "
            "Setting 'auto' here is circular and is treated as 'random'."
        ),
    )
    host_gpu_vendor: str | None = Field(
        default=None,
        alias="HOST_GPU_VENDOR",
        description=(
            "Override the GPU vendor claimed by BOTH engines: Camoufox's "
            "host-aligned profiles and Chromium's stealth WebGL override. "
            "amd, intel, nvidia or apple. Unset means infer it from the CPU "
            "vendor in /proc/cpuinfo, which is right on integrated graphics and "
            "wrong on a discrete card — that is what this is for. The container "
            "cannot see the GPU itself. Chromium only has Chrome-shaped strings "
            "for amd and intel; nvidia and apple fall back to intel there (and "
            "log a warning) while Camoufox still claims them, so on such a host "
            "the two engines describe different GPUs."
        ),
    )
    webrtc_block: bool = Field(
        default=True,
        alias="WEBRTC_BLOCK",
        description=(
            "Prevent WebRTC from leaking the real IP around the proxy by "
            "launching Chromium with --webrtc-ip-handling-policy=disable_"
            "non_proxied_udp. Set to false only if you need WebRTC features "
            "(video chat, RTCPeerConnection, etc.) inside scraped pages."
        ),
    )
    software_webgl: bool = Field(
        default=True,
        alias="SOFTWARE_WEBGL",
        description=(
            "Launch Chromium with --enable-unsafe-swiftshader so pages get a "
            "software WebGL context. On a GPU-less host a headful Chrome >= 136 "
            "otherwise hands every page a NULL context, which anti-bots read as "
            "a bot: real desktop Chrome effectively always has one. The cost is "
            "that SwiftShader JITs page-supplied shaders inside a GPU process "
            "this service does not sandbox, so set it to false to trade the "
            "fingerprint back for that isolation. No effect on Camoufox, "
            "Firefox or WebKit."
        ),
    )

    # =====================
    # Workers
    # =====================
    workers: int = Field(
        default=2,
        alias="WORKERS",
        description=(
            "taskiq worker processes per scraper-worker container (the "
            "`--workers` flag); also reported by /health for visibility."
        ),
    )
    queue_maxsize: int = Field(
        default=200,
        alias="QUEUE_MAXSIZE",
        description="Submit backpressure: reject new jobs once the Redis stream depth exceeds this.",
    )
    job_result_ttl_s: float = Field(
        default=600.0,
        ge=0,
        alias="JOB_RESULT_TTL_S",
        description=(
            "Seconds a finished job's keys (status + results) live in Redis "
            "after finalization, via a native key TTL. 0 disables the TTL. "
            "Keep comfortably above how long a consumer takes to fetch /results."
        ),
    )
    browser_idle_shutdown_s: float = Field(
        default=600.0,
        ge=0,
        alias="BROWSER_IDLE_SHUTDOWN_S",
        description=(
            "Seconds of inactivity after which a worker closes its Chromium "
            "instance to free memory. The browser is re-launched lazily on "
            "the next job (cold start adds ~1-2s of latency to that one "
            "job). Set to 0 to keep browsers warm forever — lower first-job "
            "latency at the cost of ~400-500 MB RSS per worker held "
            "permanently."
        ),
    )
    browser_idle_shutdown_s_secondary: int = Field(
        default=120,
        alias="BROWSER_IDLE_SHUTDOWN_S_SECONDARY",
        description=(
            "Idle window (seconds) for non-default native engines (e.g. a warm "
            "Firefox). Shorter than the primary so secondary engines reclaim RAM "
            "quickly. 0 disables."
        ),
    )

    # =====================
    # Queue (taskiq + Redis)
    # =====================
    redis_url: str = Field(
        default="redis://redis:6379/0",
        alias="REDIS_URL",
        description="Redis connection URL (broker, job store, session store).",
    )
    page_task_timeout_s: float = Field(
        default=120.0,
        alias="PAGE_TASK_TIMEOUT_S",
        description=(
            "Hard ceiling for one scrape_page task (all retry attempts "
            "included). Even a hung Playwright produces an error slot within "
            "this bound. Replaces the old JOB_TIMEOUT_MS role."
        ),
    )
    login_task_timeout_s: float = Field(
        default=45.0,
        alias="LOGIN_TASK_TIMEOUT_S",
        description="Hard ceiling for the worker's login task (the actual login work).",
    )
    login_result_grace_s: float = Field(
        default=15.0,
        ge=0,
        alias="LOGIN_RESULT_GRACE_S",
        description=(
            "Extra time the login endpoint waits for the result beyond "
            "LOGIN_TASK_TIMEOUT_S. The worker's timeout is measured from when it "
            "picks the task up, the API's from enqueue; this grace absorbs the "
            "queue-pickup skew so a slow-but-successful login isn't 504'd (which "
            "would mark the session failed and drop its storage_state)."
        ),
    )
    reclaim_idle_s: float = Field(
        default=240.0,
        alias="RECLAIM_IDLE_S",
        description=(
            "Pending stream entries idle longer than this are reclaimed and "
            "re-enqueued (worker died mid-task). Keep ~2x PAGE_TASK_TIMEOUT_S."
        ),
    )
    browser_max_pages: int = Field(
        default=100,
        alias="BROWSER_MAX_PAGES",
        description=(
            "Browser retirement: a worker recycles its browser after this many "
            "pages to bound Chromium memory bloat. 0 disables."
        ),
    )

    # =====================
    # Sessions (Phase 1)
    # =====================
    sessions_max: int = Field(
        default=256,
        alias="SESSIONS_MAX",
        description="Soft global cap on simultaneous sessions (Redis ZSET-backed). LRU-evicted on overflow.",
    )
    session_storage_state_max_bytes: int = Field(
        default=2 * 1024 * 1024,
        alias="SESSION_STORAGE_STATE_MAX_BYTES",
        description="Per-session storage_state byte cap. Rejected on overflow.",
    )

    log_format: Literal["text", "json"] = Field(
        default="text",
        alias="LOG_FORMAT",
        description=(
            "Log line shape. TEXT by default, deliberately: the containers use "
            "the json-file driver with no shipper, so nothing on this host can "
            "query JSON today and the only reader is a human at `docker logs`, "
            "for whom quoted keys and escaped tracebacks are strictly worse. "
            "Two of the four long-lived processes could not emit JSON anyway "
            "(the scheduler and the taskiq worker master configure their own "
            "logging), so a JSON default would advertise a guarantee this "
            "deployment cannot keep. Flip it the day a shipper lands. "
            "Correlation and event names are emitted in BOTH shapes."
        ),
    )

    # =====================
    # Service-to-service auth
    # =====================
    service_token: SecretStr | None = Field(
        default=None,
        alias="SERVICE_TOKEN",
        description=(
            "Shared secret guarding the internal endpoints: "
            "GET /api/v1/proxies/resolve, the /api/v1/sessions router and the "
            "/api/v2/prem-proxies catalog. Callers on the internal network "
            "(the crawler's /map, yozh-law-checker) present it as the "
            "X-Service-Token header. When unset, those endpoints refuse to "
            "serve rather than leak (fail-closed) — note that for prem-proxies "
            "the refusal silently strips country geo from every yozh scan."
        ),
    )

    # Plain `str`, not `list[str]`: pydantic-settings JSON-parses list-typed
    # env vars, so `EGRESS_ALLOW_HOSTS=127.0.0.1` would fail to parse rather
    # than yield one entry.
    egress_transport_guard: bool = Field(
        default=True,
        alias="EGRESS_TRANSPORT_GUARD",
        description=(
            "Route direct-path browser traffic through a local proxy that "
            "refuses non-public targets, so a blocked request is never made "
            "rather than having its content withheld. ON, because the layers "
            "that remain without it leave a real hole: a redirect into the "
            "internal network still fires one GET, and Chromium's favicon "
            "request is issued below the level a route handler can see. "
            "It does change behaviour on the direct path — a proxied Chromium "
            "context is TCP-only so HTTP/3 never negotiates, and plain-HTTP "
            "forwards cost about ten times the connections. Measured on this "
            "host, that costs nothing observable: 25/30 ok in both arms, and "
            "the only target that blocks (Google) blocks the host IP with the "
            "guard OFF too — so on the direct path the fingerprint argument "
            "protects a capability this host does not have. Set false to fall "
            "back to the pre-flight, route guard, redirect-chain and read-time "
            "checks, which all still apply."
        ),
    )
    egress_allow_hosts: str = Field(
        default="",
        alias="EGRESS_ALLOW_HOSTS",
        description=(
            "Comma-separated hostnames / IP literals that browser navigation "
            "may reach even though they are not public addresses. Empty by "
            "default. Matching is exact — there is no wildcard and no global "
            "off-switch, because a guard with an off-switch is a guard that "
            "silently does nothing. Needed only by the loopback e2e tests."
        ),
    )

    # =====================
    # Proxy (CyberYozh)
    # =====================
    cyberyozh_api_key: SecretStr | None = Field(
        default=None,
        alias="CYBERYOZH_API_KEY",
        description="API key for CyberYozh proxy service.",
    )
    cyberyozh_base_url: str = Field(
        default="https://app.cyberyozh.com",
        alias="CYBERYOZH_BASE_URL",
        description="Base URL for CyberYozh API.",
    )

    # =====================
    # LLM (presets)
    # =====================
    openai_api_key: SecretStr | None = Field(
        default=None,
        alias="OPENAI_API_KEY",
        description="Server-side OpenAI key for openai/* models.",
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        alias="ANTHROPIC_API_KEY",
        description="Server-side Anthropic key for anthropic/* models.",
    )
    gemini_api_key: SecretStr | None = Field(
        default=None,
        alias="GEMINI_API_KEY",
        description="Server-side Gemini key for gemini/* models.",
    )
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        alias="OPENROUTER_API_KEY",
        description="Server-side OpenRouter key for openrouter/* models.",
    )
    custom_llm_base_url: str | None = Field(
        default=None,
        alias="CUSTOM_LLM_BASE_URL",
        description="OpenAI-compatible base URL for custom/* models.",
    )
    custom_llm_api_key: SecretStr | None = Field(
        default=None,
        alias="CUSTOM_LLM_API_KEY",
        description="API key for the custom OpenAI-compatible endpoint.",
    )
    default_llm_model: str = Field(
        default="openai/gpt-5.4-mini",
        alias="DEFAULT_LLM_MODEL",
        description="Model used when a preset/request doesn't specify one.",
    )
    default_proxy_country: str = Field(
        default="GB",
        alias="DEFAULT_PROXY_COUNTRY",
        description=(
            "ISO-3166 alpha-2 country pinned for residential rotating proxies "
            "(res_rotating / prem_res_rotating) when a request supplies no "
            "proxy_geo.country_code. Without it the exit lands in a random "
            "country while the browser timezone/locale stay this country's "
            "defaults — a fingerprint mismatch. GB maps to Europe/London + "
            "en-GB, so the exit and browser timezone/locale stay aligned. GB "
            "is the default rather than US because several targets throttle "
            "or block the US residential exit range."
        ),
    )
    preset_llm_max_tokens: int = Field(
        default=4000,
        alias="PRESET_LLM_MAX_TOKENS",
        description="Token ceiling for a single self-heal / extraction call.",
    )
    preset_llm_timeout_s: float = Field(
        default=30.0,
        alias="PRESET_LLM_TIMEOUT_S",
        description=(
            "Hard timeout for a single LLM call. Bounds the in-worker "
            "self-heal/extract round-trip so a hung provider socket can't "
            "wedge a worker process past the job timeout."
        ),
    )



settings = Settings()


class LogTagFilter(logging.Filter):
    def __init__(self, tag: str) -> None:
        super().__init__()
        self.tag = tag

    def filter(self, record: logging.LogRecord) -> bool:
        record.tag = self.tag
        return True


def setup_logging(level: str = "INFO", tag: Optional[str] = None) -> None:
    tag = tag or "M"
    level_value = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level_value)

    for _handler in list(root.handlers):
        root.removeHandler(_handler)

    # Correlation first: the factory has to be in place before anything logs,
    # and it is idempotent so repeated calls (tests, a re-init) cannot stack.
    install_record_factory()

    handler = logging.StreamHandler()
    handler.setLevel(level_value)
    handler.setFormatter(
        JsonFormatter() if settings.log_format == "json" else TextFormatter()
    )
    # The filter stays on the SAME handler as the formatter. That pairing is
    # load-bearing: the old formatter interpolated %(tag)s, so a record that
    # reached it without one raised `ValueError: Formatting field not found in
    # record: 'tag'` and the line was LOST outright. Both renderers now default
    # the tag as well, so the pairing is belt and braces rather than the only
    # thing standing between a record and the floor.
    handler.addFilter(LogTagFilter(tag))
    root.addHandler(handler)

    for loger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _logger = logging.getLogger(loger_name)
        _logger.handlers = []
        _logger.propagate = True
        _logger.setLevel(level_value)

    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)
