from __future__ import annotations

from urllib.parse import urljoin, urldefrag
from lxml import html as lxml_html


_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "sms:", "data:", "file:", "ftp:")
_MAX_HTML_BYTES = 10 * 1024 * 1024  # 10 MB — guard against OOM on giant pages


def extract_links(html: str, base_url: str) -> list[str]:
    """Parse HTML, return absolute URLs from <a href>. Respects <base href>.
    Drops fragment identifiers and non-web schemes. Caps input at 10 MB to
    avoid DoS on pathologically large pages."""
    if not html:
        return []
    if len(html) > _MAX_HTML_BYTES:
        html = html[:_MAX_HTML_BYTES]
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return []

    base = base_url
    base_el = doc.find(".//base[@href]")
    if base_el is not None:
        base_href = (base_el.get("href") or "").strip()
        if base_href:
            base = urljoin(base_url, base_href)

    out: list[str] = []
    seen: set[str] = set()
    for el in doc.iter("a"):
        href = (el.get("href") or "").strip()
        if not href:
            continue
        lower = href.lower()
        if any(lower.startswith(s) for s in _SKIP_SCHEMES):
            continue
        absolute = urljoin(base, href)
        absolute, _ = urldefrag(absolute)
        if not absolute.startswith(("http://", "https://")):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
    return out
