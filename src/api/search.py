"""Web search endpoint: SERP + optional scrape in one call.

Reuses the built-in `google_search_chromium` preset as the SERP source — materialize it
with the query, run it through the normal job queue, parse the organic result
blocks, and (optionally) scrape each result page with the same pipeline. The
search logic is factored into `build_search` with an injected `run_job` so it
is unit-testable without the worker pool.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeGuard
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException
from lxml import etree, html as lxml_html
from pydantic import ValidationError

from src.api.presets import get_preset_store
from src.api.session_guard import validate_session_id
from src.presets.materializer import (
    MaterializeError,
    PresetScrapeRequest,
    materialize,
)
from src.presets.store import PresetNotFound, PresetStore
from src.schemas import (
    ScrapeRequest,
    ScrapeResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from src.scrape_service import QueueFull, scrape_service

log = logging.getLogger(__name__)
router = APIRouter()

@dataclass(frozen=True)
class EngineProfile:
    """How to reach and parse one SERP engine.

    `preset` supplies the URL template, locales and the `result_blocks` selector
    (one HTML fragment per organic result). `title_sel`/`snippet_sel` are CSS
    selectors applied *within* each block, so they are deliberately block-relative
    (unscoped) — they assume each `result_blocks` fragment is exactly one result.
    The result link is the first http(s) anchor in the block (shared heuristic).
    """

    preset: str
    title_sel: str
    snippet_sel: str


# One entry per engine — adding a fourth is a new preset + one line here.
#
# Each entry names an engine-suffixed variant explicitly. The three chosen here
# are the engines these presets used before they were split in two, so this
# endpoint's behaviour is unchanged by the split: google and bing ran Chromium,
# yandex ran Camoufox (which is why it was adopted — it passes SmartCaptcha).
# Letting a caller pick the other twin through /search is deliberately not
# wired up here; it is a separate change.
ENGINES: dict[str, EngineProfile] = {
    "google": EngineProfile(
        "google_search_chromium", "h3", "div[data-sncf='1'], .VwiC3b"
    ),
    "bing": EngineProfile(
        "bing_search_chromium", "h2", ".b_caption p, p.b_lineclamp2"
    ),
    "yandex": EngineProfile(
        "yandex_search_camoufox",
        ".OrganicTitle-LinkText, h2",
        ".OrganicText, .TextContainer",
    ),
}

RunJob = Callable[[list[ScrapeRequest]], Awaitable[list[ScrapeResponse | None]]]


def _is_http_url(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _first_http_link(el) -> str | None:
    for anchor in el.iter("a"):
        href = (anchor.get("href") or "").strip()
        if _is_http_url(href):
            return href
    return None


def _unwrap_redirect(url: str) -> str:
    """Return the real destination behind an engine's tracking redirect.

    Bing wraps every organic link in a ``/ck/a`` redirect with the destination
    base64url-encoded in the ``u=a1<…>`` param. Decode it so callers get the
    target site, not the tracker.

    Yandex ad/tracking links (``yabs.yandex.ru/count/…``) sometimes carry the
    destination in a ``url=`` or ``u=`` query param. This is best-effort: when
    the param is present and resolves to an http(s) URL we return it; if the
    destination is in an opaque path blob or ``etext=`` param (not decodable
    without following the redirect) we pass through unchanged.

    We only ever return an ``http(s)`` string — a crafted param cannot yield a
    ``javascript:``/``file:`` link; worst case is a passthrough.
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()

        # --- Yandex yabs tracking links ---
        if host == "yabs.yandex.ru" or (
            host.endswith(".yandex.ru") and "/count/" in parsed.path
        ):
            from urllib.parse import unquote as _unquote  # stdlib, always available

            qs = parse_qs(parsed.query)
            for key in ("url", "u"):
                vals = qs.get(key)
                if vals:
                    cand = _unquote(vals[0])
                    if cand.startswith(("http://", "https://")):
                        return cand
            return url  # opaque blob — pass through rather than drop

        # --- Bing /ck/a tracking links ---
        if host != "bing.com" and not host.endswith(".bing.com"):
            return url
        if "/ck/a" not in parsed.path:
            return url
        u = parse_qs(parsed.query).get("u", [""])[0]
        if not u.startswith("a1"):
            return url
        # Over-pad with '='; the decoder ignores any excess padding.
        decoded = base64.urlsafe_b64decode(u[2:] + "===").decode("utf-8", "ignore")
        return decoded if decoded.startswith(("http://", "https://")) else url
    except ValueError as exc:
        # The URL failed to parse, or a u=a1… payload failed to decode —
        # i.e. Bing changed its encoding; worth a server-side signal.
        log.warning("failed to unwrap redirect url=%r: %s", url, exc)
        return url


