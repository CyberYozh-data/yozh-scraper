"""Three variables, isolated: engine (camoufox vs chromium), exit (none vs
prem_res_rotating), settings (mine vs the shipped preset's)."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EXTRACT = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True},
                                     "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True}}}
def run(label, body):
    t0 = time.time()
    try:
        jid = post("/scrape/page", {**body, "extract": EXTRACT, "max_retries": 1})["job_id"]
        while time.time() - t0 < 220:
            st = get(f"/scrape/{jid}")
            if st.get("status") in ("done", "failed", "error", "cancelled"): break
            time.sleep(3)
        first = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; m = first.get("meta") or {}; d = first.get("data") or {}
        fu = urllib.parse.urlsplit(m.get("final_url") or "")
        ok = "PASS" if fu.path.startswith("/search") and (d.get("h3") or []) else "block"
        print(f"{label:<44} {ok:<5} http={m.get('status_code')} took={first.get('took_ms')}ms h3={len(d.get('h3') or [])} goto={len(d.get('goto') or [])} final={fu.netloc}{fu.path[:8]}", flush=True)
    except Exception as e:
        print(f"{label:<44} ERR {type(e).__name__}: {str(e)[:60]}", flush=True)
    time.sleep(5)
URL = "https://www.google.com/search?q=best+laptop+2026&hl=en"
PRESET_URL = "https://www.google.de/search?q=best+laptop+2026&gl=de&hl=de"
# 1. camoufox direct, but with the SHIPPED preset's settings
run("camoufox direct, preset settings", {"url": PRESET_URL, "browser_engine": "camoufox", "proxy_type": "none",
     "block_assets": True, "wait_until": "load", "warmup": {"type": "homepage"}, "timeout_ms": 34000})
# 2. camoufox through exits, my settings (assets on)
for cc in ("DE", "GB", "US"):
    run(f"camoufox via {cc} exit, assets on", {"url": URL, "browser_engine": "camoufox", "proxy_type": "prem_res_rotating",
         "proxy_geo": {"country_code": cc}, "prem_proxy_options": {"ip_filter": "quality-security"},
         "block_assets": False, "wait_until": "load", "warmup": {"type": "homepage"}, "timeout_ms": 34000})
# 3. camoufox direct again for stability
for i in range(3):
    run(f"camoufox direct, my settings #{i}", {"url": URL, "browser_engine": "camoufox", "proxy_type": "none",
         "block_assets": False, "wait_until": "load", "warmup": {"type": "homepage"}, "timeout_ms": 34000})
