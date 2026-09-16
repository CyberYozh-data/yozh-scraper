from __future__ import annotations

import base64
import logging
import re
from html import unescape
from typing import Any, Callable, Dict, Tuple
from urllib.parse import unquote, urljoin, urlsplit

from lxml import etree, html as lxml_html

from src.extract.models import ExtractRule, PostProcess

log = logging.getLogger(__name__)


def _text(node) -> str:
    return " ".join((node.text_content() or "").split())


def _outer_html(node) -> str:
    return lxml_html.tostring(node, encoding="unicode", with_tail=False)


def _select(doc, selector: str, kind: str):
    if kind == "css":
        return doc.cssselect(selector)
    return doc.xpath(selector)


def _select_rows(doc, rule) -> tuple[list | None, str | None]:
    """The row nodes for a rule, or a message naming what went wrong.

    Selected ONCE per rule rather than once per field: with the container on
    the rule, every field walks the same rows, and re-selecting them per field
    would be both wasted work and a second place for them to disagree.

    A selector can legally return a float, a bool or a list of strings
    (`count(//div)`, `boolean(//h3)`, `//div/@class`). None of those are rows,
    and iterating them raised out of `extract_fields` all the way to the
    worker's outer handler, which discards the whole ScrapeResponse -- the
    page rendered fine and the caller lost the HTML, the screenshot and the
    meta over one string in `extract`. Refused as a field warning instead.
    """
    try:
        rows = _select(doc, rule.container, rule.type)
    except Exception:  # pylint: disable=broad-exception-caught
        return None, "container selector is invalid"
    if not isinstance(rows, list):
        return None, "container selector did not match elements"
    if any(isinstance(row, str) for row in rows):
        return None, "container selector did not match elements"
    return rows, None


def _inside(node, row) -> bool:
    """Did this match actually come from `row`?

    The validator is a claim about selector SYNTAX and it has been wrong twice:
    a prefix check let `..//h3` climb, and CSS was exempted on a rule about
    `descendant-or-self` that sibling combinators break -- `.row ~ .row h3`
    compiles through `following-sibling::` and returns the NEXT row's value.
    Syntax will keep finding new ways to be wrong (`id()` in a later step is
    one this still cannot see), so the guarantee is made here, on the node:
    a value counts when it is the row or sits under it, and otherwise it does
    not count at all.

    lxml's smart strings (an `@href`, a `text()`) carry `getparent`; a computed
    string (`string(...)`) does not, and a value whose origin cannot be
    established is refused rather than assumed -- fail closed.
    """
    element = node.getparent() if isinstance(node, str) else node
    if element is None or not hasattr(element, "getparent"):
        return False
    while element is not None:
        if element is row:
            return True
        element = element.getparent()
    return False


def _one_per_row(rows, field_rule, selector_type: str) -> tuple[list | None, bool]:
    """One value per row, `None` where a row has no match.

    Both halves are load-bearing. `None` in place keeps every column the same
    length -- a hole that shifts its neighbours up is the defect the container
    removes. Taking only the first match means extra matches inside one row
    contribute nothing, which is the sitelinks case where a single card
    carrying three `h3` pushed every later row out of step.
    """
    values, escaped = [], False
    for row in rows:
        try:
            found = _select(row, field_rule.selector, selector_type)
        except Exception:  # pylint: disable=broad-exception-caught
            return None, False
        if not isinstance(found, list) or not found:
            values.append(None)
            continue
        inside = [node for node in found if _inside(node, row)]
        if len(inside) < len(found):
            escaped = True
        if not inside:
            values.append(None)
            continue
        try:
            values.append(_pick(inside[0], field_rule.attr))
        except Exception:  # pylint: disable=broad-exception-caught
            # `.//comment()` reaches `_pick` as an HtmlComment and raises. This
            # helper's whole promise is that a caller-supplied string costs the
            # field and not the response, so the promise covers reading the
            # node as well as finding it.
            return None, escaped
    return values, escaped


def _pick(node, attr: str):
    # XPath may return strings, numbers, or attributes directly — propagate as-is.
    if isinstance(node, str):
        return node.strip() if attr == "text" else node
    if attr == "text":
        return _text(node)
    if attr == "html":
        return _outer_html(node)
    return node.get(attr)


def _apply_regex(value: Any, args: list[Any]) -> Any:
    if value is None:
        return None
    pattern = args[0] if args else ""
    group = args[1] if len(args) > 1 else None
    match = re.search(pattern, str(value))
    if match is None:
        return None
    if group is not None:
        try:
            return match.group(group)
        except IndexError:
            return None
    try:
        return match.group(1)
    except IndexError:
        return match.group(0)


