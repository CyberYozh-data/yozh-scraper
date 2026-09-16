from __future__ import annotations

import re

from datetime import date
from typing import Any, Literal
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


ScrapeProxyType = Literal[
    "none", "mobile_shared", "mobile", "res_static", "res_rotating", "dc_static",
    "prem_res_rotating",
]
# Mirrored from the scraper, which is the only side that reads it: the
# crawler forwards `device` verbatim, so a value the scraper accepts and
# this literal has not heard of is refused here -- a 422 about a field the
# crawler does not use. `legacy_wap` was exactly that.
Device = Literal["desktop", "mobile", "legacy_wap"]
WaitUntil = Literal["domcontentloaded", "load", "networkidle"]
ExtractType = Literal["css", "xpath"]
# The scraper's post-process vocabulary, mirrored so an unknown op is a 422 at
# `POST /crawl` rather than a per-page failure 500 pages in. `tests/
# test_crawler_schema_parity.py` (scraper side) fails if the two lists drift.
PostProcessOp = Literal[
    "regex", "strip", "strip_tags", "parse_int", "parse_float", "parse_price",
    "lowercase", "uppercase", "replace", "base64_decode", "urljoin",
    "null_if_regex", "unwrap_param",
]
ScopeMode = Literal["same-domain", "subdomains", "all", "regex"]
CrawlJobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class ProxyGeo(BaseModel):
    country_code: str | None = None
    region: str | None = None
    city: str | None = None


class PremProxyOptions(BaseModel):
    """Targeting for proxy_type='prem_res_rotating'. Pure passthrough: the
    scraper (via /scrape or /proxies/resolve) validates/applies these — the
    crawler only forwards them. All optional."""
    sub_user_id: str | None = None
    ip_filter: str | None = None
    zip: str | None = None
    isp: str | None = None
    session_type: str | None = None
    sticky_id: str | None = None
    rotation_minutes: int | None = None
    protocol: str | None = None


class Cookie(BaseModel):
    name: str
    value: str
    domain: str | None = None
    path: str | None = "/"
    expires: int | None = None
    httpOnly: bool | None = None
    secure: bool | None = None
    sameSite: Literal["Strict", "Lax", "None"] | None = None


class PostProcess(BaseModel):
    op: PostProcessOp
    args: list[Any] = Field(default_factory=list)


class FieldRule(BaseModel):
    selector: str
    # `type` and `post_process` for the same reason `container` is here: the
    # crawler reads neither, and `extra="ignore"` threw both away. A crawl
    # asking for a per-field XPath got the rule's CSS instead, and one asking
    # for `parse_price` got the raw string -- both silently, in the response's
    # own data, where a reader has nothing to tell them the recipe was not run.
    type: ExtractType | None = None
    attr: str = "text"
    all: bool = False
    required: bool = False
    post_process: list[PostProcess] = Field(default_factory=list)


class ExtractRule(BaseModel):
    type: ExtractType
    # Mirrored verbatim, not because the crawler reads it but because it does
    # not: pydantic's default `extra="ignore"` dropped it, and `fetcher.py`
    # forwards what survives. A crawl asking for row scoping then got
    # document-scoped extraction with every field `all: true` and no warning
    # at all -- the exact misalignment the scraper added `container` to
    # remove, reintroduced at the one boundary that does not validate.
    container: str | None = None
    fields: dict[str, FieldRule]


class ScrapeOptions(BaseModel):
    """Fields forwarded verbatim to scraper's POST /scrape/page. url is injected per-page from Frontier."""
    proxy_type: ScrapeProxyType = "none"
    proxy_pool_id: str | None = None
    proxy_geo: ProxyGeo | None = None
    prem_proxy_options: PremProxyOptions | None = None

    device: Device = "desktop"
    headers: dict[str, str] | None = None
    cookies: list[Cookie] | None = None
    stealth: bool = True
    block_assets: bool | None = None
    render: bool = True
    wait_until: WaitUntil = "domcontentloaded"
    wait_for_selector: str | None = None
    timeout_ms: int | None = None

    screenshot: bool = False
    extract: ExtractRule | None = None

    session_id: str | None = None

    @model_validator(mode="after")
    def _legacy_wap_needs_no_render(self) -> "ScrapeOptions":
        # Mirrored like `container` and the `Device` literal, and for the same
        # reason: the crawler forwards these two and reads neither, so without
        # the rule a crawl asking for `legacy_wap` is refused by the scraper on
        # EVERY page -- a non-retryable 422 per fetch that also charges the
        # proxy session an error apiece -- instead of once at `POST /crawl`.
        if self.device == "legacy_wap" and self.render:
            raise ValueError(
                "device='legacy_wap' requires render=false: the identity claims "
                "a phone that predates JavaScript, and the no-JS layout it "
                "exists for needs no rendering"
            )
        return self


class CrawlScope(BaseModel):
    mode: ScopeMode = "same-domain"
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)

    max_depth: int = 3
    max_pages: int = 500
    per_domain_rps: float = 1.0
    per_domain_concurrency: int = 1

    @field_validator("include_patterns", "exclude_patterns")
    @classmethod
    def _patterns_compile(cls, patterns: list[str]) -> list[str]:
        # Rejected at the boundary; the scope filter compiles them again inside
        # the worker task, where `re.error` is far more expensive (audit H-15).
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return patterns


