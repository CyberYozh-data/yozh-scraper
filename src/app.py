import logging
import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi_mcp import FastApiMCP

from src.api import prem_proxies
from src.api.router import router
from src.presets.store import FilePresetStore
from src.queue.broker import broker
from src.queue.store import get_job_store, init_job_store
from src.sessions.store import get_session_store, init_session_store
from src.settings import settings, setup_logging
from src.utils.redaction import redact_url

_LEGACY_ENVS = ("JOBS_ENABLED", "JOB_RESULT_MAX", "JOB_TIMEOUT_MS")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log = logging.getLogger(__name__)
    for legacy in _LEGACY_ENVS:
        if os.environ.get(legacy):
            log.warning("env %s is no longer used after the taskiq migration", legacy)

    init_job_store()
    init_session_store(get_job_store().client)  # configured from settings inside
    FilePresetStore().warn_if_not_writable()
    if not broker.is_worker_process:
        await broker.startup()

    # Stale-pending reclaim now runs as the `reclaim_stale` scheduled task,
    # driven by the taskiq scheduler process — not an in-process loop here.
    background = [asyncio.create_task(_sessions_gc_loop())]
    log.info("api up: redis=%s queue_maxsize=%d", redact_url(settings.redis_url), settings.queue_maxsize)

    yield

    for task in background:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if not broker.is_worker_process:
        await broker.shutdown()


async def _sessions_gc_loop() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            await get_session_store().sweep_expired()
        except asyncio.CancelledError:
            return
        except Exception:  # pylint: disable=broad-except
            logging.getLogger(__name__).exception("sessions GC sweep failed")


# Every operation the (unauthenticated) MCP transport advertises as an agent
# tool. An ALLOWLIST, not a denylist, and the difference is not stylistic.
#
# The denylist this replaces filtered on `route.operation_id`. The eight
# `/api/v2/prem-proxies/*` routes declare no explicit id, so FastAPI generated
# names like `sub_users_api_v2_prem_proxies_sub_users_get` — names no
# hand-written exclusion could have matched even if someone had tried. They
# were live, anonymously callable agent tools while this module's docstring
# promised that nothing could drift onto the surface, and CI was green
# throughout. Generated ids are path-derived and therefore unstable too:
# renaming a handler silently renames its tool.
#
# A denylist fails by silently EXPOSING something new. An allowlist fails by a
# MISSING tool — visible, harmless, one line to fix. Only one of those is the
# right direction to be wrong in.
#
# Contents: everything advertised before this change, minus the eight
# prem-proxies operations. No existing MCP consumer loses a tool it had.
# Note this is not access control — these routes stay anonymous over REST
# either way; it decides only what is offered to agents.
MCP_TOOLS: tuple[str, ...] = (
    "health",
    "get_queue_stats",
    "run_scrape_page",
    "run_scrape_pages",
    "get_job_status",
    "get_job_result",
    "cancel_scrape_job",
    "run_scrape_preset_page",
    "run_scrape_preset_pages",
    "run_search",
    "list_presets",
    "get_preset",
    "create_preset",
    "update_preset",
    "delete_preset",
    "generate_preset",
    "test_preset",
    "preview_preset",
    "list_llm_models",
    # `list_available_proxies` is NOT here: it is gated behind SERVICE_TOKEN
    # (HIGH-03 — it enumerates the purchased proxies on the account), and the
    # MCP transport carries no such header. `list_proxy_countries` stays: a
    # static country list with nothing account-specific in it.
    "list_proxy_countries",
)


def _assert_mcp_allowlist_resolves(app: FastAPI, names: tuple[str, ...]) -> None:
    """Fail startup rather than advertise a surface nobody meant.

    Two distinct failures, both silent without this:

    * An empty allowlist. `fastapi_mcp` prunes its `operation_map` only when
      the filtered list is non-empty, so an allowlist that matches nothing
      advertises zero tools while leaving EVERY operation callable by name
      through `tools/call` — strictly worse than the denylist it replaced.
    * A name that no longer exists. Renaming a handler or dropping a route
      would quietly shrink the surface, and a tool that vanished is only
      obvious to whoever was using it.
    """
    if not names:
        raise RuntimeError(
            "MCP_TOOLS is empty: fastapi-mcp would leave every operation "
            "callable while advertising none"
        )
    known = {
        operation.get("operationId")
        for path in app.openapi()["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict)
    }
    missing = sorted(name for name in names if name not in known)
    if missing:
        raise RuntimeError(f"MCP_TOOLS names operations that do not exist: {missing}")


def create_app() -> FastAPI:
    setup_logging(settings.log_level, tag="M")

    app = FastAPI(
        title="Open Scraper",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(prem_proxies.router)

    _assert_mcp_allowlist_resolves(app, MCP_TOOLS)
    mcp = FastApiMCP(app, include_operations=list(MCP_TOOLS))
    mcp.mount_http()

    return app
