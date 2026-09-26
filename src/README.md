# Yozh Scraper — Service Docs

Web-scraping API built on Playwright. Renders any URL in a real browser and
returns structured data (CSS/XPath extraction), raw HTML, and/or a full-page
screenshot. Async job queue for single-page and batch scrapes, device
emulation, rich proxy integration via CyberYozh.

**Base URL:** `http://localhost:8000`
**OpenAPI docs:** `http://localhost:8000/docs`
**MCP endpoint:** `http://localhost:8000/mcp`

For the one-command start-up see the [root README](../README.md).

## Proxy Support

**For reliable web scraping, using proxies is essential.** Most modern websites
(especially search engines, e-commerce, social media) have anti-bot protection
that blocks direct scraping attempts. Proxies help you:

- Avoid IP bans and CAPTCHAs
- Bypass geo-restrictions
- Scale scraping operations
- Appear as real users from different locations

### CyberYozh Proxy Integration

This scraper integrates with **CyberYozh Proxy Service** which provides
residential, mobile (LTE), and datacenter proxies.

- **Get your API key:** https://app.cyberyozh.com/api-access/
- **Proxy documentation:** https://apidoc.cyberyozh.com/

Set `CYBERYOZH_API_KEY` in `.env` to enable proxy support.

### Available proxy types

- `res_rotating` — Residential rotating (recommended for most scraping)
- `res_static` — Residential static (dedicated)
- `mobile` — Mobile / LTE, dedicated
- `mobile_shared` — Mobile / LTE, shared
- `dc_static` — Datacenter static
- `none` — No proxy (direct connection)

### Proxy discovery endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/proxies/available?proxy_type=...` | Lists the current user's purchased proxies of a given type (filtered by `access_type` for shared vs dedicated) |
| `GET /api/v1/proxies/countries` | Lists the ~250 countries supported by CyberYozh's rotating residential proxies (mirrors the CyberYozh static list) |

### Proxy type → CyberYozh mapping

`proxy_type` is mapped to the CyberYozh `category` used against
`GET /api/v1/proxies/history/` and filtered by `access_type`:

| `proxy_type`     | CyberYozh category     | `access_type` filter |
|------------------|------------------------|----------------------|
| `res_rotating`   | `residential_rotating` | — (single tier)      |
| `res_static`     | `residential_static`   | `private`            |
| `mobile`         | `lte`                  | `private`            |
| `mobile_shared`  | `lte`                  | `shared`             |
| `dc_static`      | `datacenter`           | `private`            |

`res_rotating` additionally calls `POST /api/v1/proxies/rotating-credentials/`
to obtain per-session credentials (optionally with `proxy_geo`).
The mapping lives in [`proxy/cyberyozh/provider.py`](proxy/cyberyozh/provider.py).

### Retries on failure

When a fetch fails in a way that looks like a proxy issue or a captcha/block,
the request is retried with a freshly rotated proxy. The number of attempts is
the **`MAX_RETRIES`** env var (default `3`). Direct (`proxy_type: none`)
requests never retry — they always make a single attempt.

A request (or a preset's `request_defaults`) may override the server default per
call with **`max_retries`** (`1`–`10`; `1` = no retry). It is ignored for
`proxy_type: none`. The `retries` field in each result's `meta` reports how many
retries actually happened.

## API

### Scrape (async jobs)

Every scrape endpoint creates a background job and returns `job_id`. Poll job
status, then fetch results.

Routes:
* `POST   /api/v1/scrape/page`
* `POST   /api/v1/scrape/pages`
* `GET    /api/v1/scrape/{job_id}`
* `GET    /api/v1/scrape/{job_id}/results`
* `DELETE /api/v1/scrape/{job_id}`  — soft-cancel. In-flight pages finish;
  remaining pages are skipped with `warnings: ["cancelled"]`; job transitions
  to `status="cancelled"`.

---

### 1) Scrape single page (no proxy)

Create job:

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none"
  }'
```

Example response:

```json
{"job_id":"req_..."}
```

Check status:

```bash
curl -s http://localhost:8000/api/v1/scrape/req_...
```

Fetch results (available for any job; while queued or running, returns a 200 partial snapshot where unfinished slots are `null`, index-aligned with `pages`; fully populated for `done`, `failed`, and `cancelled` jobs):

```bash
curl -s http://localhost:8000/api/v1/scrape/req_.../results
```

Example response:

```json
{
  "job_id": "req_...",
  "status": "done",
  "total": 1,
  "done": 1,
  "pages": [{"url": "https://example.com", "proxy_type": "none", "...": "..."}],
  "error": null,
  "results": [
    {
      "request_id": "req_...",
      "took_ms": 1234,
      "meta": {
        "url": "https://example.com",
        "final_url": "https://example.com/",
        "status_code": 200,
        "device": "desktop",
        "proxy_type": "none",
        "retries": 0
      },
      "data": null,
      "raw_html": null,
      "screenshot_base64": null,
      "warnings": []
    }
  ]
}
```

For a failed job `results` is `null` and `error` contains the reason.

---

### 2) One-liner: create job → wait → print results (requires jq)

```bash
JOB_ID=$(
  curl -s -X POST http://localhost:8000/api/v1/scrape/page \
    -H "Content-Type: application/json" \
    -d '{"url":"https://example.com","proxy_type":"none"}' \
  | jq -r .job_id
)

