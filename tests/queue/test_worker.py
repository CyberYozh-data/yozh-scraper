from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.queue import worker as worker_mod

pytestmark = pytest.mark.asyncio


class _FakeRunner:
    def __init__(self, started: bool = True) -> None:
        self._started = started
        self.stop_calls = 0

    def is_started(self) -> bool:
        return self._started

    async def stop(self) -> None:
        self.stop_calls += 1
        self._started = False


def _state(*, last_activity: float, pages: int = 0) -> SimpleNamespace:
    """Build a fake worker state with per-engine dict layout."""
    return SimpleNamespace(
        runners={"chromium": _FakeRunner()},
        browser_lock=asyncio.Lock(),
        pages_since_launch={"chromium": pages},
        last_activity={"chromium": last_activity},
    )


async def test_tick_closes_browser_after_idle_window(monkeypatch):
    """After BROWSER_IDLE_SHUTDOWN_S of no activity the maintenance pass closes
    the browser to free its ~400-500 MB RSS."""
    monkeypatch.setattr(worker_mod.settings, "browser_idle_shutdown_s", 600.0)
    monkeypatch.setattr(worker_mod.settings, "browser_max_pages", 0)
    now = asyncio.get_running_loop().time()
    state = _state(last_activity=now - 700)  # idle 700s > 600
    await worker_mod._lifecycle_tick(state)
    assert state.runners["chromium"].stop_calls == 1


async def test_tick_does_not_refresh_last_activity(monkeypatch):
    """Regression: the pass must only READ last_activity, never overwrite it.
    The old loop refreshed last_activity every 5s, so `now - last_activity` was
    always ~5s and the 600s idle window never elapsed — the browser stayed warm
    forever and idle shutdown was dead code."""
    monkeypatch.setattr(worker_mod.settings, "browser_idle_shutdown_s", 600.0)
    monkeypatch.setattr(worker_mod.settings, "browser_max_pages", 0)
    now = asyncio.get_running_loop().time()
    stamp = now - 100  # recent: NOT idle
    state = _state(last_activity=stamp)
    await worker_mod._lifecycle_tick(state)
    assert state.runners["chromium"].stop_calls == 0
    assert state.last_activity["chromium"] == stamp  # untouched by the maintenance pass


async def test_tick_retires_browser_after_max_pages(monkeypatch):
    """Retirement: once pages_since_launch hits BROWSER_MAX_PAGES the pass closes
    the browser and resets the counter (next task lazy-launches a fresh one)."""
    monkeypatch.setattr(worker_mod.settings, "browser_idle_shutdown_s", 0.0)  # idle off
    monkeypatch.setattr(worker_mod.settings, "browser_max_pages", 50)
    now = asyncio.get_running_loop().time()
    state = _state(last_activity=now, pages=50)
    await worker_mod._lifecycle_tick(state)
    assert state.runners["chromium"].stop_calls == 1
    assert state.pages_since_launch["chromium"] == 0


async def test_tick_noop_when_browser_not_started(monkeypatch):
    """Idle window is meaningless while the browser is down (lazy-launch); the
    pass must not try to stop a stopped runner."""
    monkeypatch.setattr(worker_mod.settings, "browser_idle_shutdown_s", 600.0)
    monkeypatch.setattr(worker_mod.settings, "browser_max_pages", 0)
    now = asyncio.get_running_loop().time()
    state = _state(last_activity=now - 700)
    state.runners["chromium"]._started = False
    await worker_mod._lifecycle_tick(state)
    assert state.runners["chromium"].stop_calls == 0


@pytest.mark.asyncio
async def test_startup_probes_the_preset_store(monkeypatch):
    """The worker startup hook runs the preset-store writability probe once."""
    calls: list[str] = []
    monkeypatch.setattr(worker_mod, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_mod, "init_job_store", lambda: None)
    monkeypatch.setattr(worker_mod, "get_job_store", lambda: SimpleNamespace(client=None))
    monkeypatch.setattr(worker_mod, "init_session_store", lambda _client: None)
    monkeypatch.setattr(worker_mod, "_new_runner", lambda engine: engine)
    monkeypatch.setattr(worker_mod.FilePresetStore, "warn_if_not_writable", lambda self: calls.append("probe"))
    state = SimpleNamespace()

    await worker_mod.on_worker_startup(state)
    state.lifecycle_task.cancel()

    assert calls == ["probe"]
