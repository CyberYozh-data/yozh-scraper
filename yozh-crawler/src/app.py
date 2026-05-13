from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_mcp import FastApiMCP

from .api.crawl import router as crawl_router
from .api.health import router as health_router
from .fetcher import ScraperClient
from .jobs import JobRunner, JobStore
from .limiter import domain_limiter
from .settings import settings, setup_logging


log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):

    scraper = ScraperClient(
        base_url=settings.scraper_url,
        poll_interval_ms=settings.scraper_job_poll_interval_ms,
        timeout_ms=settings.scraper_job_timeout_ms,
    )
    store = JobStore()
    runner = JobRunner(store=store, scraper=scraper, limiter=domain_limiter, settings=settings)
    await runner.start()

    app.state.scraper_client = scraper
    app.state.job_store = store
    app.state.job_runner = runner

    log.info(
        "open-crawler started workers=%d scraper_url=%s",
        settings.workers,
        settings.scraper_url,
    )

    yield

    await runner.stop()
    await scraper.close()


def create_app() -> FastAPI:
    setup_logging(settings.log_level, tag="C")

    app = FastAPI(
        title="Open Crawler",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        # MCP Streamable HTTP returns `mcp-session-id` in a response header that
        # the client must echo back on subsequent calls. Browsers hide custom
        # response headers from JS unless they're explicitly exposed.
        expose_headers=["mcp-session-id", "Mcp-Session-Id"],
    )

    api = APIRouter(prefix="/api/v1", tags=["api"])
    api.include_router(health_router, prefix="/health")
    api.include_router(crawl_router, prefix="/crawl")
    app.include_router(api)

    # MCP endpoint at /mcp. Exclude the SSE stream endpoint — streaming
    # responses don't translate well to a request/response MCP tool.
    mcp = FastApiMCP(app, exclude_operations=["stream_crawl_events"])
    mcp.mount_http()

    return app
