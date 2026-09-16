"""`resolve_redirect_fields`: turn a page's own redirect stubs into destinations.

Google rewrote every search-result href to `/goto?url=<opaque token>` (confirmed
2026-08-26). The token is not decodable, but the destination is in the
`Location` header of a plain GET on the stub — measured 2026-09-04 from this
host: no proxy, no cookies, no Referer, no User-Agent, a six-day-old token,
`302 Location: https://www.wired.com/story/best-laptops/`. `HEAD` answers 200
with no Location, so it has to be GET with redirects NOT followed.

Two kinds of test here. Most inject a client over `httpx.MockTransport`, so
they touch no network and no guard. The last class hands the resolver the REAL
egress guard as its `proxy` — the same object the runner's `resolve_proxy(None)`
hands the worker — against a loopback origin, permitted through the allowlist
in one test and refused in the other. The guard is the only thing standing
between a page-controlled URL and this host's network, and a resolver that
merely *documents* dialling through it is the shape of guard this repo keeps
shipping empty.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from src.extract.resolve import resolve_redirect_fields
from src.proxy.models import ProxyConfig
from src.security.egress import EgressBlocked
from src.security.egress_guard import open_egress_guard
from src.settings import settings

PAGE = "https://www.google.com/search?q=best+laptop"
STUB = "/goto?url=CAES"


def _factory(handler):
    """A client_factory over a MockTransport. `handler` may be sync or async."""
    @contextlib.asynccontextmanager
    async def _cm():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            yield client
    return _cm


def _routes(routes, calls):
    """routes: {path: (status, headers)}; every request is appended to `calls`."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status, headers = routes.get(request.url.path, (404, {}))
        return httpx.Response(status, headers=headers)
    return handler


async def _allow(url: str, *, resolve: bool = True) -> None:
    return None


async def _deny(url: str, *, resolve: bool = True) -> None:
    raise EgressBlocked("refused")


async def _resolve(data, fields=("links",), *, handler, egress_check=_allow, **kw):
    """No `proxy` here, so the resolver's own pre-dial check runs; `_allow`
    stands in for `assert_navigable` so nothing resolves a real name."""
    return await resolve_redirect_fields(
        data, list(fields), PAGE, client_factory=_factory(handler), egress_check=egress_check, **kw
    )


# --- the happy path -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_302_location_replaces_the_stub():
    calls = []
    data = {"links": [f"{STUB}1", f"{STUB}2"], "titles": ["a", "b"]}
    out, warnings = await _resolve(
        data, handler=_routes({"/goto": (302, {"Location": "https://www.wired.com/story/best-laptops/"})}, calls)
    )
    assert out["links"] == ["https://www.wired.com/story/best-laptops/"] * 2
    assert out["titles"] == ["a", "b"], "untouched fields stay untouched"
    assert [c.method for c in calls] == ["GET", "GET"], "GET, never HEAD"
    assert warnings == ["field 'links': resolved 2 of 2 redirect stubs"]


@pytest.mark.asyncio
async def test_the_relative_stub_is_joined_against_the_page_url():
    calls = []
    await _resolve({"links": [STUB]}, handler=_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls))
    assert str(calls[0].url) == "https://www.google.com/goto?url=CAES"


@pytest.mark.asyncio
async def test_no_referer_and_a_browser_shaped_user_agent():
    """The measurement was no headers at all. What is sent is the desktop
    profile's Chrome UA and NO Referer — a Referer would carry the query."""
    calls = []
    await _resolve({"links": [STUB]}, handler=_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls))
    assert "referer" not in calls[0].headers
    assert calls[0].headers["user-agent"].startswith("Mozilla/5.0 (Windows NT 10.0")
    assert "python-httpx" not in calls[0].headers["user-agent"]


@pytest.mark.asyncio
async def test_a_location_on_a_non_3xx_is_still_read():
    """autom.dev, 2026-08-31: Google sometimes answers 402 with a Location."""
    calls = []
    out, _ = await _resolve({"links": [STUB]}, handler=_routes({"/goto": (402, {"Location": "https://a.example/p"})}, calls))
    assert out["links"] == ["https://a.example/p"]


