"""Hypothesis: Google refuses when the requested ccTLD/locale contradicts the
exit's geography. Test coherent vs incoherent pairs, both egress classes."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EX = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True}, "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True}}}
def run(label, url, cc=None):
    body = {"url": url, "browser_engine": "camoufox", "block_assets": False, "wait_until": "load",
            "warmup": {"type": "homepage"}, "timeout_ms": 34000, "max_retries": 1, "extract": EX}
    if cc: body |= {"proxy_type": "prem_res_rotating", "proxy_geo": {"country_code": cc}, "prem_proxy_options": {"ip_filter": "quality-security"}}
    else: body |= {"proxy_type": "none"}
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
        print(f"{label:<46} {ok} http={m.get('status_code')} h3={len(d.get('h3') or [])} goto={len(d.get('goto') or [])} tz={m.get('applied_timezone')}", flush=True)
    except Exception as e:
        print(f"{label:<46} ERR {type(e).__name__}: {str(e)[:50]}", flush=True)
    time.sleep(6)
q = "best+laptop+2026"
print("--- proxied, COHERENT pairs (exit country == ccTLD/gl)")
run("DE exit -> google.de gl=de hl=de", f"https://www.google.de/search?q={q}&gl=de&hl=de", "DE")
run("GB exit -> google.co.uk gl=uk hl=en", f"https://www.google.co.uk/search?q={q}&gl=uk&hl=en", "GB")
run("US exit -> google.com gl=us hl=en", f"https://www.google.com/search?q={q}&gl=us&hl=en", "US")
print("--- proxied, INCOHERENT control")
run("DE exit -> google.com hl=en (no gl)", f"https://www.google.com/search?q={q}&hl=en", "DE")
print("--- direct (k12 egress is Armenian, AS44395 Ucom)")
run("direct -> google.am hl=hy (coherent?)", f"https://www.google.am/search?q={q}&hl=hy")
run("direct -> google.com no gl/hl", f"https://www.google.com/search?q={q}")
run("direct -> google.com gl=us hl=en", f"https://www.google.com/search?q={q}&gl=us&hl=en")
