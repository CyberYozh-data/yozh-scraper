"""One page scrape: attempt loop + proxy rotation included.

This is the body of the old worker_pool._worker_main per-page path, moved
verbatim (zero logic changes). Transport-agnostic: give it a runner and a
request dict, get a ScrapeEnvelope back.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import time
import traceback
from itertools import zip_longest
from urllib.parse import urlsplit
from typing import Any, Coroutine, TypeVar

from pydantic import ValidationError

from src.browser.runner import (
    HTTP_BAN_STATUSES,
    HTTP_TRANSIENT_STATUSES,
    SELECTOR_MISS_PREFIX,
    FetchResult,
    PlaywrightRunner,
)
from src.browser.teardown import close_quietly
from src.proxy.models import ProxyConfig
from src.security.egress_guard import open_egress_guard
from src.extract.resolve import BATCH_TIMEOUT_S, resolve_redirect_fields
from src.markdown_build import apply as apply_markdown, resolve_formats
from src.queue.envelope import ScrapeEnvelope, ScrapeErr, ScrapeOk
from src.presets.models import PresetMeta
from src.queue.field_guards import (
    DEVICES,
    PROXY_TYPES,
    literal_or,
    model_or_none,
    str_or_none,
)
from src.schemas import (
    AppliedWarmup,
    ScrapeMeta,
    ScrapeResponse,
)
from src.presets.worker_parse import apply as apply_parser
from src.proxy.base import ProxyConfigError, ProxyFailure
from src.proxy.resolver import proxy_resolver
from src.settings import settings

log = logging.getLogger(__name__)


# HTTP status codes that indicate a proxy-related failure. Canonical sets live
# in browser.runner (the runner uses them to mark such a fetch not-ok); here we
# reuse them so the retry policy and the fetch verdict can't drift apart.
_RETRYABLE_HTTP_STATUSES = HTTP_BAN_STATUSES | HTTP_TRANSIENT_STATUSES

# `scrape_page` runs this whole function under asyncio.wait_for(PAGE_TASK_TIMEOUT_S),
# and a cancelled coroutine returns nothing: the block verdict, the html and the
# retry count are all destroyed and the caller gets a bare "page task exceeded".
# So the loop sizes every attempt against the same ceiling instead of ignoring it.
#
# Slack the attempt loop may not spend. It covers two things the phase model
# does not: packing the result after the loop, and the per-attempt cost no
# `timeout_ms` governs — the fresh Camoufox launch inside every fetch (which
# yandex_search asks for), `read_content_settling_navigation`'s retries, the
# full-page screenshot scroll, teardown. The expensive post-fetch work (parser
# and markdown, both of which can call an LLM) is bounded explicitly by
# `within_deadline` rather than reserved for, so a preset request does not pay
# up front for a self-heal it will usually not make. A fraction rather than a
# constant because the ceiling is configurable and a constant tuned for 120s
# would starve a deliberately short one.
_ATTEMPT_SLACK_FRACTION = 0.15
_ATTEMPT_SLACK_MAX_S = 5.0
# Under this, a navigation cannot even reach the target — spending an exit on it
# would burn a proxy to learn nothing, so the loop stops and reports what it has.
_MIN_ATTEMPT_TIMEOUT_MS = 100


def attempt_slack_s(ceiling_s: float) -> float:
    """The slack above, for a ceiling: what the attempt loop may not spend of it."""
    return min(_ATTEMPT_SLACK_MAX_S, ceiling_s * _ATTEMPT_SLACK_FRACTION)

# Residential rotating proxy types whose exit country is otherwise random when
# no proxy_geo.country_code is pinned. We default their country so the exit and
# the browser timezone/locale stay aligned (see settings.default_proxy_country).
# Mobile pools (mobile / mobile_shared) are intentionally excluded — their exit
# country is set by the provider's pool, not steered by country_code here.
_COUNTRY_DEFAULTED_PROXY_TYPES = frozenset({"res_rotating", "prem_res_rotating"})


def apply_default_proxy_country(
    proxy_type: str | None, proxy_geo: dict[str, str] | None
) -> dict[str, str] | None:
    """Pin a default country for residential rotating proxies with no country.

    Keeps the proxy exit and the browser fingerprint (timezone/locale, derived
    from the same proxy_geo via geo_profile) in the same country. An explicit
    country_code always wins; other proxy types and pinned requests pass through.
    """
    if proxy_type not in _COUNTRY_DEFAULTED_PROXY_TYPES:
        return proxy_geo
    if proxy_geo and proxy_geo.get("country_code"):
        return proxy_geo
    country = settings.default_proxy_country
    log.debug("defaulted %s exit country to %s (no proxy_geo.country_code)", proxy_type, country)
    return {**(proxy_geo or {}), "country_code": country}


# Playwright appends a call log naming the URL it was navigating to. The
# needles below are as short as three letters, so matching them against the
# whole message reads the *path being crawled* — /companies/dns/, /proxy-guide —
# as a transport fault.
_CALL_LOG_MARKER = "call log:"
# The exception class name each runner prefixes: PlaywrightRunner writes a
# literal "PlaywrightError: ", camoufox_runner uses type(exc).__name__. Playwright
# names its timeout class TimeoutError, which puts a needle in the prefix itself.
# Matched by name rather than \w*error so a class that *is* the evidence
# (ProxyError, DNSError) keeps classifying as a proxy failure.
_ERROR_CLASS_PREFIX_RE = re.compile(r"^(?:playwrighterror|timeouterror|unexpectederror): ")
# Playwright's navigation-budget expiry. Deliberately anchored to page.goto:
# Page.wait_for_selector's deadline is a different signal (for SERP presets a
# missing selector usually means a block) and must keep rotating.
_NAVIGATION_DEADLINE_RE = re.compile(r"page\.goto: timeout \d+ms exceeded\.?")
# A URL sitting inline in the message, outside the call log. The call log is
# already dropped above, which is what keeps a crawled host out of the needle
# match -- but `net::ERR_ABORTED at https://club.dns-shop.ru/p` puts the host in
# the message itself, and "dns" is a three-letter needle. The host is chosen by
# the TARGET, so leaving it in hands a site the lever that decides whether we
# spend a fresh exit. Measured: `club.dns-shop.ru` is a real result host from
# our own RU SERP corpus.
_INLINE_URL_RE = re.compile(r"https?://\S+")

# These shapes are pinned to playwright==1.57 wording; re-verify on a bump.


def is_navigation_deadline(error: str | None) -> bool:
    """The page did not finish loading inside its budget.

    Says nothing about the exit's health: a proxy that accepts the connection
    and then hangs — or answers 407 to an HTTPS CONNECT, which is how expired
    or exhausted credentials surface — produces a byte-identical message.
    """
    if not error:
        return False
    return bool(_NAVIGATION_DEADLINE_RE.search(error.lower()))


def is_selector_miss(error: str | None) -> bool:
    """The page loaded but never grew the selector the request asked for.

    On a SERP preset that usually means an interstitial replaced the results,
    so the exit is worth rotating. Matched explicitly rather than by the word
    "timeout" leaking into the proxy-failure needles, which is how the runners'
    old raw `PlaywrightError` text reached the same decision by accident.
    """
    return error is not None and error.startswith(SELECTOR_MISS_PREFIX)


def _host_of(url: str) -> str:
    """Host only — never the full URL.

    A query string routinely carries an API key, a session token or PII, and a
    full-URL field is unbounded cardinality besides. The whole URL stays in the
    human message; this is the part that may be queried.
    """
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def egress_warnings(fetch_result) -> list[str]:
    """That a refusal happened — a COUNT, never the hostnames.

    Without this the field was written and never read, and a denied hop
    reached the caller as `net::ERR_TUNNEL_CONNECTION_FAILED`, which explains
    nothing and reads as a broken exit.

    But the hostnames are chosen by the TARGET, and `warnings` is consumed by
    a substring classifier in another repository: yozh-law-checker matches
    ("captcha", "block detected", "anti-bot", "antibot") anywhere in a warning
    and treats a hit on the seed page as proof the scan was blocked. Copying a
    refused host in verbatim hands a target the lever — redirect a subresource
    to `captcha.example` that resolves privately, and the scan reports itself
    blocked.

    So the same split as EGRESS_BLOCKED_ERROR: the caller learns THAT it
    happened, the log says WHAT. Nothing target-controlled crosses into a
    field somebody pattern-matches.
    """
    denied = getattr(fetch_result, "egress_denied", None)
    if not isinstance(denied, list) or not denied:
        return []
    count = len(denied)
    noun = "target" if count == 1 else "targets"
    return [f"egress_blocked: {count} non-public {noun} refused"]


def ignored_field_warnings(fetch_result) -> list[str]:
    """A request field the engine dropped, said to the caller and not only logged.

    `egress_warnings` withholds the target-chosen hostnames because `warnings`
    is pattern-matched across repositories. Here the opposite reasoning applies
    and lands in the same place: the field NAME is ours, not the target's, so
    naming it leaks no lever -- and without it the only record is a worker log
    line the caller cannot read.

    The concrete case: the scraper-tester UI offers four User-Agent header
    presets, and a `User-Agent` is dropped on every engine. Silently, the
    operator picks one and watches nothing change.
    """
    ignored = getattr(fetch_result, "ignored_request_fields", None)
    if not isinstance(ignored, list) or not ignored:
        return []
    return [f"ignored_request_field: {name} (the engine states its own)"
            for name in ignored]


def should_rotate_for(fetch_result) -> bool:
    """Whether this failure is worth a fresh exit.

    A target the egress guard refused is NOT: the refusal is deterministic, so
    every rotation reproduces it and the whole retry budget burns for nothing.
    It matters because the browser reports a denied CONNECT as
    `net::ERR_TUNNEL_CONNECTION_FAILED`, and both "tunnel" and "net::err" are
    proxy-failure needles — so the guard handed an attacker the pool-spending
    lever that `EGRESS_BLOCKED_ERROR` was made a constant to close.
    """
    # A LIST, specifically. Suppressing a rotation is a behaviour change, and
    # only a real record of refused targets justifies it — anything else falls
    # through to the existing decision, which is the safe direction.
    denied = getattr(fetch_result, "egress_denied", None)
    if isinstance(denied, list) and denied:
        return False
    return bool(
        fetch_result.blocked
        or is_selector_miss(fetch_result.error)
        or looks_like_proxy_failure(fetch_result.status_code, fetch_result.error)
    )


def looks_like_proxy_failure(status_code: int | None, error: str | None) -> bool:
    if status_code is not None and status_code in _RETRYABLE_HTTP_STATUSES:
        return True
    if not error:
        return False
    error = error.lower().split(_CALL_LOG_MARKER, 1)[0]
    error = _ERROR_CLASS_PREFIX_RE.sub("", error)
    # Drop a bare navigation deadline: on its own it means the page was slow,
    # and rotating on every attempt spends the whole budget three times over.
    # It is not proof the exit is healthy (see is_navigation_deadline), so the
    # caller grants one rotation for the recoverable cases; this only stops the
    # deadline from being retried to exhaustion. Removing the phrase rather
    # than returning early keeps a transport fault named alongside it retryable.
    error = _NAVIGATION_DEADLINE_RE.sub("", error)
    error = _INLINE_URL_RE.sub("", error)
    needles = (
        "proxy",
        "tunnel",
        "timed out",
        "timeout",
        "econnreset",
        "econnrefused",
        "enotfound",
        "dns",
        "net::err",
        "ns_error_",  # Firefox (Camoufox) network-error codes
        "connection closed",
        "socket hang up",
        "tls",
        "handshake",
    )
    return any(needle in error for needle in needles)


# Enough of a relayed error to recognise it; the reported attempt's own error is
# still carried verbatim.
_SUMMARY_CHARS = 120


def summarise_error(error: str) -> str:
    """A discarded attempt's failure, said in one line and in our own words.

    Playwright's raw text is a paragraph with a call log naming the URL, and it
    spells the navigation deadline as "Page.goto: Timeout ...". Relaying that for
    an attempt we are NOT reporting hands a downstream reader the words it scans
    for — yozh-law-checker re-crawls a whole site when "timeout" or "goto" shows
    up in `warnings` — on the strength of an attempt whose page was thrown away.
    The classification is what a consumer acts on, so relay that.
    """
    head = error.split("\n", 1)[0]
    # Both shared patterns are lowercase-only (their other caller folds the whole
    # message first), and Playwright writes "Call log:" — so fold a copy to find
    # the cut and slice the original. Without this the call log survives on the
    # rare single-line message, carrying the crawled URL into a field that is
    # pattern-matched downstream.
    cut = head.lower().find(_CALL_LOG_MARKER)
    if cut != -1:
        head = head[:cut]
    prefix = _ERROR_CLASS_PREFIX_RE.match(head.lower())
    if prefix:
        head = head[prefix.end():]
    # Remove the deadline phrase rather than returning on it, the way
    # `looks_like_proxy_failure` does: a transport fault named alongside it is
    # exactly the "attempts 2-3 both failed to connect" evidence this relay
    # exists to surface, and returning early threw it away.
    deadline = is_navigation_deadline(error)
    if deadline:
        head = re.sub(_NAVIGATION_DEADLINE_RE.pattern, "", head, flags=re.IGNORECASE)
    rest = " ".join(head.split()).strip(" .;,-")
    if deadline:
        phrase = "the page did not finish loading in time"
        rest = f"{phrase}; {rest}" if rest else phrase
    if not rest:
        return "failed"
    # Marked when cut, or a truncated message reads as a complete one.
    return rest if len(rest) <= _SUMMARY_CHARS else rest[: _SUMMARY_CHARS - 3] + "..."


def affordable_attempt_timeout_ms(
    remaining_s: float,
    configured_ms: int,
    *,
    wait_for_selector: str | None = None,
    warmup: dict[str, Any] | None = None,
) -> int | None:
    """The per-navigation budget this attempt may promise, or None if it cannot.

    `timeout_ms` is not the cost of an attempt — the runners apply it to *each*
    phase they perform: the optional warmup navigation, the real `page.goto`, and
    `wait_for_selector`. The Yandex preset asks for all three at 45 s, so one
    attempt can cost 137 s against a 120 s ceiling: it is cancelled mid-flight and
    everything it learned is lost. Dividing by the phase count is what makes the
    promise honest.

    Returning a *smaller* timeout is preferred over refusing the attempt: a
    shortened navigation that fails still comes back classified (blocked, HTTP
    status, selector miss), which is strictly more than a cancellation yields.
    """
    phases = 1 + (1 if wait_for_selector else 0) + (1 if warmup else 0)
    dwell_ms = 0
    if warmup:
        dwell = warmup.get("dwell_ms")
        dwell_ms = settings.warmup_dwell_ms if dwell is None else int(dwell)
    affordable = int((remaining_s * 1000 - dwell_ms) / phases)
    if affordable < _MIN_ATTEMPT_TIMEOUT_MS:
        return None
    return min(configured_ms, affordable)


_T = TypeVar("_T")


async def within_deadline(
    work: Coroutine[Any, Any, _T],
    *,
    deadline: float,
    label: str,
    warnings: list[str],
    default: _T,
) -> _T:
    """Run post-fetch work inside what is left of the task ceiling.

    Everything after the attempt loop — the preset parser (up to two LLM calls at
    PRESET_LLM_TIMEOUT_S each) and the markdown filter (a third) — runs inside the
    same `asyncio.wait_for` the whole task does, and none of it was ever budgeted.
    A fetch that ends near the ceiling would start a 30 s call against a few
    seconds of budget and the cancellation destroys the envelope: a page that was
    fetched perfectly well comes back as "page task exceeded". Losing the
    extraction is a degradation the caller can see and act on; losing the page is
    not.

    Bounds only work that yields: on the plain /scrape path `apply_parser` has no
    await points, so a pathological lxml parse still runs to completion. It is a
    coroutine, not any awaitable — a Task would keep running past the ceiling
    instead of being cancelled with it.
    """
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        # Never awaited, so close it explicitly rather than leaving a
        # "coroutine was never awaited" warning behind.
        getattr(work, "close", lambda: None)()
        warnings.append(f"{label} skipped: the page-task ceiling was already reached")
        return default
    try:
        return await asyncio.wait_for(work, timeout=remaining)
    except asyncio.TimeoutError:
        log.warning("%s stopped at the page-task ceiling (%.1fs budget)", label, remaining)
        warnings.append(f"{label} stopped at the page-task ceiling")
        return default


def _evidence_rank(result: FetchResult) -> int:
    """How much a failed attempt explains, highest first.

    A page that merely lacks its selector outranks an interstitial: it is the
    only one `apply_parser` will touch (parsing is gated on `not blocked`
    AND on the same retryable-status set used here) and
    the only input self-heal can regenerate selectors from. An interstitial in
    turn outranks nothing at all, because "this exit is burned" and "the
    connection failed" send the caller to different remedies.

    `blocked` alone is not enough to tell the first rank from the second.
    `classify_fetch` leaves a transient 5xx `blocked=False` while calling it "not
    genuine content" in the same docstring, and a gateway error page has a
    perfectly good body — so ranking on `blocked` promotes it above a real
    captcha, hides the block from the caller, and clears the parse gate for a
    self-heal that would regenerate selectors from an error page and persist
    them over the working ones.
    """
    if not result.html:
        return 0
    if result.blocked or result.status_code in _RETRYABLE_HTTP_STATUSES:
        return 1
    return 2


def better_failure_evidence(
    candidate: FetchResult, incumbent: FetchResult | None
) -> FetchResult:
    """Of two failed attempts, the one that actually explains the failure.

    Reporting the *last* attempt is only a convention, and a costly one once
    attempts are shortened to fit the deadline: the tail attempt frequently dies
    at the transport with no page at all, and letting it overwrite an earlier
    captcha loses the verdict. Ties go to the later attempt — it was fetched on
    the exit the rotation moved to, so it describes the current state.
    """
    if incumbent is None:
        return candidate
    return candidate if _evidence_rank(candidate) >= _evidence_rank(incumbent) else incumbent


# Query terms: word characters (Unicode-aware) of three or more, so "a", "of",
# "in" and Russian prepositions drop out. Operator tokens (`site:example.com`,
# `inurl:...`) and exclusions (`-snake`, `-"snake oil"`) are constraints, not
# words a result is expected to echo -- a correct SERP for `python -snake` has
# no snakes -- and a query with alternatives (`python OR rust`, `|`) is not
# judged at all: the rule requires every term, and an alternative is not. Pure
# numbers are not judged: a year the rows do not echo must not fail a real
# page (a shopping page for "laptop 2026" lists this year's models under last
# year's names). Words of scripts written without spaces are not judged
# either: `\w{3,}` cannot cut terms out of such a query -- a Japanese query is
# ONE token, and its prefix matches nothing on a real page. That list is a
# denylist by nature (there is no Unicode property for "space-separated"), so
# a query in a script it misses is judged as one whole token and fails; it
# carries every such script with a shipped or plausible locale. Each remaining
# term is cut to a prefix ("laptops" -> "lapto", "ноутбуки" -> "ноутбу") so an
# inflected form still counts, and matched as a lowercase substring of the row
# text. At most a dozen terms are judged: a longer query is not a query, and
# every term is one scan of the rows' text.
_QUERY_OPERATOR_RE = re.compile(r"\b\w+:\S+|(?<!\S)-\"[^\"]*\"|(?<!\S)-\S+")
_QUERY_ALTERNATIVES_RE = re.compile(r"\b(?:OR|AND)\b|\|")
_QUERY_TERM_RE = re.compile(r"\w{3,}")
_UNSEGMENTED_SCRIPT_RE = re.compile(  # Han, kana, bopomofo, Thai, Lao, Tibetan, Myanmar, Khmer, Yi, Javanese, Balinese
    "[\u3040-\u30ff\u3100-\u312f\u31a0-\u31bf\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\uff66-\uff9f\U00020000-\U0003ffff\u0e00-\u0eff\u0f00-\u0fff\u1000-\u109f\u1780-\u17ff"
    "\ua000-\ua4cf\ua980-\ua9df\u1b00-\u1b7f]"
)
_RELEVANCE_MIN_ROWS = 3
_RELEVANCE_MAX_TERMS = 12
# A term counts as answered when it appears in at least a fifth of the rows.
# Measured over 256 recorded runs: on every real SERP and product listing each
# term reaches 0.29 of the rows or more (the lowest is "laptop" on a German
# shopping page of model names); on the 25 wrong Bing pages the missing term
# reaches 0.10 at most -- one Best Buy snippet that happens to list laptops.
_RELEVANCE_MIN_COVERAGE = 0.2


def _fold(text: str) -> str:
    """Lowercase, fold `ё` to `е`, and drop Latin diacritics.

    Both sides of the comparison go through this, because the two sides come
    from different writers. A caller types `ёлка` and the shop writes `елка`;
    a caller types `café münchen` and the listing writes `Cafe Munchen`. Every
    one of those was a false `serp_query_mismatch` on a page that answered the
    question perfectly (measured: the diacritic pair flagged BOTH terms).

    Cyrillic is folded by the explicit `ё`/`е` rule only, never by dropping
    combining marks: NFKD decomposes `й` into `и` + breve, and stripping that
    would make `мой` match `мои` -- a different word. Latin is decomposed,
    where the marks really are decoration over the same letter.
    """
    text = text.lower().replace("\u0451", "\u0435")
    out: list[str] = []
    for char in text:
        if "\u0400" <= char <= "\u04ff":
            out.append(char)
            continue
        out.extend(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c))
    return "".join(out)


def _query_stems(query: str) -> list[str]:
    if _QUERY_ALTERNATIVES_RE.search(query):
        return []
    words = _QUERY_TERM_RE.findall(_QUERY_OPERATOR_RE.sub(" ", _fold(query)))
    stems = dict.fromkeys(
        word[: max(4, len(word) - 2)]
        for word in words
        # ANY digit disqualifies the word, not only an all-digit one: a caller
        # writes `16gb` and the listing writes `16 GB`, so the glued token
        # appears in no row and reads as an unanswered term. The number is
        # never the part that tells a right page from a wrong one anyway --
        # the words around it are.
        if not any(char.isdigit() for char in word)
        and not _UNSEGMENTED_SCRIPT_RE.search(word)
    )
    return list(stems)[:_RELEVANCE_MAX_TERMS]


def _without_query_string(cell: str) -> str:
    """A URL cell minus its query string and fragment.

    Marketplaces echo the search in every result href (`?keywords=laptop`,
    `_nkw=laptop`), which would answer for a page of unrelated titles; the
    path's slug is kept, it names the result.
    """
    if cell.startswith(("http://", "https://", "/")):
        return cell.partition("?")[0].partition("#")[0]
    return cell


def _rows(data: Any) -> list[str]:
    """One lowercased string per extracted row: the i-th cell of every list column, joined.

    Scalar fields are left out on purpose: a page-title field would echo the
    query on the very pages this guard exists to catch. So are markup cells
    (`attr: html` columns such as the search presets' `result_blocks`): their
    text is already in the text columns, and their tags and hrefs would answer
    any query term that is also a markup token. Rows with no text are dropped,
    so a numeric column (`prices` after `parse_price`) or a run of nulls cannot
    make a one-title page look like a full one. Records nested as `list[dict]`
    (the LLM output path) are not read.
    """
    if not isinstance(data, dict):
        return []
    columns = [column for column in data.values() if isinstance(column, list)]
    rows: list[str] = []
    for cells in zip_longest(*columns):
        text = " ".join(
            _without_query_string(cell)
            for cell in cells
            if isinstance(cell, str) and not cell.lstrip().startswith("<")
        )
        if text.strip():
            rows.append(_fold(text))
    return rows


def query_relevance_warning(data: Any, query: str | None) -> str | None:
    """Report a page that answered a different query than the one asked.

    Measured 2026-09-04: Bing serves a CACHED SERP FOR ANOTHER QUERY to some
    clients — stable per IP, our query echoed in the page's own title and search
    box above someone else's results. From this host `best laptop 2026` came
    back as dictionary definitions of "best", Honda forums, UPS tracking,
    Netflix threads and Arabic TikTok, ten rows each; in the recorded proxied
    audits every `ru` Bing run on both engines was such a page. Every
    count-based check (fill rate, row count, HTTP 200, `required`) scored them
    as full pages, and this repo's own rule is to judge a scrape by its meaning.

    The test: every content term of the query appears in at least a fifth of
    the extracted rows. A real SERP echoes each query word across its titles,
    snippets and URL slugs; the dictionary page has "best" in every row and
    "laptop" in none, and the one that lists laptops in a single Best Buy
    snippet is caught by the fifth. Calibrated on the fourteen recorded audits
    (256 judged runs): the 25 pages it flags are exactly the Bing SERPs for
    someone else's query -- 23 `ru`, 2 `us` -- and the 231 real SERPs and
    product listings pass, including a one-word query that names a third of a
    shopping page's rows. Below three rows nothing is judged: the shape
    measured is a FULL page of the wrong results, and one or two rows can be
    legitimately terse. An engine's autocorrect trips it too ("best lapotp"
    answered with laptops), which is the same statement about the page; so
    does a constraint word the results need not echo (`laptop under 1000` on
    a marketplace whose titles carry prices, not the word) -- the warning is
    advisory and carries only its counts, never a title or the query.
    """
    if not query:
        return None
    stems = _query_stems(query)
    if not stems:
        return None
    rows = _rows(data)
    if len(rows) < _RELEVANCE_MIN_ROWS:
        return None
    floor = _RELEVANCE_MIN_COVERAGE * len(rows)
    missing = sum(1 for stem in stems if sum(stem in row for row in rows) < floor)
    if not missing:
        return None
    return (
        f"serp_query_mismatch: {missing} of {len(stems)} query terms appear in under a "
        f"fifth of the {len(rows)} extracted rows -- the page answered a different query "
        "than the one asked (an engine's autocorrect, or Bing's cached SERP for another "
        "query)."
    )


def self_referential_link_warning(data: Any, final_url: str | None) -> str | None:
    """Report extracted URLs that never really left the page they came from.

    A SERP whose every result links to the search engine is not a SERP. Bing
    wraps organic hrefs in `bing.com/ck/a` redirects, so a `links` field read
    straight from href was 100% populated and 100% useless — a failure no
    fill-rate, row-count or status check can see, because nothing is missing.

    THE RULE: a redirect wrapper crowds every URL onto the SAME (or very few)
    path(s), varying only the query string; a marketplace's own search gives
    each result its own distinct path (eBay's `/itm/<id>`, Amazon's
    `/dp/<asin>`). Fires when the number of distinct paths is (a) AT MOST
    ONE (an absolute floor, not a ratio -- needed so this function's own
    minimum judgeable case, 3 urls sharing a single path, can still fire;
    a pure ratio would require 4+ urls before one shared path counts,
    putting the guard's foundational case in a dead zone) OR (b) at most a
    QUARTER of the total url count (`n_distinct * 4 <= len(urls)`), for a
    wrapper spread across more than one endpoint. Both edges pinned exactly
    in tests. The ratio arm is deliberately tighter than "at most half": two
    ordinary, benign HTML shapes sit right at a 2x duplication factor --
    eBay-shaped cards that link their destination twice (title anchor, image
    anchor: 60 urls over 30 real distinct paths) and a page mixing 10 real
    distinct product links with 12 `?page=N` pagination links (11 distinct
    paths over 22 urls) -- both exactly the ratio a "half" boundary would
    have fired on. The shape the ratio arm exists to catch, Google mixing
    organic `/url?q=...` with ad `/aclk?sa=...`, sits at 2 distinct paths
    over a realistic 10-20 urls (ratio 0.1-0.2), comfortably inside a quarter
    with room to spare -- unlike a naive ">1 distinct path" boundary, which
    goes quiet on any wrapper using more than one endpoint at all.

    Host is compared EXACTLY (`urlsplit(u).netloc == urlsplit(final_url)
    .netloc`), not by registrable domain or subdomain. A previous version of
    this guard widened that comparison and normalized away long path
    segments to (wrongly) treat Yandex's ad redirects as a false negative —
    re-measurement showed the real Yandex record is mostly OFF-host organic
    results (dns-shop.ru, ozon.ru, mvideo.ru, ...) with a handful of on-host
    ads mixed in, correctly silent either way (see
    tests/queue/test_self_referential_links.py::TestYandexWasCorrectlySilentAllAlong
    for the measured composition); ablating both mechanisms against the full
    92-run 2026-08-27 audit changed 0 of 92 verdicts. Measured trade-off, not
    a claimed security property: a 32-hex or 36-char UUID redirect token
    sits under a length-based "opaque" threshold and stays silent under the
    old mechanism; a look-alike domain (e.g. one host merely ending in the
    same letters as another) would read as the same site under a naive
    subdomain check. Exact-host + distinct-path is simpler, has one fewer
    thing to get wrong, and — run through this repo's own shipped
    unwrap+urljoin pipeline — still catches a constructed regression where
    that pipeline breaks and every amazon_search url stays wrapped (see
    tests).

    No preset-name exemption: a name-based fast path was tried and removed
    -- it could only ever produce a false NEGATIVE (amazon_search's own
    unwrap regressing, wrapped urls on amazon_search's own host, would be
    silenced by the very name meant to protect it), while the distinct-path
    property alone already keeps every real marketplace preset in the audit
    silent without needing to name any of them.

    A SINGLE genuine off-host link disables this guard entirely: the
    "is every url on this exact host" gate requires ALL of them to match, so
    29 wrapped urls plus one real destination is silent, the same as a
    healthy page. This is a bigger practical hole than either measured
    trade-off above and is not mitigated by anything in this function.

    Descriptive, never fatal — a category page can legitimately link within
    itself, and a handful of distinct internal links proves nothing either
    way. The point is to put the fact in front of a human, not to decide.

    Silent below three URLs: one or two self-links prove nothing, and engines
    legitimately link to themselves for pagination and cached copies.
    """
    if not final_url or data is None:
        return None
    own_host = urlsplit(final_url).netloc.lower()
    if not own_host:
        return None

    rows = data if isinstance(data, list) else [data]
    urls: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row.values():
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    urls.append(item)

    if len(urls) < 3:
        return None

    split = [urlsplit(u) for u in urls]
    on_own = [s for s in split if s.netloc.lower() == own_host]
    if len(on_own) != len(urls):
        return None

    distinct_paths = {s.path for s in on_own}
    n_distinct = len(distinct_paths)
    # Wrapper-shaped when there is (almost) only ONE real endpoint (the
    # absolute floor -- needed so a 3- or 4-url single-path wrapper, the
    # minimum this function ever judges at all, can still fire; a pure
    # ratio would require 4+ urls before a lone shared path counts, putting
    # the guard's own foundational case in a dead zone) OR the endpoint
    # count is small RELATIVE to the row count (the quarter ratio, for a
    # wrapper spread across more than one endpoint).
    if n_distinct > 1 and n_distinct * 4 > len(urls):
        # More than a quarter of the rows have their OWN distinct path -- a
        # real catalog listing (the page's own marketplace search, possibly
        # with some benign duplication -- two anchors per card, a pagination
        # bar), not evidence of an unwrapped redirect.
        return None

    return (
        f"all_extracted_links_self_referential: every one of the {len(urls)} "
        f"extracted URLs is on {own_host}, the page's own host, crowded onto "
        f"just {len(distinct_paths)} distinct path(s). On a search preset "
        "that means the results were never unwrapped — the field is "
        "populated but carries no destination."
    )


async def run_scrape(
    runner: PlaywrightRunner,
    request_id: str,
    request: dict[str, Any],
    storage_state: dict[str, Any] | None,
    *,
    deadline: float | None = None,
) -> ScrapeEnvelope:
    """Returns a ScrapeEnvelope: ScrapeOk carrying the ScrapeResponse that
    lands in the job's result slot, or ScrapeErr with a message (and a
    traceback only for an unexpected crash).

    `deadline` is a `time.perf_counter()` value: when the caller's own ceiling
    expires. It belongs to the caller because the ceiling starts there — a
    session-pinned page waits on the session lock (up to 65s) before this
    function is even entered, and deriving the deadline here would place it that
    far past the real one. Defaults to this call's own start plus
    PAGE_TASK_TIMEOUT_S, which is right for a caller that enforces nothing else.
    """
    start_time = time.perf_counter()
    task_deadline = (
        start_time + settings.page_task_timeout_s if deadline is None else deadline
    )

    proxy_type_raw = request.get("proxy_type")
    proxy_pool_id = request.get("proxy_pool_id")
    proxy_geo_data = request.get("proxy_geo")
    url = str(request.get("url"))

    proxy_geo: dict[str, str] | None = None
    if proxy_geo_data:
        proxy_geo = {
            "country_code": proxy_geo_data.get("country_code"),
            "region": proxy_geo_data.get("region"),
            "city": proxy_geo_data.get("city"),
        }
        proxy_geo = {k: v for k, v in proxy_geo.items() if v is not None}
        if not proxy_geo:
            proxy_geo = None

    proxy_geo = apply_default_proxy_country(proxy_type_raw, proxy_geo)

    # The full url stays in the human message; the queryable field is the HOST
    # only. A query string routinely carries an API key or a session token, and
    # a full-url field is unbounded cardinality besides.
    log.info(
        "job received request_id=%s proxy_type=%s proxy_pool_id=%s url=%s",
        request_id,
        proxy_type_raw,
        proxy_pool_id,
        url,
        extra={"event": "scrape.job.received", "url_host": _host_of(url)},
    )

    try:
        session = await proxy_resolver.open_session(
            proxy_type=proxy_type_raw,
            proxy_pool_id=proxy_pool_id,
            proxy_geo=proxy_geo,
            max_retries=request.get("max_retries"),
            prem_proxy_options=request.get("prem_proxy_options"),
        )

        attempts = session.max_attempts()
        fetch_result: FetchResult | None = None
        # The attempt loop decides on the attempt it just made; what gets
        # reported is the attempt that best explains the outcome. They diverge
        # once a shortened tail attempt fails with no page (see
        # better_failure_evidence).
        reported_result: FetchResult | None = None
        retries_used = 0
        # Every way the ceiling changed what the loop did, in order. Silent
        # degradation is how a shortened attempt gets blamed on the target.
        budget_notes: list[str] = []
        attempt_deadline = task_deadline - attempt_slack_s(settings.page_task_timeout_s)

        # raw_html / screenshot can be requested via the legacy
        # booleans OR by listing them in `formats` (union). Resolve
        # once so capture and output agree.
        effective_formats = resolve_formats(
            request.get("formats"),
            raw_html=request.get("raw_html", False),
            screenshot=request.get("screenshot", False),
        )
        want_raw_html = "raw_html" in effective_formats
        want_screenshot = "screenshot" in effective_formats

        configured_timeout_ms = request.get("timeout_ms") or settings.request_timeout_ms

        def _affordable_now() -> int | None:
            """What the attempt starting right now may promise per navigation."""
            return affordable_attempt_timeout_ms(
                attempt_deadline - time.perf_counter(),
                configured_timeout_ms,
                wait_for_selector=request.get("wait_for_selector"),
                warmup=request.get("warmup"),
            )

        # Deliberately free of the words a downstream consumer scans for
        # ("timeout", "goto"): a truncated rotation is not a render fault, and
        # yozh-law-checker re-crawls with a different wait strategy when it sees
        # those in `warnings`.
        def _not_started(attempt_no: int) -> str:
            return (
                f"attempt {attempt_no}/{attempts} not started: the page-task "
                f"ceiling ({settings.page_task_timeout_s}s) left no budget for it"
            )

        for attempt in range(1, attempts + 1):
            proxy_cfg = session.current_proxy()
            timeout_ms = _affordable_now()
            if timeout_ms is None:
                # Normally attempt 1 (a ceiling too small for any attempt at
                # all): later attempts are vetted before the rotation that leads
                # to them. It can still fire past attempt 1 when `on_failure`
                # itself — a live provider call — spends the slack that was
                # there when it was checked.
                budget_notes.append(_not_started(attempt))
                log.warning(
                    "%s (request_id=%s)", budget_notes[-1], request_id,
                    extra={"event": "scrape.attempt.not_started"},
                )
                break
            log.debug(
                "attempt %d/%d for request_id=%s with proxy=%s",
                attempt,
                attempts,
                request_id,
                proxy_cfg.server if proxy_cfg else "none",
                # AFTER the budget gate: emitted before it, an attempt the
                # deadline refused was logged as started and then, one line
                # later, as not started — so any query counting attempts
                # overcounted the fetches actually made.
                extra={"event": "scrape.attempt.started"},
            )
            if timeout_ms < configured_timeout_ms:
                budget_notes.append(
                    f"attempt {attempt}/{attempts} shortened to {timeout_ms}ms of the "
                    f"requested {configured_timeout_ms}ms to fit the page-task ceiling"
                )
                log.info("%s (request_id=%s)", budget_notes[-1], request_id)
            log.debug(
                "fetching with timeout_ms=%d, wait_until=%s",
                timeout_ms,
                request.get("wait_until", "domcontentloaded")
            )

            fetch_result = await runner.fetch(
                url=request["url"],
                device=request.get("device", "desktop"),
                proxy=proxy_cfg,
                headers=request.get("headers"),
                wait_until=request.get("wait_until", "domcontentloaded"),
                wait_for_selector=request.get("wait_for_selector"),
                timeout_ms=timeout_ms,
                screenshot=want_screenshot,
                element_selector=request.get("element_selector"),
                stealth=request.get("stealth", True),
                block_assets=request.get("block_assets"),
                proxy_geo=proxy_geo,
                render=request.get("render", True),
                cookies=request.get("cookies"),
                storage_state=storage_state,
                viewport=request.get("viewport"),
                # Camoufox premium options; accepted-and-ignored by PlaywrightRunner
                humanize=request.get("humanize", False),
                spoof_os=request.get("spoof_os"),
                fingerprint_profile=request.get("fingerprint_profile"),
                block_webgl=request.get("block_webgl", False),
                addons=request.get("addons"),
                warmup=request.get("warmup"),
            )

            reported_result = (
                fetch_result if fetch_result.ok
                else better_failure_evidence(fetch_result, reported_result)
            )

            # If it is success - exit from cycle
            if fetch_result.ok:
                log.info(
                    "fetch succeeded on attempt %d for request_id=%s",
                    attempt,
                    request_id,
                    extra={"event": "scrape.fetch.succeeded"},
                )
                break

            # If is not success - check, should rotate proxy
            log.warning(
                "fetch failed on attempt %d for request_id=%s: %s",
                attempt,
                request_id,
                fetch_result.error,
                extra={"event": "scrape.attempt.failed"},
            )

            # Rotate + retry on a proxy/network failure OR a captcha/block: a
            # block means this IP is burned, so a fresh proxy is the fix (bounded
            # by max_attempts). Anything else is a genuine target response — don't
            # waste proxies retrying it.
            should_rotate = should_rotate_for(fetch_result)
            # A navigation deadline is not evidence of a bad exit, but it is not
            # evidence of a good one either — a hung exit, or one answering 407
            # because the credentials expired, looks exactly like a slow page.
            # Grant exactly one rotation: `recover()` mints fresh credentials, so
            # this keeps the recoverable cases recoverable, while still ending the
            # runaway where all three attempts burned the full budget in a row.
            deadline_retry = (
                not should_rotate
                and attempt == 1
                and attempts > 1
                and is_navigation_deadline(fetch_result.error)
            )
            if not (should_rotate or deadline_retry):
                if is_navigation_deadline(fetch_result.error):
                    log.warning(
                        "navigation deadline after %d attempt(s), giving up: "
                        "request_id=%s url=%s timeout_ms=%d exit=%s — the page "
                        "did not load in time; if this repeats across targets, "
                        "suspect the exit",
                        attempt,
                        request_id,
                        request.get("url"),
                        timeout_ms,
                        proxy_cfg.server if proxy_cfg else "none",
                    )
                else:
                    log.info(
                        "error is neither proxy failure nor block, "
                        "stopping retries for request_id=%s",
                        request_id
                    )
                break

            # Increment retry counter
            retries_used = attempt

            # `on_failure` mints a fresh exit through the provider's live API, so
            # both reasons the next attempt cannot happen have to be settled
            # BEFORE it runs — otherwise the exit is bought and thrown away.
            if attempt == attempts:
                log.info(
                    "attempts exhausted (%d/%d) for request_id=%s", attempt, attempts, request_id
                )
                break
            if _affordable_now() is None:
                budget_notes.append(_not_started(attempt + 1))
                log.warning("%s (request_id=%s)", budget_notes[-1], request_id)
                break

            # Call on_failure for proxy rotation
            should_retry = await session.on_failure(
                ProxyFailure(
                    status_code=fetch_result.status_code,
                    error=fetch_result.error
                )
            )

            if not should_retry:
                log.warning(
                    "session.on_failure returned False, stopping retries for request_id=%s",
                    request_id
                )
                break

            log.info(
                "rotating to next proxy for request_id=%s, attempt %d/%d",
                request_id,
                attempt + 1,
                attempts
            )

        # --- Render result ---
        warnings: list[str] = []
        applied_preset = model_or_none(
            PresetMeta, request.get("preset_meta"), field="applied_preset", sink=warnings
        )
        data = None

        # From here on the reported attempt is the subject: meta, warnings, the
        # parsed data and the storage_state all describe the page being returned.
        last_result = fetch_result
        fetch_result = reported_result

        if fetch_result is None:
            warnings.append("fetch_result_is_none")
            fetch_result = FetchResult(
                html="",
                final_url=None,
                status_code=None,
                screenshot_b64=None,
                ok=False,
                error="No fetch result"
            )

        if not fetch_result.ok:
            if fetch_result.error:
                warnings.append(fetch_result.error)
            if fetch_result.status_code:
                warnings.append(f"status_code={fetch_result.status_code}")
        # A configured warmup that failed, on a fetch that SUCCEEDED as well as
        # one that did not. `applied_warmup=None` cannot carry this — it also
        # means "no warmup configured" — so without the warning the only trace
        # is a line in a worker log nobody keeps. The google presets spent a
        # working day looking like a network fault for exactly that reason:
        # their homepage warmup navigated into a host the gateway refused, every
        # attempt, and the response never said so.
        if fetch_result.warmup_error:
            warnings.append(f"warmup_failed: {fetch_result.warmup_error}")
        # A warmup that RAN and was turned away. Reported separately from a
        # failure because it is a different fact and a more useful one: the
        # exit was refused before the request we care about, so an empty
        # result page afterwards is the site, not the recipe. No URL in the
        # text -- `applied_warmup` already carries it, and this string is
        # substring-scanned in another repository, so nothing target-chosen
        # goes into it (same rule as `egress_warnings`).
        if (fetch_result.applied_warmup or {}).get("blocked"):
            warnings.append(
                "warmup_blocked: the warmup navigation landed on a challenge page"
            )
        # What the transport guard refused, if anything. The browser reports a
        # denied hop as a bare transport error, so without this the caller sees
        # a proxy fault where the truth is "that target is not public".
        warnings.extend(egress_warnings(fetch_result))
        warnings.extend(ignored_field_warnings(fetch_result))
        # The last attempt's error when it is not the one being reported: the
        # reported page explains the failure, but "attempts 2-3 both failed to
        # connect" is how a broken pool becomes visible, and dropping it makes
        # three dead exits read as a plain captcha.
        if last_result is not None and last_result is not fetch_result and last_result.error:
            warnings.append(f"last attempt: {summarise_error(last_result.error)}")
        # Say so when the rotation budget was cut short rather than exhausted:
        # otherwise "blocked after 2 attempts" and "blocked after 2 of 3 attempts,
        # the third never affordable" are indistinguishable to the caller.
        warnings.extend(budget_notes)

        # Parse data: preset pipeline (self-heal / AI) when a
        # parser_plan is present, else the plain deterministic
        # extract for raw /scrape callers.
        # `blocked` gates this, not html emptiness: an interstitial has a
        # perfectly good DOM, and self-heal would regenerate selectors from
        # it — an LLM call per blocked request, and for a user preset those
        # regenerated selectors are persisted over the working ones.
        #
        # A retryable status has to be excluded here for the SAME reason, and it
        # is not covered by `blocked`: `classify_fetch` leaves a transient 5xx
        # blocked=False while its own docstring calls it "not genuine content",
        # so a gateway error page — a perfectly parseable DOM — reached the
        # parser and could be persisted over a working user preset. The
        # attempt-ranking function above already excludes these statuses for
        # exactly this reason; the gate did not, so the two disagreed about what
        # counts as a page.
        page_url = fetch_result.final_url or url
        wants_parsing = bool(request.get("extract") or request.get("parser_plan"))
        not_genuine = fetch_result.blocked or (
            fetch_result.status_code in _RETRYABLE_HTTP_STATUSES
        )
        if wants_parsing and fetch_result.html and not_genuine:
            # Otherwise "why is data null on a preset scrape" is answerable only
            # by inferring it from status_code, and the suppression is the whole
            # point of the gate.
            warnings.append(
                "parsing skipped: the page is not genuine content "
                f"(blocked={fetch_result.blocked}, status={fetch_result.status_code})"
            )
        if wants_parsing and fetch_result.html and not not_genuine:
            parsed_data, parse_warnings = await within_deadline(
                apply_parser(
                    fetch_result.html,
                    request.get("extract"),
                    request.get("parser_plan"),
                ),
                deadline=attempt_deadline,
                label="preset parsing",
                warnings=warnings,
                default=(None, []),
            )
            data = parsed_data
            warnings.extend(parse_warnings)
            # Resolve the page's own redirect stubs BEFORE the self-link check
            # below, which would otherwise (correctly) flag every stub as a link
            # back to the page's host. The transport is the runner's own —
            # `resolve_proxy(None)` is the egress guard on the direct path — so
            # a page-controlled stub URL is judged and dialled in one act, and
            # there is one lifecycle for it, unwound here the way fetch() does.
            resolve_fields = request.get("resolve_redirects")
            if resolve_fields and data is not None:
                # 2-tuple from CamoufoxRunner, 3-tuple from PlaywrightRunner;
                # only the first two matter here.
                resolve_proxy, resolve_cm, *_ = await runner.resolve_proxy(None)
                if resolve_proxy is None and settings.egress_transport_guard:
                    # The runner put nothing in the slot — Camoufox guards its
                    # own requests by route interception, which httpx never
                    # sees. Same guard, opened here, unwound here.
                    resolve_cm = open_egress_guard(resolve=True)
                    resolve_proxy = ProxyConfig(server=(await resolve_cm.__aenter__()).url)
                try:
                    # Sized inside what remains of the attempt so the batch's
                    # own deadline fires first and what already resolved is
                    # kept — the outer wait_for would discard the partials. No
                    # floor: a floor above the remaining budget is the same
                    # discard. With too little left, say so and skip.
                    batch_timeout_s = min(
                        BATCH_TIMEOUT_S, attempt_deadline - time.perf_counter() - 0.5
                    )
                    if batch_timeout_s < 0.5:
                        warnings.append(
                            "redirect resolution skipped: the page-task ceiling left no budget for it"
                        )
                    else:
                        data, resolve_warnings = await within_deadline(
                            resolve_redirect_fields(
                                data, list(resolve_fields), page_url,
                                proxy=resolve_proxy, batch_timeout_s=batch_timeout_s,
                            ),
                            deadline=attempt_deadline,
                            label="redirect resolution",
                            warnings=warnings,
                            default=(data, []),
                        )
                        warnings.extend(resolve_warnings)
                finally:
                    if resolve_cm is not None:
                        await close_quietly(
                            "egress guard",
                            lambda: resolve_cm.__aexit__(None, None, None),
                            owner=page_url,
                        )
            # A preset whose results ARE the site's own pages (mobile.de's
            # query-addressed details.html?id=..., 24 of 24 on one path) says
            # so; the guard would report every such page as an unwrapped
            # redirect. The declaration lives on the preset, not on a name.
            if not (applied_preset and applied_preset.links_stay_on_site):
                self_link_note = self_referential_link_warning(
                    data, fetch_result.final_url
                )
                if self_link_note:
                    warnings.append(self_link_note)
            # What the caller asked for reaches the worker only through the
            # materializer's echo; a preset whose URL has no `{query}` is not judged.
            if applied_preset and applied_preset.query:
                relevance_note = query_relevance_warning(data, applied_preset.query)
                if relevance_note:
                    warnings.append(relevance_note)
                    log.info("%s (request_id=%s)", relevance_note, request_id)

        raw_html = fetch_result.html if want_raw_html else None
        screenshot_b64 = fetch_result.screenshot_b64 if want_screenshot else None

        # Markdown-family outputs (markdown / fit_markdown / html /
        # links). Best-effort: failures degrade to warnings, never
        # fail the scrape. No-op for legacy callers.
        markdown_outputs, md_warnings = await within_deadline(
            apply_markdown(
                request,
                fetch_result.html,
                base_url=page_url,
            ),
            deadline=attempt_deadline,
            label="markdown rendering",
            warnings=warnings,
            default=({}, []),
        )
        warnings.extend(md_warnings)

        took_ms = int((time.perf_counter() - start_time) * 1000)

        # Echo the resolved premium targeting (suffix only — no credentials) so
        # clients can see which country/session/filter actually hit the gateway.
        # Set only by the prem provider; None for every other proxy type.
        final_proxy = session.current_proxy()
        prem_targeting = final_proxy.targeting_suffix if final_proxy else None

        # Send result. Built as the pydantic models rather than a dict: the
        # same ScrapeResponse is what the API validates on the way out, so
        # constructing it here closes the loop instead of hand-rolling its
        # shape a second time.
        try:
            response = ScrapeResponse(
                request_id=request_id,
                took_ms=took_ms,
                meta=ScrapeMeta(
                    url=url,
                    final_url=fetch_result.final_url,
                    status_code=fetch_result.status_code,
                    device=literal_or(
                        request.get("device"), DEVICES, "desktop", field="device", sink=warnings
                    ),
                    proxy_type=literal_or(
                        proxy_type_raw, PROXY_TYPES, "none", field="proxy_type", sink=warnings
                    ),
                    proxy_pool_id=str_or_none(
                        proxy_pool_id, field="proxy_pool_id", sink=warnings
                    ),
                    retries=retries_used,
                    fetch_ok=fetch_result.ok,
                    applied_fingerprint=fetch_result.applied_fingerprint,
                    applied_user_agent=fetch_result.applied_user_agent,
                    applied_locale=fetch_result.applied_locale,
                    applied_timezone=fetch_result.applied_timezone,
                    applied_accept_language=fetch_result.applied_accept_language,
                    applied_preset=applied_preset,
                    applied_prem_targeting=prem_targeting,
                    applied_warmup=model_or_none(
                        AppliedWarmup, fetch_result.applied_warmup, field="applied_warmup",
                        sink=warnings,
                    ),
                ),
                data=data,
                raw_html=raw_html,
                markdown=markdown_outputs.get("markdown"),
                fit_markdown=markdown_outputs.get("fit_markdown"),
                markdown_references=markdown_outputs.get("markdown_references"),
                links=markdown_outputs.get("links"),
                html=markdown_outputs.get("html"),
                screenshot_base64=screenshot_b64,
                element_screenshot_status=fetch_result.element_status,
                warnings=warnings,
            )
        except ValidationError as exc:
            # A completed page whose metadata will not validate. Distinct and
            # greppable on purpose: this is NOT a browser failure, and the
            # generic worker-error line would hide that we threw away real data.
            log.error(
                "result_schema_rejected request_id=%s url=%s status=%s: %s",
                request_id, url, fetch_result.status_code, exc,
            )
            # The caller gets the field names, not pydantic's paragraph: this
            # lands in `ScrapeResponse.error` AND `warnings[0]`, and the raw text
            # carries `input_value=` — i.e. a slice of whatever was rejected —
            # plus internal paths and a docs URL into the public API surface.
            fields = sorted({
                ".".join(str(p) for p in err["loc"]) or "<root>"
                for err in exc.errors()
            })
            # The run's own request_id, not "see the log": `make_error_payload`
            # mints a fresh one for the slot, so the id the client receives and
            # the id in the log line are different strings with nothing joining
            # them.
            return ScrapeErr(
                error=(
                    f"result_schema_rejected: {', '.join(fields)} "
                    f"(request_id={request_id})"
                )
            )
        return ScrapeOk(result=response, storage_state=fetch_result.storage_state)
    except ProxyConfigError as e:
        # User input, not a code failure — WARNING without traceback.
        log.warning("proxy config error for request_id=%s: %s", request_id, e)
        # Same "TypeName: message" shape the session errors use in tasks.py.
        return ScrapeErr(error=f"{type(e).__name__}: {e}")

    except Exception as e:
        error_traceback = traceback.format_exc(limit=20)
        log.error(
            "unexpected error in worker for request_id=%s: %s\n%s",
            request_id,
            str(e),
            error_traceback,
        )
        # `or type(e).__name__` because the envelope no longer carries a
        # "worker_failed" default at the read side: an exception whose str() is
        # empty would otherwise surface as a blank message.
        return ScrapeErr(error=str(e) or type(e).__name__, traceback=error_traceback)