@pytest.mark.asyncio
async def test_a_scalar_field_and_non_string_rows_are_handled():
    calls = []
    data = {"link": STUB, "links": [None, 42, f"{STUB}Y"]}
    out, _ = await _resolve(data, ("link", "links"), handler=_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls))
    assert out["link"] == "https://a.example/"
    assert out["links"] == [None, 42, "https://a.example/"]
    assert len(calls) == 2


# --- what is refused, and why -------------------------------------------

@pytest.mark.asyncio
async def test_no_location_leaves_the_value_unchanged_and_says_which_status():
    calls = []
    out, warnings = await _resolve({"links": [STUB]}, handler=_routes({"/goto": (200, {})}, calls))
    assert out["links"] == [STUB]
    assert warnings == ["field 'links': resolved 0 of 1 redirect stubs (statuses 200x1)"]


@pytest.mark.asyncio
@pytest.mark.parametrize("location", [
    "/relative/path",                # not a destination anyone can follow
    "javascript:alert(1)",           # page-controlled header, refused
    "//evil.example/x",              # scheme-relative authority, refused
    "http:///no-host",               # http with no authority
    "https://exa mple.com/",         # whitespace in the host
    "http://[::1",                   # unparseable; httpx raises on it too
])
async def test_a_location_that_is_not_a_well_formed_absolute_http_url_is_refused(location):
    calls = []
    out, _ = await _resolve({"links": [STUB]}, handler=_routes({"/goto": (302, {"Location": location})}, calls))
    assert out["links"] == [STUB], location


@pytest.mark.asyncio
async def test_a_block_page_is_not_a_destination():
    """THE case a rate-limited Google actually produces: the stub answers with
    `/sorry`. Shipping that as the link would be worse than the raw stub — a
    consumer that follows links (yozh-law-checker does) lands on a captcha
    presented as the result."""
    calls = []
    sorry = "https://www.google.com/sorry/index?continue=https://www.google.com/goto%3Furl%3DCAES"
    out, warnings = await _resolve({"links": [STUB, f"{STUB}2"]}, handler=_routes({"/goto": (302, {"Location": sorry})}, calls))
    assert out["links"] == [STUB, f"{STUB}2"]
    assert warnings == ["field 'links': resolved 0 of 2 redirect stubs (2 answered with a block page, statuses 302x2)"]


@pytest.mark.asyncio
async def test_a_location_still_on_the_pages_host_is_not_a_destination():
    """A second hop, a canonical redirect, an http->https upgrade: absolute,
    well-formed, and not where the result goes."""
    calls = []
    out, warnings = await _resolve(
        {"links": [STUB]}, handler=_routes({"/goto": (301, {"Location": "https://www.google.com/goto?url=SECONDHOP"})}, calls)
    )
    assert out["links"] == [STUB]
    assert "1 still on the page's host" in warnings[0]


@pytest.mark.asyncio
async def test_the_same_stub_on_another_host_is_not_a_destination():
    """google.de answering with the same /goto on google.com. Not yet seen in
    the wild; kept because the consumer follows any absolute value."""
    calls = []
    out, warnings = await _resolve(
        {"links": [STUB]}, handler=_routes({"/goto": (302, {"Location": "https://www.google.de/goto?url=CAES"})}, calls)
    )
    assert out["links"] == [STUB]
    assert "1 still a redirect stub" in warnings[0]


@pytest.mark.asyncio
async def test_only_the_pages_own_host_is_ever_requested():
    """A wrapper is by definition on the page's own host. A value already on
    another host is a destination, and GETting destinations would mean
    fetching every result site's first response — never do that."""
    calls = []
    data = {"links": ["https://www.wired.com/story/", STUB, "https://www.google.com/goto?url=Y"]}
    out, _ = await _resolve(data, handler=_routes({"/goto": (302, {"Location": "https://dest.example/"})}, calls))
    assert out["links"] == ["https://www.wired.com/story/", "https://dest.example/", "https://dest.example/"]
    assert len(calls) == 2
    assert all(c.url.host == "www.google.com" for c in calls)


