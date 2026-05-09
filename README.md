# Yozh Scraper by CyberYozh

![ScrapingYozh](scraper-tester/public/ScrapingYozh.png)

A two-service scraping stack built on **Playwright**, with an optional Web
tester and MCP exposure on both services.

| Service | Port | Docs | What it does |
|---|---|---|---|
| **Scraper** ([`src/`](src/README.md)) | `8000` | [src/README.md](src/README.md) | Async job API — renders a URL in a real browser, returns extracted fields / raw HTML / full-page screenshot. Built-in CyberYozh proxy integration. |
| **Crawler** ([`yozh-crawler/`](yozh-crawler/README.md)) | `8001` | [yozh-crawler/README.md](yozh-crawler/README.md) | Walks a site from a seed URL; fetches every page through the scraper over HTTP; streams results via SSE. Dedup, scope, rate-limiting, retries. |
| **Tester** ([`scraper-tester/`](scraper-tester/README.md)) | `7000` | [scraper-tester/README.md](scraper-tester/README.md) | Node.js + vanilla HTML UI for both services — every knob as a form, live progress, MCP explorer. |

Both API services mount an **MCP endpoint** at `/mcp` (Streamable HTTP) and
publish their OpenAPI at `/docs`.

## Quick start

```bash
cp .env.example .env          # edit CYBERYOZH_API_KEY if using proxies
docker compose up --build
```

That brings up the scraper (`:8000`) and the crawler (`:8001`). Verify both:

```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok","workers":2}

curl http://localhost:8001/api/v1/health
# {"status":"ok","workers":2,"scraper_reachable":true,...}
```

Full scraper smoke test:

```bash
python3 scripts/e2e_smoke.py
```

### Individual services

- **Scraper only**: `docker compose up --build web-scraper`
- **Crawler only**: `docker compose up --build yozh-crawler`
  (depends on `web-scraper`; compose starts it too)

### Visual tester

The [tester](scraper-tester/README.md) is not part of the compose stack —
run it separately:

```bash
cd scraper-tester
npm install
node server.js
# → http://localhost:7000
```

It has tabs for Scrape Page, Batch Scrape, **Crawler** (with SSE live updates
and a site-map tree), Jobs, and MCP (with a target dropdown for scraper vs
crawler). Active tab and form state persist across page refreshes.

## Proxy support (CyberYozh App)

**For reliable web scraping, using proxies is essential.** Most modern sites
(search engines, e-commerce, social media) have anti-bot protection that
blocks direct scraping — proxies help you avoid IP bans, bypass
geo-restrictions, and appear as real users from different locations.

The scraper integrates with **CyberYozh Proxy Service** (residential, mobile
LTE, and datacenter proxies). The crawler forwards proxy config to the
scraper as-is — no duplication.

1. Get an API key: https://app.cyberyozh.com/api-access/
2. Drop it into `.env` as `CYBERYOZH_API_KEY=...`
3. Restart the scraper container.
4. Use any `proxy_type` in a scrape / crawl request.

Available types (full details, GEO targeting, CyberYozh category mapping
and discovery endpoints in [src/README.md](src/README.md#proxy-support)):

| `proxy_type`     | What it is |
|------------------|------------|
| `res_rotating`   | Residential rotating — recommended default |
| `res_static`     | Residential static (dedicated IP)          |
| `mobile`         | Mobile / LTE, dedicated                    |
| `mobile_shared`  | Mobile / LTE, shared pool                  |
| `dc_static`      | Datacenter static                          |
| `none`           | Direct connection, no proxy                |

The tester lists purchased proxies in each dropdown via
`GET /api/v1/proxies/available?proxy_type=...` — no manual pool-id hunting.

## MCP integration

Both services speak [Model Context Protocol](https://modelcontextprotocol.io/)
over Streamable HTTP:

- **Scraper** → `http://localhost:8000/mcp` — tools: `run_scrape_page`,
  `run_scrape_pages`, `get_job_status`, `get_job_result`, `cancel_scrape_job`,
  `health`.
- **Crawler** → `http://localhost:8001/mcp` — tools: `create_crawl`,
  `get_crawl`, `get_crawl_results`, `cancel_crawl`, `health` (the SSE
  `events` stream is excluded — streams don't fit the MCP tool model).

### Claude Code / Claude Desktop

Add both servers to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "open-scraper": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    },
    "open-crawler": {
      "type": "http",
      "url": "http://localhost:8001/mcp"
    }
  }
}
```

Restart Claude. Both sets of tools appear automatically — you can just ask:
*"Scrape https://example.com and tell me what's on the page"* or
*"Crawl app.cyberyozh.com, scope same-domain, depth 2, up to 50 pages"*.

### LangChain agent

```bash
pip install langchain-anthropic langchain-mcp-adapters langgraph
export ANTHROPIC_API_KEY=sk-ant-...
python examples/agent.py
```

See [`examples/agent.py`](examples/agent.py) — chat agent that handles the
full scraper job lifecycle (submit → poll → fetch → summarize). Same
`langchain-mcp-adapters` pattern works for the crawler endpoint — point at
`http://localhost:8001/mcp`.

### n8n

n8n has a native **MCP Client Tool** node (or the community
`n8n-nodes-mcp` package for older versions). Add it to an **AI Agent**
workflow:

1. Drop an *AI Agent* node.
2. Add a *MCP Client Tool* node connected to the Agent's **Tools** input.
3. Configure:
   - **SSE Endpoint / Server URL**: `http://localhost:8000/mcp` (scraper)
     or `http://localhost:8001/mcp` (crawler). If n8n runs in Docker too,
     use the container's network name instead of `localhost`.
   - **Transport**: Streamable HTTP.
4. The agent can now call any scraper/crawler tool by name. Repeat the
   node to wire both services into one agent.

### Debug UI (MCP Inspector)

```bash
npx @modelcontextprotocol/inspector http://localhost:8000/mcp
# or
npx @modelcontextprotocol/inspector http://localhost:8001/mcp
# Opens at http://localhost:6274 — list + invoke tools visually
```

The Web tester's **MCP** tab does the same thing in-app, with a target
dropdown to switch between scraper and crawler.

## Documentation map

- **[src/README.md](src/README.md)** — scraper service: full REST API
  reference with 14 curl examples, request/response schema, ExtractRule
  details, CyberYozh proxy integration (all 5 types + GEO targeting + proxy
  discovery endpoints), MCP tools, proxy-type → CyberYozh category mapping,
  tests.
- **[open-crawler/README.md](open-crawler/README.md)** — crawler service:
  features, API reference, configuration env vars, `enable_scraping` toggle
  semantics, MCP tools, architecture diagram, known limitations, running
  without Docker.
- **[scraper-tester/README.md](scraper-tester/README.md)** — tester UI:
  per-tab features, how the cancel buttons / site map / custom selects /
  state persistence work.

## Top-level layout

```
yozh-scraper-clone/
├── src/                      # scraper service
├── yozh-crawler/             # crawler service
├── scraper-tester/           # Web UI (Node.js)
├── examples/                 # Python examples (incl. MCP agent)
├── scripts/                  # e2e_smoke.py etc.
├── tests/                    # scraper pytest suite
├── docker-compose.yml        # scraper + crawler
├── Dockerfile                # scraper image (Playwright base)
└── .env.example              # scraper env template
```