echo "job_id=$JOB_ID"

while true; do
  STATUS=$(curl -s "http://localhost:8000/api/v1/scrape/$JOB_ID" | jq -r .status)
  echo "status=$STATUS"
  if [ "$STATUS" = "done" ]; then
    curl -s "http://localhost:8000/api/v1/scrape/$JOB_ID/results" | jq .
    break
  fi
  if [ "$STATUS" = "failed" ] || [ "$STATUS" = "cancelled" ]; then
    curl -s "http://localhost:8000/api/v1/scrape/$JOB_ID" | jq .
    exit 1
  fi
  sleep 0.5
done
```

---

### 3) Batch scrape (multiple pages)

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/pages \
  -H "Content-Type: application/json" \
  -d '{
    "pages": [
      {"url":"https://example.com","proxy_type":"none"},
      {"url":"https://example.org","proxy_type":"none"}
    ]
  }'
```

Poll status + fetch results via the same `GET /api/v1/scrape/{job_id}` and `/results`.

---

### 4) Extract data with CSS

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "extract": {
      "type": "css",
      "fields": {
        "title": {"selector": "h1", "attr": "text", "required": true}
      }
    }
  }'
```

Result payload contains:

```json
{ "data": { "title": "Example Domain" } }
```

---

### 5) Extract data with XPath

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "extract": {
      "type": "xpath",
      "fields": {
        "title": {"selector": "//h1", "attr": "text", "required": true}
      }
    }
  }'
```

---

### 6) Return raw HTML

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "raw_html": true
  }'
```

---

### 6b) LLM-ready Markdown (`formats`)

Request output formats with `formats: []` (Firecrawl-style). It's **additive
and backward compatible** — omit it and the legacy `raw_html` / `screenshot`
booleans behave exactly as before; when set, the effective outputs are the
union of the list and those booleans.

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "formats": ["markdown", "fit_markdown", "links"],
    "markdown_options": {"content_filter": "pruning", "citations": true}
  }'
```

Formats: `markdown` (clean LLM-ready Markdown), `fit_markdown` (noise-filtered —
`pruning` heuristic or `llm`), `raw_html`, `html` (cleaned), `links`,
`screenshot`. The response gains the matching fields: `markdown`,
`fit_markdown`, `markdown_references` (for `⟨n⟩` citations), `links`, `html`.

`markdown_options`: `only_main_content` (default **off** — keeps header/footer/
cookie/nav, which compliance consumers need), `content_filter`
(`none`/`pruning`/`llm`), `filter_instruction` + `filter_model` (for `llm`),
`citations`, `ignore_links`, `ignore_images`, `body_width`. Markdown is
best-effort: a filter failure degrades to a `warning`, never fails the scrape.

---

### 7) Screenshot (base64)

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "screenshot": true
  }'
```

**Element screenshot.** Add `element_selector` to crop the screenshot to a
single element (plus 24px padding) instead of the full page. Requires
`screenshot: true` (ignored otherwise). On any failure — no match, invalid
CSS, zero-size element, or a scroll/visibility timeout — the response falls
back to a full-page screenshot and reports why in `element_screenshot_status`.

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "screenshot": true,
    "element_selector": "#main .alert"
  }'
```

The response carries a diagnostic `element_screenshot_status`:

| `element_screenshot_status` | Meaning |
|------------------------------|---------|
| `element`            | `screenshot_base64` is the cropped element                                   |
| `fallback_not_found` | selector matched nothing → full-page screenshot                             |
| `fallback_invalid`   | selector was invalid CSS → full-page screenshot                            |
| `fallback_zero_size` | element had zero rendered size → full-page screenshot                       |
| `fallback_timeout`   | element could not be scrolled into view in time → full-page screenshot      |
| `not_requested`      | no `element_selector` given → normal full-page screenshot                   |
| `no_screenshot`      | `screenshot: false`, or capture failed entirely (`screenshot_base64` is `null`) |

---

### 8) Device emulation (mobile)

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "device": "mobile",
    "extract": {
      "type": "css",
      "fields": {"title": {"selector": "h1", "attr": "text"}}
    }
  }'
```

---

### 9) Custom headers

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://httpbin.org/headers",
    "proxy_type": "none",
    "headers": {
      "Accept-Language": "en-US,en;q=0.9",
      "X-Request-Id": "demo-42"
    },
    "raw_html": true
  }'
```

> `User-Agent` is the one header that is **not** forwarded. Every engine states
> the only UA its transport can back -- a header claiming otherwise contradicts
> the TLS handshake and, on Chromium, the Client Hints, which is a bot tell
> rather than a disguise. A `User-Agent` you send is dropped, `warnings` carries
> `ignored_request_field: headers['User-Agent']`, and `meta.applied_user_agent`
> reports what actually went out. The one identity the caller may choose is
> `device` -- including `legacy_wap`, whose Symbian UA no engine can back either,
> offered because the endpoint it exists for keys on the string and not on the
> handshake.

---

### 10) Cookies

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "cookies": [
      {"name":"session","value":"abc123","domain":"example.com","path":"/"}
    ],
    "raw_html": true
  }'
```

---

### 11) Wait strategy / selector / timeout

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "proxy_type": "none",
    "wait_until": "networkidle",
    "wait_for_selector": "h1",
    "timeout_ms": 45000
  }'
```

---

### 12) Proxies (CyberYozh)

