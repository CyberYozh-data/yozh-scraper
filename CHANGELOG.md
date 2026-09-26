# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.14] - 2026-09-26

Google release. Google had stopped answering this service entirely — a 108-run
sweep on 2026-09-17 returned data on 0 of 12 `google_search` runs, every one a
captcha served at HTTP 200, while Bing scored 12 of 12 and Yandex 8 of 8. The
cause was the exit address, not the recipe: Google refuses the residential
pool's exits by address. `engine: google` therefore moves to the Camoufox twin,
on `google.com`, from the service's own egress.

### Changed

- **BREAKING (Google):** `GET /search?engine=google` and the built-in Google
  presets now resolve to `google_search_camoufox` / `google_shopping_camoufox`,
  which carry `proxy_type: none` — the SERP is fetched from the address the
  service itself runs on, not from the proxy pool. A caller that supplies its
  own proxy still gets it; only the default changed.
- **BREAKING (Google locales):** every Google locale now requests
  `www.google.com` with `gl`/`hl` rather than the country domain, so
  `locale: de` returns `google.com?gl=de&hl=de` instead of `google.de`. The
  result set and its ordering differ from the country domain. Preset `version`
  bumps 5 → 6.
- The chromium Google twins stay on the residential pool and are marked as
  refused in their descriptions. They remain callable, but they are measured at
  0 of 3 and are kept off the direct address deliberately: a request known to be
  refused would spend the one egress the working recipe depends on.
- A preset that runs on direct egress no longer applies a locale's proxy-country
  override — there is no exit for it to override. The locale's market country is
  still pinned, so `locale: de` remains a German browser identity; only the
  exit-only override is dropped, and dropping it is logged. Presets that run
  through a proxy are unaffected.

### Added

- `resolve_redirects` on the Google presets resolves the SERP's `/goto` stubs
  from the service's own address, so `links` carries real destinations on the
  new route too.
- Measurement data for the whole investigation under `research/`: the 108-run
  sweep that found the outage, the isolation matrix that separated engine from
  domain from exit, the per-country pool calibration, and the end-to-end runs of
  the shipped configuration.

### Fixed

- The `/search` route is now pinned by tests to the engine map and to
  `www.google.com` for every locale, instead of to a word in a preset
  description — renaming that word used to let the route slide back to the
  refused engine with the suite still green.
- Re-measured `ozon_*` and `mobile_de_*` descriptions: what each preset returns
  today, on which engine, and what a refusal looks like.
- The tester's Search tab no longer advises picking a residential pool for
  Google, and `src/README.md` no longer names `google_search_chromium` as the
  SERP source.

### Known limits

- The working route rests on one address, and its budget is finite and not yet
  measured: the shipped configuration returned 6 of 8 on 2026-09-23, both misses
  a refusal on that address, after 39 consecutive passes on 09-18 to 09-20.
- Five of the six Google locales ship the new `.com` shape without an
  end-to-end measurement; `us` is the measured one.

## [0.1.13] - 2026-09-16

Extraction-correctness and identity-coherence release: a rule can be scoped to
its rows so columns cannot drift apart, the browser owns the identity headers a
caller used to be able to contradict, Camoufox moves to 0.5.5 with a pinned
binary, and a refused page is now named as a block instead of reading as a
broken recipe. One breaking change for callers that send a `User-Agent`.

### Added

- **Row-scoped extraction.** An extract rule takes a `container` selector; every
  field is then evaluated inside each row, one value per row, `None` where a row
  has no match. Columns stay the same length and the Nth value of two columns
  comes from the same row. Membership is checked on the matched node, not on the
  selector's spelling, so a selector that escapes its row contributes nothing.
- **`device=legacy_wap`** — a feature-phone identity that unlocks Google's no-JS
  layout. Requires `render: false` (422 otherwise, because JavaScript would
  contradict the claim), is refused on Camoufox, and cannot be pinned to a
  session.
- **`resolve_redirects`** — names extracted fields whose values are the page's
  own redirect stubs; the worker resolves them through the egress guard.
  `google_search` uses it, so `links` carries real destinations again, and a
  `display_urls` column ships alongside.
- New built-in presets: `ozon_search`, `ozon_product`, `mobile_de_search`,
  `mobile_de_ad` (per engine, as all built-ins are).
