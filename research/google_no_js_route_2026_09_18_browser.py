"""The JS /search path with JavaScript OFF in our own browser: does Google's
/sorry redirect (a JS redirect on the loaded page) still happen?"""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(path, body):
    req = urllib.request.Request(BASE+path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
def get(path):
    with urllib.request.urlopen(BASE+path, timeout=60) as r: return json.load(r)
for cc, host, hl in (("DE", "www.google.de", "de"), ("GB", "www.google.co.uk", "en")):
    body = {"url": f"https://{host}/search?q=best+laptop+2026&gl={cc.lower()}&hl={hl}", "device": "desktop", "render": False,
            "browser_engine": "chromium", "stealth": True, "block_assets": True, "wait_until": "domcontentloaded", "timeout_ms": 34000,
            "proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": cc}, "prem_proxy_options": {"ip_filter": "quality-security"},
            "warmup": {"type": "homepage"},
            "extract": {"type": "css", "fields": {"h3": {"selector": "h3", "all": True},
                                                  "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True},
                                                  "urlq": {"selector": "a[href^='/url?']", "attr": "href", "all": True},
                                                  "ext": {"selector": "a[href^='http']", "attr": "href", "all": True}}}}
    t0 = time.time(); jid = post("/scrape/page", body)["job_id"]
    while time.time() - t0 < 200:
        st = get(f"/scrape/{jid}")
        if st.get("status") in ("done", "failed", "error", "cancelled"): break
        time.sleep(3)
    first = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; meta = first.get("meta") or {}; d = first.get("data") or {}
    fu = urllib.parse.urlsplit(meta.get("final_url") or "")
    ext = [urllib.parse.urlsplit(u).netloc for u in (d.get("ext") or []) if "google." not in u and "gstatic" not in u]
    print(f"{cc} render=false: http={meta.get('status_code')} fetch_ok={meta.get('fetch_ok')} retries={meta.get('retries')} took={first.get('took_ms')}ms final={fu.netloc}{fu.path[:8]}")
    print(f"   h3={len(d.get('h3') or [])} goto={len(d.get('goto') or [])} urlq={len(d.get('urlq') or [])} external_hosts={sorted(set(ext))[:8]}")
    print(f"   h3 sample={[t[:40] for t in (d.get('h3') or [])[:3]]}  warnings={[w[:60] for w in (first.get('warnings') or [])][:2]}")