def _strip_tags(value: Any) -> str | None:
    """Render an HTML fragment down to its text content.

    Same normalisation as `_text` (tags dropped, entities decoded, whitespace
    collapsed), so `attr='html'` + `regex` + `strip_tags` yields exactly what
    `attr='text'` on the same node would have — which is what lets a field
    anchor to an always-present container instead of an optional inner node
    without changing the value it returns.
    """
    if value is None:
        return None
    text = str(value)
    if "<" not in text and "&" not in text:
        return " ".join(text.split())
    # create_parent wraps loose/unbalanced fragments (a regex slice of a
    # container's html is rarely a single well-formed element). A parser
    # failure propagates to _apply_post_process, which warns and yields None
    # rather than silently substituting cruder output.
    frag = lxml_html.fragment_fromstring(text, create_parent="div")
    return " ".join((frag.text_content() or "").split())


# One integer, and only one. The grouped spelling must consume the WHOLE digit
# run — `(?!\d)` is what makes "exactly three digits" true rather than merely
# intended; without it a longer run was partially eaten and glued, so
# "+1 800 555 0199" came back as 1800555019, a plausible number with a digit
# silently missing.
#
# The backreference is what tells a grouped number from two numbers that merely
# look like one. A genuinely grouped number uses ONE separator throughout, and
# whatever follows it must not reuse that separator: a decimal tail always
# switches ("12 345,67"), a phone number's next group does not. `(?!\1\d)` is
# therefore the whole locale question answered without knowing the locale.
#
# The spaces that group thousands, in every spelling a page can carry: the
# ASCII space (what `_text` collapses every other one to on the attr="text"
# path, so refusing it would break every grouped Russian and French count),
# U+00A0 (localised pages), U+2009 (the thin space Ozon prints, "55 119 ₽")
# and U+202F (French narrow no-break space). They matter on the attr="html"
# path, where the row-alignment recipes dig a value out of a container's
# serialised html and no collapse ever ran: `_parse_price("55\u2009119")` was
# 55.0, a price 1000x low with a full fill-rate. Not `\s`: a newline or a tab
# between two runs of digits separates two numbers, it never groups one.
_GROUPING_SPACES = " \u00a0\u2009\u202f"
_INT_RE = re.compile(
    rf"\d{{1,3}}(?:([.,{_GROUPING_SPACES}])\d{{3}})(?:\1\d{{3}})*(?!\d)(?!\1\d)|\d+"
)
_INT_GROUPING = str.maketrans("", "", ".," + _GROUPING_SPACES)
_SPACE_GROUPING = str.maketrans("", "", _GROUPING_SPACES)
# A separator may open the number ("$.99" is ordinary sub-dollar retail
# typography) but only when a digit follows it, which is what keeps a bare
# grouping space out of the match (see `_parse_price`).
_PRICE_RE = re.compile(rf"(?:\d|[.,]\d)[\d.,{_GROUPING_SPACES}]*")
# A separator only groups or points a decimal when it stands BETWEEN two digits.
_INNER_SEPARATOR_RE = re.compile(rf"\d[.,{_GROUPING_SPACES}]\d")
# "3999,-" / "3999.-": zero cents, stated. Matched from the separator itself.
_STATED_WHOLE_RE = re.compile(r"[.,]\s*-")
# Currencies whose prices this repo has measured as two-decimal AND grouped:
# every one of them writes 1299 as "1.299,00", "1,299.00" or "1 299". A POSITIVE
# list, because the alternative -- excluding the zero-decimal currencies we
# happen to know (yen, won, rupiah) -- admits every one we did not enumerate,
# and a yen page would then warn on every genuine price. Rouble stays off it:
# Ozon writes "55 119 RUB" grouped, so a bare "3999 RUB" is ordinary elsewhere.
#
# A symbol counts wherever it sits; a three-letter CODE only counts next to the
# number. `\bEUR` would have missed "3999EUR" outright -- a digit and a letter
# are both word characters, so there is no boundary between them -- while
# allowing the code anywhere would read the "EUR" inside "NEUROLOGY 4000" as a
# currency.
_CURRENCY_SYMBOL_RE = re.compile(r"[€$£]")
_CURRENCY_CODE_BEFORE_RE = re.compile(r"(?:EUR|USD|GBP)[\s]*$", re.IGNORECASE)
_CURRENCY_CODE_AFTER_RE = re.compile(r"^[\s]*(?:EUR|USD|GBP)", re.IGNORECASE)
# Below this a missing separator says nothing: "Now $199" is a real price.
_UNGROUPED_PRICE_FLOOR = 1000


def _parse_int(value: Any) -> int | None:
    """First integer in the text, not every digit in it concatenated.

    Stripping all non-digits reads a rating beside a count, a date, or a price
    as part of the number: "4.5 out of 5" became 455. That is worse than the
    None it returns for text with no digits — a null gets noticed, a plausible
    integer does not.
    """
    if value is None:
        return None
    text = str(value)
    match = _INT_RE.search(text)
    if match is None:
        return None
    # The sign must be ATTACHED to the number and must itself open the string or
    # follow whitespace: a hyphen inside a token ("XL-5") is punctuation and a
    # dash used as a separator ("Sale - 19") is not a sign. Same rule as
    # `_parse_price`, which learned it the same way.
    sign = 1
    head = text[: match.start()]
    if head.endswith("-") and (len(head) == 1 or head[-2].isspace()):
        sign = -1
    try:
        return sign * int(match.group(0).translate(_INT_GROUPING))
    except ValueError:
        return None