- A warning when a search engine answered a different query than the one asked.
- A warning when a price arrives with its decimal separator missing.
- `request_defaults` saved on a preset are validated against what the scraper
  would accept, so a bad profile is a 422 at save time instead of a failure on
  every later scrape.

### Changed

- **BREAKING (callers sending identity headers):** `User-Agent` and the whole
  `Sec-CH-UA` client-hints family are now engine-owned and dropped from
  caller-supplied `headers` on every engine — a header contradicting the
  engine's real fingerprint is a bot tell, not a disguise. The response says so
  in `warnings` as `ignored_request_field: headers['User-Agent'] (the engine
  states its own)`, and `meta.applied_user_agent` reports what actually went
  out. Other headers (`Accept-Language`, `Referer`, `Cookie`, custom ones) are
  unaffected.
- **BREAKING (crawler):** `POST /crawl` forwarded extract rules through a schema
  mirror that silently dropped a per-field `type` and every `post_process` step,
  so a rule reached extraction altered with nothing in the response to say it.
  Both are forwarded now, and an unknown `post_process` op is a 422 on the crawl
  request instead of a failure on every page.
- Camoufox upgraded to 0.5.5, with the browser binary pinned.
- The Chrome User-Agent is stated only on the engine that can back it.
- A country-pinned proxy lease now yields that country or an error, never a
  silent fallback to another one.

### Fixed

- A page the site refused is classified as a block rather than as a failed
  extraction: Akamai's behavioural interstitial (HTTP 200, no redirect, empty
  columns) is recognised, and a warmup hop that lands on a block is reported
  instead of passing as a successful warmup.
- Self-heal no longer overwrites a preset edited while the scrape was running:
  a job carries a fingerprint of the preset as it read it, and the write lands
  only if the stored file still matches. Preset writes are serialised per file.
- `bing_search` keeps working when a content blocker decodes its tracking links.
- Column defects across built-in presets: nullable columns declared by the
  recipe, a one-digit tail read as a decimal in every locale, honest timeouts.
- An error message containing a hostname chosen by the target page can no longer
  decide that a fresh proxy exit is spent.
- `GET /map` reads response bodies up to a cap and uses what it read.

### Security

- The browser is launched with a narrowed environment instead of inheriting the
  service's: `SERVICE_TOKEN`, LLM API keys and proxy credentials no longer reach
  the process that renders caller-supplied pages.
- Compose hardening: capabilities dropped, new privileges forbidden, process
  count capped.
- Secrets are kept out of request echoes, and the premium-proxy relay is bounded.
- `qs` and `express` bumped in the bundled tester.

## [0.1.12] - 2026-09-01

Security-hardening and reliability release: browser navigation is refused to
non-public addresses (SSRF), the premium-proxy surface is gated behind
`SERVICE_TOKEN` and the MCP tool surface is an allowlist, presets ship per
browser engine, and logs are correlated with an opt-in JSON shape. Contains
two breaking changes — see below.

### Added

- Every built-in preset now ships as an explicit per-engine variant,
  `<name>_chromium` and `<name>_camoufox` (e.g. `google_search_chromium`,
  `yandex_search_camoufox`). A request names the exact variant it wants; each
  preset carries its own `browser_engine`.
- Structured, correlated logging: business events get stable names and a
  request/job correlation id threaded through the scrape -> queue -> browser
  chain, so success/error rate and latency are answerable from logs. An opt-in
  JSON log shape is available via `LOG_FORMAT=json` (default `text`).
- Browser egress policy: an `EGRESS_ALLOW_HOSTS` allowlist (empty by default)
  and an `EGRESS_TRANSPORT_GUARD` toggle (on by default) for the transport-level
  guard.

### Changed

- **BREAKING (presets):** the old single-name built-ins (`google_search`,
  `google_shopping`, `amazon_product`, `amazon_search`, `bing_search`,
  `ebay_search`, `linkedin_profile`, `walmart_product`, `yandex_search`,
  `youtube_video`) are replaced by their per-engine variants. A request using a
  bare old name now returns 404 `preset_not_found`; append the engine suffix
  (`_chromium`, or `_camoufox` for the two anti-bot targets `yandex_search` and
  `walmart_product`).