def _parse_results(data: dict[str, Any], profile: EngineProfile) -> list[SearchResult]:
    """Turn the preset's per-result HTML blocks into clean (url, title, snippet).

    We parse `result_blocks` (one HTML fragment per organic result) so
    title/url/snippet stay together per result and non-organic blocks (no title
    element) and duplicate URLs can be dropped.

    When the preset's `links` column is aligned 1:1 with `result_blocks`, that
    column is AUTHORITATIVE for the URL — a None is a row the preset excluded
    (bing nulls anything still on bing.com), a non-absolute value is a stub
    nothing resolved, and both drop the row. Falling back to the block's own
    anchor for such a row would resurrect exactly what the preset refused:
    measured, bing's `/ck/a` wrapper re-decoded and shipped as a result its
    author had nulled. Only a preset with no aligned column at all is parsed
    from the block, first http anchor and all.

    This is what makes Google work: since 2026-08 every organic href in a Google
    block is a relative `/goto?url=` stub, so the block carries no http anchor
    except an occasional `translate.google.com` link, and `_first_http_link`
    either dropped the result or returned the translate link as its destination.
    The google_search presets resolve the stubs into `links`
    (`resolve_redirects`), and this reads the resolved value.
    """
    out: list[SearchResult] = []
    seen: set[str] = set()
    blocks = data.get("result_blocks") or []
    links = data.get("links")
    aligned = links if isinstance(links, list) and len(links) == len(blocks) else None
    for block_html, link in zip(blocks, aligned if aligned is not None else [None] * len(blocks)):
        if not block_html:
            continue
        try:
            el = lxml_html.fromstring(block_html)
        except (ValueError, etree.ParserError):  # pylint: disable=c-extension-no-member
            continue
        title_els = el.cssselect(profile.title_sel)
        if not title_els:
            continue
        url = link if aligned is not None else _first_http_link(el)
        if not _is_http_url(url):
            continue
        url = _unwrap_redirect(url)
        if url in seen:
            continue
        seen.add(url)
        snippet_els = el.cssselect(profile.snippet_sel)
        out.append(
            SearchResult(
                url=url,
                title=title_els[0].text_content().strip() or None,
                snippet=snippet_els[0].text_content().strip() if snippet_els else None,
            )
        )
    return out


def _proxy_override(req: SearchRequest) -> dict[str, Any]:
    """Proxy fields the caller set, as a ScrapeRequest-shaped dict.

    Only includes fields that were explicitly provided, so an unset proxy
    leaves the SERP preset's own proxy untouched (default-off).
    """
    override: dict[str, Any] = {}
    if req.proxy_type is not None:
        override["proxy_type"] = req.proxy_type
    if req.max_retries is not None:
        override["max_retries"] = req.max_retries
    if req.proxy_pool_id is not None:
        override["proxy_pool_id"] = req.proxy_pool_id
    if req.proxy_geo is not None:
        geo = req.proxy_geo.model_dump(exclude_none=True)
        # An all-empty geo is treated as "not provided" so the materializer's
        # locale-derived default (country_code) survives rather than being
        # wiped to {}.
        if geo:
            override["proxy_geo"] = geo
    return override


def _engine_override(req: SearchRequest) -> dict[str, Any]:
    """Browser-engine and Camoufox premium fields the caller explicitly set.

    Only includes fields that differ from their silent defaults so an
    unset engine leaves the SERP preset's own engine untouched (preset wins).
    """
    override: dict[str, Any] = {}
    if req.browser_engine is not None:
        override["browser_engine"] = req.browser_engine
    if req.humanize:
        override["humanize"] = req.humanize
    if req.spoof_os is not None:
        override["spoof_os"] = req.spoof_os
    if req.fingerprint_profile is not None:
        override["fingerprint_profile"] = req.fingerprint_profile
    if req.block_webgl:
        override["block_webgl"] = req.block_webgl
    if req.addons:
        override["addons"] = req.addons
    if req.warmup is not None:
        override["warmup"] = req.warmup.model_dump()
    if req.prem_proxy_options is not None:
        override["prem_proxy_options"] = req.prem_proxy_options.model_dump()
    return override