def _parse_price(value: Any) -> float | None:
    """Parse a price. See `_parse_price_detail` for the separator rules."""
    return _parse_price_detail(value)[0]


def _parse_price_detail(value: Any) -> tuple[float | None, bool]:
    """Parse a numeric string with currency and thousands/decimal separators.

    The second element says whether the number's digits were SEPARATED at all,
    which is what lets the caller name a four-figure price whose decimal point
    never arrived (`tests/extract/test_price_ungrouped_digits.py`).

    The separator convention is read from the TEXT, never from a hint:
      - both `.` and `,` present: the last one is the decimal point.
      - a lone separator followed by one or two digits is the decimal point
        ("899,00" == 899.0, "4,2" == 4.2) -- a thousands group is three
        digits in every locale, so neither shape can be grouping.
      - a repeated separator of one kind, or a tail of three or more digits
        (or none), is grouping ("1.399" == 1399.0, "1.234.567" == 1234567.0).

    Measured, not assumed: a preset's declared country and the page it is
    served disagree (a preset for Germany got an English dot-decimal page and
    "€32.99" parsed as 3299.0), which is why every vote a locale hint once had
    went to the text. The `parse_price` op still accepts a locale arg for
    older presets and ignores it.
    """
    if value is None:
        return None, False
    text = str(value)
    # The class keeps the grouping spaces (`_GROUPING_SPACES`) because a space
    # is a thousands separator in several locales ("1 234,56", "55 119 ₽").
    # Requiring the match to START with a digit is what
    # separates that from a space merely sitting in front of the number: with a
    # leading `[\d., ]`, "Now $199.00" matched the single space at index 3,
    # which `replace(" ", "")` then reduced to "" and the guard below rejected.
    # Every labelled price ("From $12.50", "Sale price $9.99") parsed as None.
    # A separator may open the number ("$.99" is ordinary sub-dollar retail
    # typography) but only when a digit follows it, which is what keeps the
    # bare-space match out.
    match = _PRICE_RE.search(text)
    if match is None:
        return None, False
    # BETWEEN digits, and read BEFORE the grouping spaces are stripped. Both
    # halves matter: a space is one of the separators that makes a large number
    # unambiguous ("55 119"), while `_PRICE_RE` also swallows a separator that
    # merely sits between the number and its currency ("3999 EUR" matches
    # "3999 "), which looked like grouping and silenced the warning on the
    # commonest rendering of the defect. `,-` is the exception: it STATES zero
    # cents rather than omitting them, so it counts as a decimal point.
    # The window starts at the match so a separator the regex swallowed is still
    # visible: "3999, - EUR" matches "3999, " including the space, so anchoring
    # on the last matched character looked at the space and missed the ",-".
    separator_seen = bool(
        _INNER_SEPARATOR_RE.search(match.group(0))
        or _STATED_WHOLE_RE.search(text[match.start():match.end() + 3])
    )
    raw = match.group(0).translate(_SPACE_GROUPING)
    # The sign no longer rides along in the match, so read it from whatever sits
    # between the minus and the digits — a currency symbol, typically.
    # The sign must be ADJACENT: only currency symbols may sit between the minus
    # and the number, and the minus itself must open the string or follow
    # whitespace. Anything looser reads a separator dash ("Sale - $19.99") or a
    # hyphenated name ("XL-$19.99") as a negative price — silently, which is
    # worse than the null this function used to return.
    prefix = text[: match.start()].rstrip("$€£¥₽")
    sign = 1.0
    if prefix.endswith("-"):
        head = prefix[:-1]
        if not head or head[-1].isspace():
            sign = -1.0
    if not raw:
        return None, False

    last_dot = raw.rfind(".")
    last_comma = raw.rfind(",")
    decimal: str | None
    if last_dot > -1 and last_comma > -1:
        decimal = "." if last_dot > last_comma else ","
    else:
        sep = "." if last_dot > -1 else ","
        # Read from the text alone: a lone separator followed by one or two
        # digits is a decimal point in every locale (a thousands group is three
        # digits); repeated, or with a 3+-digit or empty tail ("19."), it is
        # grouping. No separator at all falls through the same way.
        decimal = sep if raw.count(sep) == 1 and len(raw.rsplit(sep, 1)[-1]) in (1, 2) else None

    if decimal == ".":
        clean = raw.replace(",", "")
    elif decimal == ",":
        clean = raw.replace(".", "").replace(",", ".")
    else:
        clean = raw.replace(",", "").replace(".", "")

    try:
        return sign * float(clean), separator_seen
    except ValueError:
        return None, False


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _parse_price(value)