- **BREAKING (deploy):** `GET /api/v1/proxies/available` and the entire
  `/api/v2/prem-proxies/*` catalog now require `SERVICE_TOKEN` and fail closed
  (503 when the token is unconfigured, 401 without it). Any consumer of these
  endpoints must send the `X-Service-Token` header; deploy the consumer first,
  or their calls degrade (for the proxy geo catalog, silently).

### Security

- Browser navigation to non-public addresses is refused by default. The main
  scrape, warmup and session-login navigations validate the resolved IP and
  re-check every redirect hop, blocking loopback, private (RFC1918),
  link-local (incl. the cloud-metadata address), CGNAT and IPv4-mapped-IPv6
  targets. A transport-level guard (on by default) dials only validated public
  IPs, closing the redirect / DNS-rebinding / sub-resource vectors the
  navigation check alone cannot.
- The MCP tool surface is now an allowlist: only explicitly named operations
  are advertised as agent tools, so a new route is hidden by default and the
  premium-proxy and credential-bearing operations are no longer reachable
  anonymously through MCP.
- Proxy-catalog responses redact credentialed proxy URLs, keeping `host:port`
  while stripping `user:pass`.

### Fixed

- Browser teardown failures (page / context / proxy-bridge close) are now
  logged and time-bounded instead of being swallowed silently, on both the
  scrape and the session-login paths, so a wedged or leaked browser resource is
  visible and cannot hang the worker.

## [0.1.11] - 2026-08-24

Reliability and hardening release: partial job results survive one unreadable
slot, self-heal can no longer overwrite a working preset with a degraded one,
the credentialed login is masked like every other page, and proxy credentials
are kept out of logs and error bodies. The only API change is a new
`unreadable_slots` field on the results response.

### Added

- The job results response carries `unreadable_slots`. A job with one
  corrupt or schema-skewed result slot now returns its other pages (HTTP 200)
  and names the bad slot, instead of the whole job failing with a 500 and
  becoming un-cancellable until its TTL expires.

### Changed

- The login-replay page is masked with the same host-aligned WebGL/GPU,
  native-looking `navigator`, and Client-Hints (`Sec-CH-UA`) as the main fetch
  path, via a shared page-preparation helper. A credentialed login no longer
  submits a contradictory fingerprint (previously a macOS GPU and a
  `HeadlessChrome` Client-Hint under a Windows user agent).

### Fixed

- Self-heal can no longer persist a degraded preset over a working one. It now
  contributes only the regenerated selector (and its dialect), keeping the
  preset's `all` / `attr` / `post_process` and required fields, and it is
  graded against the original contract — so a heal that returns a bare string
  where a coerced list belongs is no longer counted as a recovery. It also
  never heals from a transient 5xx error page. This closes a class of silent
  preset corruption.

### Security

- Proxy credentials are kept out of logs, error messages and 502 response
  bodies. The SOCKS bridge, the rotating-credentials response and the username
  log no longer emit `user:pass`; a shared redactor masks proxy URLs while
  preserving `host:port` for diagnostics.
- `litellm` is bounded `<1.98`: 1.98.0 imports `NotRequired` from `typing`
  unguarded and fails to import on the Python 3.10 this project targets, so a
  fresh install or image build would produce a container that cannot start.

## [0.1.10] - 2026-08-20

Chromium anti-detection release: the browser now exposes a working WebGL context
that claims the host's actual GPU, closing a headless "no WebGL" tell and the
SwiftShader-renderer tell behind it. Plus a README project-site link. No API
change.

### Added

- Chromium WebGL now claims the host's actual GPU vendor/renderer, kept coherent
  with the Windows fingerprint the browser already presents, reusing the
  host-GPU detection from the Camoufox path (`HOST_GPU_VENDOR`). Without it the
  restored context reported a generic SwiftShader/ANGLE software-renderer
  string, itself an automation tell.

### Fixed

- Chromium returned a null WebGL context on GPU-less / headless hosts (Chrome
  136+ dropped the automatic SwiftShader fallback), and "no WebGL context at
  all" is a strong bot tell that real desktop Chrome never shows. A working
  software WebGL context is restored via `--enable-unsafe-swiftshader`, gated by
  the new `SOFTWARE_WEBGL` setting (default on, Chromium-only; revertible by env
  without a code change).

### Changed

- README now links the project site (`data.cyberyozh.pro`) with per-service
  pages for the scraper and crawler.

## [0.1.9] - 2026-08-17

