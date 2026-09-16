"""Custom-URL Google experiments through the real pipeline (Chrome, prem pool).

Records per run: final_url, http, block?, and the SHAPE of the result links —
how many /goto wrappers vs direct hrefs sit under #rso h3s, how many <cite>
display URLs, how many translate leaks. One row per run, JSON lines to OUT.
"""
import json, os, re, sys, time, urllib.request
BASE = "http://localhost:18000/api/v1"
OUT = sys.argv[1]
SPECS = json.loads(open(sys.argv[2]).read())

def post(p, body):
    r = urllib.request.Request(BASE+p, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"}, method="POST")
    return json.load(urllib.request.urlopen(r, timeout=60))
def get(p):
    return json.load(urllib.request.urlopen(BASE+p, timeout=60))

BASE_REQ = {"device":"desktop","proxy_type":"prem_res_rotating","warmup":{"type":"homepage"},
            "stealth":False,"wait_until":"load","wait_for_selector":"#rso","timeout_ms":45000,
            "block_assets":True,"browser_engine":"chromium","raw_html":True,"render":True}

def shape(html):
    if not html: return {}
    rso_i = html.find('id="rso"'); body = html[rso_i:] if rso_i>=0 else html
    return {
        "goto_hrefs": len(re.findall(r'href="/goto\?url=', body)),
        "direct_h3_hrefs": len(re.findall(r'<a [^>]*href="https?://(?!www\.google\.)[^"]+"[^>]*>\s*<h3', body)),
        "any_h3": len(re.findall(r'<h3', body)),
        "cites": len(re.findall(r'<cite', body)),
        "translate_leaks": body.count("translate.google.com/translate?u="),
        "bytes": len(html),
    }

with open(OUT, "a") as out:
    for spec in SPECS:
        for i in range(spec.get("runs", 1)):
            req = dict(BASE_REQ); req.update(spec["req"])
            rec = {"label": spec["label"], "run": i, "url": req["url"]}
            try:
                job = post("/scrape/page", req)["job_id"]
                dl = time.time()+300; st=None
                while time.time()<dl:
                    st = get(f"/scrape/{job}").get("status")
                    if st in ("done","failed","error","cancelled"): break
                    time.sleep(3)
                res = get(f"/scrape/{job}/results"); first = (res.get("results") or [{}])[0]
                m = first.get("meta") or {}
                rec.update(status=st, http=m.get("status_code"), final_url=(m.get("final_url") or "")[:110],
                           blocked=("/sorry" in (m.get("final_url") or "")), took_ms=first.get("took_ms"),
                           warnings=[str(w)[:70] for w in (first.get("warnings") or [])][:3])
                rec.update(shape(first.get("raw_html") or first.get("html") or ""))
                # keep one /goto token per successful run for a resolution test later
                html = first.get("raw_html") or ""
                tok = re.search(r'href="(/goto\?url=[^"]+)"', html)
                rec["sample_goto"] = ("https://www.google.com"+tok.group(1)) if tok else None
            except Exception as e:
                rec.update(status="exception", error=f"{type(e).__name__}: {e}"[:120])
            out.write(json.dumps(rec, ensure_ascii=False)+"\n"); out.flush()
            print(json.dumps({k:v for k,v in rec.items() if k not in ("sample_goto","url")}, ensure_ascii=False), flush=True)
