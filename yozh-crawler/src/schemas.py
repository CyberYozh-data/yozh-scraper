from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, HttpUrl


ScrapeProxyType = Literal[
    "none", "mobile_shared", "mobile", "res_static", "res_rotating", "dc_static",
]
Device = Literal["desktop", "mobile"]
WaitUntil = Literal["domcontentloaded", "networkidle"]
ExtractType = Literal["css", "xpath"]
ScopeMode = Literal["same-domain", "subdomains", "all", "regex"]
CrawlJobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class ProxyGeo(BaseModel):
    country_code: str | None = None
    region: str | None = None
    city: str | None = None


class Cookie(BaseModel):
    name: str
    value: str
    domain: str | None = None
    path: str | None = "/"
    expires: int | None = None
    httpOnly: bool | None = None
    secure: bool | None = None
    sameSite: Literal["Strict", "Lax", "None"] | None = None


class FieldRule(BaseModel):
    selector: str
    attr: str = "text"
    all: bool = False
    required: bool = False


class ExtractRule(BaseModel):
    type: ExtractType
    fields: dict[str, FieldRule]


class ScrapeOptions(BaseModel):
    """Fields forwarded verbatim to scraper's POST /scrape/page. url is injected per-page from Frontier."""
    proxy_type: ScrapeProxyType = "none"
    proxy_pool_id: str | None = None
    proxy_geo: ProxyGeo | None = None

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


class CrawlScope(BaseModel):
    mode: ScopeMode = "same-domain"
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    max_depth: int = 3
    max_pages: int = 500
    per_domain_rps: float = 1.0
    per_domain_concurrency: int = 1


class CrawlRequest(BaseModel):
    seed_url: HttpUrl
    scope: CrawlScope = Field(default_factory=CrawlScope)
    scrape_options: ScrapeOptions = Field(default_factory=ScrapeOptions)
    crawl_proxy: ScrapeOptions | None = Field(
        default=None,
        description=(
            "Proxy used when enable_scraping=false (cheap discovery-only crawl). "
            "Only proxy_type/proxy_pool_id/proxy_geo are used; other fields are ignored. "
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