Anti-detection reliability release: a failed warmup is now visible, a blocked
page no longer waits out its selector deadline, Bing's click-tracking links are
unwrapped to their real destinations, and Amazon's throttle page is treated as
the block it is. No API change.

### Added

- Bing organic results now unwrap Bing's click-tracking redirect links
  (`bing.com/ck/a?...&u=a1<base64url>`) to the real destination URL. A field
  that should have been unwrapped but yielded nothing raises a warning instead
  of silently shipping the tracking link.

### Changed

- A failed warmup navigation is now reported (in `meta.applied_warmup`) instead
  of being indistinguishable from a request that ran no warmup at all, so a
  warmup that silently failed is visible rather than looking like a no-op.

### Fixed

- A page already classified as blocked/CAPTCHA no longer waits out the full
  `wait_for_selector` timeout for an extraction anchor it will never grow: the
  block is returned at once (roughly a 45s saving per blocked page) instead of
  after the selector deadline.
- Yandex's self-resolving browser-check interstitial is no longer mistaken for a
  hard block, so it no longer burns a proxy rotation on a page that resolves
  itself.
- Amazon's "Sorry! Something went wrong!" throttle page is now classified as a
  block, so it is rotated and retried instead of returned as a successful but
  empty fetch that pollutes results. Guarded behind a page-size ceiling so a
  normal product/search page cannot false-positive on the phrase.

## [0.1.8] - 2026-08-10

Anti-detection and reliability release: a new `fingerprint_profile` request
field pins the Camoufox OS/GPU instead of a per-launch random draw, the retry
loop now stays inside the task deadline, extraction reports a silently-nulled
column, and the queue result is a typed envelope. Two Camoufox fingerprint
tells are removed and three options are no longer silently ignored. Adds a
GitHub Actions CI pipeline. No breaking API change.

### Added

- `fingerprint_profile` on `POST /scrape/page`, `POST /scrape/pages` and the
  crawler/search `scrape_options`: pin the Camoufox fingerprint's OS and WebGL
  vendor instead of Camoufox's per-launch random draw (which claimed an OS the
  server is not two launches in three). Profiles: `auto` (default, resolves to
  `CAMOUFOX_FINGERPRINT_PROFILE`, ships `windows_on_host`), `windows_on_host`,
  `host`, `random`, and the bare names `windows` / `macos` / `linux`. The old
  `spoof_os` keeps working and equals the three bare names; a caller stating
  both must not name conflicting operating systems. `meta.applied_fingerprint`
  reports what actually ran, including when a profile degraded. New env vars
  `CAMOUFOX_FINGERPRINT_PROFILE` (default `windows_on_host`) and
  `HOST_GPU_VENDOR`.
- Extraction warns when a `post_process` pipeline nulls an entire column (every
  row) — a fourth class of silent failure the invalid/empty/length-mismatch
  selector checks did not cover. Reported only for a fully-nulled column with
  more than one row, or a scalar field; an optional value missing on a single
  row stays quiet.
- GitHub Actions CI: `pylint` and `pytest -m "not e2e"` on every push and pull
  request, the checks the repo already defined but never enforced.

### Changed

- `run_scrape` now returns a typed envelope (`ScrapeOk` / `ScrapeErr`) built as
  the same pydantic models the API validates on the way out, with `mypy` over
  the queue surface. An older worker meeting a metadata value a newer API
  introduced degrades that field and keeps the fetched page, rather than
  dropping it. Internal refactor; the public HTTP response is unchanged.
- `session_id`, `cookies` and `render` are now rejected with 422 on the
  Camoufox engine instead of being accepted and silently ignored (which
  returned a logged-out or non-rendered page that read as the site having
  changed). `/search` maps the rejection to a 400 rather than a 500.
- `WEBRTC_BLOCK` no longer deletes `RTCPeerConnection` on Camoufox: deleting the
  constructor was itself a fingerprint tell (real Firefox always has it). The
  native constructor is kept and Camoufox's `webrtc:ipv4` spoof re-enabled.
- Camoufox `geoip` is now toggleable via `CAMOUFOX_GEOIP` (default on), so the
  per-request exit-IP lookup and its credentialed cache can be turned off where
  the locale/timezone alignment is not worth the cost.

### Fixed

