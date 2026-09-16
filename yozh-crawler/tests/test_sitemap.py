from __future__ import annotations

import httpx
import pytest

from src.sitemap import (
    collect_sitemap_urls,
    discover_sitemap_urls,
    parse_robots_sitemaps,
    parse_sitemap_xml,
)


class TestParseSitemapXml:
    def test_urlset_returns_pages(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://x.com/a</loc></url>"
            "<url><loc>https://x.com/b</loc></url>"
            "</urlset>"
        )
        pages, sitemaps = parse_sitemap_xml(xml)
        assert pages == [("https://x.com/a", None), ("https://x.com/b", None)]
        assert sitemaps == []

    def test_urlset_extracts_lastmod(self):
        xml = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://x.com/a</loc><lastmod>2026-06-01</lastmod></url>"
            "<url><loc>https://x.com/b</loc>"
            "<lastmod>2026-06-10T08:30:00+00:00</lastmod></url>"
            "<url><loc>https://x.com/c</loc></url>"  # no lastmod
            "</urlset>"
        )
        pages, _ = parse_sitemap_xml(xml)
        assert pages == [
            ("https://x.com/a", "2026-06-01"),
            ("https://x.com/b", "2026-06-10T08:30:00+00:00"),
            ("https://x.com/c", None),
        ]

    def test_sitemapindex_returns_child_sitemaps(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://x.com/sitemap1.xml</loc></sitemap>"
            "<sitemap><loc>https://x.com/sitemap2.xml</loc></sitemap>"
            "</sitemapindex>"
        )
        pages, sitemaps = parse_sitemap_xml(xml)
        assert pages == []
        assert sitemaps == ["https://x.com/sitemap1.xml", "https://x.com/sitemap2.xml"]

    def test_malformed_returns_empty(self):
        pages, sitemaps = parse_sitemap_xml("not xml <<<")
        assert pages == [] and sitemaps == []

    def test_blank_locs_skipped(self):
        xml = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc></loc></url><url><loc>https://x.com/a</loc></url></urlset>"
        )
        pages, _ = parse_sitemap_xml(xml)
        assert pages == [("https://x.com/a", None)]


class TestParseRobotsSitemaps:
    def test_extracts_sitemap_directives(self):
        robots = (
            "User-agent: *\nDisallow: /private\n"
            "Sitemap: https://x.com/sitemap.xml\n"
            "sitemap: https://x.com/news.xml\n"
        )
        out = parse_robots_sitemaps(robots, base_url="https://x.com")
        assert out == ["https://x.com/sitemap.xml", "https://x.com/news.xml"]

    def test_relative_sitemap_absolutized(self):
        out = parse_robots_sitemaps("Sitemap: /sm.xml", base_url="https://x.com")
        assert out == ["https://x.com/sm.xml"]

    def test_no_directives_returns_empty(self):
        assert parse_robots_sitemaps("User-agent: *\nDisallow:", base_url="https://x.com") == []


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


_URLSET = (
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://x.com/a</loc></url><url><loc>https://x.com/b</loc></url></urlset>"
)
_INDEX = (
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<sitemap><loc>https://x.com/child.xml</loc></sitemap></sitemapindex>"
)


class TestDiscoverSitemapUrls:
    @pytest.mark.asyncio
    async def test_uses_robots_directives(self):
        client = _client({
            "https://x.com/robots.txt": _resp(200, "Sitemap: https://x.com/custom.xml"),
        })
        out = await discover_sitemap_urls(client, "https://x.com/")
        assert out == ["https://x.com/custom.xml"]

    @pytest.mark.asyncio
    async def test_falls_back_to_common_paths(self):
        client = _client({})  # robots.txt 404
        out = await discover_sitemap_urls(client, "https://x.com/")
        assert out == ["https://x.com/sitemap.xml", "https://x.com/sitemap_index.xml"]


