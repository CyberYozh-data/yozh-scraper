"""The working combination (camoufox + www.google.com) through pool exits in
countries that are probably not farmed against Google: AM (this host's own
country), GE, KZ. Control: DE, which is measured refused.

Run twice on 2026-09-18. Round one used n = AM 4, GE 3, KZ 3, DE 2 and scored
AM 1/4, GE 1/3, KZ 0/3, DE 0/2. Round two extended the two arms that had
passed to AM 8 and GE 6 and scored 0/8 and 0/6, i.e. neither pass reproduced.
The loop below carries the union of both rounds."""
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
def run(cc, i):
    body = {"url": "https://www.google.com/search?q=best+laptop+2026&hl=en", "browser_engine": "camoufox",
            "proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": cc},
            "prem_proxy_options": {"ip_filter": "quality-security"},
            "block_assets": False, "wait_until": "load", "warmup": {"type": "homepage"},
            "timeout_ms": 34000, "max_retries": 1, "extract": EX}
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
        tally.setdefault(cc, []).append(ok)
        print(f"  {cc} #{i}: {'PASS ' if ok else 'block'} http={m.get('status_code')} h3={len(h3)} goto={len(d.get('goto') or [])} tz={m.get('applied_timezone')} took={f.get('took_ms')}ms", flush=True)
        if ok and i == 0: print(f"     titles={[t[:40] for t in h3[1:3]]}", flush=True)
    except Exception as e:
        tally.setdefault(cc, []).append(False)
        print(f"  {cc} #{i}: ERR {type(e).__name__}: {str(e)[:60]}", flush=True)
    time.sleep(5)
for cc, n in (("AM", 12), ("GE", 9), ("KZ", 3), ("DE", 2)):
    print(f"--- {cc}")
    for i in range(n): run(cc, i)
print("TOTALS:", {k: f"{sum(v)}/{len(v)}" for k, v in tally.items()})
