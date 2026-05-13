from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    """Crawler-side session: proxy identity + health score. Cookies NOT held
    here — scraper is treated as stateless and each request sends the same
    static cookie jar from CrawlRequest."""
    id: str
    proxy_type: str
    proxy_pool_id: str | None
    proxy_geo: dict[str, Any] | None
    error_score: float = 0.0
    usage_count: int = 0
    retired: bool = False

    def overrides(self) -> dict[str, Any]:
        d: dict[str, Any] = {"proxy_type": self.proxy_type}
        if self.proxy_pool_id is not None:
            d["proxy_pool_id"] = self.proxy_pool_id
        if self.proxy_geo:
            d["proxy_geo"] = self.proxy_geo
        return d


class ManagedSession:
    """Per-job pool. All sessions share the base proxy config from CrawlRequest.
    On retire, a fresh Session is created — for CyberYozh rotating proxies this
    implicitly yields a new IP on the next scrape call; for static pools it
    just resets health counters."""

    def __init__(
        self,
        base_proxy_type: str,
        base_proxy_pool_id: str | None,
        base_proxy_geo: dict[str, Any] | None,
        *,
        max_error_score: float,
        max_usage: int,
        blocked_codes: list[int],
    ) -> None:
        self._base = {
            "proxy_type": base_proxy_type,
            "proxy_pool_id": base_proxy_pool_id,
            "proxy_geo": base_proxy_geo,
        }
        self._max_error_score = max_error_score
        self._max_usage = max_usage
        self._blocked_codes = set(blocked_codes)
        self._current: Session | None = None

    def _fresh(self) -> Session:
        return Session(
            id=secrets.token_hex(6),
            proxy_type=self._base["proxy_type"],
            proxy_pool_id=self._base["proxy_pool_id"],
            proxy_geo=self._base["proxy_geo"],
        )

    def acquire(self) -> Session:
        s = self._current
        if s is None or s.retired:
            s = self._fresh()
            self._current = s
        s.usage_count += 1
        return s

    def release(self, s: Session, *, status_code: int | None, ok: bool) -> None:
        if ok:
            s.error_score = max(0.0, s.error_score - 0.5)
        else:
            s.error_score += 1.0

        if status_code is not None and status_code in self._blocked_codes:
            s.retired = True
            return

        if s.error_score >= self._max_error_score:
            s.retired = True
            return

        if s.usage_count >= self._max_usage:
            s.retired = True
            return

    def stats(self) -> dict[str, Any]:
        s = self._current
        if s is None:
            return {"active": False}
        return {
            "active": True,
            "id": s.id,
            "error_score": s.error_score,
            "usage_count": s.usage_count,
            "retired": s.retired,
        }