Set `CYBERYOZH_API_KEY` in `.env`, then:

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://httpbin.org/ip",
    "proxy_type": "res_static"
  }'
```

---

### 13) Proxies (CyberYozh) with GEO targeting

`proxy_geo` accepts:

| Field          | Type   | Description                        |
|----------------|--------|------------------------------------|
| `country_code` | string | ISO 3166-1 alpha-2 (e.g. `"US"`, `"GB"`) |
| `region`       | string | Region / state name                |
| `city`         | string | City name (e.g. `"London"`)        |

GEO targeting at the **proxy** level is supported only for `res_rotating`. For
`res_static`, `mobile`, `mobile_shared` and `dc_static` the proxy already has
a fixed location; `proxy_geo` in those requests is accepted but does not
change the exit IP (a warning is logged).

Regardless of the proxy type, when `proxy_geo.country_code` is provided the
browser context is automatically aligned with that country — `locale`,
`timezone_id` and `Accept-Language` are set accordingly so the fingerprint
matches the IP. For US, CA, RU, AU and BR a `city` hint further refines the
timezone.

#### Country only

```bash
curl -s -X POST http://localhost:8000/api/v1/scrape/page \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://httpbin.org/ip",
    "proxy_type": "res_rotating",
    "proxy_geo": {"country_code": "US"}
  }'
```

#### 14) Proxies (CyberYozh) with country and city + verification

Request through a London residential proxy and verify the exit IP is actually
in London:

```bash
# Step 1 – create job
JOB_ID=$(
  curl -s -X POST http://localhost:8000/api/v1/scrape/page \
    -H "Content-Type: application/json" \
    -d '{
      "url": "https://httpbin.org/ip",
      "proxy_type": "res_rotating",
      "proxy_geo": {"country_code": "GB", "city": "London"},
      "raw_html": true
    }' \
  | jq -r .job_id
)

# Step 2 – wait for completion
while true; do
  STATUS=$(curl -s "http://localhost:8000/api/v1/scrape/$JOB_ID" | jq -r .status)
  [ "$STATUS" = "done" ] && break
  [ "$STATUS" = "failed" ] && echo "failed" && exit 1
  sleep 0.5
done

# Step 3 – extract IP from result
IP=$(
  curl -s "http://localhost:8000/api/v1/scrape/$JOB_ID/results" \
  | jq -r '.results[0].raw_html' \
  | python3 -c "
import sys, re, json
html = sys.stdin.read()
m = re.search(r'<pre[^>]*>(.*?)</pre>', html, re.DOTALL)
data = json.loads(m.group(1) if m else html)
print(data['origin'].split(',')[0].strip())
"
)
echo "Proxy IP: $IP"

# Step 4 – verify geolocation via ipapi.co
curl -s "https://ipapi.co/$IP/json/" | jq '{ip, city, country_code}'
```

Expected output:

```json
{"ip": "185.x.x.x", "city": "London", "country_code": "GB"}
```

For a ready-to-run Python script that tests multiple cities see
[`../examples/geo_scraping.py`](../examples/geo_scraping.py):

```bash
cd ../examples
python geo_scraping.py
```

## Request Schema

### POST /api/v1/scrape/page

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | required | Target URL |
| `render` | boolean | `true` | Run full JS rendering in browser |
| `wait_until` | `domcontentloaded` \| `load` \| `networkidle` | `domcontentloaded` | When to consider page loaded |
| `wait_for_selector` | string | — | Wait for CSS selector before extracting |
| `timeout_ms` | integer | — | Per-page timeout (ms), overrides global |
| `device` | `desktop` \| `mobile` \| `legacy_wap` | `desktop` | Device emulation. `legacy_wap` claims a 2004 feature phone and suppresses Client Hints, for sites that serve a JavaScript-free layout to legacy User-Agents -- Google's `/wml/search` answers it with real `/url?q=` links instead of `/goto?url=` stubs. **Requires `render: false`** (422 otherwise: rendering runs the JavaScript the identity claims not to have), rejected with 422 on `camoufox`, which owns its own fingerprint, and cannot be pinned to a session, whose login runs JS |
| `browser_engine` | `chromium` \| `firefox` \| `webkit` \| `camoufox` | `chromium` | Rendering engine. `chromium` drives a real Google Chrome when `CHROME_CHANNEL=chrome`; `camoufox` is anti-detect Firefox for hostile anti-bot targets. Each states its own User-Agent -- `firefox` and `webkit` never claim Chrome |
| `viewport` | object | `1920×1080` (desktop) | `{ width, height }` in CSS px; `window.screen` is set to match. `device` picks the default: mobile its preset's size, `legacy_wap` a 176×208 feature-phone screen. Sessions pin it across login + scrapes |
| `headers` | object | — | Custom HTTP headers |
| `cookies` | array | — | Cookies to inject |
| `proxy_type` | string | `none` | See proxy types above |
| `proxy_pool_id` | string | — | Pin request to a specific purchased proxy id (required when `proxy_type != none` from the tester UI) |
| `proxy_geo` | object | — | `{ country_code, region, city }` — targets the exit IP for `res_rotating`; also drives browser locale/timezone alignment for every type |
| `block_assets` | boolean | env | Block images/fonts/media to speed up load; falls back to the `BLOCK_ASSETS` env var when unset |
| `raw_html` | boolean | `false` | Include raw HTML in response |
| `screenshot` | boolean | `false` | Include full-page screenshot as base64 (triggers a scroll pass for lazy images when assets are not blocked) |
| `stealth` | boolean | `true` | Apply anti-detection patches (`navigator.webdriver`, WebGL vendor/renderer). The UA, platform and other navigator getters are aligned natively (CDP / real engine) so their `.toString()` stays native — not JS-patched |
| `extract` | object | — | Structured extraction rules |

### ExtractRule

```json
{
  "type": "css",
  "fields": {
    "title": {
      "selector": "h1",
      "attr": "text",
      "all": false,
      "required": false
    }
  }
}
```

| Field | Values | Description |
|-------|--------|-------------|
| `type` | `css` \| `xpath` | Selector type |
| `container` | string | **Rule-level.** Selector for the ROW every field belongs to — see below |
| `selector` | string | CSS selector or XPath expression |
| `attr` | `text` (default) \| `html` \| attribute name | What to extract |
| `all` | boolean | Return all matches as array instead of first match |
| `required` | boolean | Log warning if field not found |

#### `container` — one value per row

Set `container` on the rule and every field's `selector` is evaluated relative
to each matching row, contributing exactly one entry per row — `null` where
that row has no match, never a shorter list. All columns are therefore the
same length, so `titles[i]`, `links[i]` and `snippets[i]` describe the same
result.

```json
{
  "type": "css",
  "container": "#rso div.tF2Cxc",
  "fields": {
    "titles":   {"selector": "h3", "all": true},
    "links":    {"selector": "a:has(h3)", "attr": "href", "all": true},
    "snippets": {"selector": "div.VwiC3b", "all": true}
  }
}
```

Without it, a field matching more nodes in one card than its neighbours do
shifts every later row: one Google result with sitelinks carries three `h3`,
so `titles` and `links` return 4 entries while `snippets` returns 2.

Rules:

- Every field must set `all: true` — a container makes each field a column.
- A `selector` must not be able to leave its row. XPath is checked per step:
  the first step opens with `.`, `./`, `.//`, `self::`, `child::`,
  `descendant::`, `descendant-or-self::` or `attribute::`, no later step uses
  `..` or a climbing axis, and `|` unions are refused (predicates and string
  literals are not read as steps, so `.//a[descendant::h3]` is fine). Use
  `.//` for a descendant at any depth — `./` and a bare step name are the
  CHILD axis and match nothing on nested markup. `.` (or `self::*`) addresses
  the row itself, which is how a field captures the whole card.
