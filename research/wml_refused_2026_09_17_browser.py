"""/wml through the deployed stack: device=legacy_wap, render=false, the layout
the device exists for. Prints status, size, final host, and the destination
hosts behind the /url?q= links -- judged by values, not by row count."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(path, body):
    req = urllib.request.Request(BASE+path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
def get(path):
    with urllib.request.urlopen(BASE+path, timeout=60) as r: return json.load(r)
def wml_url(q, hl, cc):
    return (f"https://www.google.com/wml/search?q={urllib.parse.quote_plus(q)}"
            f"&sca_esv=1&hl={hl}&lr=lang_{hl}&cr=country{cc}&ie=utf8&oe=utf8")
def hosts(links):
    out = []
    for l in links or []:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(l).query)
        dest = (qs.get("q") or [""])[0]
        h = urllib.parse.urlsplit(dest).netloc
        if h and h not in out: out.append(h)
    return out
for cc, hl in (("DE", "de"), ("GB", "en"), ("US", "en")):
    for engine in ("chromium", "firefox"):
        body = {"url": wml_url("best laptop 2026", hl, cc), "device": "legacy_wap", "render": False,
                "browser_engine": engine, "stealth": True, "block_assets": True, "wait_until": "domcontentloaded",
                "timeout_ms": 34000, "proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": cc},
                "prem_proxy_options": {"ip_filter": "quality-security"},
                "extract": {"type": "css", "fields": {
                    "links": {"selector": "a[href*='/url?q=']", "attr": "href", "all": True},
                    "titles": {"selector": "a[href*='/url?q=']", "all": True}}}}
        t0 = time.time()
        try:
            jid = post("/scrape/page", body)["job_id"]
            while time.time() - t0 < 200:
                st = get(f"/scrape/{jid}")
                if st.get("status") in ("done", "failed", "error", "cancelled"): break
                time.sleep(3)
            first = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]
            meta = first.get("meta") or {}; data = first.get("data") or {}
            links = data.get("links") or []
            print(f"{cc}/{engine}: http={meta.get('status_code')} fetch_ok={meta.get('fetch_ok')} retries={meta.get('retries')} "
                  f"took={first.get('took_ms')}ms final_host={urllib.parse.urlsplit(meta.get('final_url') or '').netloc}"
                  f"{urllib.parse.urlsplit(meta.get('final_url') or '').path[:12]} ua={str(meta.get('applied_user_agent'))[:14]}")
            print(f"   links={len(links)} hosts={hosts(links)[:6]} titles={[t[:30] for t in (data.get('titles') or [])[:3]]}")
            print(f"   warnings={[w[:70] for w in (first.get('warnings') or [])][:3]}")
        except Exception as exc:  # noqa: BLE001
            print(f"{cc}/{engine}: exception {type(exc).__name__}: {str(exc)[:120]}")
