"""SSRF guard for the /map direct-egress path.

Unlike the crawl path (which fetches through the scraper service), /map makes
raw HTTP requests from the crawler container to user-supplied URLs. This module
resolves the target host and refuses private / loopback / link-local / reserved
addresses, and follows redirects manually so each hop is re-validated.

Residual risk: DNS rebinding between this check and httpx's own connect-time
resolution is not fully closed (would require pinning the resolved IP onto the
connection). This blocks the common metadata/internal-service SSRF vectors.
"""
from __future__ import annotations

import asyncio
import codecs
import ipaddress
import logging
import socket
from typing import NamedTuple
from urllib.parse import urljoin, urlparse


log = logging.getLogger(__name__)

# Carrier-grade NAT (shared-ISP address space). Python's ipaddress never
# classifies 100.64.0.0/10 as private (verified through 3.14), so a target
# resolving into CGNAT/Tailscale space reachable from this host would otherwise
# pass as "public". Mirrors the scraper's preset sample-fetch guard
# (src/presets/service.py).
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
# 6to4 relay anycast and IPv6 site-local. Like CGNAT, both pass every stdlib
# predicate as "public"; `fec0::/10` is deprecated but still routed on plenty
# of internal networks. Mirrored in src/security/egress.py — the scraper is a
# separate image, so this is a deliberate second copy and
# tests/security/test_policy_parity.py fails when the two drift.
_6TO4_RELAY_NET = ipaddress.ip_network("192.88.99.0/24")
_SITE_LOCAL_V6_NET = ipaddress.ip_network("fec0::/10")


class SSRFError(Exception):
    """Raised when a target resolves to a non-public address."""


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # Unwrap IPv4-mapped IPv6 (::ffff:a.b.c.d) so a mapped private/link-local
    # address is classified by its IPv4 value regardless of Python version.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Membership tests are version-specific: `IPv6Address in IPv4Network`
    # raises rather than returning False.
    if ip.version == 4:
        if ip in _CGNAT_NET or ip in _6TO4_RELAY_NET:
            return False
    elif ip in _SITE_LOCAL_V6_NET:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def host_is_public(host: str) -> bool:
    """True only if every resolved address for `host` is a public IP."""
    try:
        ips = await _resolve(host)
    except (socket.gaierror, OSError):
        return False
    return bool(ips) and all(_ip_is_public(ip) for ip in ips)


def usable_charset(declared: str | None) -> str | None:
    """The declared charset if Python can actually decode with it, else None.

    A header charset is attacker-and-CMS-supplied text, not a promise. Real
    responses carry `utf8mb4`, a trailing space, or two charsets merged into
    one header, and every one of those raises `LookupError` -- both in
    `bytes.decode` and in `etree.XMLParser(encoding=...)`, which is a hard
    failure where lxml used to sniff the document's own declaration and carry
    on. Unknown means "we learned nothing": returning None puts us back on
    that sniffing path and lets `.text` fall back to utf-8. Deliberately NOT
    clever about a merged `cp1251, utf-8` -- picking one of two declarations
    is a guess, and the document's own declaration is the better source.
    """
    if not declared:
        return None
    name = declared.strip().strip('"\'')
    if not name:
        return None
    try:
        codecs.lookup(name)
    except (LookupError, ValueError):
        return None
    return name


class CappedResponse(NamedTuple):
    """What `safe_get` hands back: the body up to the cap, and whether it was cut."""

    status_code: int
    url: str
    body: bytes
    # The charset the Content-Type header declared, or None: an XML parser
    # must be able to tell "declared" from "assumed" and fall back to the
    # document's own declaration/BOM.
    charset: str | None
    truncated: bool

    @property
    def text(self) -> str:
        return self.body.decode(self.charset or "utf-8", errors="replace")


async def _read_capped(resp, max_bytes: int) -> CappedResponse:
    """Read at most `max_bytes` of the DECODED body and stop.

    httpx decompresses each raw read before it reaches this loop, so the cap
    bounds what is kept whatever the Content-Encoding; a 204 KB gzip that
    inflates to 200 MB used to be materialised whole before the caller's
    `[:cap]` slice (audit 2026-09-03, H-17: +228 MB RSS measured, in a
    container with a 1 GiB limit). The cap is soft by one decoded chunk: one
    64 KiB raw read of a gzip bomb inflates to ~64 MiB before the slice, a
    transient the limit absorbs; and one chunk past the cap is read (and
    dropped) only to learn that the body went on.
    """
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk[: max_bytes + 1 - size])
            size += len(chunks[-1])
            if size > max_bytes:
                break
    finally:
        await resp.aclose()
    body = b"".join(chunks)
    return CappedResponse(
        status_code=resp.status_code,
        url=str(resp.url),
        body=body[:max_bytes],
        charset=usable_charset(resp.charset_encoding),
        truncated=len(body) > max_bytes,
    )


async def safe_get(client, url: str, *, max_bytes: int, check_ssrf: bool = True,
                   max_redirects: int = 5) -> CappedResponse:
    """GET with the SSRF policy, reading the body up to `max_bytes` decoded.

    Raises SSRFError on a non-public host or too many redirects. The client
    must have follow_redirects disabled -- redirects are followed here so each
    hop is checked; redirect hops are closed unread.

    When `check_ssrf` is False (the request egresses through an upstream proxy,
    so the crawler isn't the SSRF vector and local DNS doesn't reflect the
    proxy's resolution), the host check is skipped and the client follows
    redirects itself.
    """
    async def _open(target: str, *, follow: bool):
        return await client.send(client.build_request("GET", target), stream=True, follow_redirects=follow)

    if not check_ssrf:
        return await _read_capped(await _open(url, follow=True), max_bytes)
    current = url
    for _ in range(max_redirects + 1):
        host = urlparse(current).hostname
        if not host or not await host_is_public(host):
            raise SSRFError(f"blocked non-public host: {host}")
        resp = await _open(current, follow=False)
        location = resp.headers.get("location") if resp.headers else None
        if getattr(resp, "is_redirect", False) and location:
            await resp.aclose()
            current = urljoin(str(resp.url), location)
            continue
        return await _read_capped(resp, max_bytes)
    raise SSRFError("too many redirects")