class TestCollectSitemapUrls:
    @pytest.mark.asyncio
    async def test_follows_sitemapindex(self):
        client = _client({
            "https://x.com/sitemap.xml": _resp(200, _INDEX),
            "https://x.com/child.xml": _resp(200, _URLSET),
        })
        out = await collect_sitemap_urls(
            client, ["https://x.com/sitemap.xml"], max_urls=100, max_sitemaps=10
        )
        assert out == [("https://x.com/a", None), ("https://x.com/b", None)]

    @pytest.mark.asyncio
    async def test_respects_max_urls(self):
        client = _client({"https://x.com/sitemap.xml": _resp(200, _URLSET)})
        out = await collect_sitemap_urls(
            client, ["https://x.com/sitemap.xml"], max_urls=1, max_sitemaps=10
        )
        assert out == [("https://x.com/a", None)]

    @pytest.mark.asyncio
    async def test_match_filter_counts_matches_against_cap(self):
        # /blog/ is the 3rd of 3 urls; max_urls=1 with match must still return it
        # (the cap counts matches, not the first N) — regression for the search
        # filter starving on a big sitemap.
        sitemap = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://x.com/about</loc></url>"
            "<url><loc>https://x.com/contact</loc></url>"
            "<url><loc>https://x.com/blog/post-1</loc></url>"
            "</urlset>"
        )
        client = _client({"https://x.com/sitemap.xml": _resp(200, sitemap)})
        out = await collect_sitemap_urls(
            client, ["https://x.com/sitemap.xml"],
            max_urls=1, max_sitemaps=10, match="/blog/",
        )
        assert out == [("https://x.com/blog/post-1", None)]

    @pytest.mark.asyncio
    async def test_missing_sitemap_yields_nothing(self):
        client = _client({})
        out = await collect_sitemap_urls(
            client, ["https://x.com/sitemap.xml"], max_urls=100, max_sitemaps=10
        )
        assert out == []

    @pytest.mark.asyncio
    async def test_sitemapindex_cycle_terminates(self):
        # A -> B -> A must not loop forever (seen-set guard).
        a = (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://x.com/b.xml</loc></sitemap></sitemapindex>"
        )
        b = (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://x.com/a.xml</loc></sitemap></sitemapindex>"
        )
        client = _client({
            "https://x.com/a.xml": _resp(200, a),
            "https://x.com/b.xml": _resp(200, b),
        })
        out = await collect_sitemap_urls(
            client, ["https://x.com/a.xml"], max_urls=100, max_sitemaps=10
        )
        assert out == []
        assert client.calls == ["https://x.com/a.xml", "https://x.com/b.xml"]

    @pytest.mark.asyncio
    async def test_respects_max_sitemaps(self):
        index = (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://x.com/s1.xml</loc></sitemap>"
            "<sitemap><loc>https://x.com/s2.xml</loc></sitemap></sitemapindex>"
        )
        client = _client({
            "https://x.com/index.xml": _resp(200, index),
            "https://x.com/s1.xml": _resp(200, _URLSET),
            "https://x.com/s2.xml": _resp(200, _URLSET),
        })
        # max_sitemaps=1 -> only the index doc is fetched, no children.
        out = await collect_sitemap_urls(
            client, ["https://x.com/index.xml"], max_urls=100, max_sitemaps=1
        )
        assert out == []
        assert client.calls == ["https://x.com/index.xml"]


class TestSitemapsThatDoNotParseCleanly:
    def test_a_document_cut_at_the_cap_still_yields_the_entries_before_the_cut(self):
        """Audit H-17: the body cap used to hand lxml a truncated document,
        which raised and returned nothing -- the cap was a silent reject."""
        entries = "".join(f"<url><loc>https://x.com/p{i}</loc></url>" for i in range(200))
        cut = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries)[:2000]
        pages, _ = parse_sitemap_xml(cut)
        assert 20 < len(pages) < 200 and pages[0] == ("https://x.com/p0", None)

    def test_a_comment_or_processing_instruction_does_not_lose_the_document(self):
        """Audit M-19: `QName` on a comment node raised ValueError out of the
        whole sitemap branch."""
        xml = ('<?xml version="1.0"?><!-- generated --><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<url><!-- first --><loc>https://x.com/a</loc><?pi hint?></url><url><loc>https://x.com/b</loc></url></urlset>')
        pages, _ = parse_sitemap_xml(xml)
        assert [p[0] for p in pages] == ["https://x.com/a", "https://x.com/b"]

    def test_a_declared_encoding_is_honoured_from_bytes(self):
        xml = ('<?xml version="1.0" encoding="windows-1251"?><urlset><url><loc>https://x.com/п</loc></url></urlset>').encode("cp1251")
        pages, _ = parse_sitemap_xml(xml)
        assert pages == [("https://x.com/п", None)]

    @pytest.mark.asyncio
    async def test_a_cut_sitemap_is_reported_to_the_caller(self, monkeypatch):
        from src import sitemap as sitemap_mod
        big = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
            f"<url><loc>https://x.com/p{i}</loc></url>" for i in range(500))
        client = _client({"https://x.com/sitemap.xml": _resp(200, big)})
        notes: list[str] = []
        pages = await sitemap_mod.collect_sitemap_urls(
            client, ["https://x.com/sitemap.xml"], max_urls=1000, max_sitemaps=5,
            max_body_bytes=4096, on_warning=notes.append,
        )
        assert 0 < len(pages) < 500
        assert notes == ["body cut at 4096 bytes: https://x.com/sitemap.xml"]