async def build_search(
    req: SearchRequest, *, store: PresetStore, run_job: RunJob
) -> SearchResponse:
    """Run the SERP preset, parse results, optionally scrape each page."""
    started = time.perf_counter()
    profile = ENGINES[req.engine]
    preset = store.get(profile.preset)
    proxy_override = _proxy_override(req)
    engine_override = _engine_override(req)
    combined_override = {**proxy_override, **engine_override}
    serp_req = materialize(
        preset,
        PresetScrapeRequest(
            source=profile.preset,
            preset_params={"query": req.query},
            locale=req.locale,
            request_override=combined_override or None,
        ),
    )
    serp_results = await run_job([serp_req])
    serp = serp_results[0] if serp_results else None
    warnings = list(serp.warnings) if serp and serp.warnings else []
    if serp is None:
        # Distinguish "the SERP job didn't complete" from "0 organic results".
        warnings.append("serp_unavailable: search job did not complete")
        log.warning("search serp unavailable query=%r", req.query)
    results = _parse_results((serp.data if serp else None) or {}, profile)[: req.limit]
    log.info("search engine=%s query=%r results=%d", req.engine, req.query, len(results))

    if req.scrape and results:
        await _scrape_result_pages(req, results, proxy_override, engine_override, run_job, warnings)

    return SearchResponse(
        query=req.query,
        count=len(results),
        results=results,
        took_ms=int((time.perf_counter() - started) * 1000),
        warnings=warnings,
    )


async def _scrape_result_pages(
    req: SearchRequest,
    results: list[SearchResult],
    proxy_override: dict[str, Any],
    engine_override: dict[str, Any],
    run_job: RunJob,
    warnings: list[str],
) -> None:
    """Scrape each SERP result page and attach the responses in place."""
    # Strip internal-only fields a caller must not steer via scrape_options.
    opts = {
        k: v
        for k, v in (req.scrape_options or {}).items()
        if k not in ("preset_meta", "parser_plan")
    }
    # The panel's proxy and engine settings apply to the result scrapes too,
    # taking precedence over any proxy/engine passed inside scrape_options.
    try:
        scrape_reqs = [
            ScrapeRequest(**{**opts, **proxy_override, **engine_override, "url": r.url})
            for r in results
        ]
    except ValidationError as exc:
        # `scrape_options` is caller-supplied and merged with the panel's engine
        # here, so an unsupported combination (camoufox + session_id, say) first
        # becomes visible at this construction — AFTER the SERP has been fetched
        # and paid for. Uncaught it is a 500 for what is a bad request, and the
        # engine override is ours, not the caller's, so the message has to name
        # the pair rather than blame one side.
        raise MaterializeError(
            f"scrape_options are not valid for browser_engine="
            f"{engine_override.get('browser_engine')!r}: {exc.errors()[0]['msg']}"
        ) from exc
    # Session is validated here (after the SERP) because it is checked
    # against the materialized per-result request, which only exists now.
    for scrape_req in scrape_reqs:
        await validate_session_id(scrape_req)
    responses = await run_job(scrape_reqs)
    for result, response in zip(results, responses):
        result.scrape = response
    # Count what actually got attached: a mid-job timeout yields a full-length
    # list padded with None placeholders, not a short one (and the record's
    # list keeps being mutated by the worker, so don't recount `responses`).
    scraped = sum(1 for r in results if r.scrape is not None)
    if scraped < len(results):
        warnings.append(f"scrape_incomplete: {scraped} of {len(results)} pages scraped")
        log.warning(
            "search scrape incomplete query=%r scraped=%d of %d",
            req.query, scraped, len(results),
        )


@router.post("", response_model=SearchResponse, operation_id="run_search")
async def run_search(
    req: SearchRequest, store: PresetStore = Depends(get_preset_store)
) -> SearchResponse:
    try:
        return await build_search(req, store=store, run_job=scrape_service.run_and_wait)
    except QueueFull as exc:
        raise HTTPException(status_code=503, detail="queue_full") from exc
    except PresetNotFound as exc:
        raise HTTPException(
            status_code=500,
            detail=f"{ENGINES[req.engine].preset} preset is missing",
        ) from exc
    except MaterializeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
