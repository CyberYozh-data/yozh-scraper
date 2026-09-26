"""Two variables confirmed so far: host (.com vs ccTLD) and egress. Pin both."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EX = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True}, "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True}}}
def run(label, url, proxy=None):
    body = {"url": url, "browser_engine": "camoufox", "block_assets": False, "wait_until": "load",
            "warmup": {"type": "homepage"}, "timeout_ms": 34000, "max_retries": 1, "extract": EX}
    body |= proxy or {"proxy_type": "none"}
    t0 = time.time()
    try:
        jid = post("/scrape/page", body)["job_id"]
        while time.time() - t0 < 220:
            st = get(f"/scrape/{jid}")
            if st.get("status") in ("done", "failed", "error", "cancelled"): break
            time.sleep(3)
        f = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; m = f.get("meta") or {}; d = f.get("data") or {}
        fu = urllib.parse.urlsplit(m.get("final_url") or "")
        ok = "PASS " if fu.path.startswith("/search") and (d.get("h3") or []) else "block"
        print(f"{label:<44} {ok} http={m.get('status_code')} h3={len(d.get('h3') or [])} goto={len(d.get('goto') or [])}", flush=True)
    except Exception as e:
        print(f"{label:<44} ERR {type(e).__name__}: {str(e)[:50]}", flush=True)
    time.sleep(6)
q = "best+laptop+2026"
COM = f"https://www.google.com/search?q={q}&hl=en"
print("--- host variable, direct egress")
run("direct google.co.uk", f"https://www.google.co.uk/search?q={q}&hl=en")
run("direct google.com (stability #1)", COM)
run("direct google.com (stability #2)", COM)
print("--- exit variable, always google.com")
run("DE exit, filter=max-size-security", COM, {"proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": "DE"}, "prem_proxy_options": {"ip_filter": "max-size-security"}})
run("DE exit, sticky session", COM, {"proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": "DE"}, "prem_proxy_options": {"ip_filter": "quality-security", "session_type": "sticky"}})
run("US exit, filter=speed-quality-security", COM, {"proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": "US"}, "prem_proxy_options": {"ip_filter": "speed-quality-security"}})
run("res_rotating (non-premium pool)", COM, {"proxy_type": "res_rotating"})