def _base64_decode(value: Any) -> str | None:
    """Decode url-safe base64, padding it back on.

    Bing strips the `=` padding and uses the url-safe alphabet (`-`/`_` for
    `+`/`/`), which the standard decoder rejects outright — so both have to be
    handled or every link comes back null.

    `validate=True` is deliberate: without it the decoder silently DISCARDS
    characters outside the alphabet, so an href that is not a wrapper at all
    decodes to plausible-looking binary instead of failing, and the field ships
    a wrong URL rather than an honest null.
    """
    raw = str(value)
    padded = raw + "=" * (-len(raw) % 4)
    # b64decode(validate=True) rather than urlsafe_b64decode: the latter has no
    # validation and SILENTLY DISCARDS anything outside the alphabet, so an href
    # that is not a wrapper decodes to plausible-looking bytes and the field
    # ships a wrong URL instead of an honest null. altchars maps the url-safe
    # pair back onto the standard one.
    return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")


# Matches `?param=value` or `&param=value`, capturing everything up to the
# next UNESCAPED `&` -- which is exactly the boundary of a query parameter's
# raw (still percent-encoded) value. A nested query string belonging to the
# wrapped destination survives inside that capture as `%26`-escaped `&`s and
# is recovered correctly by the unquote() below; only the OUTER query's own
# separators end the match.
def _unwrap_param_pattern(param: str) -> re.Pattern[str]:
    return re.compile(rf"(?:^|[?&]){re.escape(param)}=([^&]*)")


# Characters a URL parser deletes or trims BEFORE it looks at the value at all:
# tab/CR/LF are removed anywhere, C0 controls and space are trimmed from both
# ends (WHATWG; `urlsplit` has mirrored this since 3.10). `_leading_slash_run`
# has to see the same string the parser will, or `" //host"` slips past it.
_URL_DELETED = str.maketrans("", "", "\t\r\n")
_URL_TRIMMED = "".join(chr(c) for c in range(0x21))


def _leading_slash_run(candidate: str) -> int:
    """How many `/` or `\\` characters the value opens with, as a parser sees it.

    Backslash counts as a slash because a WHATWG parser's "special authority
    ignore slashes" state skips a run of BOTH -- so `//h`, `///h`, `/\\h`,
    `\\\\h` and `\\/h` are all one spelling of the same authority.
    """
    probe = candidate.translate(_URL_DELETED).strip(_URL_TRIMMED)
    run = 0
    for char in probe:
        if char not in "/\\":
            break
        run += 1
    return run


def _is_followable_destination(candidate: str) -> bool:
    """True when `candidate` is a value this op may hand on as a destination.

    The decoded parameter is PAGE-CONTROLLED text: whatever a redirect's query
    string says, percent-decoded. Only two shapes are destinations a caller can
    follow, and everything else is refused:

    * an absolute ``http(s)`` URL with a host; or
    * a plain relative path (``/dp/ASIN/ref=...``), which the ``urljoin`` step
      that normally follows resolves against the page's own URL.

    Refused, specifically:

    * Any other scheme (``javascript:``, ``data:``, ``file:``, ``ftp:`` ...),
      which ``urljoin`` and ``null_if_regex`` pass through verbatim into a
      field whose contract is a followable link. An ``http(s)`` value without
      an authority (``http:///etc/passwd``, ``http:/foo``) is refused for the
      same reason: it names no host, so it is not the absolute URL it looks
      like.
    * Anything OPENING WITH TWO OR MORE SLASHES, backslashes included. Only
      ``//host`` carries an authority as far as ``urlsplit`` is concerned, but
      a WHATWG parser -- a browser href, a Playwright navigation, any JS
      consumer -- resolves ``///host``, ``////host``, ``/\\host``, ``\\\\host``
      and ``\\/host`` to ``https://host`` identically, because its "special
      authority ignore slashes" state skips the whole run. The docstring
      rationale ("carries an authority, is not relative at all") applies to
      every spelling, so the check counts the run rather than trusting
      ``netloc``.
    * A relative reference naming no path segment of its own. ``""``, ``?a=b``
      and ``#frag`` resolve to the page BEING SCRAPED; ``.`` and ``..`` to one
      of its ancestors. None is a destination the parameter actually supplied,
      and shipping one would put the search page itself in every unwrapped
      row's product link.

    Same property, and the same fail-safe passthrough, as
    ``src/api/search.py:_unwrap_redirect``.
    """
    if _leading_slash_run(candidate) >= 2:
        return False
    try:
        parts = urlsplit(candidate)
    except ValueError:
        # Unparseable (e.g. a malformed IPv6 literal) -- not a destination.
        return False
    if parts.scheme:
        return parts.scheme in ("http", "https") and bool(parts.netloc)
    if parts.netloc:
        # `//host/path`: no scheme, but an authority. Not a relative path.
        return False
    return any(
        seg not in (".", "..") for seg in parts.path.split("/") if seg
    )


def _host_matches(value: str, host: str) -> bool:
    """Whether `value`'s HOSTNAME is `host` or a subdomain of it.

    A substring test is not good enough and was measured wrong: a legitimate
    destination whose query merely mentions the host
    (`https://example.org/article?source=bing.com`) matched one, and matching it
    dropped a real row out of half the columns. Compared on DNS-label
    boundaries via a leading dot, so `bing.com` and `www.bing.com` match while
    `notbing.com` and `bing.com.evil.test` do not.
    """
    try:
        netloc = urlsplit(value).netloc.lower()
    except ValueError:
        return False
    hostname = netloc.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".")
    return hostname == host or hostname.endswith("." + host)


