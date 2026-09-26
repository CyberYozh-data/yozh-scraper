"""Interleaved control: the SAME request body, direct egress vs an AM pool exit,
alternating, so time-of-day cannot explain a difference between the two arms."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EX = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True},
                                "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True}}}
tally = {}
def run(arm, i):
    body = {"url": "https://www.google.com/search?q=best+laptop+2026&hl=en", "browser_engine": "camoufox",
            "block_assets": False, "wait_until": "load", "warmup": {"type": "homepage"},
            "timeout_ms": 34000, "max_retries": 1, "extract": EX}
    if arm == "direct":
        body["proxy_type"] = "none"
    else:
        body.update({"proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": "AM"},
                     "prem_proxy_options": {"ip_filter": "quality-security"}})
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
        tally.setdefault(arm, []).append(ok)
        print(f"  {arm:6} #{i}: {'PASS ' if ok else 'block'} http={m.get('status_code')} path={fu.path} h3={len(h3)} goto={len(d.get('goto') or [])} took={f.get('took_ms')}ms", flush=True)
    except Exception as e:
        tally.setdefault(arm, []).append(False)
        print(f"  {arm:6} #{i}: ERR {type(e).__name__}: {str(e)[:60]}", flush=True)
    time.sleep(4)
for i in range(5):
    run("direct", i); run("AM", i)
print("TOTALS:", {k: f"{sum(v)}/{len(v)}" for k, v in tally.items()})
