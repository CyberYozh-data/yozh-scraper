"""Worker-process entrypoint: `taskiq worker src.queue.worker:broker`. Owns the
browser lifecycle: lazy launch, retirement after BROWSER_MAX_PAGES, idle
shutdown (BROWSER_IDLE_SHUTDOWN_S parity with PR #15), clean close on drain.

A single asyncio.Lock (state.browser_lock) serialises browser teardown against
in-flight scrapes so the maintenance loop never closes a browser mid-task."""
from __future__ import annotations

import asyncio
import logging

from taskiq import TaskiqEvents, TaskiqState

from src.browser.runner import PlaywrightRunner
from src.presets.store import FilePresetStore
# Side-effect import: registers the @broker.task definitions on the broker.
from src.queue import tasks  # noqa: F401  # pylint: disable=unused-import
from src.queue.broker import broker
from src.queue.store import get_job_store, init_job_store
from src.sessions.store import init_session_store
from src.settings import settings, setup_logging

log = logging.getLogger(__name__)


def _new_runner(engine: str = "chromium") -> PlaywrightRunner:
    return PlaywrightRunner(
        engine=engine,
        headless=settings.headless,
        block_assets=settings.block_assets,
        timeout_ms=settings.request_timeout_ms,
    )


async def _lifecycle_tick(state: TaskiqState) -> None:
    """One maintenance pass under browser_lock: for each started native runner,
    retire after BROWSER_MAX_PAGES or close after the engine's idle window.
    `last_activity[engine]` is owned by task execution (_record_page); this pass
    only READS it — refreshing it here would reset the idle window every pass so
    it could never elapse.

    Primary engine (chromium): uses BROWSER_IDLE_SHUTDOWN_S.
    Any other engine:          uses BROWSER_IDLE_SHUTDOWN_S_SECONDARY (shorter)
                               so secondary engines reclaim RAM quickly without
                               affecting the warm Chromium instance."""
    now = asyncio.get_running_loop().time()
    async with state.browser_lock:
        for engine, runner in list(state.runners.items()):
            if not runner.is_started():
                continue
            idle_s = (
                settings.browser_idle_shutdown_s
                if engine == "chromium"
                else settings.browser_idle_shutdown_s_secondary
            )
            pages = state.pages_since_launch.get(engine, 0)
            retire = settings.browser_max_pages > 0 and pages >= settings.browser_max_pages
            idle = idle_s > 0 and now - state.last_activity.get(engine, now) > idle_s
            if retire:
                log.info("browser[%s] retirement after %d pages", engine, pages)
                await runner.stop()
                state.pages_since_launch[engine] = 0
            elif idle:
                log.info("browser[%s] idle %.0fs, closing", engine, idle_s)
                await runner.stop()


async def _lifecycle_loop(state: TaskiqState) -> None:
    """Retirement + idle shutdown, checked every 5s, always under browser_lock so
    a teardown can never overlap an in-flight scrape."""
    while True:
        await asyncio.sleep(5)
        try:
            await _lifecycle_tick(state)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — a wedged close must not kill the loop (PR #15 parity)
            log.exception("browser lifecycle maintenance failed")


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def on_worker_startup(state: TaskiqState) -> None:
    setup_logging(settings.log_level, tag="W")
    init_job_store()
    init_session_store(get_job_store().client)
    FilePresetStore().warn_if_not_writable()
    state.runners = {"chromium": _new_runner("chromium")}  # registry seam for the anti-detect epic
    state.browser_lock = asyncio.Lock()
    state.pages_since_launch = {"chromium": 0}
    state.last_activity = {"chromium": asyncio.get_running_loop().time()}
    state.lifecycle_task = asyncio.create_task(_lifecycle_loop(state))
    log.info("worker ready (browser lazy-launches on first task)")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def on_worker_shutdown(state: TaskiqState) -> None:
    # Tolerate a shutdown that races a never-completed startup (e.g. an
    # InMemoryBroker lifecycle in tests, or a worker that crashed mid-startup):
    # the handler must not raise on a half-populated state.
    task = getattr(state, "lifecycle_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    lock = getattr(state, "browser_lock", None)
    runners = getattr(state, "runners", None) or {}
    if lock is not None:
        async with lock:
            for runner in runners.values():
                try:
                    await runner.stop()
                except Exception:  # noqa: BLE001
                    log.exception("browser close on shutdown failed")
    log.info("worker drained and closed")
