from __future__ import annotations

import re
from urllib.parse import urlparse

from .schemas import CrawlScope


class CompositeScope:
    """Decides whether a discovered URL is in-scope for the crawl.

    Combines: mode (domain-based), regex include/exclude, and max depth.
    """

    def __init__(self, scope: CrawlScope, seed_url: str) -> None:
        self._scope = scope
        seed = urlparse(str(seed_url))
        self._seed_host = (seed.hostname or "").lower()
        self._seed_reg_domain = self._registrable(self._seed_host)
        self._includes = [re.compile(p) for p in scope.include_patterns]
        self._excludes = [re.compile(p) for p in scope.exclude_patterns]

    @staticmethod
    def _registrable(host: str) -> str:
        """Naive 'registrable domain' — last two labels. NOTE: this is wrong
        for public suffixes like ``a.github.io`` / ``foo.co.uk`` — ``subdomains``
        mode will over-match there (treat ``a.github.io`` and ``b.github.io`` as
        the same registrable). Document this behaviour and prefer ``same-domain``
        or explicit ``regex`` mode for hosts on the Public Suffix List. A proper
        fix would add ``tldextract`` as a dependency."""
        if not host:
            return ""
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    def _in_mode(self, host: str) -> bool:
        mode = self._scope.mode
        if mode == "all":
            return True
        if mode == "same-domain":
            return host == self._seed_host
        if mode == "subdomains":
            return host == self._seed_host or host.endswith("." + self._seed_reg_domain) or host == self._seed_reg_domain
        if mode == "regex":
            if not self._includes:
                return False
            return True
        return False

    def allows(self, url: str, depth: int) -> bool:
        return self.reason(url, depth) is None

    def reason(self, url: str, depth: int) -> str | None:
        if depth > self._scope.max_depth:
            return "max_depth"

        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return "scheme"
        host = (p.hostname or "").lower()
        if not host:
            return "no_host"

        if not self._in_mode(host):
            return "mode"

        if self._excludes and any(rx.search(url) for rx in self._excludes):
            return "exclude"

        if self._includes:
            if not any(rx.search(url) for rx in self._includes):
                return "include"

        return None
