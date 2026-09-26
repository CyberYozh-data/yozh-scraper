"""Google surfaces I never tried: are any of them server-rendered and open?"""
import os, json, re, urllib.request, urllib.parse
from curl_cffi import requests
BASE = "http://localhost:18000/api/v1"; tok = os.environ["SERVICE_TOKEN"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
def resolve(cc):
    req = urllib.request.Request(f"{BASE}/proxies/resolve?proxy_type=prem_res_rotating&country_code={cc}&ip_filter=quality-security", headers={"X-Service-Token": tok})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)["proxy_url"]
q = urllib.parse.quote_plus("best laptop 2026")
SURFACES = [
    ("news RSS",        f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"),
    ("news RSS de",     f"https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de"),
    ("suggest JSON",    f"https://www.google.com/complete/search?client=chrome&q={q}"),
    ("suggest firefox", f"https://suggestqueries.google.com/complete/search?client=firefox&q={q}"),
    ("books API",       f"https://www.googleapis.com/books/v1/volumes?q={q}"),
    ("scholar",         f"https://scholar.google.com/scholar?q={q}"),
    ("shopping tbm",    f"https://www.google.com/search?q={q}&tbm=shop&gl=us&hl=en"),
    ("news tbm rss",    f"https://www.google.com/search?q={q}&tbm=nws&output=rss&gl=us&hl=en"),
]
proxy = resolve("DE")
for label, url in SURFACES:
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en", "Cookie": "CONSENT=YES+"},
                         proxies={"https": proxy, "http": proxy}, impersonate="chrome150", allow_redirects=False, timeout=40)
        b = r.text; loc = r.headers.get("location", "")
        items = b.count("<item>") + b.count('"title"') if ("<item>" in b or '"title"' in b) else 0
        links = len(re.findall(r'<link>(https?://[^<]+)</link>', b)) or len(re.findall(r'"link"\s*:\s*"https?://', b))
        hosts = sorted({urllib.parse.urlsplit(u).netloc for u in re.findall(r'<link>(https?://[^<]+)</link>', b)})[:4]
        print(f"{label:<16} http={r.status_code} size={len(b):>7} redirect={'/sorry' if '/sorry' in loc else (loc[:22] or '-'):<22} items~{items:<4} links={links:<4} hosts={hosts}", flush=True)
    except Exception as e:
        print(f"{label:<16} {type(e).__name__}: {str(e)[:70]}", flush=True)
