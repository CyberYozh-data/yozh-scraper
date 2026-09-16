from __future__ import annotations

import httpx
import pytest

from src import ssrf
from src.ssrf import usable_charset, SSRFError, _ip_is_public, host_is_public, safe_get


class TestIpIsPublic:
    @pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0"])
    def test_rejects_non_public(self, ip):
        assert _ip_is_public(ip) is False

    @pytest.mark.parametrize(
        "ip",
        [
            "100.64.0.1",        # CGNAT / RFC 6598 (shared-ISP NAT; Tailscale etc.)
            "100.127.255.254",   # CGNAT upper edge
        ],
    )
    def test_rejects_cgnat(self, ip):
        assert _ip_is_public(ip) is False

    @pytest.mark.parametrize(
        "ip",
        [
            "::ffff:169.254.169.254",  # IPv4-mapped link-local (cloud metadata)
            "::ffff:10.0.0.1",         # IPv4-mapped RFC1918
            "::ffff:127.0.0.1",        # IPv4-mapped loopback
        ],
    )
    def test_rejects_ipv4_mapped_private(self, ip):
        assert _ip_is_public(ip) is False

    @pytest.mark.parametrize(
        "ip",
        [
            "::ffff:100.64.0.1",  # IPv4-mapped CGNAT (unwrap + CGNAT rule together)
            "100.64.0.0",         # CGNAT lower edge
            "::10.0.0.1",         # IPv4-compatible IPv6 embedding RFC1918
            "::169.254.169.254",  # IPv4-compatible IPv6 embedding link-local metadata
            "64:ff9b::a00:1",     # NAT64 (RFC 6052) embedding 10.0.0.1
            "2002:0a00:0001::",   # 6to4 embedding 10.0.0.1
            "2002:a9fe:a9fe::",   # 6to4 embedding 169.254.169.254 (metadata)
            "0.0.0.1",            # 0.0.0.0/8 "this network"
            "240.0.0.1",          # class-E reserved
        ],
    )
    def test_rejects_embedded_and_reserved(self, ip):
        # These rely on ipaddress's is_reserved/is_private classification, which
        # is version-dependent stdlib behaviour — pin it so an interpreter bump
        # can't silently reopen an SSRF hole.
        assert _ip_is_public(ip) is False

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"])
    def test_allows_public(self, ip):
        assert _ip_is_public(ip) is True


class TestHostIsPublic:
    @pytest.mark.asyncio
    async def test_private_resolution_blocked(self, mocker):
        mocker.patch.object(ssrf, "_resolve", mocker.AsyncMock(return_value=["10.0.0.1"]))
        assert await host_is_public("evil.internal") is False

    @pytest.mark.asyncio
    async def test_any_private_ip_blocks(self, mocker):
        # A host resolving to one public + one private IP must be blocked.
        mocker.patch.object(ssrf, "_resolve", mocker.AsyncMock(return_value=["8.8.8.8", "127.0.0.1"]))
        assert await host_is_public("rebind.test") is False

    @pytest.mark.asyncio
    async def test_public_resolution_allowed(self, mocker):
        mocker.patch.object(ssrf, "_resolve", mocker.AsyncMock(return_value=["93.184.216.34"]))
        assert await host_is_public("example.com") is True


def _resp(status_code: int, text: str = "", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, text=text, headers=headers)


def _client(routes):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    client.calls = calls  # type: ignore[attr-defined]
    return client


CAP = 64 * 1024


class TestSafeGet:
    @pytest.mark.asyncio
    async def test_blocks_private_host(self, mocker):
        mocker.patch.object(ssrf, "host_is_public", mocker.AsyncMock(return_value=False))
        with pytest.raises(SSRFError):
            await safe_get(_client({}), "http://169.254.169.254/", max_bytes=CAP)

    @pytest.mark.asyncio
    async def test_follows_public_redirect(self, mocker):
        mocker.patch.object(ssrf, "host_is_public", mocker.AsyncMock(return_value=True))
        client = _client({
            "https://a.com/": _resp(302, headers={"location": "https://b.com/x"}),
            "https://b.com/x": _resp(200, "landed"),
        })
        resp = await safe_get(client, "https://a.com/", max_bytes=CAP)
        assert resp.status_code == 200 and resp.text == "landed" and resp.url == "https://b.com/x"
        assert client.calls == ["https://a.com/", "https://b.com/x"]

    @pytest.mark.asyncio
    async def test_blocks_redirect_to_private(self, mocker):
        # First hop public, redirect target private -> blocked on re-validation.
        async def fake_public(host):
            return host == "a.com"

        mocker.patch.object(ssrf, "host_is_public", side_effect=fake_public)
        client = _client({
            "https://a.com/": _resp(302, headers={"location": "http://169.254.169.254/"}),
        })
        with pytest.raises(SSRFError):
            await safe_get(client, "https://a.com/", max_bytes=CAP)
        assert client.calls == ["https://a.com/"], "the private hop was never requested"


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.consumed = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


