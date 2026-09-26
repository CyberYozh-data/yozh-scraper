"""sticky vs rotating, and whether sticky also unlocks the ccTLDs."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EX = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True}, "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True}}}
res = {}
def run(label, url, cc, session_type):
    body = {"url": url, "browser_engine": "camoufox", "block_assets": False, "wait_until": "load",
            "warmup": {"type": "homepage"}, "timeout_ms": 34000, "max_retries": 1, "extract": EX,
            "proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": cc},
            "prem_proxy_options": {"ip_filter": "quality-security", "session_type": session_type}}
    t0 = time.time()
    try:
        jid = post("/scrape/page", body)["job_id"]
        while time.time() - t0 < 220:
            st = get(f"/scrape/{jid}")
            if st.get("status") in ("done", "failed", "error", "cancelled"): break
            time.sleep(3)
        f = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; m = f.get("meta") or {}; d = f.get("data") or {}
        fu = urllib.parse.urlsplit(m.get("final_url") or "")
        ok = fu.path.startswith("/search") and bool(d.get("h3"))
        res.setdefault(label.split("#")[0].strip(), []).append(ok)
        print(f"{label:<40} {'PASS ' if ok else 'block'} http={m.get('status_code')} h3={len(d.get('h3') or [])} goto={len(d.get('goto') or [])}", flush=True)
    except Exception as e:
        res.setdefault(label.split('#')[0].strip(), []).append(False)
        print(f"{label:<40} ERR {type(e).__name__}: {str(e)[:50]}", flush=True)
    time.sleep(6)
q = "best+laptop+2026"
COM = f"https://www.google.com/search?q={q}&hl=en"
for i in range(3): run(f"DE sticky .com #{i}", COM, "DE", "sticky")
for i in range(2): run(f"GB sticky .com #{i}", COM, "GB", "sticky")
for i in range(2): run(f"US sticky .com #{i}", COM, "US", "sticky")
for i in range(2): run(f"DE ROTATING .com (control) #{i}", COM, "DE", "rotating")
for i in range(2): run(f"DE sticky google.de gl+hl #{i}", f"https://www.google.de/search?q={q}&gl=de&hl=de", "DE", "sticky")
print("--- totals:", {k: f"{sum(v)}/{len(v)}" for k, v in res.items()})