- **CSS is checked the same way**, on the XPath it compiles to. Descendant and
  child combinators compile to `descendant-or-self::` and are fine; the
  sibling combinators do not — `.row ~ .row h3` compiles through
  `following-sibling::` and, measured, returns the NEXT row's value from
  inside the first one. Both are refused.
- Regardless of the selector, a match that did not come from the row is
  discarded at extraction time and the field is named in `warnings`. The
  validator reads syntax; this reads the node.

### Response

Each result in `results[]` contains:

| Field | Type | Description |
|-------|------|-------------|
| `request_id` | string | Job ID |
| `took_ms` | integer | Render time in milliseconds |
| `meta.url` | string | Original URL |
| `meta.final_url` | string | Final URL after redirects |
| `meta.status_code` | integer | HTTP status code |
| `meta.device` | string | Device used |
| `meta.proxy_type` | string | Proxy used |
| `meta.proxy_pool_id` | string | Specific proxy id used (if any) |
| `meta.retries` | integer | Number of retries |
| `meta.applied_user_agent` | string | UA that was actually sent |
| `meta.applied_locale` | string | Browser locale (`es-ES`, `en-US`, …) |
| `meta.applied_timezone` | string | Browser timezone id |
| `meta.applied_accept_language` | string | Effective `Accept-Language` |
| `meta.applied_preset` | object | Preset runs only: `name`, `source`, `locale`, `version` and, for search presets, the `query` the URL was rendered with |
| `data` | object | Extracted fields (if `extract` was set) |
| `raw_html` | string | Raw HTML (if `raw_html: true`) |
| `screenshot_base64` | string | Base64 PNG (if `screenshot: true`) |
| `warnings` | array | Non-fatal warnings (`["cancelled"]` for pages skipped by a cancel) |

## Presets

A **preset** is a named bundle of three things so you don't hand-assemble a
`ScrapeRequest` per target site: a request profile (proxy/device/wait), a
per-locale URL template, and a parsing recipe (deterministic CSS/XPath with
optional LLM self-heal / AI extraction). Built-ins ship read-only;
user-defined presets persist under `data/presets/` (mount the volume — see
`docker-compose.yml`).

That directory also holds one `*.lock` file per user preset. **They are
permanent by design — do not clean them up.** Writers (the API and every
worker container) take them to make the compare-and-replace of a preset one
step; deleting one mid-write reintroduces the race where a self-heal
overwrites an edit its owner just saved. They are empty, one per preset, and
never grow.

### Built-in presets

Every built-in ships twice, once per browser engine — `<name>_chromium` and
`<name>_camoufox` — so `GET /api/v1/presets` returns twenty-eight names, never
the bare `<name>`:

