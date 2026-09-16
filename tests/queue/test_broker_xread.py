"""The stream broker takes ONE message per read (audit 2026-09-03, H-05)."""
from __future__ import annotations

from src.queue import broker as broker_mod


def test_the_stream_broker_takes_one_message_at_a_time(monkeypatch):
    captured: dict = {}

    class _Fake:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(broker_mod, "RedisStreamBroker", _Fake)
    monkeypatch.delenv("TASKIQ_INMEMORY", raising=False)
    broker_mod._build_broker()  # pylint: disable=protected-access
    assert captured["xread_count"] == 1
    assert captured["maxlen"] == broker_mod.STREAM_MAXLEN
