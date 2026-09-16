"""Turn a page's own redirect stubs back into the destinations they hide.

Google rewrote every search-result href to `/goto?url=<opaque token>`
(confirmed by Google 2026-08-26; ~100% rollout across residential providers per
Nozzle). The token is not an encoding of anything — nothing on the page or in
the token recovers the destination. What does recover it is the stub itself:
a plain GET, redirects not followed, answers with the destination in
`Location`. Measured 2026-09-04 from this host — no proxy, no cookies, no
Referer, no User-Agent, a six-day-old token — `302 Location:
https://www.wired.com/story/best-laptops/`. `HEAD` answers 200 with no
Location, so it must be GET.

What bounds it. Each rule is load-bearing, and each is pinned by a test that
fails when the rule is deleted:

  * **Only the page's own host is ever requested.** A wrapper is, by
    definition, on the host that served the page. A value already on another
    host is a destination, and GETting destinations would mean fetching every
    result site's first response on the caller's behalf. Never.
  * **The transport is the caller's, not this module's.** `proxy` is whatever
    the runner's `resolve_proxy` handed the caller — on the direct path, the
    egress guard — so a page-controlled stub URL is judged and dialled in ONE
    act, exactly as the browser's own requests are. A pre-flight name check
    followed by a GET by name was measured to be the very TOCTOU that guard
    exists to close. There is one lifecycle for the guard and it is the
    caller's; a refusal comes back as a proxy error and leaves the value as it
    was — counted, never named, in `warnings`. When the caller has NO guard to
    hand over (the transport guard disabled by setting; a runner that guards
    its own requests by route interception, which httpx never sees), each stub
    is checked with `assert_navigable` before it is dialled — the same fallback
    layer the browser keeps in that configuration, TOCTOU and all, which is
    exactly why the guard is the default.
  * **The body is never read.** The request is opened as a stream and closed
    after the headers. A page host that answers a stub with a multi-hundred-MB
    body would otherwise be read in full, five at a time, inside a worker
    budgeted at ~1.5 GB. And there is one deadline for the whole batch, not
    only per socket operation; stubs still pending at the deadline are left
    as-is and what already resolved is kept. The caller sizes that deadline
    inside what remains of the page-task budget, so it fires first.
  * **`Location` is page-controlled text and is not "the destination" just
    because it is absolute.** Refused, each with its own count: a block page
    (`/sorry`, `/showcaptcha` — the shape a rate-limited Google actually
    answers with, so shipping it as a link would be worse than the raw stub);
    a Location still on the page's host (a second hop, a canonical redirect,
    an http→https upgrade); a Location whose path is the stub's own path on
    any host; anything not an absolute http(s) URL with a well-formed host.

Direct from this host rather than through the scrape's residential exit —
measured to work without one, it spares the exit, ~120 ms per stub — is the
CALLER's choice (`resolve_proxy(None)`), not this module's; the same call
with the scrape's own proxy would resolve through the exit instead. The
User-Agent is the same Chrome-shaped string the desktop browser profile starts
from; the header was measured not to be load-bearing, and no Referer is sent
(it would carry the query, which the token already implies).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import ssl
from collections import Counter
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from src.browser.runner import DESKTOP, redirected_to_block
from src.extract.extractor import _host_matches
from src.proxy.models import ProxyConfig
from src.schemas import RESOLVE_REDIRECTS_MAX_FIELDS, RESOLVE_REDIRECTS_MAX_LINKS
from src.security.egress import EgressBlocked, _hostname_is_well_formed, assert_navigable

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 6.0
BATCH_TIMEOUT_S = 12.0
_CONCURRENCY = 5
_HEADERS: dict[str, str] = {"User-Agent": str(DESKTOP["user_agent"])}

ClientFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]
EgressCheck = Callable[..., Awaitable[None]]

# Outcome -> how the warning line names it, in this order. `no_location` is
# deliberately absent: a stub that answered without a usable Location is
# explained by the status histogram alone ("statuses 200x1") — the status IS
# the explanation there.
_NOTES = {
    "refused": "refused by egress policy",
    "failed": "request failed",
    "blocked": "answered with a block page",
    "still_on_host": "still on the page's host",
    "still_a_stub": "still a redirect stub",
    "timed_out": "timed out",
}


@dataclass
class _Hop:
    field: str
    index: int | None  # None for a scalar field
    stub_url: str


@dataclass
class _Result:
    destination: str | None
    outcome: str
    status: int | None


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """One CA-bundle parse per process, not two per page.

    `httpx.AsyncClient(proxy=...)` builds two transports and each parses
    certifi eagerly — measured 27 ms of CPU per resolved page, 0.17 ms with a
    shared context. Lazy, so each forked worker builds its own.
    """
    return httpx.create_ssl_context()


def _is_absolute_http_url(candidate: str) -> bool:
    """An absolute http(s) URL with a well-formed host — the only shape that can
    be a destination at all. Page-controlled text, so every check is explicit."""
    try:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.netloc)
        and not any(ch.isspace() for ch in parts.netloc)
        and _hostname_is_well_formed(host)
    )


def _accept_location(location: str, *, stub_url: str, page_host: str) -> tuple[str | None, str]:
    """(destination, outcome) for a Location header, or (None, why not)."""
    candidate = location.strip()
    if not _is_absolute_http_url(candidate):
        return None, "no_location"
    if redirected_to_block(stub_url, candidate):
        return None, "blocked"
    if _host_matches(candidate, page_host):
        return None, "still_on_host"
    stub_path = urlsplit(stub_url).path
    if stub_path and stub_path != "/" and urlsplit(candidate).path == stub_path:
        # Not yet observed in the wild — no probe has recorded a cross-host
        # hop. Kept because the consumer treats any absolute value as a
        # destination, and a stub on another ccTLD is one it would follow
        # straight into a second redirect.
        return None, "still_a_stub"
    return candidate, "resolved"


async def _resolve_one(
    client: httpx.AsyncClient, hop: _Hop, page_host: str, egress_check: EgressCheck | None
) -> _Result:
    """One stub, one GET, headers only. Never raises for anything the page or the network does.

    `egress_check` is the fallback for a client that dials by name (no guard in
    the proxy slot); it is None when the guard is the transport.
    """
    if egress_check is not None:
        try:
            await egress_check(hop.stub_url, resolve=True)
        except EgressBlocked:
            return _Result(None, "refused", None)
    try:
        async with client.stream("GET", hop.stub_url, headers=_HEADERS) as response:
            status = response.status_code
            location = response.headers.get("location") or ""
    except httpx.ProxyError:
        # The egress guard answered the CONNECT with its refusal.
        return _Result(None, "refused", None)
    except httpx.InvalidURL:
        # httpx builds `next_request` from a 3xx Location even when it is not
        # following redirects, and a page-controlled Location it cannot parse
        # (`javascript:alert(1)`) surfaces HERE, as InvalidURL — which is not
        # an HTTPError. That is a Location we refuse, not a transport failure.
        return _Result(None, "no_location", None)
    except httpx.HTTPError as exc:
        log.info("redirect resolution failed: %s", type(exc).__name__)
        return _Result(None, "failed", None)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Last resort, so the contract in the module docstring holds: a
        # successful fetch is never failed by whatever a stub did to us. Logged
        # louder than a transport error because it is a shape nobody expected.
        log.warning("redirect resolution raised %s: %s", type(exc).__name__, exc)
        return _Result(None, "failed", None)
    # Read Location whatever the status: Google has been seen answering a
    # /goto stub with 402 + Location (autom.dev, 2026-08-31).
    destination, outcome = _accept_location(location, stub_url=hop.stub_url, page_host=page_host)
    return _Result(destination, outcome, status)


async def _resolve_all(
    client: httpx.AsyncClient,
    hops: list[_Hop],
    page_host: str,
    batch_timeout_s: float,
    egress_check: EgressCheck | None,
) -> list[_Result]:
    """Every hop, `_CONCURRENCY` at a time, under ONE deadline; partials survive.

    Results are written in place as each hop finishes, so a deadline leaves the
    finished ones in `results` and the rest as `timed_out`. `gather` reaps its
    children on cancellation, so nothing outlives the client that follows.
    """
    results = [_Result(None, "timed_out", None)] * len(hops)
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def _run(index: int, hop: _Hop) -> None:
        async with semaphore:
            results[index] = await _resolve_one(client, hop, page_host, egress_check)

    try:
        await asyncio.wait_for(
            asyncio.gather(*(_run(i, hop) for i, hop in enumerate(hops))),
            timeout=batch_timeout_s,
        )
    except asyncio.TimeoutError:
        pass
    return results


def _client(proxy: ProxyConfig | None, timeout_s: float) -> httpx.AsyncClient:
    """The production client, dialling whatever the caller's transport is.

    `trust_env=False`: an HTTP(S)_PROXY in the worker's environment must not
    silently route requests somewhere else.
    """
    proxy_arg = None
    if proxy is not None:
        auth = (proxy.username or "", proxy.password or "") if (proxy.username or proxy.password) else None
        proxy_arg = httpx.Proxy(proxy.server, auth=auth)
    return httpx.AsyncClient(
        proxy=proxy_arg, verify=_ssl_context(), trust_env=False,
        follow_redirects=False, timeout=timeout_s,
    )


def _collect_hops(data: dict, fields: list[str], base_url: str, page_host: str) -> list[_Hop]:
    hops: list[_Hop] = []
    for field in fields:
        value = data.get(field)
        items = list(enumerate(value)) if isinstance(value, list) else [(None, value)]
        for index, item in items:
            if not isinstance(item, str) or not item:
                continue
            try:
                absolute = urljoin(base_url, item)
                on_page_host = _host_matches(absolute, page_host)
            except ValueError:
                # `http://[::1` and friends: urllib raises before any request
                # is made. The value is page text; it stays as it was.
                continue
            if on_page_host:
                hops.append(_Hop(field, index, absolute))
    return hops


def _warning_lines(hops: list[_Hop], results: list[_Result], max_links: int) -> list[str]:
    """One line per field; counts and HTTP statuses only, never a URL.

    `results` covers the first `len(results)` hops — the ones attempted — so
    `zip(hops, results)` is the attempted set and the remainder is the cap.
    """
    lines: list[str] = []
    for field in dict.fromkeys(hop.field for hop in hops):
        total = sum(1 for hop in hops if hop.field == field)
        attempted = [result for hop, result in zip(hops, results) if hop.field == field]
        outcomes = Counter(result.outcome for result in attempted)
        resolved = outcomes["resolved"]
        notes = [f"{outcomes[key]} {label}" for key, label in _NOTES.items() if outcomes[key]]
        if len(attempted) < total:
            notes.append(f"{total - len(attempted)} beyond the cap of {max_links} left as-is")
        if resolved < len(attempted):
            # Status codes are not page content; this is what tells an operator
            # "Google started answering 429" from "the pool never returned".
            statuses = Counter(r.status for r in attempted if r.status is not None)
            if statuses:
                notes.append("statuses " + "/".join(f"{code}x{n}" for code, n in sorted(statuses.items())))
        line = f"field '{field}': resolved {resolved} of {total} redirect stubs"
        if notes:
            line += f" ({', '.join(notes)})"
        lines.append(line)
    return lines


async def resolve_redirect_fields(
    data: Any,
    fields: list[str],
    base_url: str | None,
    *,
    proxy: ProxyConfig | None = None,
    max_links: int = RESOLVE_REDIRECTS_MAX_LINKS,
    timeout_s: float = REQUEST_TIMEOUT_S,
    batch_timeout_s: float = BATCH_TIMEOUT_S,
    client_factory: ClientFactory | None = None,
    egress_check: EgressCheck | None = assert_navigable,
) -> tuple[Any, list[str]]:
    """Replace redirect stubs in the named fields of `data` with their destinations.

    Returns the (possibly rewritten) data and warning lines carrying COUNTS and
    HTTP statuses only — never a URL, since every URL here is page content.
    Never raises for anything the page or the network does: a stub that cannot
    be resolved stays exactly as it was. `proxy` is the caller's transport (see
    the module docstring); when it is None every stub passes `egress_check`
    first — `assert_navigable` unless a test says otherwise. `client_factory`
    exists for tests.
    """
    if not isinstance(data, dict) or not base_url:
        return data, []
    page_host = (urlsplit(base_url).hostname or "").lower()
    if not page_host:
        return data, []

    wanted = list(dict.fromkeys(f for f in fields if isinstance(f, str) and f))[:RESOLVE_REDIRECTS_MAX_FIELDS]
    warnings = [f"field '{field}': no such field to resolve" for field in wanted if field not in data]
    hops = _collect_hops(data, [f for f in wanted if f in data], base_url, page_host)
    if not hops:
        return data, warnings

    attempted = hops[:max_links]
    client_cm = client_factory() if client_factory else _client(proxy, timeout_s)
    check = None if proxy is not None else egress_check
    async with client_cm as client:
        results = await _resolve_all(client, attempted, page_host, batch_timeout_s, check)

    out = dict(data)
    columns: dict[str, list] = {}
    for hop, result in zip(attempted, results):
        if result.destination is None:
            continue
        if hop.index is None:
            out[hop.field] = result.destination
        else:
            columns.setdefault(hop.field, list(out[hop.field]))[hop.index] = result.destination
    out.update(columns)
    warnings.extend(_warning_lines(hops, results, max_links))
    return out, warnings
