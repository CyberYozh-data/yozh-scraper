"""The exact configuration #136 ships, end to end through /scrape/preset: the
camoufox twins on the direct path with the market-country pin (proxy_country
no longer applies there). us x2 per twin, plus de and ru on search."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=90) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
def wait(jid, limit=240):
    t0 = time.time()
    while time.time() - t0 < limit:
        if get(f"/scrape/{jid}").get("status") in ("done", "failed", "error", "cancelled"): break
        time.sleep(3)
    return (get(f"/scrape/{jid}/results").get("results") or [{}])[0]
runs = [("google_search_camoufox", "best laptop 2026", "us"), ("google_search_camoufox", "python asyncio tutorial", "us"),
        ("google_shopping_camoufox", "wireless mouse", "us"), ("google_shopping_camoufox", "mechanical keyboard", "us"),
        ("google_search_camoufox", "wärmepumpe kosten", "de"), ("google_search_camoufox", "купить ноутбук", "ru")]
out = []
for source, q, loc in runs:
    f = wait(post("/scrape/preset/page", {"source": source, "preset_params": {"query": q}, "locale": loc})["job_id"])
    m = f.get("meta") or {}; d = f.get("data") or {}
    titles = [t for t in (d.get("titles") or []) if t]; links = [l for l in (d.get("links") or []) if l]
    ok = bool(titles) and not m.get("blocked")
    out.append({"preset": source, "locale": loc, "query": q, "pass": ok, "http": m.get("status_code"), "titles": len(titles),
                "http_links": sum(1 for l in links if str(l).startswith("http")), "applied_locale": m.get("applied_locale"),
                "applied_timezone": m.get("applied_timezone"), "proxy_type": m.get("proxy_type"), "took_ms": f.get("took_ms")})
    print(json.dumps(out[-1], ensure_ascii=False), flush=True)
    time.sleep(12)
print("TOTAL", sum(o["pass"] for o in out), "/", len(out))
json.dump(out, open("/tmp/user/1000/claude-1000/-home-nick-dev-open-scraper/2fcc3486-509a-436f-a3fc-fec18a1b6cad/scratchpad/confirm_136.json", "w"), ensure_ascii=False, indent=1)
