from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

from types import SimpleNamespace

from fastapi import HTTPException

from src.api.map import build_map, create_map
from src.fetcher import ScraperError
from src.schemas import CrawlScope, MapRequest


@pytest.fixture(autouse=True)
def _allow_all_hosts(mocker):
    # Bypass the SSRF DNS check so unit tests don't hit the network.
    mocker.patch("src.ssrf.host_is_public", mocker.AsyncMock(return_value=True))


def _resp(status_code: int, text: str = "", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, text=text, headers=headers)


def _client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    """A real client over `httpx.MockTransport`: what `safe_get` streams,
    redirects and closes is exactly what a network response would be."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    client.calls = calls  # type: ignore[attr-defined]
    return client


_SITEMAP = (
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://x.com/a</loc></url>"
    "<url><loc>https://x.com/a</loc></url>"  # duplicate -> deduped
    "<url><loc>https://y.com/evil</loc></url>"  # other domain -> out of scope
    "</urlset>"
)
_SEED_HTML = '<html><body><a href="/p1">1</a><a href="https://x.com/p2">2</a></body></html>'
_SITEMAP_WWW = (
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://www.x.com/</loc></url>"  # twin of the seed -> deduped
    "<url><loc>https://www.x.com/a</loc></url>"
    "<url><loc>https://y.com/evil</loc></url>"  # other domain -> still out of scope
    "</urlset>"
)
_SITEMAP_DATED = (
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://x.com/old</loc><lastmod>2020-01-01</lastmod></url>"
    "<url><loc>https://x.com/new</loc><lastmod>2026-06-15</lastmod></url>"
    "<url><loc>https://x.com/mid</loc><lastmod>2023-03-03</lastmod></url>"
    "<url><loc>https://x.com/undated</loc></url>"
    "</urlset>"
)


def _req(**kw):
    return MapRequest(seed_url="https://x.com/", **kw)


class TestBuildMap:
    @pytest.mark.asyncio
    async def test_sitemap_discovery_scope_and_dedup(self):
        client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP)})
        res = await build_map(
            _req(include_page_links=False), http_client=client, scraper_fetch=None
        )
        # seed + /a ; /a dedup'd ; y.com dropped by same-domain scope
        assert res.urls == ["https://x.com/", "https://x.com/a"]
        assert res.stats.from_sitemap == 3
        assert res.stats.unique_in_scope == 2

    @pytest.mark.asyncio
    async def test_sitemap_www_alias_urls_stay_in_scope(self):
        # www-canonical site, bare-domain seed: the sitemap lists www URLs;
        # same-domain scope must keep them (they are the same site).
        client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP_WWW)})
        res = await build_map(
            _req(include_page_links=False), http_client=client, scraper_fetch=None
        )
        assert res.urls == ["https://x.com/", "https://www.x.com/a"]

    @pytest.mark.asyncio
    async def test_published_after_filters_by_lastmod(self):
        client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP_DATED)})
        res = await build_map(
            _req(include_page_links=False, published_after=date(2024, 1, 1)),
            http_client=client, scraper_fetch=None,
        )
        # Only /new (2026) is on/after the cutoff; old/mid/undated + the seed
        # (no lastmod) are dropped. lastmod is echoed for the survivor.
        assert res.urls == ["https://x.com/new"]
        assert res.lastmod == {"https://x.com/new": "2026-06-15"}
        assert res.stats.with_lastmod == 1

    @pytest.mark.asyncio
    async def test_sort_newest_orders_by_lastmod_undated_last(self):
        client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP_DATED)})
        res = await build_map(
            _req(include_page_links=False, sort="newest"),
            http_client=client, scraper_fetch=None,
        )
        assert res.urls[:3] == [
            "https://x.com/new", "https://x.com/mid", "https://x.com/old",
        ]
        # seed + /undated carry no date -> sorted last (order among them unspecified)
        assert set(res.urls[3:]) == {"https://x.com/", "https://x.com/undated"}

    @pytest.mark.asyncio
    async def test_recent_days_keeps_dated_drops_undated(self):
        client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP_DATED)})
        # Near-max window (~98y): every dated URL qualifies, undated (+seed) drop.
        res = await build_map(
            _req(include_page_links=False, recent_days=36_000),
            http_client=client, scraper_fetch=None,
        )
        assert set(res.urls) == {
            "https://x.com/old", "https://x.com/mid", "https://x.com/new",
        }

    def test_recent_days_rejects_out_of_range(self):
        # Bounded so today()-timedelta(days=N) can't OverflowError into a 500.
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            _req(recent_days=10_000_000)

    @pytest.mark.asyncio
    async def test_published_after_then_sort_newest(self):
        client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP_DATED)})
        res = await build_map(
            _req(include_page_links=False, published_after=date(2021, 1, 1), sort="newest"),
            http_client=client, scraper_fetch=None,
        )
        # /old(2020) filtered out; /new(2026) before /mid(2023) after sort.
        assert res.urls == ["https://x.com/new", "https://x.com/mid"]

    @pytest.mark.asyncio
    async def test_date_filter_empty_result_warns(self):
        client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP_DATED)})
        res = await build_map(
            _req(include_page_links=False, published_after=date(2099, 1, 1)),
            http_client=client, scraper_fetch=None,
        )
        assert res.urls == []
        assert any("date filter matched no URLs" in w for w in res.warnings)

    def test_lastmod_date_parsing(self):
        from src.api.map import _lastmod_date
        assert _lastmod_date("2026-06-01") == date(2026, 6, 1)
        assert _lastmod_date("2026-06-01T08:30:00+00:00") == date(2026, 6, 1)
        for bad in (None, "", "garbage", "2026-13-01", "2026"):
            assert _lastmod_date(bad) is None

    @pytest.mark.asyncio
    async def test_page_links_via_httpx(self):
        client = _client({"https://x.com/": _resp(200, _SEED_HTML)})
        res = await build_map(
            _req(include_sitemap=False), http_client=client, scraper_fetch=None
        )
        assert "https://x.com/p1" in res.urls
        assert "https://x.com/p2" in res.urls
        assert res.stats.from_page == 2

    @pytest.mark.asyncio
    async def test_render_uses_scraper(self):
        calls = []

        async def fake_fetch(url, opts):
            calls.append((url, opts))
            return {"raw_html": _SEED_HTML}

        client = _client({})
        res = await build_map(
            _req(include_sitemap=False, render=True),
            http_client=client,
            scraper_fetch=fake_fetch,
        )
        assert calls and calls[0][1].get("render") is True
        assert "https://x.com/p1" in res.urls

    @pytest.mark.asyncio
    async def test_render_blocked_seed_is_not_used(self):
        # HIGH-23: a rendered seed the scraper marked fetch_ok=False (captcha/
        # ban) must not have its block body walked for links; it degrades to a
        # warning like a non-200 seed, yielding no page links.
        async def fake_fetch(url, opts):
            return {
                "meta": {"status_code": 503, "fetch_ok": False},
                "raw_html": _SEED_HTML,
                "error": "HTTP 503",
            }

        client = _client({})
        res = await build_map(
            _req(include_sitemap=False, render=True),
            http_client=client,
            scraper_fetch=fake_fetch,
        )
        assert "https://x.com/p1" not in res.urls
        assert any("render" in w.lower() for w in res.warnings)

    @pytest.mark.asyncio
    async def test_search_filter(self):
        client = _client({"https://x.com/": _resp(200, _SEED_HTML)})
        res = await build_map(
            _req(include_sitemap=False, search="p2"),
            http_client=client,
            scraper_fetch=None,
        )
        assert res.urls == ["https://x.com/p2"]

    @pytest.mark.asyncio
    async def test_limit_caps_results(self):
        client = _client({"https://x.com/": _resp(200, _SEED_HTML)})
        res = await build_map(
            _req(include_sitemap=False, limit=1),
            http_client=client,
            scraper_fetch=None,
        )
        assert len(res.urls) == 1
        assert res.count == 1

    @pytest.mark.asyncio
    async def test_seed_fetch_failure_is_warning_not_error(self):
        client = _client({"https://x.com/": _resp(500)})
        res = await build_map(
            _req(include_sitemap=False), http_client=client, scraper_fetch=None
        )
        assert res.urls == ["https://x.com/"]  # only the seed
        assert any("seed fetch returned 500" in w for w in res.warnings)

    @pytest.mark.asyncio
    async def test_output_is_canonicalized(self):
        # Fragment dropped, default port stripped, duplicate variants collapsed —
        # so /map returns the same URL shape as /crawl.
        sitemap = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://x.com:443/a#frag</loc></url>"
            "<url><loc>https://x.com/a</loc></url>"
            "</urlset>"
        )
        client = _client({"https://x.com/sitemap.xml": _resp(200, sitemap)})
        res = await build_map(
            _req(include_page_links=False), http_client=client, scraper_fetch=None
        )
        assert res.urls == ["https://x.com/", "https://x.com/a"]

    @pytest.mark.asyncio
    async def test_ssrf_private_seed_is_blocked_with_warning(self, mocker):
        # Override the autouse allow-all: a private/internal host must be refused.
        mocker.patch("src.ssrf.host_is_public", mocker.AsyncMock(return_value=False))
        client = _client({})
        res = await build_map(
            MapRequest(seed_url="http://169.254.169.254/", include_sitemap=False),
            http_client=client,
            scraper_fetch=None,
        )
        assert res.urls == ["http://169.254.169.254/"]  # seed echoed, never fetched
        assert any("seed fetch failed" in w for w in res.warnings)

    @pytest.mark.asyncio
    async def test_page_links_resolve_against_post_redirect_base(self):
        # Seed redirects to /new/; relative links must resolve against the final URL.
        client = _client({
            "https://x.com/": _resp(302, headers={"location": "https://x.com/new/"}),
            "https://x.com/new/": _resp(200, '<a href="rel">r</a>'),
        })
        res = await build_map(
            _req(include_sitemap=False), http_client=client, scraper_fetch=None
        )
        assert "https://x.com/new/rel" in res.urls

    @pytest.mark.asyncio
    async def test_regex_scope_filters(self):
        client = _client({"https://x.com/": _resp(200, _SEED_HTML)})
        scope = CrawlScope(mode="regex", include_patterns=[r"/p2$"])
        res = await build_map(
            _req(include_sitemap=False, scope=scope),
            http_client=client,
            scraper_fetch=None,
        )
        assert res.urls == ["https://x.com/p2"]


class TestMapTiming:
    @pytest.mark.asyncio
    async def test_response_includes_took_ms(self):
        client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP)})
        res = await build_map(
            _req(include_page_links=False), http_client=client, scraper_fetch=None
        )
        assert isinstance(res.took_ms, int)
        assert res.took_ms >= 0


class TestMapProxy:
    @pytest.mark.asyncio
    async def test_request_accepts_proxy_fields(self):
        req = MapRequest(
            seed_url="https://x.com/",
            proxy_type="res_rotating",
            proxy_pool_id="pool1",
            proxy_geo={"country_code": "US"},
        )
        assert req.proxy_type == "res_rotating"
        assert req.proxy_pool_id == "pool1"
        assert req.proxy_geo.country_code == "US"

    @pytest.mark.asyncio
    async def test_proxy_type_defaults_none(self):
        assert MapRequest(seed_url="https://x.com/").proxy_type == "none"

    @pytest.mark.asyncio
    async def test_render_forwards_proxy_to_scraper(self):
        calls = []

        async def fake_fetch(url, opts):
            calls.append(opts)
            return {"raw_html": _SEED_HTML}

        req = MapRequest(
            seed_url="https://x.com/", include_sitemap=False, render=True,
            proxy_type="res_rotating", proxy_pool_id="pool1",
            proxy_geo={"country_code": "US"},
        )
        await build_map(req, http_client=_client({}), scraper_fetch=fake_fetch)
        assert calls and calls[0]["proxy_type"] == "res_rotating"
        assert calls[0]["proxy_pool_id"] == "pool1"
        assert calls[0]["proxy_geo"] == {"country_code": "US", "region": None, "city": None}

    @pytest.mark.asyncio
    async def test_check_ssrf_false_skips_host_block(self, mocker):
        # When proxied (check_ssrf=False) a private host must NOT be blocked —
        # egress goes through the proxy, the crawler isn't the SSRF vector.
        mocker.patch("src.ssrf.host_is_public", mocker.AsyncMock(return_value=False))
        client = _client({"https://x.com/": _resp(200, _SEED_HTML)})
        res = await build_map(
            _req(include_sitemap=False), http_client=client,
            scraper_fetch=None, check_ssrf=False,
        )
        assert "https://x.com/p1" in res.urls  # fetched despite private verdict

    @pytest.mark.asyncio
    async def test_extra_warnings_surfaced(self):
        res = await build_map(
            _req(include_sitemap=False, include_page_links=False),
            http_client=_client({}), scraper_fetch=None,
            extra_warnings=["proxy resolve failed; used direct"],
        )
        assert any("proxy resolve failed" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_seed_render_timeout_falls_back_to_sitemap(monkeypatch):
    # A stalled seed render must not hold the fast sitemap result hostage:
    # /map should time out the render and return sitemap-only.
    import src.api.map as mapmod

    monkeypatch.setattr(mapmod.settings, "map_render_timeout_ms", 50)

    async def hanging_render(url, opts):
        await asyncio.sleep(5)  # never returns within the 50ms render cap

    client = _client({"https://x.com/sitemap.xml": _resp(200, _SITEMAP)})
    res = await build_map(
        _req(include_page_links=True, render=True),
        http_client=client,
        scraper_fetch=hanging_render,
    )
    assert "https://x.com/a" in res.urls  # sitemap result survived the render timeout
    assert any("sitemap-only" in w for w in res.warnings)


class TestProxyScrapeOptions:
    def test_includes_prem_options_on_render_path(self):
        from src.api.map import _proxy_scrape_options
        req = _req(
            proxy_type="prem_res_rotating",
            proxy_geo={"country_code": "RU"},
            prem_proxy_options={"ip_filter": "quality-security"},
        )
        opts = _proxy_scrape_options(req)
        assert opts["proxy_type"] == "prem_res_rotating"
        assert opts["proxy_geo"]["country_code"] == "RU"
        # prem targeting must reach the JS-render seed fetch too (exclude_none)
        assert opts["prem_proxy_options"] == {"ip_filter": "quality-security"}

    def test_empty_for_none(self):
        from src.api.map import _proxy_scrape_options
        assert _proxy_scrape_options(_req()) == {}


# ─── HIGH-11: /map fails closed when an explicit proxy can't be provided ────────

def _app_req(scraper, http_client):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(scraper_client=scraper, http_client=http_client)
        )
    )


@pytest.mark.asyncio
async def test_map_explicit_proxy_unavailable_fails_closed():
    # An explicit proxy the scraper can't provide (its fail-closed 422 → a
    # ScraperError here) must FAIL the /map request, not silently degrade to a
    # direct fetch that leaks the crawler's real IP.
    async def resolve_proxy(*a, **k):
        raise ScraperError(status_code=422, message="no CyberYozh key", retryable=False)

    scraper = SimpleNamespace(resolve_proxy=resolve_proxy, fetch=None)
    req = _req(include_sitemap=False, proxy_type="res_rotating")

    with pytest.raises(HTTPException) as exc:
        await create_map(req, _app_req(scraper, _client({})))
    assert exc.value.status_code == 502
    assert "proxy required" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_map_proxy_none_still_goes_direct():
    # proxy_type=none is a legitimate explicit direct request — the fail-closed
    # guard must not touch it (nor try to resolve a proxy).
    resolve_called = False

    async def resolve_proxy(*a, **k):
        nonlocal resolve_called
        resolve_called = True
        return None

    scraper = SimpleNamespace(resolve_proxy=resolve_proxy, fetch=None)
    client = _client({"https://x.com/": _resp(200, _SEED_HTML)})
    req = _req(include_sitemap=False)  # proxy_type defaults to "none"

    res = await create_map(req, _app_req(scraper, client))
    assert resolve_called is False
    assert "https://x.com/p2" in res.urls
