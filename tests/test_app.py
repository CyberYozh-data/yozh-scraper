from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app import create_app
from src.queue.store import get_job_store


class TestAppCreation:
    def test_create_app(self):
        """Create FastAPI App"""
        app = create_app()

        assert isinstance(app, FastAPI)

    def test_app_title(self):
        """Check App title"""
        app = create_app()

        assert app.title == "Open Scraper"

    def test_app_version(self):
        """Check App version"""
        app = create_app()

        assert app.version == "0.2.0"

    def test_app_routers_included(self):
        """Check routers"""
        app = create_app()

        # Check, that there are routes
        # Not app.routes: include_router wraps sub-routers in FastAPI's own
        # _IncludedRouter, which exposes neither .path nor .routes, and its
        # original_router holds unprefixed fragments. openapi() is the public
        # accessor and yields the prefixed paths.
        routes = list(app.openapi()["paths"])

        assert "/api/v1/health" in routes
        assert any("/api/v1/scrape" in route for route in routes)


class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_startup_initializes_store(self):
        """Lifespan startup calls init_job_store so get_job_store() is reachable."""
        app = create_app()
        async with app.router.lifespan_context(app):
            # After startup the store is set and operational
            store = get_job_store()
            assert store is not None

    @pytest.mark.asyncio
    async def test_lifespan_broker_startup_and_shutdown(self, mocker):
        """Lifespan calls broker.startup on entry and broker.shutdown on exit."""
        mock_startup = AsyncMock()
        mock_shutdown = AsyncMock()
        mocker.patch("src.app.broker.startup", mock_startup)
        mocker.patch("src.app.broker.shutdown", mock_shutdown)
        mocker.patch("src.app.broker.is_worker_process", False)

        app = create_app()
        async with app.router.lifespan_context(app):
            mock_startup.assert_awaited_once()

        mock_shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_probes_the_preset_store(self, mocker):
        """Startup runs the preset-store writability probe exactly once.

        The broker is stubbed: the test-mode InMemoryBroker runs the worker
        startup hook in-process, which probes again and is not this seam.
        """
        probe = mocker.patch("src.app.FilePresetStore.warn_if_not_writable")
        mocker.patch("src.app.broker.startup", AsyncMock())
        mocker.patch("src.app.broker.shutdown", AsyncMock())

        app = create_app()
        async with app.router.lifespan_context(app):
            probe.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_lifespan_health_endpoint_works(self):
        """After lifespan startup the /health endpoint responds."""
        app = create_app()
        async with app.router.lifespan_context(app):
            with TestClient(app) as c:
                resp = c.get("/api/v1/health")
                assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_lifespan_does_not_run_reclaim_in_process(self, mocker):
        """Stale-pending reclaim runs via the taskiq scheduler, not as an
        in-process background task. The API lifespan starts only the sessions
        GC loop — never the reaper/reclaim."""
        started = []
        original_create_task = __import__("asyncio").create_task

        def tracking_create_task(coro, **kwargs):
            started.append(getattr(coro, "__qualname__", repr(coro)))
            return original_create_task(coro, **kwargs)

        mocker.patch("src.app.asyncio.create_task", side_effect=tracking_create_task)

        app = create_app()
        async with app.router.lifespan_context(app):
            pass

        # The GC loop IS in-process (proves the tracker sees real tasks)...
        assert any("_sessions_gc_loop" in name for name in started)
        # ...but reclaim/reaper is NOT (moved to the scheduler process).
        assert not any(
            "reaper" in name.lower() or "reclaim" in name.lower() for name in started
        )


class TestLogging:
    def test_app_logging_setup(self, mocker):
        """Check call setup_logging"""
        mock_setup_logging = mocker.patch("src.app.setup_logging")

        with patch("src.app.settings") as mock_settings:
            mock_settings.log_level = "DEBUG"

            app = create_app()

            # Check setup_logging was called
            mock_setup_logging.assert_called_once_with("DEBUG", tag="M")
