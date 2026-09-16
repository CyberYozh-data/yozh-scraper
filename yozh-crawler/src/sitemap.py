from __future__ import annotations

from typing import Callable

import logging
from urllib.parse import urljoin, urlparse

import httpx
from lxml import etree  # pylint: disable=c-extension-no-member

from .settings import settings
from .ssrf import CappedResponse, SSRFError, safe_get


log = logging.getLogger(__name__)

# Common sitemap locations to probe when robots.txt names none.
_FALLBACK_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml")


_RECOVERING = etree.XMLParser(recover=True)


def _localname(el) -> str:
    """The element's local name, or "" for a tag the recovering parser kept
    literally (an undeclared prefix such as `<image:image/>`), which `QName`
    would otherwise raise on -- out of the whole sitemap walk."""
    try:
        return etree.QName(el).localname.lower()
    except ValueError:
        return ""


def parse_sitemap_xml(
    xml: bytes | str, *, charset: str | None = None, truncated: bool = False,
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Parse a sitemap document into (page_entries, child_sitemap_urls).
    A page entry is ``(loc, lastmod)`` where ``lastmod`` is the raw ``<lastmod>``
    text (or None if absent). Handles both <urlset> (pages) and <sitemapindex>
    (nested sitemaps), namespace-agnostic. Returns empty lists on malformed input.

    Bytes go to lxml as they are, so the document's own declaration or BOM is
    honoured; `charset` is the HTTP header's explicit charset, which wins
    when present (a `windows-1251` body with no XML declaration would
    otherwise be read as utf-8). The parser recovers: a document cut at the
    body cap yields the entries before the cut instead of nothing -- and with
    `truncated`, the entry the cut landed in is dropped, since recovery would
    close an unfinished `<loc>` into a plausible-looking prefix. A comment or
    processing instruction inside the document is skipped rather than raising
    out of the whole sitemap branch (audit M-19).
    """
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    parser = etree.XMLParser(recover=True, encoding=charset) if charset else _RECOVERING
    try:
        root = etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError, LookupError):
        return [], []
    if root is None:
        return [], []

    # A sitemap doc is either a <urlset> (pages) or a <sitemapindex> (child
    # sitemaps) — never mixed — so the root tag decides which bucket applies.
    if _localname(root) == "sitemapindex":
        child = [
            loc
            for el in root.iter(tag=etree.Element)
            if _localname(el) == "loc" and (loc := (el.text or "").strip())
        ]
        return [], child[:-1] if truncated else child

    # <urlset>: pair each <loc> with its sibling <lastmod> inside the <url> entry.
    pages: list[tuple[str, str | None]] = []
    for url_el in root.iter(tag=etree.Element):
        if _localname(url_el) != "url":
            continue
        loc: str | None = None
        lastmod: str | None = None
        for child_el in url_el.iterchildren(tag=etree.Element):
            name = _localname(child_el)
            if name == "loc":
                loc = (child_el.text or "").strip()
            elif name == "lastmod":
                lastmod = (child_el.text or "").strip() or None
        if loc:
            pages.append((loc, lastmod))
    return (pages[:-1] if truncated else pages), []


def parse_robots_sitemaps(robots_txt: str, base_url: str) -> list[str]:
    """Return absolute sitemap URLs declared via `Sitemap:` lines in robots.txt."""
    out: list[str] = []
    for line in robots_txt.splitlines():
        line = line.strip()
        if not line.lower().startswith("sitemap:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value:
            out.append(urljoin(base_url, value))
    return out


async def _fetch(
    client: httpx.AsyncClient, url: str, *, max_bytes: int, check_ssrf: bool = True,
    on_warning: Callable[[str], None] | None = None,
) -> CappedResponse | None:
    try:
        resp = await safe_get(client, url, max_bytes=max_bytes, check_ssrf=check_ssrf)
    except (httpx.HTTPError, SSRFError) as exc:
        log.debug("sitemap fetch failed url=%s err=%s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    if resp.truncated:
        note = f"body cut at {max_bytes} bytes: {url}"
        log.warning("sitemap %s", note)
        if on_warning is not None:
            on_warning(note)
    return resp


async def discover_sitemap_urls(
    client: httpx.AsyncClient, seed_url: str, *, check_ssrf: bool = True,
    max_body_bytes: int | None = None,
) -> list[str]:
    """Find sitemap URLs for a site: robots.txt directives, else common paths."""
    # robots.txt lives at the origin root; resolve declared/relative sitemap
    # URLs against the origin, not the (possibly deep) seed path.
    parsed = urlparse(seed_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    max_body_bytes = max_body_bytes or settings.map_max_body_bytes
    robots_resp = await _fetch(client, f"{origin}/robots.txt", max_bytes=max_body_bytes, check_ssrf=check_ssrf)
    robots = robots_resp.text if robots_resp is not None else None
    if robots:
        declared = parse_robots_sitemaps(robots, base_url=origin)
        if declared:
            return declared
    return [f"{origin}{path}" for path in _FALLBACK_SITEMAP_PATHS]


async def collect_sitemap_urls(
    client: httpx.AsyncClient,
    sitemap_urls: list[str],
    *,
    max_urls: int,
    max_sitemaps: int,
    check_ssrf: bool = True,
    match: str | None = None,
    max_body_bytes: int | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> list[tuple[str, str | None]]:
    """Breadth-first walk of sitemaps (following <sitemapindex>) collecting page
    entries ``(loc, lastmod)``, bounded by max_urls and the number of sitemap
    docs fetched.

    When ``match`` is given, only locs containing it (case-insensitive) are
    collected, so ``max_urls`` bounds *matching* pages rather than all pages —
    otherwise a substring search on a big sitemap is starved by the cap.
    """
    max_body_bytes = max_body_bytes or settings.map_max_body_bytes
    needle = match.lower() if match else None
    pages: list[tuple[str, str | None]] = []
    queue = list(sitemap_urls)
    seen: set[str] = set()
    fetched = 0
    while queue and fetched < max_sitemaps and len(pages) < max_urls:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        resp = await _fetch(client, url, max_bytes=max_body_bytes, check_ssrf=check_ssrf, on_warning=on_warning)
        if resp is None:
            continue
        fetched += 1
        found_pages, child_sitemaps = parse_sitemap_xml(resp.body, charset=resp.charset, truncated=resp.truncated)
        for loc, lastmod in found_pages:
            if needle is None or needle in loc.lower():
                pages.append((loc, lastmod))
        queue.extend(s for s in child_sitemaps if s not in seen)
    return pages[:max_urls]
