"""Measure the Google `/wml/search` recipe: is it stable at home, and does it
survive OUR residential exits?

Background (measured 2026-09-10 from the k12 host, bare curl, no proxy, one
endpoint, one query, ONLY the User-Agent changed): a UA from SearXNG's
`nokia_useragents` tuple returns a real SERP (HTTP 200, ~12 result blocks,
~19 `/url?q=` links); any other UA -- an unlisted Nokia, a Symbian N97, a
modern desktop Chrome -- returns HTTP 200 with zero blocks and "Update your
browser". The UA is a whitelist KEY, not a device hint. An earlier exit-
dependency run used unlisted UAs and therefore measured nothing.

This script answers the two questions that are still open:

  Q1  Is the recipe stable on the home egress? n>=10 spaced requests, all six
      whitelisted UAs, several queries and locales.
  Q2  Does a whitelisted UA still get a SERP through our CyberYozh residential
      exits? n>=4 per country, at least two countries, plus a no-proxy control
      series in the same hour.

JUDGED BY VALUES, NOT BY COUNTS. "Rows came back" is exactly what Bing fooled
this repo with (a poisoned SERP carries MORE rows than a clean one). So every
response is reduced to the ORDERED SET OF DESTINATION HOSTS behind the top-10
organic links -- `/url?q=` unwrapped -- and that set is compared against what
the shipped `google_search_chromium` preset returns for the same query in the
same hour. The repo's own `query_relevance_warning`
(`src/queue/scrape_runner.py`) is run over the extracted rows too, in a second
pass inside the container image (`judge` mode) because the host python has no
`src` on its path.

LIMIT OF `exit_net`, found in review AFTER this data was gathered: the proxy is
resolved as `prem_res_rotating` with no sticky session, and `exit_ip_24()` and
the page request are two separate curl processes -- two connections, which a
rotating gateway may place on different exits. So a record's `exit_net`
describes the connection that asked for the IP, not necessarily the one that
fetched the page, and a single block MUST NOT be attributed to that subnet. The
aggregate the run was for does not depend on it: whether a request came back a
SERP or a `/sorry` is read from that request itself. Deliberately not "fixed"
here -- pinning a sticky session now would describe a run that did not happen;
a future probe should resolve one sticky session per record and use it for both
requests.

NO SECRETS ARE STORED. The service token is read from `$SERVICE_TOKEN` or, if
unset, from the repo `.env` at runtime; the resolved upstream proxy URL (which
embeds credentials) is handed to curl on stdin via `-K -`, never on argv and
never into the JSON. Exit addresses are recorded truncated to /24, the same
policy as `research/preset_audit_dual_engine_2026_08_27.py`.

RESTARTABLE. Every unit of work carries a stable `id`; a re-run loads the JSON
that is already there and performs only the missing ids. Delete the JSON (or
set `FRESH=1`) to start over.

Usage
-----
    python3 research/wml_probe_2026_09_11.py            # fetch (host python)
    docker run --rm -v <worktree>:/app -w /app \
        open-scraper-clone-web-scraper:latest \
        python research/wml_probe_2026_09_11.py judge   # add relevance verdicts

Env: SLEEP (default 20s between live Google requests), FRESH, RUN_PRESET=0 to
skip the preset comparison, BASE (default http://localhost:18000/api/v1).
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = HERE / "wml_probe_2026_09_11.json"

BASE = os.environ.get("BASE", "http://localhost:18000/api/v1")
SLEEP = float(os.environ.get("SLEEP", "20"))
RUN_PRESET = os.environ.get("RUN_PRESET", "1") != "0"
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "300"))

# github.com/searxng/searxng issue 6359 -- the six UAs Google's /wml endpoint
# answers. Copied from searx/engines/google.py (`nokia_useragents`). These are
# facts about Google's whitelist, not an expression of SearXNG.
NOKIA_UAS = (
    "Nokia7610/2.0 (5.0509.0) SymbianOS/7.0s Series60/2.1 Profile/MIDP-2.0 Configuration/CLDC-1.0",
    "Nokia7610/2.0 (7.0642.0) SymbianOS/7.0s Series60/2.1 Profile/MIDP-2.0 Configuration/CLDC-1.0",
    "Nokia6230/2.0 (05.50) Profile/MIDP-2.0 Configuration/CLDC-1.1",
    "Nokia6230i/2.0 (03.80) Profile/MIDP-2.0 Configuration/CLDC-1.1",
    "Nokia6280/2.0 (03.60) Profile/MIDP-2.0 Configuration/CLDC-1.1",
    "NokiaN72/2.0617.1.0.3 Series60/2.8 Profile/MIDP-2.0 Configuration/CLDC-1.1",
)

# (label, query, hl, country) -- `country` feeds cr=country<CC> and, for the
# proxied series, the exit country and the preset locale.
CASES = {
    "us_en": ("best laptop 2026", "en", "US"),
    "ru_ru": ("best laptop 2026", "ru", "RU"),
    "de_de": ("best laptop 2026", "de", "DE"),
    "ru_native": ("купить ноутбук", "ru", "RU"),
    "de_native": ("beste kaffeemaschine", "de", "DE"),
}

# The locale name the shipped preset uses for the same market.
PRESET_LOCALE = {"US": "us", "RU": "ru", "DE": "de"}


def wml_url(query: str, hl: str, country: str) -> str:
    return (
        "https://www.google.com/wml/search?q="
        + urllib.parse.quote_plus(query)
        + f"&sca_esv=1&hl={hl}&lr=lang_{hl}&cr=country{country}&ie=utf8&oe=utf8"
    )


# ---------------------------------------------------------------- parsing

# One organic result is one `div class="zMzFAb"`; its first `a.fuLhoc` carries
# the `/url?q=` wrapper, the title span and (for most) a display-url span. The
# extra `/url?q=` hrefs in a page are that result's sitelinks, which is why the
# raw link count is always higher than the block count -- counting links would
# overcount, so blocks are counted and only the FIRST anchor of each is read.
_BLOCK_RE = re.compile(r'<div class="zMzFAb">(.*?)(?=<div class="zMzFAb">|$)', re.S)
_ANCHOR_RE = re.compile(r'<a class="fuLhoc[^"]*" href="(/url\?q=[^"]+)"(.*?)</a>', re.S)
_TITLE_RE = re.compile(r'<span class="CVA68e[^"]*">(.*?)</span>', re.S)
_CITE_RE = re.compile(r'<span class="qXLe6d dXDvrc">\s*<span class="fYyStc">(.*?)</span>', re.S)
_SNIPPET_RE = re.compile(r'<span class="qXLe6d FrIlee">(.*?)</span>\s*</span>', re.S)
_TAGS_RE = re.compile(r"<[^>]+>")
_BLOCKED_MARKERS = (
    "Update your browser",
    "unusual traffic",
    "/sorry/",
    "captcha",
    "Наш<wbr/>и системы",
)


def _text(fragment: str) -> str:
    return html.unescape(_TAGS_RE.sub(" ", fragment)).replace("\xa0", " ").strip()


def _unwrap(href: str) -> str:
    """`/url?q=https://x/y&sa=U&...` -> `https://x/y`. Unlike the desktop SERP's
    opaque `/goto?url=CAES...` token, the wml wrapper carries the destination in
    clear text, so it can be read without a network round-trip."""
    query = urllib.parse.urlsplit(html.unescape(href)).query
    return urllib.parse.parse_qs(query).get("q", [""])[0]


def parse_serp(body: str) -> dict:
    blocks = _BLOCK_RE.findall(body)
    results = []
    for block in blocks:
        anchor = _ANCHOR_RE.search(block)
        if not anchor:
            continue
        dest = _unwrap(anchor.group(1))
        if not dest.startswith(("http://", "https://")):
            continue
        inner = anchor.group(2)
        title_m = _TITLE_RE.search(inner)
        cite_m = _CITE_RE.search(inner)
        snip_m = _SNIPPET_RE.search(block)
        results.append({
            "url": dest,
            "host": urllib.parse.urlsplit(dest).netloc.lower(),
            "title": _text(title_m.group(1)) if title_m else None,
            "display_url": _text(cite_m.group(1)) if cite_m else None,
            "snippet": _text(snip_m.group(1)) if snip_m else None,
        })
    return {
        "blocks": len(blocks),
        "url_q_links": len(re.findall(r"/url\?q=", body)),
        "results": results,
        "blocked_marker": next((m for m in _BLOCKED_MARKERS if m in body), None),
        "title": (re.search(r"<title>(.*?)</title>", body, re.S).group(1)
                  if "<title>" in body else None),
    }


def top_hosts(results: list[dict], n: int = 10) -> list[str]:
    """Destination hosts behind the top-n organic links, order preserved,
    deduplicated. This -- not a row count -- is what two runs are compared on."""
    seen, out = set(), []
    for r in results[:n]:
        host = r["host"]
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


# ---------------------------------------------------------------- fetching

def _service_token() -> str:
    token = os.environ.get("SERVICE_TOKEN")
    if token:
        return token
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("SERVICE_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("SERVICE_TOKEN not in env and not in the repo .env")


def resolve_proxy_url(country: str) -> str:
    """Ask OUR api for a premium residential exit in `country`. Returns a
    credentialed URL -- keep it out of argv, out of logs and out of the JSON."""
    url = (f"{BASE}/proxies/resolve?proxy_type=prem_res_rotating"
           f"&country_code={urllib.parse.quote(country)}")
    req = urllib.request.Request(url, headers={"X-Service-Token": _service_token()})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["proxy_url"]


def _curl(url: str, ua: str | None, proxy_url: str | None, timeout: int = 45) -> dict:
    """One request through curl. The proxy (with its credentials) goes in on
    stdin as a curl config file, so `ps` never sees it."""
    args = ["curl", "-sS", "-m", str(timeout), "-o", "-",
            "-w", "\n__META__%{http_code} %{size_download}\n"]
    if ua:
        args += ["-H", f"User-Agent: {ua}", "-H", "Accept: */*", "-H", "Cookie: CONSENT=YES+"]
    stdin = ""
    if proxy_url:
        args += ["-K", "-"]
        stdin = f'proxy = "{proxy_url}"\n'
    args.append(url)
    proc = subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)
    body, _, meta = proc.stdout.rpartition("\n__META__")
    status, _, size = meta.strip().partition(" ")
    return {
        "http": int(status) if status.isdigit() else None,
        "size": int(size) if size.strip().isdigit() else 0,
        "body": body,
        "curl_error": proc.stderr.strip()[:300] or None,
        "curl_rc": proc.returncode,
    }


def exit_ip_24(proxy_url: str | None) -> str | None:
    """The exit's /24, for the record. A separate host from Google on purpose --
    never ask Google who we are."""
    res = _curl("https://ipinfo.io/json", None, proxy_url, timeout=30)
    try:
        addr = ipaddress.ip_address(json.loads(res["body"])["ip"])
    except Exception:  # noqa: BLE001
        return None
    return str(ipaddress.ip_network(f"{addr}/{24 if addr.version == 4 else 48}", strict=False))


def probe(rec_id: str, case: str, ua_index: int, country: str | None) -> dict:
    query, hl, cc = CASES[case]
    proxy_url = None
    exit_net = None
    error = None
    if country:
        try:
            proxy_url = resolve_proxy_url(country)
            exit_net = exit_ip_24(proxy_url)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
    rec = {
        "id": rec_id,
        "ts": int(time.time()),
        "case": case,
        "query": query,
        "hl": hl,
        "cr": cc,
        "ua_index": ua_index,
        "ua": NOKIA_UAS[ua_index],
        "exit_country": country,
        "exit_net": exit_net,
        "proxy_error": error,
    }
    if country and proxy_url is None:
        rec.update({"http": None, "blocks": 0, "url_q_links": 0, "results": [],
                    "top_hosts": [], "blocked_marker": None, "size": 0,
                    "curl_error": None, "serp_title": None})
        return rec
    res = _curl(wml_url(query, hl, cc), NOKIA_UAS[ua_index], proxy_url)
    parsed = parse_serp(res["body"])
    rec.update({
        "http": res["http"],
        "size": res["size"],
        "curl_error": res["curl_error"],
        "blocks": parsed["blocks"],
        "url_q_links": parsed["url_q_links"],
        "blocked_marker": parsed["blocked_marker"],
        "serp_title": parsed["title"],
        "results": parsed["results"][:10],
        "top_hosts": top_hosts(parsed["results"]),
    })
    return rec


# ------------------------------------------------------------ preset side

def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.load(resp)


def preset_run(rec_id: str, case: str) -> dict:
    """The shipped `google_search_chromium`, as shipped, same query, same hour.
    Nothing is overridden -- overriding engine/stealth produces false verdicts."""
    query, _hl, cc = CASES[case]
    locale = PRESET_LOCALE[cc]
    rec = {"id": rec_id, "ts": int(time.time()), "case": case, "query": query,
           "locale": locale, "preset": "google_search_chromium"}
    try:
        job = _post("/scrape/preset/page", {
            "source": "google_search_chromium", "locale": locale,
            "preset_params": {"query": query}})
        job_id = job["job_id"]
        deadline = time.time() + POLL_TIMEOUT
        status = None
        while time.time() < deadline:
            status = _get(f"/scrape/{job_id}").get("status")
            if status in ("done", "failed", "error", "cancelled"):
                break
            time.sleep(3)
        res = _get(f"/scrape/{job_id}/results")
        first = (res.get("results") or [{}])[0]
        data = first.get("data") or {}
        links = [l for l in (data.get("links") or []) if isinstance(l, str)]
        hosts, seen = [], set()
        for link in links[:10]:
            host = urllib.parse.urlsplit(link).netloc.lower()
            if host and host not in seen:
                seen.add(host)
                hosts.append(host)
        rec.update({
            "status": status,
            "job_error": res.get("error"),
            "http": (first.get("meta") or {}).get("status_code"),
            "warnings": (first.get("warnings") or [])[:5],
            "titles": (data.get("titles") or [])[:10],
            "links": links[:10],
            "snippets": (data.get("snippets") or [])[:10],
            "display_urls": (data.get("display_urls") or [])[:10],
            "top_hosts": hosts,
        })
    except Exception as exc:  # noqa: BLE001
        rec.update({"status": "exception", "job_error": f"{type(exc).__name__}: {exc}",
                    "top_hosts": [], "titles": [], "links": [], "snippets": [],
                    "display_urls": [], "warnings": []})
    return rec


# ----------------------------------------------------------------- judge

def judge(store: dict) -> dict:
    """Second pass, run inside the container image: apply THIS repo's own
    relevance guard to the wml rows, shaped as the preset's columns."""
    sys.path.insert(0, str(REPO))
    from src.queue.scrape_runner import query_relevance_warning  # noqa: PLC0415

    for rec in store["wml"] + store["preset"]:
        if "results" in rec:
            rows = rec["results"]
            data = {
                "titles": [r.get("title") for r in rows],
                "links": [r.get("url") for r in rows],
                "snippets": [r.get("snippet") for r in rows],
                "display_urls": [r.get("display_url") for r in rows],
            }
        else:
            data = {k: rec.get(k) or [] for k in
                    ("titles", "links", "snippets", "display_urls")}
        rec["relevance_warning"] = query_relevance_warning(data, rec["query"])
    return store


