"""/wml with the Nokia UA over curl_cffi TLS impersonation (SearXNG's recipe
since 2026-09-03: impersonate="chrome99_android"). Direct and via our exits."""
import os, json, time, urllib.request, urllib.parse
from curl_cffi import requests
BASE = "http://localhost:18000/api/v1"; tok = os.environ["SERVICE_TOKEN"]
UA = "Nokia7610/2.0 (5.0509.0) SymbianOS/7.0s Series60/2.1 Profile/MIDP-2.0 Configuration/CLDC-1.0"
def resolve(cc):
    req = urllib.request.Request(f"{BASE}/proxies/resolve?proxy_type=prem_res_rotating&country_code={cc}", headers={"X-Service-Token": tok})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)["proxy_url"]
def url(hl, cc):
    return "https://www.google.com/wml/search?" + urllib.parse.urlencode({"q": "best laptop 2026", "sca_esv": "1", "hl": hl, "lr": f"lang_{hl}", "cr": f"country{cc}", "ie": "utf8", "oe": "utf8"})
def hit(label, u, proxy=None, imp="chrome99_android"):
    try:
        r = requests.get(u, headers={"User-Agent": UA, "Accept": "*/*", "Cookie": "CONSENT=YES+", "Accept-Language": "en"},
                         proxies={"https": proxy, "http": proxy} if proxy else None, impersonate=imp, allow_redirects=False, timeout=40)
        body = r.text; loc = r.headers.get("location", "")
        tail = " | Update your browser" if "Update your browser" in body else ""
        red = "/sorry" if "/sorry" in loc else (loc[:30] or "-")
        print(f"{label:<28} http={r.status_code} size={len(body)} /url?q= links={body.count('/url?q=')} redirect={red}{tail}", flush=True)
    except Exception as e:
        print(f"{label:<28} {type(e).__name__}: {str(e)[:90]}", flush=True)
for i in range(2): hit(f"DIRECT impersonated #{i}", url("en", "US")); time.sleep(3)
hit("DIRECT plain-tls #0", url("en", "US"), imp=None); time.sleep(3)
for cc, hl in (("DE", "de"), ("GB", "en")):
    for i in range(2): hit(f"{cc} via exit impersonated #{i}", url(hl, cc), proxy=resolve(cc)); time.sleep(3)
