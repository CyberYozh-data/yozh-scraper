"""/wml through the product path with a CARRIER exit: prem_res_rotating +
prem_proxy_options.isp, device=legacy_wap, render=false."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(path, body):
    req = urllib.request.Request(BASE+path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
def get(path):
    with urllib.request.urlopen(BASE+path, timeout=60) as r: return json.load(r)
def wml(hl, cc):
    return "https://www.google.com/wml/search?" + urllib.parse.urlencode({"q": "best laptop 2026", "sca_esv": "1", "hl": hl, "lr": f"lang_{hl}", "cr": f"country{cc}", "ie": "utf8", "oe": "utf8"})
for cc, hl, isp in (("GB", "en", "Three"), ("US", "en", "AT&T Wireless"), ("DE", "de", "O2 Deutschland")):
    body = {"url": wml(hl, cc), "device": "legacy_wap", "render": False, "browser_engine": "chromium", "stealth": True,
            "block_assets": True, "wait_until": "domcontentloaded", "timeout_ms": 34000,
            "proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": cc},
            "prem_proxy_options": {"ip_filter": "quality-security", "isp": isp},
            "extract": {"type": "css", "fields": {"links": {"selector": "a[href*='/url?q=']", "attr": "href", "all": True}}}}
    t0 = time.time()
    try:
        jid = post("/scrape/page", body)["job_id"]
        while time.time() - t0 < 200:
            st = get(f"/scrape/{jid}")
            if st.get("status") in ("done", "failed", "error", "cancelled"): break
            time.sleep(3)
        first = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; meta = first.get("meta") or {}; data = first.get("data") or {}
        fu = urllib.parse.urlsplit(meta.get("final_url") or "")
        print(f"{cc}/{isp:<14} http={meta.get('status_code')} fetch_ok={meta.get('fetch_ok')} retries={meta.get('retries')} took={first.get('took_ms')}ms "
              f"final={fu.netloc}{fu.path[:14]} tz={meta.get('applied_timezone')} links={len(data.get('links') or [])}")
        print(f"   warnings={[w[:80] for w in (first.get('warnings') or [])][:3]}")
    except Exception as e:
        print(f"{cc}/{isp}: {type(e).__name__}: {str(e)[:160]}")