- The retry loop stays inside `PAGE_TASK_TIMEOUT_S`: an attempt that cannot fit
  the remaining task budget is shortened (or not started) instead of cancelled
  mid-fetch, so the block verdict, the html, the status and the retry count
  reach the caller instead of a bare `page task exceeded` timeout. A transient
  5xx is no longer promoted over a real captcha as the reported attempt.
- `parse_int` no longer fabricates numbers by stripping every non-digit and
  concatenating what is left (`4.5 out of 5` returned `455`); it takes the
  first integer with locale-correct thousands grouping (`4.5 out of 5` -> `4`,
  `12 345,67` -> `12345`).
- Camoufox window/screen geometry is now physically coherent: the screen floor
  is stated alongside the forced window, so a spoofed window is never larger
  than its own monitor (a tell in 5 of 6 launches before), and an oversized
  request no longer fails the whole scrape.

## [0.1.7] - 2026-08-04

Build fix.

### Fixed

- Pinned `mcp>=1.28.1,<2` in both `requirements.txt` and
  `yozh-crawler/requirements.txt`. `mcp` 2.0.0 made `Server.__init__`
  keyword-only, which `fastapi-mcp` 0.4.x calls positionally, so a fresh
  `docker build` shipped services that raised `TypeError` in `create_app()`
  before serving anything. The floor keeps the GHSA-vj7q-gjh5-988w fix. Build
  time only; no runtime or API change.

## [0.1.6] - 2026-07-30

Extraction and preset reliability release: repaired marketplace/SERP presets,
more robust price parsing, and a browser fix that tells a blocked page apart
from a slow one (with a session-persistence correctness fix). No API change.

### Fixed

- Preset extraction repaired for current site layouts: eBay search (new s-card
  layout, hardened price regex), Walmart (recovered price/rating; dropped
  google_shopping's dead `urls`), and Yandex search (one row per organic block,
  so titles/links/snippets no longer drift out of alignment).
- Price parsing no longer lets a label before the price swallow it: a leading
  digit is required, so `From $19.99` / `Now 19.99` parse the number, not the
  label. Currency symbols, thousands separators (US and EU), leading decimals
  and negatives still parse as before.
- Browser: a `wait_for_selector` timeout is now classified (interstitial/blocked
  vs slow-but-fine) instead of treated uniformly.
- Sessions: the storage-state persistence gate reads the fetch outcome from the
  correct result path, so cookies from a captcha page (HTTP 200 but not genuine
  content) are no longer written into a shared session and inherited later.

## [0.1.5] - 2026-07-24

Patch release: a queue retry fix plus dependency security updates.

### Fixed

- The queue no longer retries a slow page as if the proxy were bad. A
  navigation (`goto`) timeout now gets at most one proxy rotation and otherwise
  returns a timeout to the caller instead of burning the retry budget. Genuine
  proxy failures (connection reset, tunnel/auth errors, `net::ERR_*`) still
  rotate and retry as before.

### Security

- Dependency bumps clearing the outstanding advisories in auxiliary tooling:
  `examples/` (`langchain-anthropic`) and the local `scraper-tester` dev harness
  (`express`, `qs`, `http-proxy-middleware`, `follow-redirects`). No shipped
  scraper runtime or API change.

## [0.1.4] - 2026-07-22

Patch release fixing headful (Xvfb) startup after a container restart.

### Fixed

- Headful scrapes could fail silently after a `docker restart`: a stale
  `/tmp/.X99-lock` (whose recorded PID a restart made live again) made X refuse
  the display and Xvfb exit, while the old readiness check — which only tested
  that the display socket existed — still reported success. Xvfb now starts with
  `-nolock`, readiness is a real liveness signal (`-displayfd`), and startup
  fails closed with a loud warning (leaving `DISPLAY` unset) instead of silently
  handing requests a dead display. Only affects headful (`HEADLESS=false`)
  deployments.

## [0.1.3] - 2026-07-21

Security-hardening release plus a per-request launch mode and extraction/preset
reliability fixes.

### Added

- Per-request launch mode: choose headless or headful per request (`headless`),
  headless by default. Non-default modes run in a throwaway browser that is
  never pooled.
- Extraction emits a `row_alignment_mismatch` warning when parallel arrays
  (titles/prices/urls) have mismatched lengths.

### Changed

- `SERVICE_TOKEN` now gates `/proxies/resolve` (CRIT-01) and the entire
  `/sessions` surface (CRIT-02): unauthenticated callers get 401, and the
  endpoints fail closed (503) when the token is unconfigured. New required env
  var — see `.env.example`.
