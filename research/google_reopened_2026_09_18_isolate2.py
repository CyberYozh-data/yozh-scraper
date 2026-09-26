"""Which preset setting kills the pass? assets vs locale/host. Direct egress."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EXTRACT = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True}, "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True}}}
def run(label, body):
    t0 = time.time()
    try:
        jid = post("/scrape/page", {**body, "extract": EXTRACT, "max_retries": 1, "warmup": {"type": "homepage"}, "timeout_ms": 34000})["job_id"]
        while time.time() - t0 < 220:
            st = get(f"/scrape/{jid}")
            if st.get("status") in ("done", "failed", "error", "cancelled"): break
            time.sleep(3)
        f = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; m = f.get("meta") or {}; d = f.get("data") or {}
        fu = urllib.parse.urlsplit(m.get("final_url") or "")
        ok = "PASS " if fu.path.startswith("/search") and (d.get("h3") or []) else "block"
        print(f"{label:<40} {ok} http={m.get('status_code')} h3={len(d.get('h3') or [])} goto={len(d.get('goto') or [])}", flush=True)
        return ok.strip() == "PASS"
    except Exception as e:
        print(f"{label:<40} ERR {type(e).__name__}: {str(e)[:50]}", flush=True); return False
    finally: time.sleep(5)
COM = "https://www.google.com/search?q=best+laptop+2026&hl=en"
DE  = "https://www.google.de/search?q=best+laptop+2026&gl=de&hl=de"
base = {"browser_engine": "camoufox", "proxy_type": "none", "wait_until": "load"}
run("direct .com hl=en, assets OFF (control)", {**base, "url": COM, "block_assets": False})
run("direct .com hl=en, assets ON", {**base, "url": COM, "block_assets": True})
run("direct .de gl+hl, assets OFF", {**base, "url": DE, "block_assets": False})
run("direct .de gl+hl, assets ON (preset)", {**base, "url": DE, "block_assets": True})
