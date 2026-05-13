from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import Any, Callable
from urllib.parse import urlparse

from .dedup import DedupSet, canonicalize_url
from .fetcher import ScraperClient, ScraperError
from .frontier import Frontier, Request
from .limiter import DomainRateLimiter
from .linkextract import extract_links
from .schemas import (
    CrawlPageRecord,
    CrawlRequest,
    CrawlStats,
    ScrapeProxyType,
)
from .scope import CompositeScope
from .session import ManagedSession
from .settings import Settings


log = logging.getLogger(__name__)


EventCallback = Callable[[dict], None]
PageCallback = Callable[[CrawlPageRecord], None]
StatsCallback = Callable[[CrawlStats], None]


class CrawlEngine:
    """One per job. Drives the crawl loop, emits events through callbacks."""

    def __init__(
        self,
        *,
        job_id: str,
        request: CrawlRequest,
        scraper: ScraperClient,
        limiter: DomainRateLimiter,
        settings: Settings,
        on_event: EventCallback,
        on_page: PageCallback,
        on_stats: StatsCallback,
    ) -> None:
        self.job_id = job_id
        self._req = request
        self._scraper = scraper
        self._limiter = limiter
        self._max_retries = settings.max_retries
        self._retry_codes = set(settings.retry_http_codes)
        self._backoff_base = settings.retry_backoff_base
        self._backoff_max = settings.retry_backoff_max

        self._frontier = Frontier()
        self._dedup = DedupSet()
        self._scope = CompositeScope(request.scope, str(request.seed_url))

        # Resolve active proxy by mode (no fallback — each mode owns its proxy).
        active = request.scrape_options if request.enable_scraping else request.crawl_proxy
        active_type: ScrapeProxyType = active.proxy_type if active else "none"
        active_pool_id = active.proxy_pool_id if active else None
        active_geo = (
            active.proxy_geo.model_dump(exclude_none=True)
            if (active and active.proxy_geo) else None
        )

        self._sessions = ManagedSession(
            base_proxy_type=active_type,
            base_proxy_pool_id=active_pool_id,
            base_proxy_geo=active_geo,
            max_error_score=settings.session_max_error_score,
            max_usage=settings.session_max_usage,
            blocked_codes=settings.session_blocked_codes,
        )

        # Base scrape-options dict — computed once per job, merged with
        # per-session proxy overrides on each fetch. Avoids re-serializing the
        # full Pydantic model (cookies, extract rule, geo) 500× during a crawl.
        base_opts = request.scrape_options.model_dump(mode="json", exclude_none=True)
        base_opts["raw_html"] = True  # crawler always needs HTML to extract links
        if not request.enable_scraping:
            base_opts.pop("extract", None)
            base_opts["screenshot"] = False
        # Proxy fields come from SessionPool on every fetch — drop defaults here.
        for k in ("proxy_type", "proxy_pool_id", "proxy_geo"):
            base_opts.pop(k, None)
        self._base_scrape_options = base_opts

        self._on_event = on_event
        self._on_page = on_page
        self._on_stats = on_stats

        self._stats = CrawlStats()
        self._last_stats_snapshot: dict | None = None
        self._in_flight = 0
        self._cancelled = False
        self._tasks: list[asyncio.Task] = []
        self._per_domain_semas: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(max(1, request.scope.per_domain_concurrency))
        )

        seed_domain = (urlparse(str(request.seed_url)).hostname or "").lower()
        if seed_domain:
            self._limiter.set_rps(seed_domain, request.scope.per_domain_rps)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    # ──────────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        self._stats.started_at = time.time()
        seed = canonicalize_url(str(self._req.seed_url))
        if self._dedup.add(seed):
            self._frontier.push(Request(url=seed, parent_url=None, depth=0))
            self._stats.queued = 1

        worker_count = max(1, min(20, self._req.scope.per_domain_concurrency * 4))
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(worker_count)]
        stats_task = asyncio.create_task(self._stats_loop())

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            stats_task.cancel()
            try:
                await stats_task
            except asyncio.CancelledError:
                pass
            self._stats.finished_at = time.time()
            self._on_stats(self._stats)

    def cancel(self, *, hard: bool) -> None:
        self._cancelled = True
        if hard:
            for t in self._tasks:
                t.cancel()

    async def _stats_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                self._stats.queued = len(self._frontier)
                snap = self._stats.model_dump()
                if snap != self._last_stats_snapshot:
                    self._on_stats(self._stats)
                    self._last_stats_snapshot = snap
        except asyncio.CancelledError:
            return

    async def _worker(self, idx: int) -> None:
        while not self._cancelled:
            # Hard cap: visited + in-flight already reserves a slot per active
            # worker, so we never dispatch past max_pages. Check BEFORE pop.
            if self._stats.visited + self._in_flight >= self._req.scope.max_pages:
                if self._in_flight == 0:
                    return
                await asyncio.sleep(0.05)
                continue

            req = self._frontier.pop()
            if req is None:
                if self._in_flight == 0 and len(self._frontier) == 0:
                    return
                await asyncio.sleep(0.05)
                continue

            self._in_flight += 1
            try:
                await self._process(req)
            finally:
                self._in_flight -= 1

    # ──────────────────────────────────────────────────────────────────────
    async def _process(self, req: Request) -> None:
        domain = (urlparse(req.url).hostname or "").lower()
        sema = self._per_domain_semas[domain]

        async with sema:
            await self._limiter.acquire(domain, job_id=self.job_id)

            session = self._sessions.acquire()
            scrape_options = self._build_scrape_options(session.overrides())

            start = time.perf_counter()
            try:
                scrape_response = await self._scraper.fetch(req.url, scrape_options)
            except ScraperError as e:
                took_ms = int((time.perf_counter() - start) * 1000)
                self._sessions.release(session, status_code=e.status_code, ok=False)
                await self._handle_failure(req, e, took_ms)
                return
            except Exception as e:  # defensive
                took_ms = int((time.perf_counter() - start) * 1000)
                self._sessions.release(session, status_code=None, ok=False)
                log.exception("unexpected fetch error job=%s url=%s", self.job_id, req.url)
                await self._handle_failure(
                    req,
                    ScraperError(status_code=None, message=str(e), retryable=False),
                    took_ms,
                )
                return

            took_ms = int((time.perf_counter() - start) * 1000)
            meta = scrape_response.get("meta") or {}
            status_code = meta.get("status_code")

            ok = status_code is not None and 200 <= int(status_code) < 400
            self._sessions.release(session, status_code=status_code, ok=ok)

            if status_code in (429, 503):
                self._limiter.on_429(domain)
            elif ok:
                self._limiter.on_success(domain)

        # Extract links from raw_html BEFORE dropping heavy payload below —
        # otherwise disabling scraping leaves the crawler with nothing to walk.
        raw_html = scrape_response.get("raw_html") or ""
        links = extract_links(raw_html, meta.get("final_url") or req.url)

        stored = scrape_response if self._req.enable_scraping else None

        page = CrawlPageRecord(
            url=req.url,
            parent_url=req.parent_url,
            depth=req.depth,
            fetched_at=time.time(),
            took_ms=took_ms,
            status_code=status_code,
            scrape_response=stored,
        )
        self._stats.visited += 1
        self._on_page(page)

        for raw_link in links:
            try:
                canon = canonicalize_url(raw_link)
            except Exception:
                continue

            if not self._dedup.add(canon):
                self._stats.dedup_skipped += 1
                continue

            child_depth = req.depth + 1
            reason = self._scope.reason(canon, child_depth)
            if reason is not None:
                self._stats.out_of_scope += 1
                continue

            # Do NOT cap frontier push here — the worker loop caps actual
            # visits via (visited + in_flight >= max_pages). Capping here would
            # silently drop in-scope URLs without accounting for them in stats.
            self._frontier.push(Request(
                url=canon,
                parent_url=req.url,
                depth=child_depth,
            ))

    # ──────────────────────────────────────────────────────────────────────
    async def _handle_failure(self, req: Request, err: ScraperError, took_ms: int) -> None:
        req.attempts += 1
        retry = err.retryable and req.attempts <= self._max_retries

        # If the scraper told us an HTTP status, respect retry_codes too
        if err.status_code is not None and err.status_code not in self._retry_codes:
            retry = False

        if retry:
            backoff = min(self._backoff_max, self._backoff_base ** req.attempts)
            backoff = backoff * (0.8 + random.random() * 0.4)
            req.next_attempt_at = time.monotonic() + backoff
            self._frontier.push(req)
            self._stats.retries_total += 1
            self._on_event({
                "type": "page_error",
                "url": req.url,
                "attempt": req.attempts,
                "message": err.message,
                "will_retry": True,
                "retry_in_s": round(backoff, 2),
            })
            return

        self._stats.failed += 1
        page = CrawlPageRecord(
            url=req.url,
            parent_url=req.parent_url,
            depth=req.depth,
            fetched_at=time.time(),
            took_ms=took_ms,
            status_code=err.status_code,
            scrape_response=None,
            error=err.message,
        )
        self._on_page(page)
        self._on_event({
            "type": "page_error",
            "url": req.url,
            "attempt": req.attempts,
            "message": err.message,
            "will_retry": False,
        })

    def _build_scrape_options(self, session_overrides: dict[str, Any]) -> dict[str, Any]:
        return {**self._base_scrape_options, **session_overrides}
