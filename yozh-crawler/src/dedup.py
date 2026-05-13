from __future__ import annotations

import hashlib
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


_DEFAULT_PORTS = {"http": 80, "https": 443}


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
    netloc = host if port is None else f"{host}:{port}"

    path = p.path or "/"
    query_items = sorted(parse_qsl(p.query, keep_blank_values=True))
    query = urlencode(query_items, doseq=True)
    return urlunparse((scheme, netloc, path, p.params, query, ""))


def fingerprint(url: str) -> str:
    return hashlib.sha1(canonicalize_url(url).encode("utf-8")).hexdigest()


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