def _unwrap_param(
    value: Any, param: str, encoding: str = "percent", prefix: str = "", host: str = ""
) -> Any:
    """Recover a redirect's real destination from one of its own query params.

    Amazon's sponsored-result links carry the destination inline rather than
    behind an opaque token: `/sspa/click?ie=UTF8&spc=...&url=%2FAcer-...` --
    percent-decoding the `url` parameter's value yields the real (still
    relative) product path directly. That is `encoding="percent"`, the default.

    `encoding="base64url"` (with `prefix`) covers the other shape this repo
    meets, Bing's `bing.com/ck/a?...&u=a1<base64url>`: strip `prefix` off the
    parameter's value and base64url-decode the rest. It exists because the
    `regex` + `base64_decode` pair that used to do this NULLS a value it cannot
    match, and Bing links no longer arrive in one shape -- uBlock Origin, which
    Camoufox loads on every launch, rewrites the organic ones to their
    destination before we ever see them (see CAMOUFOX_DISABLE_UBO). A field now
    has to survive both, which is exactly the mixed-shape problem this op's
    pass-through already solves for Amazon.

    A value that does NOT carry `param` is returned UNCHANGED, not null --
    the defining property that lets this run over a field mixing wrapped and
    unwrapped values (amazon_search's `urls`: sponsored rows carry `url=`,
    organic rows don't) in one pipeline without a separate branch per shape.
    A value that carries `param` but not `prefix`, or whose payload does not
    base64-decode, passes through the same way and for the same reason.

    The decoded value is returned only when it is an http(s) URL or a plain
    relative path (see `_is_followable_destination`); anything else is
    refused the same way -- the ORIGINAL value passes through UNCHANGED, not
    null. A crafted `url=javascript:...` / `data:...` / `file:...` / `//host`
    parameter therefore cannot turn this op into a link the caller is told is
    "a real, followable destination"; worst case is a passthrough of the
    wrapper the page already served.
    """
    if value is None:
        return None
    if host and not _host_matches(str(value), host):
        # Not a redirect this wrapper serves, so its query is the DESTINATION's
        # own and must not be read as the wrapper's. Without this, a real result
        # whose URL happens to carry the same parameter name
        # (`https://trusted.example/page?u=a1<base64>`) is "unwrapped" a second
        # time and the column ships a different site than the page linked to.
        return value
    match = _unwrap_param_pattern(param).search(str(value))
    if match is None:
        return value
    raw = match.group(1)
    if encoding == "base64url":
        if prefix:
            if not raw.startswith(prefix):
                # The parameter is there but not in the shape this preset
                # described, so nothing has been recovered and there is nothing
                # to hand on. Passing the wrapper through is the same fail-safe
                # the followability check below uses.
                return value
            raw = raw[len(prefix):]
        try:
            unwrapped = _base64_decode(raw)
        except Exception:  # noqa: BLE001 - _base64_decode RAISES on a bad payload
            # Not `is None`: `_base64_decode` signals failure by raising
            # (binascii.Error / UnicodeDecodeError), so a None check here would
            # be dead code and the broad handler in `_apply_post_process` would
            # null the value instead -- breaking the pass-through this op's
            # contract promises and which the mixed-shape column depends on.
            return value
    else:
        unwrapped = unquote(raw)
    return unwrapped if _is_followable_destination(unwrapped) else value


def _states_two_decimal_currency(text: str) -> bool:
    """Is this text priced in a currency we know writes cents and groups?

    A symbol anywhere is enough. A three-letter code has to touch the number it
    prices, so the "EUR" inside an ordinary word cannot make one.
    """
    if _CURRENCY_SYMBOL_RE.search(text):
        return True
    match = _PRICE_RE.search(text)
    if match is None:
        return False
    return bool(
        _CURRENCY_CODE_BEFORE_RE.search(text[: match.start()])
        or _CURRENCY_CODE_AFTER_RE.search(text[match.end():])
    )


def _warn_if_ungrouped(
    value: float | None,
    separator_seen: bool,
    raw: Any,
    field_name: str,
    warn: Callable[[str, str, str], None],
) -> None:
    """Name a four-figure price whose digits were never separated.

    Measured once in the 276 runs of the 2026-09-12 retest: a node reading
    `3999EUR` published 3999.0 for a 44 EUR product, with a full fill rate and
    no warning. The value is NOT changed -- 3999 may genuinely be 3999, and a
    substituted guess would be worse than the ambiguity -- so this only ends the
    silence, which is the rule `_parse_int` states in its own docstring: a null
    gets noticed, a plausible number does not.

    The raw text goes to the log and never into `warnings`, for the reason
    `_report_silent_nulls` gives two screens down: warnings are returned to API
    callers and substring-scanned downstream, and a price node reading "Block
    detected 1500" would otherwise publish a blocked verdict on a page that
    merely renders those words.
    """
    if value is None or separator_seen or abs(value) < _UNGROUPED_PRICE_FLOOR:
        return
    if not _states_two_decimal_currency(str(raw)):
        return
    warn(
        "parse_price", "ungrouped",
        f"field '{field_name}': parse_price read a four-figure price whose digits "
        f"carry no separator and returned {value!r} -- the shape a page rendering "
        "'39,99' as '3999' leaves behind, while a genuine price this size is "
        "grouped in this currency. The value is unchanged; the text it came from "
        "is in the worker log. Check it against the page.",
    )
    log.warning(
        "parse_price read an ungrouped four-figure price field=%r value=%r sample=%r",
        field_name, value,
        " ".join(str(raw)[: _DROPPED_SAMPLE_CHARS * 2].split())[:_DROPPED_SAMPLE_CHARS],
    )


