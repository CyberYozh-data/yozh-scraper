"""Is any server-rendered Google results page left? One exit, several endpoint
shapes, counted by what the HTML itself carries."""
import os, json, re, urllib.request, urllib.parse
from curl_cffi import requests
BASE = "http://localhost:18000/api/v1"; tok = os.environ["SERVICE_TOKEN"]
UA_C = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
UA_N = "Nokia7610/2.0 (5.0509.0) SymbianOS/7.0s Series60/2.1 Profile/MIDP-2.0 Configuration/CLDC-1.0"
def resolve(cc):
    req = urllib.request.Request(f"{BASE}/proxies/resolve?proxy_type=prem_res_rotating&country_code={cc}&ip_filter=quality-security", headers={"X-Service-Token": tok})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)["proxy_url"]
q = urllib.parse.quote_plus("best laptop 2026")
VARIANTS = [
    ("plain",        f"https://www.google.de/search?q={q}&gl=de&hl=de", UA_C),
    ("gbv=1",        f"https://www.google.de/search?q={q}&gl=de&hl=de&gbv=1", UA_C),
    ("udm=14",       f"https://www.google.de/search?q={q}&gl=de&hl=de&udm=14", UA_C),
    ("num=20&nfpr",  f"https://www.google.de/search?q={q}&gl=de&hl=de&num=20&nfpr=1", UA_C),
    ("gbv=1+nokia",  f"https://www.google.de/search?q={q}&gl=de&hl=de&gbv=1", UA_N),
    ("m/search",     f"https://www.google.de/m/search?q={q}&gl=de&hl=de", UA_C),
    ("xhtml",        f"https://www.google.de/xhtml?q={q}&gl=de&hl=de", UA_N),
]
proxy = resolve("DE")
for label, url, ua in VARIANTS:
    try:
        r = requests.get(url, headers={"User-Agent": ua, "Accept-Language": "de", "Cookie": "CONSENT=YES+"},
                         proxies={"https": proxy, "http": proxy}, impersonate="chrome150", allow_redirects=False, timeout=40)
        b = r.text; loc = r.headers.get("location", "")
        h3 = len(re.findall(r"<h3", b)); urlq = b.count('href="/url?'); goto = b.count("/goto?url=")
        ext = sorted({urllib.parse.urlsplit(u).netloc for u in re.findall(r'href="(https?://[^"]+)"', b) if "google." not in u and "gstatic" not in u})
        print(f"{label:<14} http={r.status_code} size={len(b):>6} redirect={'/sorry' if '/sorry' in loc else (loc[:26] or '-'):<26} h3={h3:<3} url?q={urlq:<3} goto={goto:<3} ext={ext[:4]}", flush=True)
    except Exception as e:
        print(f"{label:<14} {type(e).__name__}: {str(e)[:70]}", flush=True)
