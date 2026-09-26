"""Is the pool failure a coupling between runs, or between connections inside a run?
Arm 1: one FRESH sticky id per run (one pinned exit for the whole run, so warmup
and search cannot leave from different addresses). Arm 2: every exit that passes
is re-run twice on the SAME id; two blocked exits are re-run once. Then a direct
control. Exits are learned AFTER each Google run through the same sticky id and
printed as /24 only."""
import json, time, secrets, string, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
def wait(jid, limit=220):
    t0 = time.time()
    while time.time() - t0 < limit:
        if get(f"/scrape/{jid}").get("status") in ("done", "failed", "error", "cancelled"): break
        time.sleep(3)
    return (get(f"/scrape/{jid}/results").get("results") or [{}])[0]
GEX = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True}, "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True}}}
def prem(cc, sid): return {"proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": cc},
                            "prem_proxy_options": {"ip_filter": "quality-security", "session_type": "sticky", "sticky_id": sid}}
def google(label, extra):
    body = {"url": "https://www.google.com/search?q=best+laptop+2026&hl=en", "browser_engine": "camoufox", "block_assets": False,
            "wait_until": "load", "warmup": {"type": "homepage"}, "timeout_ms": 34000, "max_retries": 1, "extract": GEX, **extra}
    f = wait(post("/scrape/page", body)["job_id"]); m = f.get("meta") or {}; d = f.get("data") or {}
    path = urllib.parse.urlsplit(m.get("final_url") or "").path; h3 = d.get("h3") or []
    ok = path.startswith("/search") and bool(h3)
    print(f"  {label:26} {'PASS ' if ok else 'block'} http={m.get('status_code')} path={path} h3={len(h3)} took={f.get('took_ms')}ms", flush=True)
    time.sleep(4); return ok
def exit24(cc, sid):
    body = {"url": "https://api.ipify.org?format=json", "browser_engine": "camoufox", "block_assets": True, "wait_until": "load",
            "timeout_ms": 30000, "max_retries": 1, "extract": {"type": "css", "fields": {"body": {"selector": "body"}}}, **prem(cc, sid)}
    try:
        ip = json.loads(((wait(post("/scrape/page", body)["job_id"]).get("data") or {}).get("body") or "").strip()).get("ip", "")
        return ".".join(ip.split(".")[:3]) + ".x"
    except Exception as e: return f"?{type(e).__name__}"
def sid(): return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
fresh = {}; passed = []; blocked = []
for cc, n in (("AM", 8), ("GE", 4)):
    print(f"--- {cc}: fresh sticky exit per run")
    for i in range(n):
        s = sid(); ok = google(f"{cc} fresh #{i} [{s}]", prem(cc, s)); e = exit24(cc, s)
        print(f"      exit {e}", flush=True)
        fresh.setdefault(cc, []).append(ok); (passed if ok else blocked).append((cc, s, e))
print("--- repeats on the SAME exit")
rep = {}
for cc, s, e in passed:
    for j in range(2): rep.setdefault("after_pass", []).append(google(f"{cc} again#{j} {e} [{s}]", prem(cc, s)))
for cc, s, e in blocked[:2]:
    rep.setdefault("after_block", []).append(google(f"{cc} again#0 {e} [{s}]", prem(cc, s)))
print("--- direct control")
ctrl = [google(f"direct #{i}", {"proxy_type": "none"}) for i in range(2)]
print("FRESH-STICKY:", {k: f"{sum(v)}/{len(v)}" for k, v in fresh.items()},
      "| REPEATS:", {k: f"{sum(v)}/{len(v)}" for k, v in rep.items()}, "| direct:", f"{sum(ctrl)}/{len(ctrl)}")
