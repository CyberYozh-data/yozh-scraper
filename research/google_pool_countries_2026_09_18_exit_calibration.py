"""Instrument check before the Google experiment: does the gateway honour a
sticky id (same exit twice), does rotating change the exit per run, and does
meta echo the session token? Exits are printed as /24 only."""
import json, time, urllib.request
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EX = {"type": "css", "fields": {"body": {"selector": "body"}}}
def run(label, prem):
    body = {"url": "https://api.ipify.org?format=json", "browser_engine": "camoufox", "proxy_type": "prem_res_rotating",
            "proxy_geo": {"country_code": "AM"}, "prem_proxy_options": {"ip_filter": "quality-security", **prem},
            "block_assets": True, "wait_until": "load", "timeout_ms": 30000, "max_retries": 1, "extract": EX}
    t0 = time.time(); jid = post("/scrape/page", body)["job_id"]
    while time.time() - t0 < 150:
        if get(f"/scrape/{jid}").get("status") in ("done", "failed", "error", "cancelled"): break
        time.sleep(2)
    f = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; m = f.get("meta") or {}; d = f.get("data") or {}
    ip = ""
    try: ip = json.loads((d.get("body") or "").strip()).get("ip", "")
    except Exception: ip = (d.get("body") or "")[:30]
    slash24 = ".".join(ip.split(".")[:3]) + ".x" if ip.count(".") == 3 else repr(ip)
    print(f"  {label:22} exit={slash24:18} targeting={m.get('applied_prem_targeting')} http={m.get('status_code')} took={f.get('took_ms')}ms", flush=True)
    return ip
r = [run(f"rotating #{i}", {"session_type": "rotating"}) for i in range(3)]
a = [run(f"sticky A #{i}", {"session_type": "sticky", "sticky_id": "calibA01"}) for i in range(2)]
b = [run("sticky B #0", {"session_type": "sticky", "sticky_id": "calibB01"})]
print("rotating distinct:", len(set(r)), "of", len(r), "| sticky A same twice:", a[0] == a[1] and bool(a[0]), "| B differs from A:", b[0] != a[0])
