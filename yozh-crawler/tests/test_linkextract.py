from __future__ import annotations

from src.linkextract import extract_links


def test_extract_resolves_relative_and_absolute():
    html = """
        <html><body>
            <a href="/about">about</a>
            <a href="https://other.com/x">other</a>
            <a href="?q=1">same page</a>
        </body></html>
    """
    out = extract_links(html, "https://example.com/section/")
    assert "https://example.com/about" in out
    assert "https://other.com/x" in out
    # Relative query on same page
    assert any(u.startswith("https://example.com/section/") and "q=1" in u for u in out)


def test_extract_respects_base_href():
    html = """
        <html><head><base href="https://cdn.example.com/"></head><body>
            <a href="a/b">x</a>
        </body></html>
    """
    out = extract_links(html, "https://example.com/")
    assert out == ["https://cdn.example.com/a/b"]


def test_extract_drops_non_http_schemes():
    html = """
        <a href="javascript:void(0)">x</a>
        <a href="mailto:a@b.com">x</a>
        <a href="tel:+1">x</a>
        <a href="data:text/plain,hi">x</a>
        <a href="https://ok.com/">ok</a>
    """
    assert extract_links(html, "https://example.com/") == ["https://ok.com/"]


def test_extract_drops_fragments():
    html = '<a href="/page#section">x</a>'
    out = extract_links(html, "https://example.com/")
    assert out == ["https://example.com/page"]


def test_extract_deduplicates_absolute_urls():
    html = """
        <a href="/p">a</a><a href="/p">b</a><a href="https://example.com/p">c</a>
    """
    out = extract_links(html, "https://example.com/")
    assert out.count("https://example.com/p") == 1


def test_extract_handles_empty_input():
    assert extract_links("", "https://example.com/") == []
    assert extract_links(None, "https://example.com/") == []  # type: ignore[arg-type]