- Proxy resolution fails closed: a request that explicitly asks for a proxy
  which cannot be resolved now errors instead of silently falling back to a
  direct connection that would leak the real server IP (HIGH-11).
- Search market is decoupled from the proxy exit country: the Google `us`
  market can be served through a GB exit, keeping the browser fingerprint
  aligned with the exit.

### Fixed

- The `/map` SSRF guard now blocks CGNAT (`100.64.0.0/10`) and IPv4-mapped IPv6
  private addresses in addition to the standard private/reserved ranges
  (CRIT-03 A).
- The crawler treats blocked / CAPTCHA / failed scraper pages as failures
  rather than successful visits, so they are retried and no longer pollute
  crawl results or dedup (HIGH-23).
- `amazon_search` / `google_search` presets repaired so extracted rows stay
  co-indexed per result.
- Yandex: wait for the real SERP past the browser-check interstitial instead of
  capturing the interstitial page.

## [0.1.2] - 2026-07-16

Anti-bot and preset maintenance release: a hardened browser fingerprint, an
optional real Google Chrome engine, optional headful rendering via Xvfb, a GB
default for rotating residential exits, and repaired extraction selectors. The
public HTTP API contract is unchanged.

### Added

- Optional real Google Chrome engine via `channel=chrome`: launches an actual
  Chrome build (real branding/codecs, populated `navigator.plugins`) instead of
  bundled Chromium. Chromium-family only; disabled by default.
- Optional headful mode via Xvfb (`HEADLESS=false`): runs a visible browser
  under a virtual display to defeat headless-detection tells, without a physical
  screen.

### Changed

- Hardened fingerprint alignment: viewport/screen, UA version, stealth getters,
  `navigator.platform` and proxy geo are kept mutually consistent so the browser
  no longer contradicts its spoofed platform.
- Default residential rotating exit country is now `GB` instead of `US`, keeping
  the exit geo aligned with the default browser locale/timezone. Override with
  `DEFAULT_PROXY_COUNTRY`.

### Fixed

- Repaired stale extraction selectors for `google_shopping`, `youtube`,
  `linkedin`, and eBay search (`su-card` layout).
- Swapped the dead Amazon example ASIN for a live one in the README and examples.

## [0.1.1] - 2026-07-13

Maintenance release: tighter anti-bot fingerprinting, premium-proxy routing for
SERP/marketplace presets, and crawler fixes. The public HTTP API contract is
unchanged.

### Changed

- Client Hints (`Sec-CH-UA*`) and WebGL vendor/renderer are now aligned with the
  rest of the browser fingerprint, so they no longer contradict the spoofed
  platform.
- SERP and marketplace presets route through the premium proxy by default;
  presets migrated from `res_rotating` to `prem_res_rotating` (`amazon_product`,
  `amazon_search`, `bing_search`, `ebay_search`, `google_search`,
  `google_shopping`, `linkedin_profile`, `walmart_product`, `youtube_video`).
- Premium proxy v2 hardening: `provider_v2`, the username builder and the
  resolver, with schema tightening in `proxy/base` and `schemas`.
- Tester UI surfaces the applied parameters and request payload alongside the
  proxy/warmup controls.

### Fixed

- Crawler dedup and scope fixes (`dedup`, `scope`, `engine`) and `/map`.

## [0.1.0] - 2026-07-09

First tagged public release. Yozh Scraper becomes a horizontally scalable,
queue-backed service with a Firefox/Camoufox anti-bot engine, WebRTC leak
protection, and a rebuilt CyberYozh proxy integration with premium rotating
pools. The public HTTP API contract is unchanged; deployment topology and env
vars changed (see Changed / Removed).

### Added

- Camoufox (hardened Firefox) browser engine alongside Chromium, selectable
  per request via `browser_engine` with Camoufox-specific fingerprint options
  and `applied_*` fingerprint read-back. Yandex SERP routes through Camoufox
  (with `yabs` tracking-link unwrapping) to get past SmartCaptcha.
- WebRTC leak protection (`webrtc_stealth.js`): keeps the WebRTC API
  native-looking instead of deleting it, so the real IP cannot leak around the
  proxy without leaving a detectable "WebRTC removed" fingerprint. Toggle with
  `WEBRTC_BLOCK`.
