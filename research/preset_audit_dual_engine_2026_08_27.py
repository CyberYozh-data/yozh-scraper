"""Run all 28 builtin presets (twenty on 08-27; the four bases #122 added since 09-17) AS SHIPPED, both engines, and judge by VALUES.

Every base ships twice -- `<base>_chromium` and `<base>_camoufox` -- with the
same URL template, the same selectors and the same request defaults. The only
difference between a pair is `browser_engine` (and what follows from it:
`fingerprint_profile` on the Camoufox side, `stealth` on the Chromium side,
since Camoufox ignores playwright-stealth). This harness sends both halves of
each pair the SAME `preset_params` and the SAME locales and asks the SAME
must-fill fields of both, so the engine is the only variable and the two
columns are comparable.

Nothing is overridden. The request that goes out is exactly the one a caller of
`POST /api/v1/scrape/preset/page` gets. `fetch_ok` and `status_code` are recorded
but are NOT the verdict -- an Amazon throttle page arrives with HTTP 200, and a
poisoned Bing SERP carries MORE rows than a clean one.

TIMING: use `took_ms`, never `elapsed_s`. `elapsed_s` is wall time around this
script's own poll loop, which sleeps 3s between polls, so it is quantized to 3s
and also includes queue wait -- a "6s difference" between two engines is two
poll quanta and nothing else. `took_ms` is the worker's own measurement of the
fetch, carried in the result payload (`src/schemas.py`), and is what any timing
claim must rest on. (The 2026-08-27 record predates this field; that run's
timings are not usable for engine claims.)

REDACTION: every record passes through `_redact_ips` before it is stored, so a
proxy exit address that turns up inside a scraped URL or title never reaches the
committed JSON at full precision. Same /24 policy as
`research/fingerprint_probe_2026_08_27.py`.
"""
from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import json
import os
import re
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("BASE", "http://localhost:18000/api/v1")
# A re-run must NOT overwrite the record it is being compared against: the
# 2026-08-27 file is the before-picture and twenty preset descriptions cite it
# by name. `OUT` names the file inside this directory (basename only, so a
# re-run cannot write outside `research/`); the default is the original record,
# which is what `git diff` on that file should keep showing as unchanged.
OUT = Path(__file__).parent / Path(
    os.environ.get("OUT", "preset_audit_dual_engine_2026_08_27.json")).name
RUNS = int(os.environ.get("RUNS", "2"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))
ONLY = os.environ.get("ONLY")
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "300"))

# (base preset, locales, params, fields the caller actually came for)
_BASES = [
    ("amazon_product",   ["us", "uk", "de"], {"asin": "B0CRTYZG5C"},        ("title", "price")),
    ("amazon_search",    ["us", "uk", "de"], {"query": "laptop"},           ("titles", "urls")),
    ("bing_search",      ["us", "de", "ru"], {"query": "best laptop 2026"}, ("titles", "links")),
    ("ebay_search",      ["us", "uk", "de"], {"query": "laptop"},           ("titles", "prices")),
    ("google_search",    ["us", "uk", "de"], {"query": "best laptop 2026"}, ("titles", "links")),
    ("google_shopping",  ["us", "uk", "de"], {"query": "laptop"},           ("titles", "prices")),
    ("linkedin_profile", ["global"],         {"username": "williamhgates"}, ("name", "headline")),
    ("walmart_product",  ["us"],             {"product_id": "5689919121"},  ("title", "price")),
    ("yandex_search",    ["ru", "moscow"],   {"query": "купить ноутбук"},   ("titles", "links")),
    ("youtube_video",    ["global"],         {"video_id": "dQw4w9WgXcQ"},   ("title", "channel")),
    # The four bases #122 added (2026-09-10), with the params their own
    # 2026-09-06 measurement used (research/preset_audit_new_bases_2026_09_06.json)
    # so the numbers stay comparable: 3500/20 is BMW 525 on mobile.de, and the
    # ad is the one #127 re-measured at 0 of 6 behind Akamai.
    ("ozon_search",      ["ru"],             {"query": "ноутбук"},          ("titles", "urls", "prices")),
    # 3972904544 was DELISTED by 2026-09-17: with it the recipe takes the sold-out
    # path by design (price null, price_last set), so this tuple cannot score
    # full until a fresh in-stock id replaces it.
    ("ozon_product",     ["ru"],             {"product_id": "3972904544"},  ("title", "price")),
    ("mobile_de_search", ["de"],             {"make_id": "3500", "model_id": "20"}, ("titles", "urls", "prices")),
    ("mobile_de_ad",     ["de"],             {"ad_id": "391420794"},        ("title", "price")),
]

# Same params, same must-fill fields, both engines: the only variable is the
# engine, which is what makes the two columns comparable.
CASES = [
    (f"{base}_{engine}", locales, params, fields)
    for (base, locales, params, fields) in _BASES
    for engine in ("chromium", "camoufox")
]