class TestSafeGetCap:
    """Audit H-17: `/map` materialised whole bodies (auto-decompressed) before
    slicing. The capped reader stops at the cap, whatever the encoding."""

    @pytest.mark.asyncio
    async def test_stops_reading_at_the_cap(self, mocker):
        mocker.patch("src.ssrf.host_is_public", mocker.AsyncMock(return_value=True))
        stream = _CountingStream([b"x" * 1024] * 100)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await safe_get(client, "https://example.com/big", max_bytes=10 * 1024)
        assert resp.status_code == 200
        assert len(resp.body) == 10 * 1024 and resp.truncated is True
        assert stream.consumed < 100, "the stream was not drained past the cap"

    @pytest.mark.asyncio
    async def test_a_gzip_bomb_is_cut_at_the_decoded_cap(self, mocker):
        mocker.patch("src.ssrf.host_is_public", mocker.AsyncMock(return_value=True))
        import gzip
        body = gzip.compress(b"a" * (5 * 1024 * 1024))  # ~5 KB on the wire, 5 MB decoded
        assert len(body) < 20 * 1024
        stream = _CountingStream([body[i:i + 512] for i in range(0, len(body), 512)])
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await safe_get(client, "https://example.com/sitemap.xml.gz", max_bytes=CAP)
        assert len(resp.body) == CAP and resp.truncated is True
        assert stream.consumed < len(stream.chunks), "decoded per chunk, so the cap bit before the wire body ended"

    @pytest.mark.asyncio
    async def test_a_small_body_is_complete_and_keeps_its_charset(self, mocker):
        mocker.patch("src.ssrf.host_is_public", mocker.AsyncMock(return_value=True))
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, content="<urlset>ы</urlset>".encode("cp1251"), headers={"content-type": "text/xml; charset=windows-1251"}))
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await safe_get(client, "https://example.com/sitemap.xml", max_bytes=CAP)
        assert resp.truncated is False and resp.text == "<urlset>ы</urlset>"
        assert resp.body == "<urlset>ы</urlset>".encode("cp1251"), "the bytes are untouched for lxml"


class TestUsableCharset:
    """A declared charset is text a site sent us, not a promise Python can keep.

    Before this guard, an unknown one raised `LookupError` twice over -- in
    `CappedResponse.text` and in `etree.XMLParser(encoding=...)` -- and
    `/map`'s broad handler turned that into "sitemap discovery failed" with an
    EMPTY sitemap for a site whose sitemap had worked the day before.
    """

    @pytest.mark.parametrize("declared", ["utf-8", "UTF-8", "windows-1251", "cp1251", "iso-8859-1",
                                          "UTF-8 ", "  utf-8"])  # trailing/leading space is trimmed, not fatal
    def test_a_charset_python_knows_is_kept(self, declared):
        assert usable_charset(declared) == declared.strip()

    @pytest.mark.parametrize("declared", [
        "utf8mb4",          # a MySQL collation pasted into a header
        "cp1251, utf-8",    # two headers merged into one
        "unknown-8bit",
        "",
        None,
    ])
    def test_anything_python_cannot_decode_with_becomes_none(self, declared):
        assert usable_charset(declared) is None

    def test_quotes_are_stripped(self):
        assert usable_charset('"utf-8"') == "utf-8"

    def test_the_result_is_always_safe_to_decode_and_to_parse_with(self):
        """The point of the helper: whatever it returns must work in BOTH
        places that used to raise."""
        from lxml import etree
        for declared in ("utf-8", "utf8mb4", "UTF-8 ", "cp1251, utf-8", None, "windows-1251"):
            charset = usable_charset(declared)
            b"<x/>".decode(charset or "utf-8", errors="replace")
            etree.XMLParser(recover=True, encoding=charset) if charset else etree.XMLParser(recover=True)