@pytest.mark.asyncio
async def test_without_a_transport_guard_every_stub_is_checked_before_it_is_dialled():
    """`EGRESS_TRANSPORT_GUARD=false`, or a runner that guards by route
    interception (Camoufox), hands the resolver no proxy. Then the same
    fallback the browser keeps in that configuration runs per stub: refused
    means never requested, counted, never named."""
    calls = []
    out, warnings = await _resolve(
        {"links": [STUB, f"{STUB}2"]}, egress_check=_deny,
        handler=_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls),
    )
    assert calls == [], "a refused stub is never dialled"
    assert out["links"] == [STUB, f"{STUB}2"]
    assert warnings == ["field 'links': resolved 0 of 2 redirect stubs (2 refused by egress policy)"]


@pytest.mark.asyncio
async def test_with_a_transport_guard_the_fallback_check_is_not_consulted():
    """The guard IS the check; running the name-based one as well would only
    re-open the TOCTOU the guard closes. Pinned: a denying check is ignored
    when a proxy is given."""
    calls = []
    out, _ = await resolve_redirect_fields(
        {"links": [STUB]}, ["links"], PAGE, proxy=ProxyConfig(server="http://127.0.0.1:1"),
        client_factory=_factory(_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls)),
        egress_check=_deny,
    )
    assert out["links"] == ["https://a.example/"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_malformed_href_stays_as_it_was_and_crashes_nothing():
    """`urllib.parse` raises on `http://[::1` before any request is made. It
    is page text; the contract is that it stays untouched."""
    calls = []
    out, warnings = await _resolve(
        {"links": ["http://[::1", STUB]},
        handler=_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls),
    )
    assert out["links"] == ["http://[::1", "https://a.example/"]
    assert len(calls) == 1
    assert warnings == ["field 'links': resolved 1 of 1 redirect stubs"]


@pytest.mark.asyncio
async def test_the_cap_bounds_how_many_stubs_are_requested():
    calls = []
    data = {"links": [f"{STUB}{i}" for i in range(5)]}
    out, warnings = await _resolve(data, max_links=2, handler=_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls))
    assert out["links"][:2] == ["https://a.example/"] * 2
    assert out["links"][2:] == [f"{STUB}2", f"{STUB}3", f"{STUB}4"]
    assert len(calls) == 2
    assert warnings == ["field 'links': resolved 2 of 5 redirect stubs (3 beyond the cap of 2 left as-is)"]


@pytest.mark.asyncio
async def test_a_transport_failure_is_a_counted_failure_logged_quietly(caplog):
    """An HTTPError is the expected way a stub fails (no route, timeout): it is
    counted, and it is NOT a warning-level log — that level is reserved for a
    shape nobody expected (next test), so an operator's log stays readable."""
    def boom(request):
        raise httpx.ConnectError("no route")
    with caplog.at_level(logging.INFO, logger="src.extract.resolve"):
        out, warnings = await _resolve({"links": [STUB]}, handler=boom)
    assert out["links"] == [STUB]
    assert warnings == ["field 'links': resolved 0 of 1 redirect stubs (1 request failed)"]
    assert "goto" not in " ".join(warnings), "page content never enters warnings"
    assert [r.levelno for r in caplog.records] == [logging.INFO]


@pytest.mark.asyncio
async def test_anything_else_a_stub_does_to_us_is_a_failure_not_a_crash(caplog):
    def weird(request):
        raise RuntimeError("something nobody anticipated")
    with caplog.at_level(logging.INFO, logger="src.extract.resolve"):
        out, warnings = await _resolve({"links": [STUB]}, handler=weird)
    assert out["links"] == [STUB]
    assert warnings == ["field 'links': resolved 0 of 1 redirect stubs (1 request failed)"]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


# --- the two resource bounds ---------------------------------------------

