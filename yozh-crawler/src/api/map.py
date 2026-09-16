from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request as FastapiRequest

from ..dedup import DedupSet, canonicalize_url
from ..linkextract import extract_links
from ..schemas import MapRequest, MapResponse, MapStats
from ..scope import CompositeScope
from ..settings import settings
from ..sitemap import collect_sitemap_urls, discover_sitemap_urls
from ..ssrf import safe_get



log = logging.getLogger(__name__)
router = APIRouter()


def _lastmod_date(raw: str | None) -> date | None:
    """Parse a sitemap <lastmod> to a date (day granularity). Handles both
    'YYYY-MM-DD' and full W3C datetimes by taking the date prefix — and stays
    Python-3.10-safe (date.fromisoformat there only accepts 'YYYY-MM-DD')."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None

# (url, scrape_options) -> ScrapeResponse dict
ScraperFetch = Callable[[str, dict], Awaitable[dict]]


def _proxy_scrape_options(req: MapRequest) -> dict:
    """Proxy fields to forward to the scraper on the render path (empty for none)."""
    if not req.proxy_type or req.proxy_type == "none":
        return {}
    opts: dict = {"proxy_type": req.proxy_type}
    if req.proxy_pool_id:
        opts["proxy_pool_id"] = req.proxy_pool_id
    if req.proxy_geo:
        opts["proxy_geo"] = req.proxy_geo.model_dump()
    if req.prem_proxy_options:
        # Keep the JS-render seed fetch on the same premium target as the httpx
        # discovery path; exclude_none so no null Literal reaches the scraper.
        opts["prem_proxy_options"] = req.prem_proxy_options.model_dump(exclude_none=True)
    return opts


async def _fetch_seed_html(
    req: MapRequest,
    seed: str,
    http_client,
    scraper_fetch: ScraperFetch | None,
    *,
    check_ssrf: bool = True,
    on_warning: Callable[[str], None] | None = None,
) -> tuple[str | None, str, str | None]:
    """Return (html, base_url, warning).

    base_url is the post-redirect URL so relative links resolve correctly
    (matches the crawl engine). Renders via the scraper when requested, else a
    plain (optionally SSRF-guarded) httpx GET. Never raises — failures come back
    as warnings.
    """
    if req.render:
        if scraper_fetch is None:
            return None, seed, "render requested but scraper is unavailable"
        try:
            result = await scraper_fetch(
                seed, {"render": True, "raw_html": True, **_proxy_scrape_options(req)}
            )
            meta = result.get("meta") or {}
            # A completed scrape can still be a block/error page (captcha/ban/
            # transient all set meta.fetch_ok=False). Don't hand its body back as
            # the seed — mirror the non-render path, which warns on a non-200.
            if not meta.get("fetch_ok", True) or result.get("error"):
                reason = result.get("error") or f"HTTP {meta.get('status_code')}"
                return None, seed, f"seed render blocked/failed ({reason})"
            final_url = meta.get("final_url") or seed
            return result.get("raw_html"), final_url, None
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return None, seed, f"seed render failed: {exc}"
    # safe_get raises SSRFError or httpx errors; degrade on either. Only this
    # direct leg is capped: the render leg above gets the page back whole from
    # the scraper, which materialised the DOM in its own container.
    try:
        resp = await safe_get(http_client, seed, max_bytes=settings.map_max_body_bytes, check_ssrf=check_ssrf)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return None, seed, f"seed fetch failed: {exc}"
    if resp.status_code != 200:
        return None, seed, f"seed fetch returned {resp.status_code}"
    if resp.truncated:
        log.warning("seed body cut at %d bytes url=%s", settings.map_max_body_bytes, seed)
        if on_warning is not None:
            on_warning(f"seed body cut at {settings.map_max_body_bytes} bytes")
    return resp.text, resp.url, None


async def build_map(
    req: MapRequest,
    *,
    http_client,
    scraper_fetch: ScraperFetch | None,
    check_ssrf: bool = True,
    extra_warnings: list[str] | None = None,
) -> MapResponse:
    """Fast URL discovery: sitemap + seed-page links, scope-filtered and deduped.

    Pure orchestration with injected fetchers so it is unit-testable without a
    live scraper or network. `check_ssrf=False` when the client egresses through
    a proxy (the crawler isn't the SSRF vector then). `extra_warnings` seed the
    response's warnings (e.g. a proxy-resolution warning from the caller).
    """
    started = time.perf_counter()
    seed = str(req.seed_url)
    scope = CompositeScope(req.scope, seed)
    dedup = DedupSet()
    ordered: list[str] = []
    warnings: list[str] = list(extra_warnings or [])

    def warn(message: str) -> None:
        # Degradations must be visible server-side too, not only as response
        # warnings — silent partial results are undebuggable in production.
        warnings.append(message)
        log.warning("map %s seed=%s", message, seed)

    async def gather_sitemap() -> list[tuple[str, str | None]]:
        if not req.include_sitemap:
            return []
        try:
            sitemap_urls = await discover_sitemap_urls(http_client, seed, check_ssrf=check_ssrf)
            return await collect_sitemap_urls(
                http_client,
                sitemap_urls,
                max_urls=settings.map_max_urls,
                max_sitemaps=settings.map_max_sitemaps,
                check_ssrf=check_ssrf,
                # Filter while collecting so the max_urls cap counts matches, not
                # the first N of a huge sitemap (which starved e.g. /blog/).
                match=req.search,
                on_warning=warn,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            warn(f"sitemap discovery failed: {exc}")
            return []

    async def gather_page_links() -> list[str]:
        if not req.include_page_links:
            return []
        try:
            html, base_url, warning = await asyncio.wait_for(
                _fetch_seed_html(
                    req, seed, http_client, scraper_fetch, check_ssrf=check_ssrf, on_warning=warn
                ),
                timeout=settings.map_render_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            # A stalled seed render must not hold the fast sitemap hostage —
            # degrade to sitemap-only instead of hanging the whole /map.
            warn(
                f"seed render exceeded {settings.map_render_timeout_ms}ms — "
                "sitemap-only results"
            )
            return []
        if warning:
            warn(warning)
        return extract_links(html or "", base_url=base_url)

    # The two discovery phases are independent network work — overlap them.
    sitemap_pages, page_links = await asyncio.gather(gather_sitemap(), gather_page_links())

    lastmod_by_url: dict[str, str] = {}

    def add_if_in_scope(url: str, lastmod: str | None = None) -> None:
        # Canonicalize so /map returns the same URL shape as /crawl (the engine
        # canonicalizes before dedup/scope too). Junk locs are skipped.
        try:
            canon = canonicalize_url(url)
        except Exception:  # pylint: disable=broad-exception-caught
            return
        if scope.allows(canon, 0) and dedup.add(canon):
            ordered.append(canon)
            if lastmod:
                lastmod_by_url[canon] = lastmod

    add_if_in_scope(seed)
    for url, lastmod in sitemap_pages:
        add_if_in_scope(url, lastmod)
    for url in page_links:  # <a> links carry no lastmod
        add_if_in_scope(url)

    unique_in_scope = len(ordered)
    if req.search:
        needle = req.search.lower()
        ordered = [u for u in ordered if needle in u.lower()]

    # Recency filter: published_after, or recent_days as a relative shorthand.
    cutoff = req.published_after
    if cutoff is None and req.recent_days is not None:
        cutoff = date.today() - timedelta(days=req.recent_days)
    if cutoff is not None:
        had_any = bool(ordered)
        ordered = [
            u for u in ordered
            if (d := _lastmod_date(lastmod_by_url.get(u))) is not None and d >= cutoff
        ]
        if had_any and not ordered:
            warn("date filter matched no URLs (sitemap had no <lastmod> dates, "
                 "or all were older than the cutoff)")

    # Sort newest-first by lastmod; URLs without a date sort last (date.min).
    if req.sort == "newest":
        ordered.sort(key=lambda u: _lastmod_date(lastmod_by_url.get(u)) or date.min, reverse=True)

    returned = ordered[: req.limit]
    returned_lastmod = {u: lastmod_by_url[u] for u in returned if u in lastmod_by_url}
    return MapResponse(
        seed_url=seed,
        count=len(returned),
        urls=returned,
        lastmod=returned_lastmod,
        stats=MapStats(
            from_sitemap=len(sitemap_pages),
            from_page=len(page_links),
            unique_in_scope=unique_in_scope,
            with_lastmod=len(returned_lastmod),
        ),
        took_ms=int((time.perf_counter() - started) * 1000),
        warnings=warnings,
    )


async def _open_proxied_client(
    scraper, req: MapRequest, warns: list[str]
) -> httpx.AsyncClient | None:
    """Resolve the upstream proxy and build a per-request client over it.

    None means the requested proxy could not be provided — the caller fails the
    request closed rather than degrading to direct egress (an explicit proxy
    request must never silently leak the crawler's real IP). Appends the reason
    to ``warns`` so the caller can surface it.
    """
    try:
        proxy_url = await scraper.resolve_proxy(
            req.proxy_type,
            req.proxy_pool_id,
            req.proxy_geo.model_dump() if req.proxy_geo else None,
            req.prem_proxy_options.model_dump(exclude_none=True) if req.prem_proxy_options else None,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log.warning("map proxy resolve failed seed=%s: %s", str(req.seed_url), exc)
        warns.append(f"proxy resolve failed ({exc})")
        return None
    if not proxy_url:
        warns.append("proxy resolve returned no upstream URL")
        return None
    # Needs httpx[socks] for socks5; a malformed URL fails the request closed
    # (below) rather than silently going direct.
    try:
        return httpx.AsyncClient(
            headers={"Accept-Encoding": "gzip, deflate"},
            proxy=proxy_url,
            timeout=httpx.Timeout(settings.map_http_timeout_ms / 1000.0),
            follow_redirects=False,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Never interpolate the exception text here: an httpx proxy error can
        # embed the credentialed proxy URL, and this reason flows into the log
        # and the 502 response body. Report the type only (mirrors the scraper's
        # "never log the resolved URL" discipline).
        log.warning(
            "map proxy client init failed seed=%s: %s", str(req.seed_url), type(exc).__name__
        )
        warns.append("proxy client init failed")
        return None


@router.post("", response_model=MapResponse, operation_id="create_map")
async def create_map(req: MapRequest, app_req: FastapiRequest) -> MapResponse:
    state = app_req.app.state
    scraper = state.scraper_client
    warns: list[str] = []

    if req.proxy_type and req.proxy_type != "none":
        client = await _open_proxied_client(scraper, req, warns)
        if client is None:
            # Fail closed: an explicit proxy was requested but could not be
            # provided. Degrading to a direct fetch would leak the crawler's
            # real IP while the caller believes egress went through the proxy.
            reason = warns[-1] if warns else "proxy unavailable"
            log.warning(
                "map proxy required but unavailable seed=%s proxy_type=%s: %s",
                str(req.seed_url), req.proxy_type, reason,
            )
            raise HTTPException(status_code=502, detail=f"proxy required but unavailable: {reason}")
        # SSRF guard off: egress is via the upstream proxy, not the crawler.
        try:
            return await build_map(
                req, http_client=client, scraper_fetch=scraper.fetch,
                check_ssrf=False, extra_warnings=warns,
            )
        finally:
            await client.aclose()

    return await build_map(
        req, http_client=state.http_client, scraper_fetch=scraper.fetch,
        extra_warnings=warns or None,
    )