_EMPTY = (None, "", [], {})

# Fields that carry the LITERAL fetched page rather than extracted values.
# They are third parties' markup (Amazon, Google, Bing, eBay, Yandex, LinkedIn),
# nothing in the analysis reads them -- it judges titles/links/urls/prices/name
# and the row counts -- and they were 73% of this record set by size. The row
# COUNT is the only evidence they carried, so keep that and drop the markup.
_PAGE_HTML_FIELDS = ("result_blocks", "raw_html", "html")


def _slim(data):
    """Replace verbatim page HTML with the row count it evidenced.

    Call AFTER `_field_fill`: `entries` is derived from the longest list in
    `data`, so slimming first could change the recorded row count.
    """
    if not isinstance(data, dict):
        return data
    out = {}
    for key, value in data.items():
        if key in _PAGE_HTML_FIELDS:
            # Double underscore marks it synthetic. `f"{key}_count"` would have
            # been indistinguishable from a real extracted field -- amazon_product
            # genuinely emits `review_count`.
            out[f"{key}__slimmed_rows"] = (
                len(value) if isinstance(value, list) else int(bool(value)))
        else:
            out[key] = value
    return out


# Word-boundary-ish guards keep this off version strings ("1.2.3.4.5") and off
# digits glued to surrounding text; `ipaddress` makes the final call.
_IPV4_IN_TEXT = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")


def _truncate(addr):
    """`203.0.113.200` -> `203.0.113.0/24`; IPv6 -> /48. RFC 5737 example."""
    return str(ipaddress.ip_network(f"{addr}/{24 if addr.version == 4 else 48}",
                                    strict=False))


