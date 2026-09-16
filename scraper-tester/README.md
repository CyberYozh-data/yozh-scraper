# Scraper Tester

Lightweight web UI for manually driving both the [**Yozh Scraper**](../src/README.md)
and [**yozh-crawler**](../yozh-crawler/README.md) APIs. A thin Node.js
(Express) server on `http://localhost:7000` serves a single-page frontend with
tabs for every workflow.

The tester talks to **two** backends:

- **Scraper** via the built-in Express proxy (`/proxy` route + `x-scraper-target`
  header) — bypasses CORS so you can point it at any reachable scraper
  deployment.
- **Crawler** directly from the browser — the crawler has CORS enabled, which
  is required for `EventSource` (SSE doesn't support custom headers, so the
  proxy hop isn't workable).

## Service token

The scraper gates `/api/v2/prem-proxies/*`, `/api/v1/proxies/resolve` and every
`/api/v1/sessions` route behind a shared secret. Start the harness with it:

```bash
SERVICE_TOKEN=<the value from the scraper's .env> node server.js
```

The Express proxy injects `X-Service-Token`, so the secret never reaches the
browser — but only when **both** hold:

- the upstream is on `SCRAPER_TARGETS` (defaults to localhost / `web-scraper` /
  `open-crawler` on their usual ports), and
- the path is one of the gated prefixes.

Both constraints are load-bearing. `/proxy` takes its upstream from the
caller's `x-scraper-target` header and accepts any URL, and this listener is
not bound to loopback — so an unconditional header would hand the secret to
whatever host a caller named. That secret is not scoped to the catalog: it
also unlocks `/api/v1/proxies/resolve`, which returns a fully credentialed
paid-proxy URL. Point the tester at a scraper elsewhere by adding it to
`SCRAPER_TARGETS`; the server warns when it declines to send the token.

Without a token those panels get 401s — and `app.js` swallows fetch errors, so
they render as empty dropdowns with a silent console rather than an error.

## Tabs

- **Scrape Page** — full parameter set: URL, device, `wait_until`, selectors,
  timeout, custom headers, cookies, proxy type + geo, block assets, raw HTML,
  screenshots, CSS/XPath extraction. Cancel button next to the status bar for
  running jobs.
- **Batch Scrape** — submit multiple URLs with shared proxy / render settings.
  Live partial results as pages complete. Same cancel button.
- **Crawler** — start a site walk, stream progress over SSE (stats + per-page
  events), see the **Site Map** as an indented tree (with a one-click Copy
  button), inspect per-page `ScrapeResponse` when *Enable scraping* is on.
  **Two-step cancel**: first click = soft (finish in-flight, stop
  scheduling), second click = force stop (abort asyncio tasks, crawl exits
  immediately).
- **Presets** — split into a library panel (left) and a Preset Builder wizard
  (right). The library lists all built-in and user presets with View / Delete
  buttons, a kind filter, and a Refresh button. The wizard walks four steps:
  1. **Sample & Fetch** — enter a URL or paste raw HTML, configure scrape
     options and proxy (same widget as the other tabs), and click "Fetch
     sample" to run a real scrape job; its `raw_html` becomes the working
     sample. Step-1 settings (device, render mode, geo, proxy type, headers,
     etc. — everything except the concrete `proxy_pool_id`) are stored in the
     preset's `request_defaults`.
  2. **Method** — choose Manual (type CSS/XPath selectors into the field
     table), AI by prompt (describe what to extract and the AI fills the
     table), or AI by schema (define field names + types and the AI fills it).
     All three converge into one editable field table.
  3. **Verify** — sends the current field table to `POST /api/v1/presets/preview`
     (a dry-run endpoint that validates or AI-generates
     `parsing_instructions`, runs the parser pipeline, and returns
     `{parsing_instructions, output_schema, extracted, warnings, mode}`)
     so you can inspect extracted data and iterate without touching the store.
     The `self_heal` option can be toggled here.
  4. **Save** — names the preset (auto-prefixed `user_`) and persists it via
     `POST /api/v1/presets`; it immediately appears in the library and in the
     preset selector on the **Scrape Page** tab.

  Running presets in production (against real URLs) is done from the
  **Scrape**, **Batch Scrape**, and **Crawler** tabs — not here.

- **Jobs** — look up any scrape job by id, view status and results, history of
  recent jobs in the session. Cancel button appears when a looked-up job is
  still running.
- **MCP** — select a target (**Scraper (8000)** or **Crawler (8001)**),
  initialize a Streamable HTTP session, list available tools, call any tool
  with JSON arguments. Schemas are rendered inline from the tool's
  `inputSchema`.

## Conveniences

- **Header presets** — `en_us` / `ru_locale` / `de_locale` / `privacy` /
  `navigation`: `Accept-Language`, `DNT`/`Sec-GPC` and navigation headers. No
  identity presets and no form-factor flip — the engine states the UA and the
  `Sec-CH-UA*` family, and the API drops them from `headers` on every engine,
  so a preset offering them would have been a control that does nothing.
- **Custom selects** — every native `<select>` is transparently wrapped in a
  styled dropdown (white popup, `shadow-level-4`, checkmark on the selected
  option). The underlying `<select>` stays in the DOM, so all tab-switch
  handlers / form-reader code keep working unchanged.
- **State persistence across F5** — active tab, all form inputs, dynamic rows
  (headers / cookies / extract fields), URL fields, MCP target — everything is
  serialized to `localStorage` on change and restored on page load. Dev
  convenience only; cookies / headers are stored in clear text.
- **No caching on static** — the server sets `Cache-Control: no-store` so
  edits to `index.html` / `app.js` / `style.css` are picked up on a plain F5.

## Requirements

- Node.js 18+
- A running scraper on `http://localhost:8000` (default)
- A running crawler on `http://localhost:8001` (default — optional, only the
  **Crawler** tab and **Crawler** MCP target need it)

## Usage

```bash
cd scraper-tester
npm install
node server.js
```

Open [http://localhost:7000](http://localhost:7000). Two URL fields in the
header: **Scraper URL** and **Crawler URL**. The `Check` buttons next to
each hit the respective `/api/v1/health` and flip to a green badge on success.

## Layout

```
scraper-tester/
├── server.js                 # Express server + /proxy → scraper
├── public/
│   ├── index.html            # All tabs
│   ├── app.js                # All tab logic + SSE + custom selects + state persist
│   ├── style.css             # Global + custom-select styles
│   ├── logo.png / ScrapingYozh.png
│   └── ...
└── package.json
```

No build step. Edit → refresh.