- CyberYozh proxy integration v2: rebuilt client/provider with a structured
  username builder, plus premium rotating residential/mobile proxies
  (`prem_res_rotating`) with a warm-up path (`WARMUP_DWELL_MS`) that pre-loads
  pages on a cold session before the real scrape.
- Per-request `max_retries` (configurable; default 3). Proxy rotates on a
  ban / transient HTTP status, not just on CAPTCHA.
- Redis-backed session `storage_state`: logged-in sessions persist across
  container restarts and are shared across workers.
- `GET /api/v1/queue/stats` — live stream depth, in-flight count and consumers.
- Sessions API: server-side `SessionRecord` with Playwright `storage_state`
  (cookies + localStorage + sessionStorage), populated via a declarative login
  DSL (`goto`, `fill`, `click`, `wait_for_selector`, `wait_for_timeout`,
  `press_key`, `type_text`, `hover`). Endpoints under `/api/v1/sessions` for
  create / login / inspect / cookie-inject / delete, and a `session_id`
  parameter on `POST /scrape/page`, `POST /scrape/pages` and the crawler's
  `scrape_options`. Credentials are inline-only, never persisted.
- MCP exposure for all session endpoints (`create_session`, `login_session`,
  `inject_session_cookies`, `get_session`, `get_session_storage_state`,
  `delete_session`, `list_sessions`).
- Tester UI: Sessions tab (create form, visual login-script builder, cookie
  injection panel) and a session dropdown on the Scrape / Batch / Crawler tabs
  that auto-applies the session's pinned device / proxy settings.
- Example `examples/login_session_scraping.py` and an end-to-end session test.

### Changed

- Durable job queue (taskiq + Redis): the in-process job queue and browser
  worker pool are replaced by a taskiq stream on Redis. The `web-scraper`
  container only enqueues page tasks and reads results; browsers run in
  separate, horizontally scalable `scraper-worker` containers
  (`docker compose up -d --scale scraper-worker=N`). Job state, results and
  sessions live in Redis, so the API no longer loses jobs on restart, and
  same-session scrapes are serialized across workers by a distributed lock.
  Operators must now run the `redis` and `scraper-worker` services (both are in
  `docker-compose.yml`).
- `POST /scrape/page` and `/scrape/pages` now return 503 `queue_full` when the
  Redis stream depth exceeds `QUEUE_MAXSIZE` (previously unbounded).
- Workers recycle their browser after `BROWSER_MAX_PAGES` and shut Chromium
  down after idle (`BROWSER_IDLE_SHUTDOWN_S`); a reaper re-enqueues tasks
  abandoned by a dead worker (`RECLAIM_IDLE_S`). Per-container memory limits
  added in compose.
- New env vars: `REDIS_URL`, `PAGE_TASK_TIMEOUT_S`, `LOGIN_TASK_TIMEOUT_S`,
  `LOGIN_RESULT_GRACE_S`, `RECLAIM_IDLE_S`, `JOB_RESULT_TTL_S`,
  `BROWSER_MAX_PAGES`, `BROWSER_IDLE_SHUTDOWN_S`, `WARMUP_DWELL_MS`.

### Removed

- Env vars `JOB_TIMEOUT_MS` (use `PAGE_TASK_TIMEOUT_S`), `JOB_RESULT_MAX`
  (eviction is now a native Redis TTL, `JOB_RESULT_TTL_S`) and `JOBS_ENABLED`
  (the queue is always on). They are ignored with a startup warning if set.
- The in-memory worker pool and jobs module (superseded by the Redis queue).

### Fixed

- Authenticated SOCKS5 proxies (CyberYozh residential / mobile) now work on the
  login path too, via a shared HTTP-to-SOCKS5 bridge (`resolve_proxy()`).
- `storageState.cookies[].expires` is dropped when Playwright emits
  `-1` / `NaN` / `inf`, instead of being written as `null` and failing the
  next scrape against the session.
- LoginRunner redacts substituted credential values from `result.error`, so a
  Playwright error can no longer echo the resolved password to the client.
- Preset & SERP fixes: Yandex `lr` localisation, stealth disabled on
  `google_shopping` to stop the CAPTCHA, `walmart_product` / `youtube_video`
  fixed for available proxies, `/showcaptcha` SmartCaptcha detection, and a
  time-bounded map seed render so a stall cannot block `/map`.