# ------------------------------------------------------------------ plan

def plan() -> tuple[list[tuple], list[tuple]]:
    """Q1: 12 direct requests, all six UAs twice, three locales.
    Q2: 4 per exit country (RU, DE) + a 4-request no-proxy control in the same
    hour, so a difference cannot be blamed on the hour."""
    q1 = [(f"q1-{i:02d}", case, i % len(NOKIA_UAS), None) for i, case in enumerate(
        ["us_en", "ru_ru", "de_de", "ru_native", "de_native", "us_en",
         "ru_ru", "de_de", "ru_native", "de_native", "us_en", "ru_ru"])]
    q2: list[tuple] = []
    # n per exit country. The first pass ran 4+4; DE came back 1/4, which is a
    # number too small to carry the verdict, so the default is 8. Ids are stable
    # across the widening, so a re-run only fetches what is missing.
    for i in range(int(os.environ.get("N_PER_COUNTRY", "8"))):
        q2.append((f"q2-ru-{i}", "ru_ru", i % len(NOKIA_UAS), "RU"))
        q2.append((f"q2-de-{i}", "de_de", (i + 3) % len(NOKIA_UAS), "DE"))
    for i in range(4):
        case = "ru_ru" if i % 2 == 0 else "de_de"
        q2.append((f"q2-ctl-{i}", case, i % len(NOKIA_UAS), None))
    return q1, q2


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "judge":
        store = json.loads(OUT.read_text(encoding="utf-8"))
        OUT.write_text(json.dumps(judge(store), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print("judged", len(store["wml"]), "wml +", len(store["preset"]), "preset records")
        return

    store = {"generated_at": None, "wml": [], "preset": []}
    if OUT.exists() and os.environ.get("FRESH", "0") != "1":
        store = json.loads(OUT.read_text(encoding="utf-8"))
    done = {r["id"] for r in store["wml"]} | {r["id"] for r in store["preset"]}

    q1, q2 = plan()
    for rec_id, case, ua_index, country in q1 + q2:
        if rec_id in done:
            continue
        rec = probe(rec_id, case, ua_index, country)
        store["wml"].append(rec)
        store["generated_at"] = int(time.time())
        OUT.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{rec_id:<12} {case:<10} exit={country or 'direct':<6} "
              f"http={rec['http']} blocks={rec['blocks']:<3} "
              f"links={rec['url_q_links']:<3} hosts={len(rec['top_hosts'])} "
              f"marker={rec['blocked_marker']!r} net={rec['exit_net']}", flush=True)
        time.sleep(SLEEP + random.uniform(0, SLEEP * 0.4))

    if RUN_PRESET:
        # Which markets the shipped preset is asked for. Configurable because the
        # first pass (ru, de) came back BLOCKED on both, and a blocked comparison
        # anchor is no anchor -- adding `us_en` tells a locale-specific block
        # apart from Google walling this preset outright.
        cases = os.environ.get("PRESET_CASES", "ru_ru,de_de").split(",")
        for case in cases:
            rec_id = f"preset-{case}"
            if rec_id in done:
                continue
            rec = preset_run(rec_id, case)
            store["preset"].append(rec)
            store["generated_at"] = int(time.time())
            OUT.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{rec_id:<14} status={rec.get('status')} http={rec.get('http')} "
                  f"hosts={rec.get('top_hosts')} warn={rec.get('warnings')}", flush=True)

    print("wrote", OUT)


if __name__ == "__main__":
    main()
