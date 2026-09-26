"""Carrier-ISP exits x Nokia UA x TLS impersonation. Prints the exit's ASN org
(never its address) so a negative is interpretable."""
import os, json, time, urllib.request, urllib.parse
from curl_cffi import requests
BASE = "http://localhost:18000/api/v1"; tok = os.environ["SERVICE_TOKEN"]
UA = "Nokia7610/2.0 (5.0509.0) SymbianOS/7.0s Series60/2.1 Profile/MIDP-2.0 Configuration/CLDC-1.0"
def resolve(cc, isp):
    q = urllib.parse.urlencode({"proxy_type": "prem_res_rotating", "country_code": cc, "isp": isp, "ip_filter": "quality-security"})
    req = urllib.request.Request(f"{BASE}/proxies/resolve?{q}", headers={"X-Service-Token": tok})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)["proxy_url"]
def wml(hl, cc):
    return "https://www.google.com/wml/search?" + urllib.parse.urlencode({"q": "best laptop 2026", "sca_esv": "1", "hl": hl, "lr": f"lang_{hl}", "cr": f"country{cc}", "ie": "utf8", "oe": "utf8"})
def org(proxy):
    try:
        r = requests.get("https://ipinfo.io/json", proxies={"https": proxy, "http": proxy}, impersonate="chrome150", timeout=30)
        d = r.json(); return f"{d.get('org','?')} / {d.get('city','?')}"
    except Exception as e: return f"lookup failed: {type(e).__name__}"
def hit(label, u, proxy, imp):
    try:
        r = requests.get(u, headers={"User-Agent": UA, "Accept": "*/*", "Cookie": "CONSENT=YES+", "Accept-Language": "en"},
                         proxies={"https": proxy, "http": proxy}, impersonate=imp, allow_redirects=False, timeout=40)
        loc = r.headers.get("location", ""); body = r.text
        print(f"   {label:<24} http={r.status_code} size={len(body)} links={body.count('/url?q=')} redirect={'/sorry' if '/sorry' in loc else (loc[:30] or '-')}", flush=True)
    except Exception as e:
        print(f"   {label:<24} {type(e).__name__}: {str(e)[:80]}", flush=True)
for cc, hl, isp in (("GB", "en", "Three"), ("US", "en", "AT&T Wireless")):
    for i in range(2):
        proxy = resolve(cc, isp)
        print(f"{cc}/{isp} exit #{i}: org={org(proxy)}", flush=True)
        hit("plain-tls nokia", wml(hl, cc), proxy, None); time.sleep(3)
        hit("chrome99_android nokia", wml(hl, cc), proxy, "chrome99_android"); time.sleep(3)
