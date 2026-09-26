"""How much can the direct egress spend before Google refuses it?
Distinct queries (no per-query cache effect), ~8 s apart, sequential, and it
STOPS on three consecutive blocks rather than scorching the host's address."""
import json, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
def post(p, b):
    r = urllib.request.Request(BASE+p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as x: return json.load(x)
def get(p):
    with urllib.request.urlopen(BASE+p, timeout=60) as x: return json.load(x)
EX = {"type": "css", "fields": {"h3": {"selector": "h3", "all": True}, "goto": {"selector": "a[href*='/goto?url=']", "attr": "href", "all": True}}}
QUERIES = ["best laptop 2026", "ozon.ru отзывы", "python asyncio tutorial", "wildberries доставка",
           "mechanical keyboard review", "купить холодильник", "kubernetes ingress tls", "lufthansa baggage rules",
           "yerevan weather november", "rust vs go performance", "лучшие наушники 2026", "postgres vacuum tuning",
           "berlin apartment rent", "nvidia rtx 5090 price", "как выбрать матрас", "typescript generics guide",
           "coffee grinder burr", "docker compose healthcheck", "японская кухня рецепты", "sqlite wal mode"]
seq, streak, t_start = [], 0, time.time()
for i, q in enumerate(QUERIES):
    body = {"url": "https://www.google.com/search?q=" + urllib.parse.quote_plus(q) + "&hl=en",
            "browser_engine": "camoufox", "proxy_type": "none", "block_assets": False, "wait_until": "load",
            "warmup": {"type": "homepage"}, "timeout_ms": 34000, "max_retries": 1, "extract": EX}
    t0 = time.time()
    try:
        jid = post("/scrape/page", body)["job_id"]
        while time.time() - t0 < 200:
            st = get(f"/scrape/{jid}")
            if st.get("status") in ("done", "failed", "error", "cancelled"): break
            time.sleep(2)
        f = (get(f"/scrape/{jid}/results").get("results") or [{}])[0]; m = f.get("meta") or {}; d = f.get("data") or {}
        ok = urllib.parse.urlsplit(m.get("final_url") or "").path.startswith("/search") and bool(d.get("h3"))
    except Exception as e:
        ok = False; print(f"  #{i:02d} ERR {type(e).__name__}", flush=True)
    seq.append(ok); streak = 0 if ok else streak + 1
    print(f"  #{i:02d} {'PASS' if ok else 'block'}  t+{int(time.time()-t_start):>3}s  q={q[:26]!r}", flush=True)
    if streak >= 3:
        print(f"  -> three consecutive blocks, stopping at request {i+1}", flush=True); break
    time.sleep(8)
n = len(seq); p = sum(seq)
print(f"TOTAL: {p}/{n} passes over {int(time.time()-t_start)}s; sequence={''.join('P' if x else 'b' for x in seq)}")