class TestRecoveredDocumentsDoNotInventUrls:
    """codex (Astra), 2026-09-05: recovery closes an unfinished `<loc>` into a
    plausible prefix, keeps an undeclared-prefix tag literally (which `QName`
    raises on), and a header-only charset was dropped with `resp.text`."""

    def test_the_entry_the_cut_landed_in_is_dropped(self):
        entries = "".join(f"<url><loc>https://x.com/article-{i}</loc></url>" for i in range(50))
        doc = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries
        cut = doc[: doc.index("https://x.com/article-7") + len("https://x.com/art")]
        pages, _ = parse_sitemap_xml(cut, truncated=True)
        assert [p[0] for p in pages] == [f"https://x.com/article-{i}" for i in range(7)]
        assert not any(p[0].endswith("/art") for p in pages)
        # An index cut inside a child location drops that child too.
        idx = ("<sitemapindex><sitemap><loc>https://x.com/a.xml</loc></sitemap>"
               "<sitemap><loc>https://x.com/b.x")
        assert parse_sitemap_xml(idx, truncated=True) == ([], ["https://x.com/a.xml"])
        # Not truncated: nothing is dropped.
        assert len(parse_sitemap_xml(doc + "</urlset>")[0]) == 50

    @pytest.mark.asyncio
    async def test_an_undeclared_prefix_in_one_child_does_not_erase_the_others(self):
        good = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://x.com/a</loc></url></urlset>")
        odd = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               "<url><loc>https://x.com/b</loc><image:image/></url></urlset>")
        client = _client({"https://x.com/1.xml": _resp(200, good), "https://x.com/2.xml": _resp(200, odd)})
        pages = await collect_sitemap_urls(client, ["https://x.com/1.xml", "https://x.com/2.xml"],
                                           max_urls=100, max_sitemaps=5)
        assert [p[0] for p in pages] == ["https://x.com/a", "https://x.com/b"]

    @pytest.mark.asyncio
    async def test_a_header_only_charset_is_honoured(self):
        body = "<urlset><url><loc>https://x.com/п</loc></url></urlset>".encode("cp1251")
        client = _client({"https://x.com/s.xml": httpx.Response(
            200, content=body, headers={"content-type": "application/xml; charset=windows-1251"})})
        pages = await collect_sitemap_urls(client, ["https://x.com/s.xml"], max_urls=10, max_sitemaps=1)
        assert pages == [("https://x.com/п", None)]


class TestAnUnusableDeclaredCharsetDoesNotCostTheSitemap:
    """Regression, found in review of the H-17/M-19 branch.

    That branch started passing the HTTP header's charset to
    `etree.XMLParser(encoding=...)` so a declared windows-1251 would be
    honoured. But a header charset is text a site sent us: `utf8mb4` and two
    charsets merged into one header both exist in the wild, and lxml raises
    `LookupError` on them where it previously sniffed the document's own
    declaration. `/map`'s broad handler caught that and returned an EMPTY
    sitemap for a site whose sitemap had worked the day before -- a silent
    loss, not an error anyone would notice.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("declared", ["utf8mb4", "cp1251, utf-8", "unknown-8bit"])
    async def test_the_urls_still_come_back(self, declared):
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://x.com/a</loc></url>"
            "<url><loc>https://x.com/b</loc></url>"
            "</urlset>"
        )
        client = _client({
            "https://x.com/s.xml": _resp(
                200, body, headers={"Content-Type": f"application/xml; charset={declared}"}
            ),
        })
        pages = await collect_sitemap_urls(client, ["https://x.com/s.xml"], max_urls=10, max_sitemaps=1)
        assert [loc for loc, _ in pages] == ["https://x.com/a", "https://x.com/b"]

    @pytest.mark.asyncio
    async def test_a_charset_python_knows_is_still_honoured(self):
        """The feature the branch added must survive the guard: a real
        windows-1251 document with no XML declaration still decodes."""
        body = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://x.com/\u043f\u0443\u0442\u044c</loc></url>"
            "</urlset>"
        ).encode("windows-1251")
        client = _client({
            "https://x.com/s.xml": httpx.Response(
                200, content=body, headers={"Content-Type": "application/xml; charset=windows-1251"}
            ),
        })
        pages = await collect_sitemap_urls(client, ["https://x.com/s.xml"], max_urls=10, max_sitemaps=1)
        assert pages == [("https://x.com/\u043f\u0443\u0442\u044c", None)]