| name | source | params | locales |
|------|--------|--------|---------|
| `amazon_product_camoufox` | amazon | `asin` | us, uk, de, fr, jp |
| `amazon_product_chromium` | amazon | `asin` | us, uk, de, fr, jp |
| `amazon_search_camoufox` | amazon | `query` | us, uk, de |
| `amazon_search_chromium` | amazon | `query` | us, uk, de |
| `bing_search_camoufox` | bing | `query` | us, uk, de, fr, ru, jp |
| `bing_search_chromium` | bing | `query` | us, uk, de, fr, ru, jp |
| `ebay_search_camoufox` | ebay | `query` | us, uk, de |
| `ebay_search_chromium` | ebay | `query` | us, uk, de |
| `google_search_camoufox` | google | `query` | us, uk, de, fr, ru, jp |
| `google_search_chromium` | google | `query` | us, uk, de, fr, ru, jp |
| `google_shopping_camoufox` | google | `query` | us, uk, de |
| `google_shopping_chromium` | google | `query` | us, uk, de |
| `linkedin_profile_camoufox` | linkedin | `username` | global (needs auth session) |
| `linkedin_profile_chromium` | linkedin | `username` | global (needs auth session) |
| `mobile_de_ad_camoufox` ⚠ | mobile_de | `ad_id` | de |
| `mobile_de_ad_chromium` ⚠ | mobile_de | `ad_id` | de |
| `mobile_de_search_camoufox` ⚠ | mobile_de | `make_id`, `model_id` | de |
| `mobile_de_search_chromium` ⚠ | mobile_de | `make_id`, `model_id` | de |
| `ozon_product_camoufox` | ozon | `product_id` | ru |
| `ozon_product_chromium` | ozon | `product_id` | ru |
| `ozon_search_camoufox` | ozon | `query` | ru |
| `ozon_search_chromium` | ozon | `query` | ru |
| `walmart_product_camoufox` | walmart | `product_id` | us |
| `walmart_product_chromium` | walmart | `product_id` | us |
| `yandex_search_camoufox` | yandex | `query` | ru, moscow, spb, by, kz, ua, uz, am, az, ge, kg, tj, tm, md, tr, us, de, fr, gb, pl, ee, lv, lt |
| `yandex_search_chromium` | yandex | `query` | ru, moscow, spb, by, kz, ua, uz, am, az, ge, kg, tj, tm, md, tr, us, de, fr, gb, pl, ee, lv, lt |
| `youtube_video_camoufox` | youtube | `video_id` | global |
| `youtube_video_chromium` | youtube | `video_id` | global |

A twin can be walled on its engine (Ozon's antibot passes Camoufox only, Google
Shopping passes Chromium only) or low on both; the measured verdict lives in
each preset's `description`, returned verbatim by `GET /api/v1/presets`, and a
walled twin names the twin that carries the data.

⚠ marks a preset the site admits only some of the time. mobile.de's Akamai
let about one attempt in four through on either engine on 2026-09-06, and a
refused attempt costs roughly two minutes and three proxy leases before the
budget runs out. It returns the site's 227-byte sensor shell at HTTP 200 with
every column empty, which reads exactly like a broken recipe and is not one --
whenever the page arrives, all 24 listings extract. Re-measure before wiring
one of these into anything that needs a first-try success.

### Scrape with a preset

```bash
curl -X POST http://localhost:8000/api/v1/scrape/preset/page \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "amazon_product_chromium",
    "preset_params": {"asin": "B0CRTYZG5C"},
    "locale": "us",
    "llm": {"model": "openai/gpt-5.4-mini"}
  }'
# -> {"job_id": "..."}  then GET /api/v1/scrape/<job_id>/results
```

`llm` is optional. Without it the deterministic parser runs alone. With it,
self-heal regenerates selectors when markup drifts — but only when a
**required** field comes back empty (that's the trigger). Every shipped
built-in marks its schema-critical field required, so a drift that empties
it engages self-heal; non-required fields just return null. Healed
selectors are persisted back for *user* presets (built-ins are read-only,
regenerated per-request). Batch: `POST /api/v1/scrape/preset/pages` with
`{"pages": [<PresetScrapeRequest>...]}`.

`preset_meta`/`parser_plan` are materializer-internal — setting them on the
raw `/api/v1/scrape/page` is rejected with 422.

### Preset CRUD & generation

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/presets?kind=&source=` | List built-in + user presets |
| `GET /api/v1/presets/{name}` | Full preset |
| `POST /api/v1/presets` | Create a user preset (name must start `user_`) |
| `PUT /api/v1/presets/{name}` | Replace a user preset |
| `DELETE /api/v1/presets/{name}` | Delete a user preset |
| `POST /api/v1/presets/generate` | `manual` \| `from_schema` \| `from_prompt` |

All three write paths check `request_defaults` against `ScrapeRequest` before
storing: a profile the scrape endpoints would reject with 422 is refused with
422 here, rather than saved and failing every run off the preset. `url`,
`extract`, `preset_meta` and `parser_plan` are supplied per request and may not
be set in `request_defaults`. Each locale's `country` must be usable as
`proxy_geo.country_code` (two letters), which is what the materializer derives
from it. A locale's `proxy_country`, when set, steers a proxied preset's exit
instead; on a direct preset (`proxy_type: none`) it does not apply -- there is
no exit to steer -- and the market `country` stays the pin.
| `POST /api/v1/presets/{name}/test` | Dry-run on `sample_url` or `sample_html` |
| `GET /api/v1/presets/llm-models` | Models the configured keys can call |

`generate` modes:

```bash
# AI builds selectors from a schema you supply
curl -X POST .../api/v1/presets/generate -d '{
  "name":"user_books","mode":"from_schema","source":"custom",
  "sample_url":"https://books.toscrape.com/",
  "schema":{"title":"string","price":"number"}}'