class _BodyMustNotBeRead(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise AssertionError("the response body was read")
        yield b""  # pragma: no cover - makes this an async generator


@pytest.mark.asyncio
async def test_the_body_is_never_read():
    """Headers only. A page host answering a stub with a multi-hundred-MB body
    must cost nothing but the headers — five of these in flight would OOM the
    worker otherwise."""
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://a.example/"}, stream=_BodyMustNotBeRead())
    out, warnings = await _resolve({"links": [STUB]}, handler=handler)
    assert out["links"] == ["https://a.example/"]
    assert warnings == ["field 'links': resolved 1 of 1 redirect stubs"]


@pytest.mark.asyncio
async def test_one_deadline_for_the_whole_batch_and_partials_survive():
    """A slow drip must not spend the page-task ceiling, and what did resolve
    before the deadline is kept."""
    async def handler(request):
        if request.url.query == b"url=CAESslow":
            await asyncio.sleep(5)
        return httpx.Response(302, headers={"Location": "https://a.example/"})
    loop = asyncio.get_running_loop()
    started = loop.time()
    out, warnings = await _resolve({"links": [f"{STUB}fast", f"{STUB}slow"]}, batch_timeout_s=0.5, handler=handler)
    assert loop.time() - started < 3.0
    assert out["links"] == ["https://a.example/", f"{STUB}slow"]
    assert warnings == ["field 'links': resolved 1 of 2 redirect stubs (1 timed out, statuses 302x1)"]


# --- field handling --------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_field_names_are_requested_once():
    calls = []
    out, warnings = await _resolve({"links": [STUB]}, ("links", "links"), handler=_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls))
    assert len(calls) == 1
    assert warnings == ["field 'links': resolved 1 of 1 redirect stubs"]


@pytest.mark.asyncio
async def test_a_field_that_does_not_exist_is_said_so():
    """A caller who mistypes the field name gets a signal, not silence."""
    calls = []
    out, warnings = await _resolve({"links": [STUB]}, ("lnks",), handler=_routes({"/goto": (302, {"Location": "https://a.example/"})}, calls))
    assert calls == []
    assert out["links"] == [STUB]
    assert warnings == ["field 'lnks': no such field to resolve"]


@pytest.mark.asyncio
async def test_nothing_to_do_is_silent():
    async def never(request):
        raise AssertionError("no request expected")
    out, warnings = await _resolve({"links": ["https://a.example/x"]}, handler=never)
    assert out == {"links": ["https://a.example/x"]} and warnings == []
    out, warnings = await resolve_redirect_fields({"links": [STUB]}, ["links"], None, client_factory=_factory(never))
    assert out == {"links": [STUB]} and warnings == [], "no page URL, nothing to join or compare against"


# --- through the REAL egress guard, as the runner hands it over ----------

class _Origin(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802 - stdlib API
        type(self).hits += 1
        self.send_response(302)
        self.send_header("Location", "https://dest.example/landing")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):  # silence
        return None


@contextlib.contextmanager
def _serving():
    _Origin.hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_the_production_client_really_dials_through_the_guard(monkeypatch):
    """`proxy` is the guard, exactly as `resolve_proxy(None)` hands it to the
    worker. The loopback origin is reachable only because the test allowlists
    127.0.0.1 — which is what proves the guard was consulted."""
    monkeypatch.setattr(settings, "egress_allow_hosts", "127.0.0.1")
    with _serving() as port:
        page = f"http://127.0.0.1:{port}/search?q=x"
        async with open_egress_guard(resolve=True) as guard:
            out, warnings = await resolve_redirect_fields(
                {"links": ["/goto?url=X"]}, ["links"], page, proxy=ProxyConfig(server=guard.url)
            )
    assert out["links"] == ["https://dest.example/landing"]
    assert warnings == ["field 'links': resolved 1 of 1 redirect stubs"]
    assert _Origin.hits == 1
    assert guard.denied == []


@pytest.mark.asyncio
async def test_a_target_the_guard_refuses_is_never_dialled():
    """Same origin, no allowlist: loopback is refused by policy. The origin
    must see NO request, the guard must record the refusal, and the value
    must come back untouched — counted, never named. For a plain-http stub the
    guard's refusal arrives as a 403 response (a CONNECT refusal, the https
    case, surfaces as a ProxyError and is counted as `refused`)."""
    with _serving() as port:
        page = f"http://127.0.0.1:{port}/search?q=x"
        async with open_egress_guard(resolve=True) as guard:
            out, warnings = await resolve_redirect_fields(
                {"links": ["/goto?url=X"]}, ["links"], page, proxy=ProxyConfig(server=guard.url)
            )
    assert out["links"] == ["/goto?url=X"]
    assert _Origin.hits == 0
    assert len(guard.denied) == 1
    assert warnings == ["field 'links': resolved 0 of 1 redirect stubs (statuses 403x1)"]
    assert "127.0.0.1" not in warnings[0] and "goto" not in warnings[0]
