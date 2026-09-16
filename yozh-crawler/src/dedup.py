from __future__ import annotations

import hashlib
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from .scope import strip_www


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _netloc(host: str, port: int | None) -> str:
    """Rebuild a netloc from `urlparse`'s pieces: `hostname` strips the brackets
    off an IPv6 literal, and bare `::1:8080` is a netloc whose `.port` raises
    on the next parse (audit H-16)."""
    if ":" in host:
        host = f"[{host}]"
    return host if port is None else f"{host}:{port}"


def canonicalize_url(url: str) -> str:
    """Normalize a URL so equivalent variants share one fingerprint.

    Rules: lowercase scheme + host, drop default ports, drop fragment,
    sort query params, collapse an empty path to "/".
    """
    p = urlparse(url.strip())
    scheme = p.scheme.lower()
    host = p.hostname.lower() if p.hostname else ""
    port = p.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    # Strip userinfo — we never want to round-trip credentials through the
    # canonical URL into the scraper / logs / fingerprints.
    netloc = _netloc(host, port)

    path = p.path or "/"
    query_items = sorted(parse_qsl(p.query, keep_blank_values=True))
    query = urlencode(query_items, doseq=True)
    return urlunparse((scheme, netloc, path, p.params, query, ""))


def fingerprint(url: str) -> str:
    """SHA1 over the canonical URL with the host's leading ``www.`` stripped:
    same-domain scope treats www.X and X as one site, so the same path under
    either spelling is one page. Only identity is unified — canonicalize_url
    never rewrites the host, so emitted/queued URLs keep their spelling."""
    p = urlparse(canonicalize_url(url))
    netloc = _netloc(strip_www(p.hostname or ""), p.port)
    dewww = urlunparse((p.scheme, netloc, p.path, p.params, p.query, ""))
    return hashlib.sha1(dewww.encode("utf-8")).hexdigest()


class DedupSet:
    """Per-job URL dedup. Canonicalizes on add/contains; stores fingerprints."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def add(self, url: str) -> bool:
        fp = fingerprint(url)
        if fp in self._seen:
            return False
        self._seen.add(fp)
        return True

    def __contains__(self, url: str) -> bool:
        return fingerprint(url) in self._seen

    def __len__(self) -> int:
        return len(self._seen)