def _apply_post_process(
    value: Any,
    ops: list[PostProcess],
    field_name: str,
    warnings: list[str],
    seen_warnings: set[tuple[str, str, str]],
) -> Any:
    """Apply post_process pipeline to a single value.

    `seen_warnings` is a per-extract_fields-call set of (field, op, kind)
    tuples; we only emit one warning per combination across all items in a
    list field (otherwise a 40-item list with a bad regex produces 40
    identical log lines).
    """

    def _warn(op: str, kind: str, message: str) -> None:
        key = (field_name, op, kind)
        if key in seen_warnings:
            return
        seen_warnings.add(key)
        warnings.append(message)

    current = value
    for step in ops:
        if current is None:
            return None
        op = step.op
        args = step.args or []
        try:
            if op == "regex":
                current = _apply_regex(current, args)
            elif op == "strip":
                chars = args[0] if args else None
                current = str(current).strip(chars) if chars is not None else str(current).strip()
            elif op == "strip_tags":
                current = _strip_tags(current)
            elif op == "parse_int":
                current = _parse_int(current)
            elif op == "parse_float":
                current = _parse_float(current)
            elif op == "parse_price":
                # The locale arg is accepted for older presets and ignored:
                # the separator is read from the text (see _parse_price).
                before = current
                current, separator_seen = _parse_price_detail(current)
                _warn_if_ungrouped(current, separator_seen, before, field_name, _warn)
            elif op == "lowercase":
                current = str(current).lower()
            elif op == "uppercase":
                current = str(current).upper()
            elif op == "replace":
                current = str(current).replace(str(args[0]), str(args[1]))
            elif op == "base64_decode":
                current = _base64_decode(current)
            elif op == "unwrap_param":
                current = _unwrap_param(
                    current,
                    str(args[0]),
                    str(args[1]) if len(args) > 1 else "percent",
                    str(args[2]) if len(args) > 2 else "",
                    str(args[3]) if len(args) > 3 else "",
                )
            elif op == "urljoin":
                # extract_fields is never given the page's own URL, so a
                # preset that relies on the materializer to inject the base
                # (see materializer.inject_url_base) leaves this arg empty at
                # rest. No base -> the value is left UNCHANGED (never
                # crashed, never nulled) -- but there is no sensible default
                # transform for a URL with no base, so this is not silent:
                # it warns, once per
                # field, so "urls came back relative" has a stated cause
                # instead of looking like a healthy, absolute result.
                base = args[0] if args else None
                if base:
                    current = urljoin(str(base), str(current))
                else:
                    _warn(
                        "urljoin", "no_base",
                        f"field '{field_name}': urljoin has no base_url -- "
                        "materialize a preset (which injects the request's "
                        "own URL) or pass args=[base_url] explicitly; the "
                        "value was left unresolved"
                    )
            elif op == "null_if_regex":
                # Nulls IN PLACE rather than removing the item: extract_fields
                # matches each field independently against the whole document
                # (no notion of a "row"), so the only way to exclude one shape
                # (Amazon's /sspa/click sponsored redirects) from an all=true
                # field without shrinking it — and misaligning every later row
                # against its sibling fields — is to null that slot, not drop it.
                if re.search(str(args[0]), str(current)):
                    current = None
            else:  # pragma: no cover — guarded by Literal at validation
                _warn(op, "unknown", f"field '{field_name}': unknown op '{op}'")
                return None
        except re.error:
            _warn(op, "regex", f"field '{field_name}': invalid regex pattern")
            return None
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _warn(op, "exception", f"field '{field_name}': {op} failed: {exc}")
            return None
    return current


# How much of the dropped text to log. Sliced BEFORE normalising: whitespace-
# collapsing a 10 MB `attr="html"` value first costs ~0.1s and ~200 MB of peak
# RSS, twice per page on the presets that anchor two fields on the document.
_DROPPED_SAMPLE_CHARS = 120
# Kinds that mean "an op SUCCEEDED and the value looks odd" rather than "an op
# complained". `_report_silent_nulls` stands down when an op already named a
# field's cause; an advisory names no cause, so it must not take that slot --
# measured, it replaced "returned null for every non-empty value" with a line
# asserting the value was unchanged, on a field that shipped null.
_ADVISORY_WARNING_KINDS = frozenset({"ungrouped"})


