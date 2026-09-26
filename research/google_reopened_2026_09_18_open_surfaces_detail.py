"""Are the open surfaces REAL results? Judge by values: titles, destinations."""
import os, json, re, urllib.request, urllib.parse
from curl_cffi import requests
BASE = "http://localhost:18000/api/v1"; tok = os.environ["SERVICE_TOKEN"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
def resolve(cc):
    req = urllib.request.Request(f"{BASE}/proxies/resolve?proxy_type=prem_res_rotating&country_code={cc}&ip_filter=quality-security", headers={"X-Service-Token": tok})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)["proxy_url"]
proxy = resolve("DE")
def get(url, **kw):
    return requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en", "Cookie": "CONSENT=YES+"},
                        proxies={"https": proxy, "http": proxy}, impersonate="chrome150", timeout=40, **kw)
q = urllib.parse.quote_plus("best laptop 2026")
print("=== news RSS: real items?")
r = get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")
items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
print(f"  items={len(items)}")
for it in items[:3]:
    t = re.search(r"<title>(.*?)</title>", it, re.S); src = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
    link = re.search(r"<link>(.*?)</link>", it, re.S)
    print(f"   - {(t.group(1) if t else '?')[:70]} | source={(src.group(1) if src else '?')[:24]} | link={'news.google' if link and 'news.google' in link.group(1) else (link.group(1)[:40] if link else '?')}")
print("=== does a news.google link resolve to the publisher?")
if items:
    link = re.search(r"<link>(.*?)</link>", items[0], re.S).group(1)
    try:
        rr = get(link, allow_redirects=True)
        print(f"   final={urllib.parse.urlsplit(str(rr.url)).netloc} http={rr.status_code} size={len(rr.text)}")
    except Exception as e: print("   resolve failed:", type(e).__name__, str(e)[:60])
print("=== scholar: real results?")
r = get(f"https://scholar.google.com/scholar?q={q}")
gs = len(re.findall(r'class="gs_rt"', r.text)); ext = sorted({urllib.parse.urlsplit(u).netloc for u in re.findall(r'href="(https?://[^"]+)"', r.text) if "google" not in u})[:5]
print(f"  http={r.status_code} size={len(r.text)} gs_rt={gs} ext_hosts={ext}")
print("=== a NON-news query through news RSS (does it still answer?)")
for probe in ("ozon.ru", "site:example.com", "python asyncio tutorial"):
    rr = get(f"https://news.google.com/rss/search?q={urllib.parse.quote_plus(probe)}&hl=en-US&gl=US&ceid=US:en")
    print(f"   {probe!r}: http={rr.status_code} items={rr.text.count('<item>')}")