class CrawlRequest(BaseModel):
    seed_url: HttpUrl
    scope: CrawlScope = Field(default_factory=CrawlScope)
    scrape_options: ScrapeOptions = Field(default_factory=ScrapeOptions)
    crawl_proxy: ScrapeOptions | None = Field(
        default=None,
        description=(
            "Proxy used when enable_scraping=false (cheap discovery-only crawl). "
            "Only proxy_type/proxy_pool_id/proxy_geo/prem_proxy_options are used; other fields are ignored. "
            "When enable_scraping=true the proxy inside scrape_options is used instead. "
            "If crawl_proxy is null, the scrape_options proxy is used regardless of the mode."
        ),
    )
    enable_scraping: bool = Field(
        default=False,
        description=(
            "If false, crawler discards raw_html / screenshot / extracted data after link extraction — "
            "results contain only url/parent/depth/status. If true, the full ScrapeResponse is kept."
        ),
    )


class CrawlStats(BaseModel):
    visited: int = 0
    queued: int = 0
    failed: int = 0
    dedup_skipped: int = 0
    out_of_scope: int = 0
    # Hrefs the link step could not canonicalise or dedup (skipped one by one).
    links_dropped: int = 0
    retries_total: int = 0
    started_at: float | None = None
    finished_at: float | None = None


class CrawlPageRecord(BaseModel):
    url: str
    parent_url: str | None
    depth: int
    fetched_at: float
    took_ms: int
    status_code: int | None
    scrape_response: dict[str, Any] | None = None
    error: str | None = None


class CrawlJobRecord(BaseModel):
    job_id: str
    status: CrawlJobStatus
    request: CrawlRequest
    stats: CrawlStats
    pages: list[CrawlPageRecord] = Field(default_factory=list)
    error: str | None = None


class JobCreateResponse(BaseModel):
    job_id: str


class CancelResponse(BaseModel):
    job_id: str
    cancelled: bool
    hard: bool


class MapRequest(BaseModel):
    seed_url: HttpUrl
    scope: CrawlScope = Field(
        default_factory=CrawlScope,
        description="Reuses the crawl scope (mode + include/exclude patterns) to filter results. max_depth/rps fields are ignored — /map is a single fast pass.",
    )
    include_sitemap: bool = Field(default=True, description="Discover URLs from robots.txt + sitemap.xml.")
    include_page_links: bool = Field(default=True, description="Discover URLs from <a> links on the seed page.")
    render: bool = Field(
        default=False,
        description="Fetch the seed page through the scraper (JS render) instead of a plain httpx GET. Slower but sees SPA links. Only affects page-link discovery.",
    )
    search: str | None = Field(
        default=None,
        description="Case-insensitive substring; keep only URLs containing it. Applied during sitemap collection too (against the raw sitemap loc, pre-canonicalization), so the MAP_MAX_URLS cap counts matches.",
    )
    published_after: date | None = Field(
        default=None,
        description="Keep only URLs whose sitemap <lastmod> is on/after this date (YYYY-MM-DD). URLs without a lastmod are dropped. Sitemap-sourced URLs only.",
    )
    recent_days: int | None = Field(
        default=None,
        ge=1,
        le=36_525,  # ~100 years; bounded so today()-timedelta(days=N) can't OverflowError
        description="Convenience for published_after: keep URLs modified within the last N days (max ~100 years). Ignored if published_after is set.",
    )
    sort: Literal["newest"] | None = Field(
        default=None,
        description="'newest' sorts results by <lastmod> descending; URLs without a lastmod sort last. Default keeps discovery order.",
    )
    limit: int = Field(default=1000, ge=1, le=200_000, description="Max URLs to return.")
    proxy_type: ScrapeProxyType = Field(
        default="none",
        description="Route the robots/sitemap/seed fetches through this proxy (resolved by the scraper). 'none' = direct.",
    )
    proxy_pool_id: str | None = None
    proxy_geo: ProxyGeo | None = None
    prem_proxy_options: PremProxyOptions | None = Field(
        default=None,
        description="Targeting for proxy_type='prem_res_rotating' (IP filter / sticky / ISP / ZIP / sub-user).",
    )


class MapStats(BaseModel):
    from_sitemap: int = 0  # raw URLs seen in sitemaps (pre scope/dedup)
    from_page: int = 0  # raw <a> links on the seed page (pre scope/dedup)
    unique_in_scope: int = 0  # unique, in-scope URLs before the search filter
    with_lastmod: int = 0  # returned URLs that carry a sitemap <lastmod>


class MapResponse(BaseModel):
    seed_url: str
    count: int
    urls: list[str]
    # Additive: url -> raw <lastmod> for returned URLs that have one (sitemap
    # source only). Absent keys = no lastmod. Existing clients can ignore it.
    lastmod: dict[str, str] = Field(default_factory=dict)
    stats: MapStats
    took_ms: int = 0  # wall-clock time spent discovering the URLs
    warnings: list[str] = Field(default_factory=list)