def _redact_ips(value):
    """Truncate any IP address in a record before it is persisted.

    Same policy as `research/fingerprint_probe_2026_08_27.py:_truncate_ip` --
    these are third parties' residential exits and this file is committed. Two
    branches, because unlike that probe this harness stores free text (urls,
    titles, snippets) and not only fields that ARE addresses:

      - a value that is itself an address -> truncated (IPv4 and IPv6);
      - an IPv4 embedded in a longer string (`http://203.0.113.200/x`) ->
        truncated in place, which is the realistic case here.

    Embedded IPv6 is deliberately not regex-matched: no safe pattern separates a
    v6 address from ordinary punctuated text, and a field that *is* a v6 address
    is already caught by the first branch.

    `ipaddress` decides "is this an address" in both branches, so anything with an
    octet over 255 passes through untouched -- e.g. `1.131.313.313`, an SVG path
    fragment this record set really did contain.

    THE LIMIT, stated as a rule rather than as an example: any dotted quad whose
    four octets are all <= 255 and which is not glued to a word character or a
    dot on either side is truncated, WHEREVER it appears and whatever it means.
    The regex's guards keep this off version strings only when a fourth
    component follows (`1.2.3.4.5`); a FOUR-component version is indistinguishable
    from an address and is rewritten. The live case is a browser UA:
    `Chrome/151.0.0.0` becomes `Chrome/151.0.0.0/24`, corrupting the string.
    That exact substring already sits in the sibling probe's committed JSON under
    `applied_user_agent`; it survives only because that probe truncates named
    address fields rather than free text, and because THIS record does not carry
    a user-agent field yet. Adding one to `rec["meta"]` would corrupt it, so add
    the field and the exclusion together.

    Excluding a match preceded by `/` was considered and REJECTED: the realistic
    case this branch exists for, `http://203.0.113.200/x`, is preceded by `/`
    too, so a blanket exclusion would stop redacting exactly the URLs that carry
    an exit address. A narrower "single slash after a word character" rule would
    separate `Chrome/151.0.0.0` from `//203.0.113.200`, but it also stops
    redacting an address in a URL PATH segment, which trades a cosmetic defect
    for a disclosure one. So the bias stays deliberate and in this direction:
    over-truncating a version string or a coordinate is cosmetic;
    under-truncating an exit publishes a third party's address.
    """
    if isinstance(value, dict):
        return {k: _redact_ips(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_ips(v) for v in value]
    if not isinstance(value, str):
        return value
    try:
        return _truncate(ipaddress.ip_address(value))
    except ValueError:
        pass

    def _sub(match):
        try:
            return _truncate(ipaddress.ip_address(match.group(0)))
        except ValueError:
            return match.group(0)

    return _IPV4_IN_TEXT.sub(_sub, value)


def _post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.load(r)


def _field_fill(data, fields):
    """Fraction of ROWS that carry each field. A list of 22 nulls is not data."""
    if not isinstance(data, dict):
        return {f: 0.0 for f in fields}, 0
    counts = [len(v) for v in data.values() if isinstance(v, list)]
    entries = max(counts) if counts else (1 if data else 0)
    fill = {}
    for field in fields:
        value = data.get(field)
        if isinstance(value, list):
            fill[field] = (sum(1 for v in value if v not in _EMPTY) / entries) if entries else 0.0
        else:
            fill[field] = 0.0 if value in _EMPTY else 1.0
    return fill, entries


# Every record carries these keys whatever happened, so a consumer can write
# `r["meta"]["status_code"]` without first asking which branch produced the
# record. The failure path used to omit `meta`, `warnings`, `took_ms` and
# `data` entirely, which made an exception record a DIFFERENT SHAPE from a
# success record and a KeyError for anyone reading the file uniformly.
def _blank_record(preset, locale, params, fields, idx):
    return {
        "preset": preset,
        "locale": locale,
        "run": idx,
        "params": params,
        "job_id": None,
        "status": None,
        "meta": {
            "fetch_ok": False,
            "status_code": None,
            # "" not None: the success branch writes a truncated string, so an
            # empty string is what "no final_url" looks like in this file.
            "final_url": "",
            "retries": None,
            "proxy_type": None,
            "applied_locale": None,
            "applied_timezone": None,
            "error": None,
        },
        "took_ms": None,
        "job_error": None,
        "warnings": [],
        "data": None,
        "fill": {f: 0.0 for f in fields},
        "entries": 0,
        "has_data": False,
    }


def one_run(preset, locale, params, fields, idx):
    label = f"{preset}/{locale}#{idx}"
    started = time.perf_counter()
    rec = _blank_record(preset, locale, params, fields, idx)
    try:
        job = _post("/scrape/preset/page", {
            "source": preset, "locale": locale, "preset_params": params})
        job_id = job["job_id"]
        rec["job_id"] = job_id
        deadline = time.time() + POLL_TIMEOUT
        status = None
        while time.time() < deadline:
            st = _get(f"/scrape/{job_id}")
            status = st.get("status")
            if status in ("done", "failed", "error", "cancelled"):
                break
            time.sleep(3)
        res = _get(f"/scrape/{job_id}/results")
        rec["status"] = status
        results = res.get("results") or []
        first = results[0] if results else {}
        meta = first.get("meta") or {}
        data = first.get("data")
        rec["meta"] = {
            "fetch_ok": bool(meta.get("fetch_ok")),
            "status_code": meta.get("status_code"),
            "final_url": (meta.get("final_url") or "")[:200],
            "retries": meta.get("retries"),
            "proxy_type": meta.get("proxy_type"),
            "applied_locale": meta.get("applied_locale"),
            "applied_timezone": meta.get("applied_timezone"),
            "error": meta.get("error"),
        }
        # The worker's own fetch timing, unquantized. `elapsed_s` below is this
        # script's poll loop and cannot carry a timing claim -- see the module
        # docstring.
        rec["took_ms"] = first.get("took_ms")
        rec["job_error"] = res.get("error")
        rec["warnings"] = (first.get("warnings") or [])[:5]
        fill, entries = _field_fill(data, fields)
        rec["data"] = _slim(data)
        rec["fill"] = fill
        rec["entries"] = entries
        rec["has_data"] = any(v > 0 for v in fill.values())
    except Exception as exc:  # noqa: BLE001
        # Only the outcome fields are touched here; the rest keep the blank
        # record's values, so the shape is identical to a success record.
        rec["status"] = "exception"
        rec["job_error"] = f"{type(exc).__name__}: {exc}"
    rec["elapsed_s"] = round(time.perf_counter() - started, 1)
    rec = _redact_ips(rec)
    m = rec.get("meta") or {}
    print(f"{'ok   ' if rec['has_data'] else 'EMPTY'} {label:<28} {rec['elapsed_s']:6.1f}s "
          f"status={rec.get('status')} http={m.get('status_code')} "
          f"entries={rec['entries']:<3} fill={ {k: round(v,2) for k,v in rec['fill'].items()} }",
          flush=True)
    return rec


def main():
    cases = [c for c in CASES if ONLY in (None, c[0])]
    jobs = [(p, loc, par, f, i)
            for (p, locs, par, f) in cases for loc in locs for i in range(RUNS)]
    print(f"{len(cases)} presets, {len(jobs)} runs, {CONCURRENCY} at a time", flush=True)
    records = []
    with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = [ex.submit(one_run, *j) for j in jobs]
        for fut in cf.as_completed(futures):
            records.append(fut.result())
    records.sort(key=lambda r: (r["preset"], r["locale"], r["run"]))
    # encoding pinned: ensure_ascii=False means this file carries the Cyrillic
    # yandex query and whatever Cyrillic titles come back, so the platform
    # default must not decide how they are written.
    OUT.write_text(json.dumps(records, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
