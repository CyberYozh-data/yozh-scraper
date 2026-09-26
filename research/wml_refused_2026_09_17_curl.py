"""Control: the same /wml URL, the same exits, bare curl with the Nokia UA the
device sends (the whitelist key) -- no browser of ours at all."""
import json, pathlib, subprocess, time, urllib.request, urllib.parse
BASE = "http://localhost:18000/api/v1"
ENV = pathlib.Path("/home/nick/dev/open-scraper/open-scraper-clone/.env")
tok = next(l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines() if l.startswith("SERVICE_TOKEN="))
UA = "Nokia7610/2.0 (5.0509.0) SymbianOS/7.0s Series60/2.1 Profile/MIDP-2.0 Configuration/CLDC-1.0"
def resolve(cc):
    req = urllib.request.Request(f"{BASE}/proxies/resolve?proxy_type=prem_res_rotating&country_code={cc}", headers={"X-Service-Token": tok})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)["proxy_url"]
for cc, hl in (("DE", "de"), ("GB", "en"), ("US", "en")):
    url = (f"https://www.google.com/wml/search?q=best+laptop+2026&sca_esv=1&hl={hl}&lr=lang_{hl}&cr=country{cc}&ie=utf8&oe=utf8")
    for i in range(2):
        out = subprocess.run(["curl", "-sS", "-m", "40", "-K", "-", "-A", UA, "-H", "Accept: */*", "-H", "Cookie: CONSENT=YES+",
                              "-w", "\n__META__%{http_code} %{size_download} %{redirect_url}", url],
                             input=f'proxy = "{resolve(cc)}"\n', capture_output=True, text=True)
        body, _, meta = out.stdout.rpartition("__META__")
        code, size, redirect = (meta.strip().split(" ", 2) + ["", ""])[:3]
        n = body.count("/url?q=")
        print(f"{cc} #{i}: http={code} size={size} /url?q= links={n} redirect={'/sorry' if '/sorry' in redirect else (redirect[:30] or '-')}"
              + (" | 'Update your browser'" if "Update your browser" in body else ""), flush=True)
        time.sleep(3)