# A leftover regex for the unparseable case only. `[^<>]` rather than `[^>]`:
# letting the class match `<` makes an unclosed bracket scan to end-of-string and
# backtrack once per following `<`, which is quadratic on input the page controls.
_TAG_RE = re.compile(r"""<(?:[^<>"']|"[^"]*"|'[^']*')*>""")


def _has_content(raw: Any) -> bool:
    """True when the raw value carries text a pipeline could have used.

    `attr="html"` is the pattern this repo mandates for row-aligned fields, and
    the markup of an EMPTY container is a non-empty string — so testing the raw
    string makes this vacuous exactly where it matters most.

    Parsed rather than regexed, because "strip the markup" is not a regular
    language and two attempts at it were quadratic: a `<script>` with no closing
    tag makes any `.*?</script>` formulation scan to end-of-string and retry at
    every following `<script`, which cost 45 s on 600 KB — and the cost was paid
    per NULL VALUE, so an ordinary snippet-less SERP result on a page that merely
    mentions `<script` burned 21 s and reported nothing. An unrolled-loop regex
    measured 3x worse; the matching problem itself is quadratic when the close
    tag is absent, so no regex fixes it. lxml is linear, already relied on in
    this module for the same job, and correct on the cases a regex only
    approximated — script bodies, comments, entity-only text.
    """
    if raw is None:
        return False
    text = str(raw)
    if "<" not in text:
        return bool(unescape(text).strip() if "&" in text else text.strip())
    try:
        frag = lxml_html.fragment_fromstring(text, create_parent="div")
    except Exception:  # pylint: disable=broad-exception-caught
        # Unparseable fragment: answer "there is something here" from the cheap
        # pass rather than claiming empty. Claiming empty suppresses a real
        # warning, which is the failure direction this whole change removes.
        return bool(unescape(_TAG_RE.sub(" ", text)).strip())
    # Markup, not the value a pipeline was reaching for. `with_tail=False` keeps
    # the text that follows the element.
    etree.strip_elements(frag, "script", "style", etree.Comment, with_tail=False)
    return bool((frag.text_content() or "").strip())


def _warn_on_silent_nulls(
    field_name: str,
    ops: list[PostProcess],
    raw_values: list[Any],
    processed: list[Any],
    warnings: list[str],
    seen_warnings: set[tuple[str, str, str]],
    *,
    is_column: bool,
) -> None:
    """Report values the pipeline consumed without any op complaining.

    The three existing warnings all describe the *selector*: invalid, matched
    nothing, or matched a different number of nodes than its neighbours. Nothing
    covers "the selector matched, the node had text, and the pipeline returned
    None" — which is not an exotic case but the ordinary shape of markup drift: a
    regex anchored to a label that moved, a price format that gained a currency
    word, a separator that changed. It is also invisible to every check a caller
    can make, because the request succeeds and the field is simply null. That
    silence is why walmart_product shipped `price: null` on every live run until
    someone read the values instead of the counts.

    Not exempted: a field whose selector is the whole document. That was tried and
    reverted — it silenced `youtube_video.views`/`.likes`, whose preset documents
    them as parsed from `ytInitialData`, so a renamed payload key produced exactly
    the walmart shape with no warning at all. The discriminator would have to be
    "is this field expected to be empty", which is preset knowledge the engine does
    not have. `linkedin_profile` therefore warns on every logged-out run — and that
    warning is TRUE: its own description says to expect self-heal to carry those
    fields, i.e. the deterministic pipeline really did produce nothing.

    Only a column nulled in FULL is reported. A single missing value is ordinary
    — a SERP result without a snippet, an out-of-stock row without a price — and
    warning on it would fire on every optional field and train the reader to
    ignore the warning. Every value the selector matched being consumed is the
    unambiguous shape, and the one walmart_product shipped: it is the same
    reasoning `_missing_required` uses when it counts an all-null column as
    missing.

    Deliberately not raised: the pipeline may be right and the page may have
    changed, so this is a signal to go and look, not an error. Suppressed when an
    op already warned for this field, since that warning names the actual cause.
    """
    if any(
        key[0] == field_name and key[2] not in _ADVISORY_WARNING_KINDS
        for key in seen_warnings
    ):
        return
    if is_column and len(raw_values) < 2:
        # A one-row page cannot tell "this result has no snippet" from "the
        # snippet pipeline is dead", and single-result SERPs are common enough
        # that guessing would make the warning untrustworthy. A scalar field has
        # no such ambiguity: its null IS the observation the caller wants
        # explained, which is exactly what walmart_product's price was.
        return
    # The denominator is the values that HAD something. Counting against every
    # node instead switched the detector off whenever one slot on the page
    # happened to be blank — common on real pages, and the walmart column would
    # have gone unreported again.
    dropped = [
        raw for raw, out in zip(raw_values, processed)
        if out is None and _has_content(raw)
    ]
    if not dropped:
        return
    had_content = [raw for raw in raw_values if _has_content(raw)]
    if len(dropped) != len(had_content):
        return
    pipeline = " -> ".join(step.op for step in ops)
    warnings.append(
        f"field '{field_name}': post_process ({pipeline}) returned null for "
        f"every non-empty value ({len(dropped)} of {len(raw_values)} matched)"
    )
    # The dropped text goes to the log, never into `warnings`. Warnings are
    # returned to API callers and scanned by substring downstream — yozh-law-
    # checker publishes a scan as blocked when it sees "captcha" in one — so a
    # sample of arbitrary page content there could publish a false verdict on a
    # page that merely mentions the word. `!r` because the sample is untrusted:
    # it escapes newlines and control characters out of the log line.
    log.warning(
        "post_process nulled every value of field=%r pipeline=%r sample=%r",
        field_name, pipeline,
        " ".join(str(dropped[0])[: _DROPPED_SAMPLE_CHARS * 2].split())[:_DROPPED_SAMPLE_CHARS],
    )


