# Scraper Tester

Lightweight web UI for manually driving both the [**Yozh Scraper**](../src/README.md)
and [**open-crawler**](../open-crawler/README.md) APIs. A thin Node.js
(Express) server on `http://localhost:7000` serves a single-page frontend with
tabs for every workflow.

The tester talks to **two** backends:

- **Scraper** via the built-in Express proxy (`/proxy` route + `x-scraper-target`
  header) — bypasses CORS so you can point it at any reachable scraper
  deployment.
- **Crawler** directly from the browser — the crawler has CORS enabled, which
  is required for `EventSource` (SSE doesn't support custom headers, so the
  proxy hop isn't workable).

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
- **Jobs** — look up any scrape job by id, view status and results, history of
  recent jobs in the session. Cancel button appears when a looked-up job is
  still running.
- **MCP** — select a target (**Scraper (8000)** or **Crawler (8001)**),
  initialize a Streamable HTTP session, list available tools, call any tool
  with JSON arguments. Schemas are rendered inline from the tool's
  `inputSchema`.

## Conveniences

- **Header presets** — Chrome / Firefox / Safari / Mobile / RU / anti-bot
  fingerprints. Mobile presets also flip the Device selector to `mobile`.
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