# AI infers the schema from a natural-language description, then selectors
curl -X POST .../api/v1/presets/generate -d '{
  "name":"user_books","mode":"from_prompt","source":"custom",
  "sample_url":"https://books.toscrape.com/",
  "description":"list each book title and price"}'
```

LLM keys are server-side in `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, or a custom OpenAI-compatible
endpoint via `CUSTOM_LLM_BASE_URL`/`CUSTOM_LLM_API_KEY`). Callers pick only
the model string; the default is `DEFAULT_LLM_MODEL` (`openai/gpt-5.4-mini`).
`sample_url` fetches are SSRF-guarded (no private/loopback/metadata targets).
The same guard now covers browser navigation: `/scrape` and `/search` refuse a
target that resolves to a non-public address, on every engine, and refuse to
return content fetched through a non-public redirect hop. `EGRESS_ALLOW_HOSTS`
names exceptions one host at a time; it is empty by default and has no
wildcard.

### post_process

`FieldRule.post_process` is an ordered transform chain applied to matched
values (per-item when `all: true`): `regex` (args `[pattern, group?]`),
`parse_int`, `parse_float`, `parse_price` (reads the separator convention
from the text; its `["us"|"eu"]` arg is accepted for older presets and
ignored), `strip`, `strip_tags`, `lowercase`, `uppercase`, `replace`
(args `[old, new]`), `urljoin` (args `[base_url]`, resolves a relative href —
usually left empty and injected per-request by the materializer; with no base
at all it leaves the value unchanged and warns, since there is no sensible
default transform for a URL with no base), `unwrap_param` (args `[param_name]`,
percent-decodes a query parameter's value out of a click-tracking redirect
that carries its destination inline — e.g. Amazon's `/sspa/click?...&url=
%2Freal%2Fpath` — passing any value that doesn't have that parameter through
UNCHANGED, which lets one pipeline handle a field mixing wrapped and
unwrapped values), `null_if_regex` (args `[pattern]`, nulls a matching value
IN PLACE — for excluding one shape from an `all: true` field without
shrinking it and misaligning every later row). A field may also override the
rule's `type` (css/xpath).

`unwrap_param` only hands on a decoded value that is an absolute `http(s)` URL
**with a host**, or a plain relative path. The parameter is text the *page*
controls, so everything else is refused and the ORIGINAL wrapper passes through
UNCHANGED — never null, so a refused row keeps its slot and stays visibly a
wrapper. Refused: any other scheme (`javascript:`, `data:`, `file:`, `ftp:` …);
an `http(s)` value naming no host (`http:///x`, `http:/foo`); anything opening
with **two or more** slashes or backslashes — `//host`, and equally `///host`,
`/\host`, `\\host`, `\/host`, since a browser's URL parser skips the whole run
and lands on `host`; and a reference naming no path segment of its own (`""`,
`.`, `..`, `?a=b`, `#frag`, `/`), which can only point back at the page being
scraped. Leading whitespace and tab/CR/LF are removed before that check,
because a URL parser removes them too.

This is **not** a same-host guarantee. An absolute `http(s)` URL naming any
host is accepted by design — a redirect may legitimately point off-site — and a
following `urljoin` leaves an absolute reference unchanged, so
`url=https%3A%2F%2Fother.example%2Fx` really does come back as
`https://other.example/x`. If you need the destination to stay on the page's
own host, check the host yourself.

`parse_int` takes the FIRST integer in the text, not every digit in it: a
separator groups only as complete three-digit runs that all use the same
separator, so `"4.5 out of 5"` is `4` (not `455`), `"1,234 reviews"` is `1234`,
and `"+1 800 555 0199"` is `1` (the tail repeats the separator, so the run is
not a grouped number). A minus counts only when it is attached to the digits and
itself opens the string or follows whitespace.

When a pipeline returns null for every matched value that had content, the
response carries a `field '<name>': post_process (...) returned null for every non-empty
value (N of M matched)` warning and the worker logs a sample of the dropped text.
The selector matched and the pipeline consumed it — usually markup drift, and
otherwise invisible, since the request still succeeds with the field simply null.

`strip_tags` renders an HTML fragment down to its text (tags dropped,
entities decoded, whitespace collapsed). Pair `attr: "html"` + `regex` +
`strip_tags` to read a value out of an element that is always present even
when the value is missing — the row-alignment workaround the `amazon_search_*`
and `google_search_*` presets were written around. Fields returning flat
parallel arrays (`all: true`) must match exactly one node per row: a selector
pointed at an element that only exists when its value does silently shrinks
the array and shifts every later row.

Prefer the rule-level [`container`](#container--one-value-per-row) in new
recipes. It states the row once and yields `null` in the right slot on its
own, so the `attr: "html"` + `regex` pair — and `null_if_regex`, which exists
for the same reason — are no longer needed to keep a column aligned. The
shipped recipes still use the older shape; they are migrated separately.

## Search

`POST /api/v1/search` runs a web search and returns ranked results in one
**synchronous** call (no `job_id` to poll). It fetches the SERP with the
engine's built-in preset -- `google_search_camoufox` for `engine: google` (the
default), `bing_search_chromium` for `bing`, `yandex_search_camoufox` for
`yandex` -- parses the organic results, and can optionally scrape each result
page with the normal pipeline. The google preset runs with no proxy, so a
google search leaves from this host's own address unless the request sets a
proxy -- and the `/goto` result links on its SERP are resolved from this
host's address either way.

```bash
# SERP only
curl -s localhost:8000/api/v1/search \
  -H 'content-type: application/json' \
  -d '{"query": "open source web scraper", "limit": 5}'

# SERP + scrape each result (forward any ScrapeRequest field via scrape_options)
curl -s localhost:8000/api/v1/search \
  -H 'content-type: application/json' \
  -d '{"query": "privacy policy generator", "limit": 3,
       "scrape": true, "scrape_options": {"raw_html": true}}'
```

Request fields: `query` (required), `engine` (`google`/`bing`/`yandex`, default
`google`), `locale` (a key of the engine preset's locales: `us`/`uk`/`de`/
`fr`/`ru`/`jp` for google and bing, yandex has its own list; unset takes the
preset's default, `us` for google and bing, `ru` for yandex), `limit` (1–50,
default 10), `scrape` (bool), `scrape_options`
(forwarded to each result's scrape; internal fields are stripped, `session_id`
is validated).

Response: `{query, count, results: [{url, title, snippet, scrape?}], took_ms, warnings}`.
A blocked/empty SERP degrades to `count: 0` plus a `warnings` entry (HTTP 200)
rather than an error — results depend on a working egress for the SERP fetch
(this host's own address for google, a residential exit for bing and yandex). A SERP that answered a different query than the one asked (an
engine's autocorrect, or Bing's cached page for someone else's query) carries
a `serp_query_mismatch` warning next to its results — counts only, never the
page's text.

## MCP Integration

Yozh Scraper exposes an explicit **allowlist** of its API tools via the
[Model Context Protocol](https://modelcontextprotocol.io/) at `/mcp`. This
lets AI assistants use the scraper directly as a tool — no extra code
required.

The allowlist is `MCP_TOOLS` in [`src/app.py`](app.py), and a route is not a
tool until it is named there. It used to be a denylist keyed on
`operation_id`, which structurally could not name a route that declared none —
which is how eight `/api/v2/prem-proxies` routes became anonymously callable
agent tools while the code claimed nothing could drift onto the surface.

**`/mcp` itself carries no authentication**, so anything on this list is
callable by any client that can reach the port. That is why the list is
explicit rather than "everything except a few". Session operations and
`resolve_proxy` are gated behind `SERVICE_TOKEN` and never advertised.

### Available MCP tools

The table below is a summary; `MCP_TOOLS` is the source of truth.

| Tool | Description |
|------|-------------|
| `run_scrape_page` | Scrape a single page (supports `formats` incl. markdown), returns `job_id` |
| `run_scrape_pages` | Scrape multiple pages in batch, returns `job_id` |
| `run_search` | Web search (SERP + optional scrape), returns results inline |
| `get_job_status` | Poll job status by `job_id` |
| `get_job_result` | Fetch results for a job (partial snapshot with `null` slots while running) |
| `cancel_scrape_job` | Soft-cancel a running job |
| `health` | Health check |

### Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "yozh-scraper": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Restart Claude Code — scraper tools appear automatically. Then just ask in
chat:

```
Scrape https://example.com and tell me what's on the page
```

### LangChain Agent

```bash
pip install langchain-anthropic langchain-mcp-adapters langgraph
export ANTHROPIC_API_KEY=sk-ant-...
python ../examples/agent.py
```

See [`../examples/agent.py`](../examples/agent.py) — a chat agent that handles
the full job lifecycle: submit → poll → fetch → summarize.

### MCP Inspector (debug UI)

```bash
npx @modelcontextprotocol/inspector http://localhost:8000/mcp
# Opens at http://localhost:6274
```

## Sessions

Server-managed sessions let you authenticate once, then reuse cookies +
localStorage across subsequent scrape requests. Lifetime is in-memory only —
sessions evaporate on process restart.

### Lifecycle

1. `POST /api/v1/sessions` — create. Body pins `device`, `proxy_type`,
   `proxy_pool_id`, `proxy_geo`, `ttl_seconds` (default 86400, clamped
   `[300, 7×86400]`). Returns `{session_id, expires_at}`.
2. `POST /api/v1/sessions/{id}/login` — replay a declarative login script
   with `creds` in the request body. Body: `{script: LoginScript, creds: dict}`.
   Credentials live in RAM only — never persisted.
3. `POST /api/v1/scrape/page` with `session_id` set — scrape an authenticated
   page using the session's cookie jar.
4. `DELETE /api/v1/sessions/{id}` — clean up.

### Login DSL

The script is a list of steps, each one of:

| op                  | required fields                      | maps to                             |
| ------------------- | ------------------------------------ | ----------------------------------- |
| `goto`              | `url`                                | `page.goto(url)`                    |
| `fill`              | `selector`, `value`                  | `page.fill(selector, value)`        |
| `click`             | `selector`                           | `page.click(selector)`              |
| `wait_for_selector` | `selector` (optional `timeout_ms`)   | `page.wait_for_selector(...)`       |
| `wait_for_timeout`  | `ms`                                 | `page.wait_for_timeout(ms)`         |
| `press_key`         | `selector`, `key`                    | `page.press(selector, key)`         |
| `type_text`         | `selector`, `value`                  | `page.type(selector, value)`        |
| `hover`             | `selector`                           | `page.hover(selector)`              |

`value` and `url` support `$creds_<key>` substitution from the `creds` dict
(`$creds_email`, `$creds_password`, etc.). Unknown tokens stay literal.

### Error rules

- `session_id` + `cookies` together → **422 cannot pass both**.
- `session_id` + mismatched `device` / `proxy_type` / `proxy_pool_id` /
  `proxy_geo` → **422**.
- `proxy_type=res_rotating` without `proxy_pool_id` on create → **422**
  (rotating exit IPs invalidate the server-side session).
- TTL expired → **410 Gone**.
- Login script step failure → response carries `failed_step_index` +
  `failed_step` + base64 screenshot (always-on-failure, never-on-success).

### Example

```bash
# 1. Create
curl -X POST http://localhost:8000/api/v1/sessions \
  -H 'content-type: application/json' \
  -d '{"device":"desktop","proxy_type":"none","ttl_seconds":3600}'
# {"session_id":"sess_...","expires_at":...}

# 2. Login (creds in body)
curl -X POST http://localhost:8000/api/v1/sessions/$ID/login \
  -H 'content-type: application/json' \
  -d '{
    "creds":{"email":"tomsmith","password":"SuperSecretPassword!"},
    "script":{
      "steps":[
        {"op":"goto","url":"https://the-internet.herokuapp.com/login"},
        {"op":"fill","selector":"#username","value":"$creds_email"},
        {"op":"fill","selector":"#password","value":"$creds_password"},
        {"op":"click","selector":"button[type=submit]"},
        {"op":"wait_for_selector","selector":".flash.success"}
      ],
      "success_selector":".flash.success"
    }
  }'

# 3. Authenticated scrape
curl -X POST http://localhost:8000/api/v1/scrape/page \
  -d '{"url":"https://the-internet.herokuapp.com/secure","session_id":"'$ID'","raw_html":true}'
```

End-to-end runnable example: [`../examples/login_session_scraping.py`](../examples/login_session_scraping.py).

### Cookie injection — escape hatch for CAPTCHA / 2FA / MFA

When the login flow hits a CAPTCHA, device-verification challenge, or 2FA
prompt that the Phase 1 DSL can't solve, skip the login script and inject
cookies you exported from your own logged-in browser:

```bash
# 1. Log in manually in your real Chrome on the target site.
# 2. Export cookies via an extension like Cookie-Editor or EditThisCookie
#    (right-click → Export → JSON copies an array to the clipboard).
# 3. Create the session as usual:
curl -X POST http://localhost:8000/api/v1/sessions \
  -d '{"device":"desktop","proxy_type":"none","ttl_seconds":86400}'
# 4. Paste the cookie array into the session:
curl -X POST http://localhost:8000/api/v1/sessions/$ID/cookies \
  -H 'content-type: application/json' \
  -d '{"cookies":[{"name":"sessionid","value":"abc","domain":".example.com","path":"/","expires":1800000000,"httpOnly":true,"secure":true,"sameSite":"Lax"}, ...]}'
# 5. Scrape with session_id; the session's storage_state carries your
#    authenticated cookies into every fetch.
```

The Tester UI has the same flow under **Sessions → Inject Cookies** —
it accepts the raw extension-export JSON and converts `expirationDate` (float)
to `expires` (int), normalizes `sameSite` casing, and drops extension-only
keys (`hostOnly`, `storeId`, `session`).

The cookie endpoint is also useful when you already have a long-lived API
session cookie (CI service account, partner-supplied token) and want to skip
the login flow entirely.

### Tester UX niceties

- **Pool ID** in the Sessions tab is populated from `/api/v1/proxies/available`
  just like the Scrape / Batch / Crawler tabs do — pick the pool from the
  dropdown instead of typing a UUID. Visible only when `proxy_type != none`.
- **Picking a session** on Scrape Page / Batch / Crawler auto-copies its
  pinned `device`, `proxy_type`, `proxy_pool_id`, and `proxy_geo` into the
  form. If the form already has non-default values that differ from the pin,
  a confirm dialog asks before overwriting (the scraper would 422 a mismatched
  submit anyway).
- **Authenticated SOCKS5 proxies** — the login worker shares the same
  HTTP-to-SOCKS5 bridge that the scrape path uses, so CyberYozh residential /
  mobile pools that present as `socks5://user:pass@host:port` work for login
  as well as scrape.

### Known limitations (Phase 1)

- In-memory only — sessions are lost on container restart. Persistence is a
  future operational-pack feature.
- IndexedDB / Service Worker state is invisible to Playwright's
  `storage_state` — sites that store JWT in IndexedDB (Dexie, Firebase Auth)
  will appear logged-in mid-session but lose state on restore.
- Per-session storage_state cap is 2 MB; SPA-heavy targets may hit this.
- `res_rotating` requires `proxy_pool_id` (see above).
- No auto re-login on 401 / cookie expiry — Phase 2.
- No CAPTCHA / MFA hooks — Phase 3.

## Tests

```bash
pip install -e ".[test]"
pytest -q
```

Tests live in [`../tests/`](../tests/) and cover the API, job queue, and
proxy resolver. Browser-dependent tests assume a Playwright image is
available.