def extract_fields(page_html: str, rule: ExtractRule) -> Tuple[Dict[str, Any], list[str]]:
    doc = lxml_html.fromstring(page_html)
    data: Dict[str, Any] = {}
    warnings: list[str] = []
    seen_warnings: set[tuple[str, str, str]] = set()

    rows, rows_failure = _select_rows(doc, rule) if rule.container else (None, None)

    for name, field_rule in rule.fields.items():
        selector_type = field_rule.type or rule.type

        if rule.container:
            if rows_failure is not None:
                # Rule-level, said once: a five-field recipe with one broken
                # container used to emit five identical lines into a list that
                # is substring-scanned downstream.
                if rows_failure not in warnings:
                    warnings.append(rows_failure)
                data[name] = []
                continue
            if not rows:
                if field_rule.required:
                    warnings.append(f"field '{name}': container matched no rows")
                data[name] = []
                continue
            raw_values, escaped = _one_per_row(rows, field_rule, selector_type)
            if raw_values is None:
                warnings.append(f"field '{name}': invalid selector")
                data[name] = []
                continue
            if escaped:
                # Silence here would be the whole defect wearing a disguise:
                # the column is still full height, so nothing downstream can
                # tell that some of it came from the wrong row.
                warnings.append(
                    f"field '{name}': selector matched outside its row; "
                    f"those matches were discarded"
                )
            if field_rule.required and not any(v is not None for v in raw_values):
                # Rows present, no row matched: the drift mode a container
                # creates, because the row selector is the coarse stable one.
                # Without this the single signal that surfaces it is gone --
                # `row_alignment_mismatch` is a length check and every column
                # here is the same length by construction.
                warnings.append(
                    f"field '{name}': required selector matched no row "
                    f"(0 of {len(rows)})"
                )
        else:
            try:
                nodes = _select(doc, field_rule.selector, selector_type)
            except Exception:  # pylint: disable=broad-exception-caught
                warnings.append(f"field '{name}': invalid selector")
                data[name] = [] if field_rule.all else None
                continue

            if not nodes:
                if field_rule.required:
                    warnings.append(f"field '{name}': required selector not found")
                data[name] = [] if field_rule.all else None
                continue

            raw_values = (
                [_pick(node, field_rule.attr) for node in nodes]
                if field_rule.all
                else [_pick(nodes[0], field_rule.attr)]
            )

        if field_rule.post_process:
            processed = [
                _apply_post_process(
                    value, field_rule.post_process, name, warnings, seen_warnings
                )
                for value in raw_values
            ]
            _warn_on_silent_nulls(
                name, field_rule.post_process,
                raw_values, processed, warnings, seen_warnings,
                is_column=bool(field_rule.all),
            )
        else:
            processed = raw_values

        data[name] = processed if field_rule.all else processed[0]

    # Every `all=true` field is matched against the whole document
    # independently — there is no notion of a "row" in this engine. Callers
    # nonetheless treat the resulting lists as parallel columns and zip them
    # by index (prices[i] belongs to titles[i]), an assumption nothing here
    # has ever verified. A length mismatch — including one field coming back
    # unexpectedly empty — deserves a loud warning even though the request
    # itself should still succeed.
    #
    # This is a LENGTH check, not an alignment check: compensating errors
    # inside a single field (e.g. one card missing a rating while another
    # renders an extra node) can leave all lengths equal while the rows are
    # still misaligned. An empty `warnings` list here is not proof the rows
    # line up.
    all_field_lengths = {
        name: len(data[name]) for name, field_rule in rule.fields.items() if field_rule.all
    }
    if len(all_field_lengths) >= 2 and len(set(all_field_lengths.values())) > 1:
        summary = ", ".join(f"'{name}'={length}" for name, length in all_field_lengths.items())
        warnings.append(
            "row_alignment_mismatch: all=true fields have different lengths "
            f"({summary}) — consumers that zip these lists by index may pair "
            "values with the wrong row. If these fields are genuinely "
            "independent and not meant to align row-by-row, this warning can "
            "be ignored."
        )

    return data, warnings
