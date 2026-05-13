from __future__ import annotations

import asyncio
import logging
import secrets
from typing import AsyncIterator

from .engine import CrawlEngine
from .fetcher import ScraperClient
from .limiter import DomainRateLimiter
from .schemas import (
    CrawlJobRecord,
    CrawlJobStatus,
    CrawlPageRecord,
    CrawlRequest,
    CrawlStats,
)
from .settings import Settings


log = logging.getLogger(__name__)


def _new_job_id() -> str:
    return f"crawl_{secrets.token_hex(12)}"


class JobStore:
    """In-memory job records + per-job fan-out of SSE events."""

    def __init__(self) -> None:
        self._jobs: dict[str, CrawlJobRecord] = {}
        self._subscribers: dict[str, list[asyncio.Queue[dict]]] = {}
        self._final: dict[str, dict] = {}  # final event replayed to new subscribers
        self._lock = asyncio.Lock()

    async def create(self, request: CrawlRequest) -> CrawlJobRecord:
        job_id = _new_job_id()
        rec = CrawlJobRecord(
            job_id=job_id,
            status="queued",
            request=request,
            stats=CrawlStats(),
            pages=[],
        )
        async with self._lock:
            self._jobs[job_id] = rec
            self._subscribers[job_id] = []
        return rec

    def get(self, job_id: str) -> CrawlJobRecord | None:
        return self._jobs.get(job_id)

    def all_ids(self) -> list[str]:
        return list(self._jobs.keys())

    async def set_status(
        self,
        job_id: str,
        status: CrawlJobStatus,
        *,
        error: str | None = None,
    ) -> None:
        rec = self._jobs.get(job_id)
        if rec is None:
            return
        rec.status = status
        if error is not None:
            rec.error = error

    def append_page(self, job_id: str, page: CrawlPageRecord) -> None:
        rec = self._jobs.get(job_id)
        if rec is None:
            return
        rec.pages.append(page)
        self.publish(job_id, {"type": "page", "page": page.model_dump(mode="json")})

    def update_stats(self, job_id: str, stats: CrawlStats) -> None:
        rec = self._jobs.get(job_id)
        if rec is None:
            return
        rec.stats = stats
        self.publish(job_id, {"type": "stats", "stats": stats.model_dump(mode="json")})

    def publish(self, job_id: str, event: dict) -> None:
        for q in list(self._subscribers.get(job_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("subscriber queue full — dropping event for job=%s", job_id)
            except Exception:
                pass

    def publish_final(self, job_id: str, event: dict) -> None:
        """Publishes final terminal event and remembers it for late subscribers."""
        self._final[job_id] = event
        self.publish(job_id, event)
        # Close all current subscriber queues by sentinel
        for q in list(self._subscribers.get(job_id, [])):
            try:
                q.put_nowait({"type": "__close__"})
            except Exception:
                pass

    async def subscribe(self, job_id: str) -> AsyncIterator[dict]:
        rec = self._jobs.get(job_id)
        if rec is None:
            return
        # If already terminal — replay a snapshot + final event and close
        if rec.status in ("done", "failed", "cancelled"):
            yield {"type": "stats", "stats": rec.stats.model_dump(mode="json")}
            for p in rec.pages:
                yield {"type": "page", "page": p.model_dump(mode="json")}
            final = self._final.get(job_id) or {"type": "done", "status": rec.status, "stats": rec.stats.model_dump(mode="json")}
            yield final
            return

        # Unbounded — capped implicitly by max_pages in the crawl itself.
        q: asyncio.Queue[dict] = asyncio.Queue()
        # Replay current state then subscribe. Everything below up to the
        # subscribers.append is sync (no awaits) so no publish can interleave.
        q.put_nowait({"type": "stats", "stats": rec.stats.model_dump(mode="json")})
        for p in rec.pages:
            q.put_nowait({"type": "page", "page": p.model_dump(mode="json")})

        self._subscribers.setdefault(job_id, []).append(q)
        try:
            while True:
                event = await q.get()
                if event.get("type") == "__close__":
                    return
                yield event
        finally:
            subs = self._subscribers.get(job_id, [])
            if q in subs:
                subs.remove(q)


class JobRunner:
    """Pulls queued jobs from a per-process asyncio.Queue; runs up to `workers`
    CrawlEngine instances concurrently."""

    def __init__(
        self,
        store: JobStore,
        scraper: ScraperClient,
        limiter: DomainRateLimiter,
        settings: Settings,
    ) -> None:
        self._store = store
        self._scraper = scraper
        self._limiter = limiter
        self._settings = settings
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_maxsize)
        self._engines: dict[str, CrawlEngine] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self._settings.workers)]
        log.info("JobRunner started workers=%d", self._settings.workers)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def submit(self, job_id: str) -> None:
        try:
            self._queue.put_nowait(job_id)
        except asyncio.QueueFull:
            await self._store.set_status(job_id, "failed", error="queue_full")
            raise RuntimeError("queue_full")

    async def request_cancel(self, job_id: str, *, hard: bool) -> bool:
        engine = self._engines.get(job_id)
        if engine is not None:
            log.info("cancel requested job_id=%s hard=%s via=engine", job_id, hard)
            engine.cancel(hard=hard)
            return True
        rec = self._store.get(job_id)
        if rec is not None and rec.status in ("queued", "running"):
            log.info("cancel requested job_id=%s hard=%s via=store status=%s",
                     job_id, hard, rec.status)
            await self._store.set_status(job_id, "cancelled")
            self._store.publish_final(job_id, {
                "type": "cancelled",
                "stats": rec.stats.model_dump(mode="json"),
            })
            return True
        log.info("cancel request ignored job_id=%s reason=%s",
                 job_id, "not_found" if rec is None else f"already_{rec.status}")
        return False

    async def _worker(self, idx: int) -> None:
        log.info("worker %d started", idx)
        while not self._stop.is_set():
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

            rec = self._store.get(job_id)
            if rec is None or rec.status == "cancelled":
                continue

            def on_event(ev: dict, _job=job_id) -> None:
                self._store.publish(_job, ev)

            def on_page(page: CrawlPageRecord, _job=job_id) -> None:
                self._store.append_page(_job, page)

            def on_stats(stats: CrawlStats, _job=job_id) -> None:
                self._store.update_stats(_job, stats)

            engine = CrawlEngine(
                job_id=job_id,
                request=rec.request,
                scraper=self._scraper,
                limiter=self._limiter,
                settings=self._settings,
                on_event=on_event,
                on_page=on_page,
                on_stats=on_stats,
            )
            # Register engine BEFORE flipping status to "running" — otherwise a
            # DELETE arriving in this window falls through the non-engine cancel
            # path, marks status cancelled, then we'd overwrite it back to
            # running+done without honoring the cancel.
            self._engines[job_id] = engine
            await self._store.set_status(job_id, "running")

            # In case a cancel slipped in between create and now, honour it.
            if self._store.get(job_id).status == "cancelled":
                self._engines.pop(job_id, None)
                continue

            try:
                await asyncio.wait_for(engine.run(), timeout=self._settings.job_timeout_ms / 1000.0)
                # Preserve externally-set terminal status; don't clobber "cancelled".
                current_status = self._store.get(job_id).status
                if current_status in ("cancelled", "failed"):
                    final_status: CrawlJobStatus = current_status
                else:
                    final_status = "cancelled" if engine.cancelled else "done"
                    await self._store.set_status(job_id, final_status)
                self._store.publish_final(job_id, {
                    "type": final_status,
                    "status": final_status,
                    "stats": rec.stats.model_dump(mode="json"),
                })
            except asyncio.TimeoutError:
                await self._store.set_status(job_id, "failed", error="job_timeout")
                self._store.publish_final(job_id, {
                    "type": "done",
                    "status": "failed",
                    "stats": rec.stats.model_dump(mode="json"),
                })
            except asyncio.CancelledError:
                await self._store.set_status(job_id, "cancelled")
                self._store.publish_final(job_id, {
                    "type": "cancelled",
                    "stats": rec.stats.model_dump(mode="json"),
                })
                raise
            except Exception as e:
                log.exception("engine crashed job=%s", job_id)
                await self._store.set_status(job_id, "failed", error=str(e))
                self._store.publish_final(job_id, {
                    "type": "done",
                    "status": "failed",
                    "error": str(e),
                    "stats": rec.stats.model_dump(mode="json"),
                })
            finally:
                self._engines.pop(job_id, None)
