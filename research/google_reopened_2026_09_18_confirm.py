"""Confirm the one recipe that works: camoufox + www.google.com + direct egress.
Different queries, to rule out a per-query cached verdict. Plus chromium control."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EX = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True},
                                "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True},
                                "urlq": {"selector": "a[href^='/url?']", "attr": "href", "all": True}}}
def run(label, q, engine="camoufox"):
    body = {"url": "https://www.google.com/search?q=" + urllib.parse.quote_plus(q) + "&hl=en",
            "browser_engine": engine, "proxy_type": "none", "block_assets": False, "wait_until": "load",
            "warmup": {"type": "homepage"}, "timeout_ms": 34000, "max_retries": 1, "extract": EX}
    t0 = time.time()
    try:
        jid = post("/scrape/page", body)["job_id"]
        while time.time() - t0 < 220:
            st = get(f"/scrape/{jid}")
            if st.get("status") in ("done", "failed", "error", "cancelled"): break
            time.sleep(3)
        f = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; m = f.get("meta") or {}; d = f.get("data") or {}
        fu = urllib.parse.urlsplit(m.get("final_url") or ""); h3 = d.get("h3") or []
        ok = fu.path.startswith("/search") and bool(h3)
        print(f"{label:<34} {'PASS ' if ok else 'block'} http={m.get('status_code')} took={f.get('took_ms')}ms h3={len(h3)} goto={len(d.get('goto') or [])} urlq={len(d.get('urlq') or [])}", flush=True)
        if ok: print(f"     titles: {[t[:44] for t in h3[1:4]]}", flush=True)
    except Exception as e:
        print(f"{label:<34} ERR {type(e).__name__}: {str(e)[:50]}", flush=True)
    time.sleep(6)
for q in ("ozon.ru отзывы", "python asyncio tutorial", "wildberries доставка", "best laptop 2026"):
    run(f"camoufox direct: {q[:18]!r}", q)
run("chromium direct (control)", "python asyncio tutorial", engine="chromium")
