// ─── Proxy component constants ───────────────────────────────────────────────
// Declared at the very top because the first renderProxyComponent() call runs at
// top level in the Batch-tab init (well before the factory section); a `const`
// next to the factory would be in its temporal dead zone at that call and throw
// "Cannot access 'PROXY_TYPES' before initialization", aborting all init.
//
// Canonical proxy-type option list. Legacy types keep their plain values (the
// server still accepts them); labels mark the v2 premium gateway and tag the
// legacy CyberYozh pools so it's obvious which one needs a purchased pool id.
// Only res_rotating is legacy — it's superseded by the premium gateway
// (prem_res_rotating). res_static / mobile / mobile_shared / dc_static are
// still current products, so they carry no marker.
const PROXY_TYPES = [
  { value: 'none', label: 'none' },
  { value: 'prem_res_rotating', label: 'prem_res_rotating (premium)' },
  { value: 'res_rotating', label: 'res_rotating (legacy)' },
  { value: 'res_static', label: 'res_static' },
  { value: 'mobile', label: 'mobile' },
  { value: 'mobile_shared', label: 'mobile_shared' },
  { value: 'dc_static', label: 'dc_static' },
];

// Fallback ip_filter set — the canonical PremProxyOptions.ip_filter enum. Used
// only when /session-options is unreachable or omits `ip_filters`, so the
// dropdown is never empty (loadPremCatalogs prefers the live list).
const PREM_IP_FILTER_FALLBACK = [
  'max-size-security', 'max-speed-security',
  'quality-security', 'speed-quality-security',
];

// ─── State ───────────────────────────────────────────────────────────────────
const JOBS_STORAGE_KEY = 'scraper-tester:recent-jobs';
let recentJobs = [];
try {
  const saved = localStorage.getItem(JOBS_STORAGE_KEY);
  if (saved) recentJobs = JSON.parse(saved);
} catch {}

function saveRecentJobs() {
  try { localStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(recentJobs)); }
  catch {}
}

// Request payloads we submitted this client, keyed by job_id. Lets the result
// view echo the exact request that produced a job — including params the
// response meta doesn't carry back (e.g. session_id, extract rules). Capped to
// match the recent-jobs window so localStorage doesn't grow unbounded.
const SENT_PAYLOADS_KEY = 'scraper-tester:sent-payloads';
let sentPayloads = {};
try {
  const saved = localStorage.getItem(SENT_PAYLOADS_KEY);
  if (saved) sentPayloads = JSON.parse(saved);
} catch {}

// Strip credential-bearing values (cookies, header values) before a payload is
// persisted to localStorage — keep the keys/shape visible for debugging, but
// don't write auth tokens / cookies to disk-backed storage.
function redactSensitive(payload) {
  if (!payload || typeof payload !== 'object') return payload;
  const clone = Array.isArray(payload) ? payload.map(redactSensitive) : { ...payload };
  if (Array.isArray(clone.pages)) clone.pages = clone.pages.map(redactSensitive);
  if (clone.headers && typeof clone.headers === 'object') {
    clone.headers = Object.fromEntries(
      Object.keys(clone.headers).map(k => [k, '[redacted]'])
    );
  }
  if (clone.cookies != null) {
    const n = Array.isArray(clone.cookies) ? clone.cookies.length : 1;
    clone.cookies = `[${n} cookie(s) redacted]`;
  }
  return clone;
}

function rememberSentPayload(jobId, payload) {
  if (!jobId) return;
  sentPayloads[jobId] = redactSensitive(payload);
  // Keep only the payloads for jobs still in the recent-jobs list (plus this
  // one), so the two stores stay roughly in sync and bounded.
  const keep = new Set(recentJobs.map(j => j.id));
  keep.add(jobId);
  for (const id of Object.keys(sentPayloads)) {
    if (!keep.has(id)) delete sentPayloads[id];
  }
  try { localStorage.setItem(SENT_PAYLOADS_KEY, JSON.stringify(sentPayloads)); }
  catch {}
}

function normalizeUrl(raw) {
  const s = String(raw || '').trim();
  if (!s) return '';
  try {
    const u = new URL(s);
    if (u.protocol === 'http:' || u.protocol === 'https:') return s;
  } catch {}
  return 'https://' + s.replace(/^\/+/, '');
}

let cachedWorkers = 2;
async function loadServerConfig() {
  try {
    const { ok, data } = await apiCall('/api/v1/health');
    if (ok && data?.workers) cachedWorkers = Number(data.workers) || 2;
  } catch {}
}

async function loadCountries() {
  const selects = ['s-geo-country', 'b-geo-country', 'c-geo-country', 'cp-geo-country', 'pw-geo-country', 'sess-geo-country', 'se-geo-country', 'mp-geo-country']
    .map(id => document.getElementById(id)).filter(Boolean);
  if (!selects.length) return;

  const { ok, data } = await apiCall('/api/v1/proxies/countries');
  if (!ok || !data?.countries) {
    // Surface the failure: a silent return here is what hid the wrong-host bug.
    console.warn('loadCountries: could not load proxy countries (geo dropdowns stay empty)');
    return;
  }

  const sorted = [...data.countries].sort((a, b) => a.name.localeCompare(b.name));
  const optionsHtml = '<option value="">— any —</option>' +
    sorted.map(c => `<option value="${escapeHtml(c.code)}">${escapeHtml(c.name)} (${escapeHtml(c.code)})</option>`).join('');

  selects.forEach(sel => {
    const prev = sel.value;
    sel.innerHTML = optionsHtml;
    if (prev) sel.value = prev;
  });
}
// NB: countries are loaded after restoreState() (below), once the Scraper URL
// points at the real target — calling it here would race the URL restore and
// hit the wrong host (404), leaving every geo dropdown empty.

// Populate the Scrape Page "Preset" dropdown (loads a preset's CSS/XPath
// fields into the Extraction form — selectors only; URL/proxy stay the
// user's). Refreshed after create/delete so new presets appear.
async function populateScrapePresetSelect() {
  const sel = document.getElementById('s-extract-preset');
  if (!sel) return;
  const { ok, data } = await apiCall('/api/v1/presets');
  if (!ok || !data?.items) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">— none —</option>' +
    data.items.map(p => {
      const d = presetFaviconDomain(p);
      const fav = d
        ? ` data-favicon="${escapeHtml('https://www.google.com/s2/favicons?domain=' + encodeURIComponent(d) + '&sz=32')}"`
        : '';
      return `<option value="${escapeHtml(p.name)}"${fav}>${escapeHtml(p.name)} (${escapeHtml(p.kind)})</option>`;
    }).join('');
  if (prev) sel.value = prev;
}

let _restoringState = false;

const _sExtractPreset = document.getElementById('s-extract-preset');
if (_sExtractPreset) _sExtractPreset.addEventListener('change', async (e) => {
  // restoreState() re-dispatches a synthetic `change` on every select[id] to
  // replay saved UI state. Ignore ONLY that replay (it would refetch, clobber
  // the rows restoreState just restored, and pop a spurious alert on load).
  // Keyed on the restore flag, not e.isTrusted, so genuine programmatic
  // selection (tests, automation) still works like a real click.
  if (_restoringState) return;
  const name = e.target.value;
  if (!name) return;
  const { ok, data } = await apiCall(`/api/v1/presets/${encodeURIComponent(name)}`);
  if (!ok || !data) { alert('Failed to load preset'); e.target.value = ''; return; }
  const pi = data.parsing_instructions;
  if (!pi || !pi.fields || !Object.keys(pi.fields).length) {
    alert(`Preset "${name}" has no deterministic selectors (AI-only) — ` +
          `it can't be loaded into the Scrape Page form. Run it via the ` +
          `preset API (POST /api/v1/scrape/preset/page).`);
    e.target.value = '';
    return;
  }
  document.getElementById('s-extract-type').value = pi.type || 'css';
  document.getElementById('s-extract-container').value = pi.container || '';
  const container = document.getElementById('extract-fields');
  container.innerHTML = '';
  let lossy = false;  // post_process or per-field type can't ride the raw row UI
  for (const [fname, fr] of Object.entries(pi.fields)) {
    addExtractField('extract-fields');
    const row = container.lastElementChild;
    const inputs = row.querySelectorAll('input[type="text"]');
    inputs[0].value = fname;
    inputs[1].value = fr.selector || '';
    const attrSel = row.querySelector('select');
    const attr = fr.attr || 'text';
    if (![...attrSel.options].some(o => o.value === attr)) {
      attrSel.add(new Option(attr, attr));  // e.g. value / content
    }
    attrSel.value = attr;
    row.querySelector('input[type="checkbox"]').checked = !!fr.all;
    if ((fr.post_process && fr.post_process.length) || fr.type) lossy = true;
  }
  if (lossy) {
    alert(`Loaded ${Object.keys(pi.fields).length} selectors from "${name}". ` +
          `This preset also uses post_process and/or per-field selector types ` +
          `which the raw Scrape Page does not apply — use Presets → Preset ` +
          `Builder → Verify to preview full-fidelity extraction.`);
  }

  // Apply the preset's request profile too (device / wait / render / proxy
  // / geo). The URL stays the user's; proxy_pool_id is per-account so it is
  // intentionally not set (changing proxy_type reloads the pool dropdown).
  const rd = data.request_defaults || {};
  const setVal = (id, v) => {
    if (v === undefined || v === null) return;
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = !!v;
    else el.value = v;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  setVal('s-device', rd.device);
  setVal('s-wait-until', rd.wait_until);
  setVal('s-timeout', rd.timeout_ms);
  setVal('s-wait-selector', rd.wait_for_selector);
  setVal('s-render', rd.render);
  setVal('s-stealth', rd.stealth);
  setVal('s-block-assets', rd.block_assets);
  setVal('s-headless', rd.headless === undefined || rd.headless === null ? '' : String(rd.headless));
  setVal('s-proxy-type', rd.proxy_type);
  if (rd.proxy_geo) {
    setVal('s-geo-country', rd.proxy_geo.country_code);
    setVal('s-geo-region', rd.proxy_geo.region);
    setVal('s-geo-city', rd.proxy_geo.city);
  }
});

let mcpSessionId = null;

// ─── Helpers ─────────────────────────────────────────────────────────────────
function scraperUrl() {
  return document.getElementById('scraperUrl').value.replace(/\/$/, '');
}

async function apiCall(path, options = {}) {
  const url = `/proxy${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'x-scraper-target': scraperUrl(),
      ...(options.headers || {}),
    },
  });
  const text = await res.text();
  try { return { ok: res.ok, status: res.status, data: JSON.parse(text) }; }
  catch { return { ok: res.ok, status: res.status, data: text }; }
}

function syntaxHighlight(obj) {
  // Escape &<> BEFORE tokenizing: this string is assigned via innerHTML and
  // now renders attacker-influenced content (LLM output + data extracted
  // from an arbitrary sample page via /presets/test and /presets/generate).
  // Quotes are intentionally left intact so the JSON tokenizer regex below
  // still matches "..." string bodies.
  const json = escapeHtml(JSON.stringify(obj, null, 2));
  return json.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    (match) => {
      let cls = 'json-num';
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? 'json-key' : 'json-str';
      } else if (/true|false/.test(match)) {
        cls = 'json-bool';
      } else if (/null/.test(match)) {
        cls = 'json-null';
      }
      return `<span class="${cls}">${match}</span>`;
    }
  );
}

// Collapsible markdown block with a Rendered/Source toggle + Copy. Rendered
// view uses the vendored `marked`; if it's unavailable we degrade to source.
function makeMarkdownDetails(openKeys, key, label, text) {
  const wrap = document.createElement('details');
  wrap.dataset.key = key;
  if (openKeys.has(key)) wrap.setAttribute('open', '');
  wrap.style.cssText = 'margin-bottom:0.5rem';

  const summary = document.createElement('summary');
  summary.className = 'details-summary';
  summary.style.cssText = 'cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none';
  summary.innerHTML = `<span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span style="flex:1">${label} (${text.length} chars)</span>`;

  const rendered = document.createElement('div');
  rendered.style.cssText = 'max-height:400px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);border-top:none;padding:0.75rem;font-size:13px;border-radius:0 0 var(--radius-sm) var(--radius-sm);color:var(--text-primary)';
  const source = document.createElement('pre');
  source.style.cssText = 'max-height:400px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);border-top:none;padding:0.75rem;font-size:11px;border-radius:0 0 var(--radius-sm) var(--radius-sm);color:var(--text-primary);white-space:pre-wrap;word-break:break-word;display:none';
  source.textContent = text;
  // Markdown is built from arbitrary scraped pages and marked passes raw
  // HTML through, so the rendered view must go through DOMPurify.
  if (window.marked && window.DOMPurify) {
    rendered.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text), {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['style'],  // keep hostile pages from restyling the tester UI
    });
  } else {
    rendered.textContent = text;  // graceful fallback when vendor libs are offline
  }

  const btnToggle = document.createElement('button');
  btnToggle.textContent = 'Source';
  btnToggle.className = 'btn-secondary btn-sm';
  btnToggle.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const showSrc = source.style.display === 'none';
    source.style.display = showSrc ? 'block' : 'none';
    rendered.style.display = showSrc ? 'none' : 'block';
    btnToggle.textContent = showSrc ? 'Rendered' : 'Source';
  });

  const btnCopy = document.createElement('button');
  btnCopy.textContent = 'Copy';
  btnCopy.className = 'btn-secondary btn-sm';
  btnCopy.addEventListener('click', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      btnCopy.textContent = 'Copied!';
      setTimeout(() => { btnCopy.textContent = 'Copy'; }, 1200);
    } catch (err) {
      btnCopy.textContent = 'Failed';
    }
  });

  summary.appendChild(btnToggle);
  summary.appendChild(btnCopy);
  wrap.appendChild(summary);
  wrap.appendChild(rendered);
  wrap.appendChild(source);
  return wrap;
}

// Collapsible block with a syntax-highlighted JSON body, matching the
// markdown/raw-html details styling. `open: true` forces it expanded; otherwise
// it restores the previous open state via openKeys.
function makeJsonDetails(openKeys, key, label, obj, { open = false } = {}) {
  const wrap = document.createElement('details');
  wrap.dataset.key = key;
  if (open || openKeys.has(key)) wrap.setAttribute('open', '');
  wrap.style.cssText = 'margin-bottom:0.5rem';

  const summary = document.createElement('summary');
  summary.className = 'details-summary';
  summary.style.cssText = 'cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none';
  summary.innerHTML = `<span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span>${label}</span>`;

  const pre = document.createElement('pre');
  pre.style.cssText = 'max-height:400px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);border-top:none;padding:0.75rem;font-size:12px;border-radius:0 0 var(--radius-sm) var(--radius-sm);color:var(--text-primary);white-space:pre-wrap;word-break:break-word';
  pre.innerHTML = syntaxHighlight(obj);

  wrap.appendChild(summary);
  wrap.appendChild(pre);
  return wrap;
}

// Collapsible "Request payload" block — the exact body we POSTed for this job,
// looked up by job_id. Shows params the response meta can't echo (session_id,
// extract rules, etc.). Returns null when we have no record for this job.
function makeSentPayloadDetails(data, openKeys) {
  const jobId = data?.job_id;
  if (!jobId || !sentPayloads[jobId]) return null;
  return makeJsonDetails(openKeys, 'sent-payload', 'Request payload (as submitted)', sentPayloads[jobId]);
}

function showResult(elId, data) {
  const el = document.getElementById(elId);

  // Preserve open/close state of <details> across re-renders by their
  // data-key attribute. Also remember scroll position.
  const openKeys = new Set(
    Array.from(el.querySelectorAll('details[open][data-key]')).map(d => d.dataset.key)
  );
  const prevScroll = el.scrollTop;

  el.innerHTML = '';

  if (typeof data === 'string') {
    el.innerHTML = `<span style="color:var(--text)">${escapeHtml(data)}</span>`;
    return;
  }

  // Echo the request we submitted for this job (if we have it on record).
  const sentBlock = makeSentPayloadDetails(data, openKeys);
  if (sentBlock) el.appendChild(sentBlock);

  // For scrape job results — show summary card + separate content blocks
  if (data?.results && Array.isArray(data.results)) {
    // Summary block (without raw_html/screenshot/data payloads)
    const summary = {
      job_id: data.job_id,
      status: data.status,
      total: data.total,
      done: data.done,
      error: data.error,
      results: data.results.map((r, i) => r === null ? { slot: i + 1, status: 'pending' } : ({
        request_id: r.request_id,
        took_ms: r.took_ms,
        meta: r.meta,
        data: r.data ? '[see below]' : null,
        warnings: r.warnings,
        raw_html: r.raw_html ? `[${r.raw_html.length} chars — see below]` : null,
        screenshot_base64: r.screenshot_base64 ? '[see below]' : null,
      })),
    };

    const pre = document.createElement('pre');
    pre.innerHTML = syntaxHighlight(summary);
    el.appendChild(pre);

    // Per-result content blocks
    data.results.forEach((r, i) => {
      if (r === null) {
        const placeholder = document.createElement('div');
        placeholder.style.cssText = 'margin:0.75rem 0 0.4rem;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary)';
        placeholder.innerHTML = `<b style="color:var(--text-primary)">Result #${i+1}</b> &nbsp;&mdash;&nbsp; <i>pending&hellip;</i>`;
        el.appendChild(placeholder);
        return;
      }
      // Proxy info badge
      const meta = r.meta || {};
      const proxyBadge = document.createElement('div');
      proxyBadge.style.cssText = 'margin:0.75rem 0 0.4rem;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary)';

      const fetchFailed = meta.fetch_ok === false;
      const line1 = `<b style="color:var(--text-primary)">Result #${i+1}</b> &nbsp;|&nbsp; proxy: <b style="color:var(--color-blue)">${escapeHtml(meta.proxy_type||'—')}</b>${meta.proxy_pool_id ? ` / pool: ${escapeHtml(meta.proxy_pool_id)}` : ''} &nbsp;|&nbsp; status: <b style="color:var(--color-green)">${meta.status_code||'—'}</b> &nbsp;|&nbsp; retries: ${meta.retries??'—'} &nbsp;|&nbsp; took: ${r.took_ms}ms${fetchFailed ? ' &nbsp;|&nbsp; <b style="color:var(--color-red)">⚠ fetch failed</b>' : ''}`;

      // Second line: applied request params (only what's present). prem_targeting
      // and warmup come from the new ScrapeMeta echo; locale/tz/ua were always there.
      const applied = [];
      if (meta.applied_prem_targeting) applied.push(`prem: <b style="color:var(--color-purple)">${escapeHtml(meta.applied_prem_targeting)}</b>`);
      if (meta.applied_warmup) {
        const w = meta.applied_warmup;
        const label = w.url || w.type || 'on';
        const dwell = w.dwell_ms != null ? ` (${w.dwell_ms}ms)` : '';
        applied.push(`warmup: <b style="color:var(--color-reef)">${escapeHtml(label)}${dwell}</b>`);
      }
      if (meta.applied_locale) applied.push(`locale: ${escapeHtml(meta.applied_locale)}`);
      if (meta.applied_timezone) applied.push(`tz: ${escapeHtml(meta.applied_timezone)}`);
      if (meta.applied_user_agent) {
        const ua = meta.applied_user_agent;
        applied.push(`ua: <span title="${escapeHtml(ua)}">${escapeHtml(ua.length > 48 ? ua.slice(0, 48) + '…' : ua)}</span>`);
      }
      const line2 = applied.length ? `<div style="margin-top:0.35rem">${applied.join(' &nbsp;|&nbsp; ')}</div>` : '';

      proxyBadge.innerHTML = line1 + line2;
      el.appendChild(proxyBadge);

      // Extracted data
      if (r.data && typeof r.data === 'object' && Object.keys(r.data).length > 0) {
        const fieldCount = Object.keys(r.data).length;
        el.appendChild(makeJsonDetails(
          openKeys, `data-${r.request_id}`,
          `Extracted Data (${fieldCount} field${fieldCount === 1 ? '' : 's'})`,
          r.data, { open: true },
        ));
      }

      // Raw HTML
      if (r.raw_html) {
        const wrap = document.createElement('details');
        wrap.dataset.key = `html-${r.request_id}`;
        if (openKeys.has(wrap.dataset.key)) wrap.setAttribute('open', '');
        wrap.style.cssText = 'margin-bottom:0.5rem';

        const summary = document.createElement('summary');
        summary.className = 'details-summary';
        summary.style.cssText = 'cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none';
        summary.innerHTML = `<span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span style="flex:1">Raw HTML (${r.raw_html.length} chars)</span>`;

        const btnCopy = document.createElement('button');
        btnCopy.textContent = 'Copy';
        btnCopy.className = 'btn-secondary btn-sm';
        btnCopy.addEventListener('click', async (e) => {
          e.preventDefault();
          e.stopPropagation();
          try {
            await navigator.clipboard.writeText(r.raw_html);
            const orig = btnCopy.textContent;
            btnCopy.textContent = 'Copied!';
            setTimeout(() => { btnCopy.textContent = orig; }, 1200);
          } catch (err) {
            btnCopy.textContent = 'Failed';
          }
        });

        const btnDownload = document.createElement('button');
        btnDownload.textContent = 'Download';
        btnDownload.className = 'btn-secondary btn-sm';
        btnDownload.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          const blob = new Blob([r.raw_html], { type: 'text/html;charset=utf-8' });
          const href = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = href;
          const safeUrl = (meta.final_url || meta.url || 'page').replace(/[^a-z0-9]+/gi, '_').slice(0, 60);
          a.download = `${safeUrl}_${r.request_id}.html`;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(href);
        });

        summary.appendChild(btnCopy);
        summary.appendChild(btnDownload);
        wrap.appendChild(summary);

        const code = document.createElement('pre');
        code.style.cssText = 'max-height:400px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);border-top:none;padding:0.75rem;font-size:11px;border-radius:0 0 var(--radius-sm) var(--radius-sm);color:var(--text-primary)';
        code.textContent = r.raw_html;
        wrap.appendChild(code);
        el.appendChild(wrap);
      }

      // Markdown / Fit Markdown
      if (r.markdown) {
        el.appendChild(makeMarkdownDetails(openKeys, `md-${r.request_id}`, 'Markdown', r.markdown));
      }
      if (r.fit_markdown) {
        el.appendChild(makeMarkdownDetails(openKeys, `fitmd-${r.request_id}`, 'Fit Markdown', r.fit_markdown));
      }
      if (r.markdown_references) {
        el.appendChild(makeMarkdownDetails(openKeys, `mdref-${r.request_id}`, 'References', r.markdown_references));
      }
      if (Array.isArray(r.links) && r.links.length) {
        const wrap = document.createElement('details');
        wrap.dataset.key = `links-${r.request_id}`;
        if (openKeys.has(wrap.dataset.key)) wrap.setAttribute('open', '');
        wrap.style.cssText = 'margin-bottom:0.5rem';
        wrap.innerHTML = `<summary class="details-summary" style="cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none"><span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span>Links (${r.links.length})</span></summary>`;
        const pre = document.createElement('pre');
        pre.style.cssText = 'max-height:400px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);border-top:none;padding:0.75rem;font-size:11px;border-radius:0 0 var(--radius-sm) var(--radius-sm);color:var(--text-primary);white-space:pre-wrap;word-break:break-word';
        pre.textContent = r.links.join('\n');
        wrap.appendChild(pre);
        el.appendChild(wrap);
      }

      // Screenshot
      if (r.screenshot_base64) {
        const wrap = document.createElement('details');
        wrap.dataset.key = `shot-${r.request_id}`;
        // Default open on first render; otherwise preserve user's state
        if (openKeys.size === 0 || openKeys.has(wrap.dataset.key)) {
          wrap.setAttribute('open', '');
        }
        wrap.style.cssText = 'margin-bottom:0.5rem';
        wrap.innerHTML = `<summary class="details-summary" style="cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none"><span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span>Screenshot</span></summary>`;
        const img = document.createElement('img');
        img.src = `data:image/png;base64,${r.screenshot_base64}`;
        img.style.cssText = 'width:100%;display:block;border:1px solid var(--border);border-top:none;border-radius:0 0 var(--radius-sm) var(--radius-sm)';
        wrap.appendChild(img);
        el.appendChild(wrap);
      }
    });
    el.scrollTop = prevScroll;
    return;
  }

  // Default JSON display
  const pre = document.createElement('pre');
  pre.innerHTML = syntaxHighlight(data);
  el.appendChild(pre);
  el.scrollTop = prevScroll;
}

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function setStatus(elId, status, message) {
  const el = document.getElementById(elId);
  el.className = `status-bar ${status}`;
  const spinner = (status === 'running' || status === 'queued')
    ? '<div class="spinner"></div>' : '';
  el.innerHTML = `${spinner}<span>${escapeHtml(message)}</span>`;
}

function addRecentJob(jobId, status) {
  const existing = recentJobs.find(j => j.id === jobId);
  const ts = Date.now();
  if (existing) {
    existing.status = status;
    existing.ts = ts;
  } else {
    recentJobs.unshift({ id: jobId, status, ts });
  }
  if (recentJobs.length > 20) recentJobs.pop();
  saveRecentJobs();
  renderRecentJobs();
}

function renderRecentJobs() {
  const el = document.getElementById('recent-jobs');
  el.innerHTML = recentJobs.map(j => `
    <span class="job-chip" data-id="${escapeHtml(j.id)}">
      <span class="dot dot-${j.status}"></span>
      ${escapeHtml(j.id)}
    </span>`).join('');
  el.querySelectorAll('.job-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const jobId = chip.dataset.id;
      document.getElementById('j-job-id').value = jobId;
      // Show the result immediately — /results is safe to poll in any state
      // (returns the current status with null results while still running).
      lookupJobResults(jobId);
    });
  });
}

async function lookupJobResults(jobId) {
  const { data } = await apiCall(`/api/v1/scrape/${jobId}/results`);
  showResult('jobs-result', data);
  if (data?.status) {
    addRecentJob(jobId, data.status);
    updateJobCancelButton(jobId, data.status);
  }
}

async function pollJobBatch(jobId, statusElId, resultElId, totalExpected, cancelBtnId) {
  setStatus(statusElId, 'queued', `Job queued: ${jobId}`);
  addRecentJob(jobId, 'queued');

  const el = document.getElementById(statusElId);
  setScrapeCancelButton(cancelBtnId, 'armed', jobId);

  while (true) {
    await new Promise(r => setTimeout(r, 1000));
    const { data } = await apiCall(`/api/v1/scrape/${jobId}`);

    if (!data || data.detail) {
      setStatus(statusElId, 'failed', `Error polling job`);
      addRecentJob(jobId, 'failed');
      showResult(resultElId, data);
      setScrapeCancelButton(cancelBtnId, 'done');
      return;
    }

    addRecentJob(jobId, data.status);

    if (data.status === 'queued' || data.status === 'running') {
      const total = data.total ?? totalExpected ?? 0;
      const done = data.done ?? 0;
      const remaining = Math.max(0, total - done);
      const running = data.status === 'running' ? Math.min(cachedWorkers, remaining) : 0;
      const queued = Math.max(0, remaining - running);
      const pct = total > 0 ? Math.round((done / total) * 100) : 0;

      el.className = `status-bar ${data.status}`;
      el.innerHTML = `
        <div class="spinner"></div>
        <div style="flex:1;display:flex;flex-direction:column;gap:4px">
          <div style="display:flex;gap:0.75rem;align-items:center;font-size:13px">
            <span><b>${done}</b> done</span>
            <span>·</span>
            <span><b>${running}</b> running</span>
            <span>·</span>
            <span><b>${queued}</b> queued</span>
            <span style="margin-left:auto;color:var(--text-secondary);font-size:11px">${pct}%</span>
          </div>
          <div style="height:6px;background:var(--neutral-200);border-radius:3px;overflow:hidden">
            <div style="height:100%;width:${pct}%;background:currentColor;transition:width 0.3s"></div>
          </div>
        </div>`;

      // Show partial results as they complete
      if (done > 0) {
        const res = await apiCall(`/api/v1/scrape/${jobId}/results`);
        if (res.ok && res.data?.results) {
          showResult(resultElId, res.data);
        }
      }
      continue;
    }

    if (data.status === 'done') {
      setStatus(statusElId, 'done', `Done in ${data.done} pages`);
      const res = await apiCall(`/api/v1/scrape/${jobId}/results`);
      showResult(resultElId, res.data);
      setScrapeCancelButton(cancelBtnId, 'done');
      return;
    }

    if (data.status === 'cancelled') {
      setStatus(statusElId, 'failed', `Cancelled (${data.done}/${data.total} completed)`);
      const res = await apiCall(`/api/v1/scrape/${jobId}/results`);
      showResult(resultElId, res.data);
      setScrapeCancelButton(cancelBtnId, 'done');
      return;
    }

    if (data.status === 'failed') {
      setStatus(statusElId, 'failed', `Failed: ${data.error || 'unknown error'}`);
      showResult(resultElId, data);
      setScrapeCancelButton(cancelBtnId, 'done');
      return;
    }
  }
}

// ─── Scrape/Batch/Job cancel button state machine ────────────────────────────
// State: ready (hidden) | armed (shown, first click = soft cancel) | done (hidden)
const _scrapeJobIdByBtn = new Map();
function setScrapeCancelButton(btnId, state, jobId) {
  if (!btnId) return;
  const btn = document.getElementById(btnId);
  if (!btn) return;
  if (state === 'armed') {
    _scrapeJobIdByBtn.set(btnId, jobId);
    btn.style.display = '';
    btn.disabled = false;
    btn.textContent = 'Cancel';
    btn.classList.remove('btn-primary');
    btn.classList.add('btn-secondary');
  } else if (state === 'cancelling') {
    btn.disabled = true;
    btn.textContent = 'Cancelling…';
  } else {
    _scrapeJobIdByBtn.delete(btnId);
    btn.style.display = 'none';
    btn.disabled = true;
  }
}
async function handleScrapeCancelClick(btnId) {
  const jobId = _scrapeJobIdByBtn.get(btnId);
  if (!jobId) return;
  setScrapeCancelButton(btnId, 'cancelling');
  await apiCall(`/api/v1/scrape/${jobId}`, { method: 'DELETE' });
  // poller picks up the cancelled status on its next tick and hides the button.
}
['btnCancelScrape', 'btnCancelBatch', 'btnCancelJob'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('click', () => handleScrapeCancelClick(id));
});

// Legacy alias — all flows use the batch-style poller which supports partial
// results and preserves expand/collapse state across polls.
const pollJob = pollJobBatch;

// ─── Device compatibility ─────────────────────────────────────────────────────
// `render` is `java_script_enabled` on the context, so Legacy WAP with it on
// ships a feature phone that executes modern JS — the API answers 422. Caught
// here for the same reason as the camoufox pairing below: the four forms that
// offer both controls can state it before the request instead of after.
function warnIfDeviceNeedsNoRender(devId, renderId) {
  const dev = document.getElementById(devId);
  const render = document.getElementById(renderId);
  if (!dev || !render) return true;
  if (dev.value === 'legacy_wap' && render.checked) {
    alert('device=legacy_wap requires JS Render to be OFF: the identity ' +
          'claims a phone that predates JavaScript, and the no-JS layout it ' +
          'exists for needs no rendering. Untick JS Render.');
    return false;
  }
  return true;
}

// ─── Device / engine compatibility ────────────────────────────────────────────
// The API refuses `device=legacy_wap` with camoufox, which owns its own
// fingerprint and would keep a Firefox identity while the request claimed a
// feature phone. There is no UI guard for the older `mobile` + firefox rule
// either, so a caller meets both as a 422 — worth saying here first, since
// this pair is the one people will reach for while testing /wml by hand.
function warnIfDeviceEngineClash(devId, engId) {
  const dev = document.getElementById(devId);
  const eng = document.getElementById(engId);
  if (!dev || !eng) return true;
  if (dev.value === 'legacy_wap' && eng.value === 'camoufox') {
    alert('device=legacy_wap is not supported with browser_engine=camoufox: ' +
          'Camoufox owns its fingerprint and would keep its Firefox identity ' +
          'while the request claimed a feature phone. Use chromium, firefox ' +
          'or webkit.');
    return false;
  }
  return true;
}

// ─── Row container ────────────────────────────────────────────────────────────
// `container` is a RULE-level field: set it and every selector below is matched
// inside each row, one value per row. Read here rather than per field row,
// because that is the shape the API takes — and because a per-field version
// would let two columns disagree about what a row is, which is the defect the
// feature removes.
function readContainer(inputId, fields) {
  const el = document.getElementById(inputId);
  const value = el ? el.value.trim() : '';
  if (!value) return null;
  const notAll = Object.entries(fields).filter(([, f]) => !f.all).map(([n]) => n);
  if (notAll.length) {
    alert(`Container is set, so every field is a column: tick "all" for ` +
          `${notAll.join(', ')} — or clear the container.`);
    return undefined;  // distinct from "not set": abort the submit
  }
  return value;
}

// ─── Header Presets ───────────────────────────────────────────────────────────
// Identity headers (`User-Agent`, `sec-ch-ua*`) are deliberately absent: the
// engine states the only identity its transport can back, and the API drops
// them from `headers` on every engine — measured, a Symbian UA sent this way
// never reached the wire on chromium and DID reach it on firefox/webkit,
// over the wrong TLS with no Client Hints. Shipping them here would be a
// control that does nothing and reports `ignored_request_field` for its
// trouble. Read `meta.applied_user_agent` for what actually went out; pick
// the identity with Engine + Device instead.
const HEADER_PRESETS = {
  en_us: {
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
  },
  ru_locale: {
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
  },
  de_locale: {
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
  },
  privacy: {
    'Accept-Language': 'en-US,en;q=0.9',
    'DNT': '1',
    'Sec-GPC': '1',
  },
  navigation: {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'none',
    'sec-fetch-user': '?1',
    'upgrade-insecure-requests': '1',
  },
};


function applyHeaderPreset(presetKey) {
  const headers = HEADER_PRESETS[presetKey];
  if (!headers) return;
  document.getElementById('headers-list').innerHTML = '';
  Object.entries(headers).forEach(([k, v]) => {
    addDynamicRow('headers-list', ['Header name', 'Value']);
    const rows = document.querySelectorAll('#headers-list .dynamic-row');
    const last = rows[rows.length - 1];
    const inputs = last.querySelectorAll('input');
    inputs[0].value = k;
    inputs[1].value = v;
  });
  // No form-factor flip any more: the presets no longer claim a device, so
  // forcing the Device selector from a header choice would override a
  // deliberate one. Device is the control that actually decides the identity.
}

document.getElementById('btnApplyPreset').addEventListener('click', () => {
  const val = document.getElementById('header-preset').value;
  if (val) applyHeaderPreset(val);
});

document.getElementById('btnClearHeaders').addEventListener('click', () => {
  document.getElementById('headers-list').innerHTML = '';
  document.getElementById('header-preset').value = '';
});

// Batch variants
function applyHeaderPresetBatch(presetKey) {
  const headers = HEADER_PRESETS[presetKey];
  if (!headers) return;
  document.getElementById('b-headers-list').innerHTML = '';
  Object.entries(headers).forEach(([k, v]) => {
    addDynamicRow('b-headers-list', ['Header name', 'Value']);
    const rows = document.querySelectorAll('#b-headers-list .dynamic-row');
    const last = rows[rows.length - 1];
    const inputs = last.querySelectorAll('input');
    inputs[0].value = k;
    inputs[1].value = v;
  });
}

document.getElementById('btnApplyPresetBatch').addEventListener('click', () => {
  const val = document.getElementById('b-header-preset').value;
  if (val) applyHeaderPresetBatch(val);
});

document.getElementById('btnClearHeadersBatch').addEventListener('click', () => {
  document.getElementById('b-headers-list').innerHTML = '';
  document.getElementById('b-header-preset').value = '';
});

document.getElementById('btnAddHeaderBatch').addEventListener('click', () =>
  addDynamicRow('b-headers-list', ['Header name', 'Value']));

document.getElementById('btnAddCookieBatch').addEventListener('click', () =>
  addDynamicRow('b-cookies-list', ['name', 'value', 'domain', 'path']));

// ─── Tabs ─────────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
  });
});

// ─── Health Check ─────────────────────────────────────────────────────────────
// One button checks both services in parallel; each gets its own mini-badge so
// it's still clear which side is down.
async function checkServiceHealth(badge, call, okText) {
  badge.className = 'badge';
  badge.textContent = '…';
  try {
    const { data, ok } = await call('/api/v1/health');
    if (ok && data?.status === 'ok') {
      badge.className = 'badge ok';
      badge.textContent = okText(data);
    } else {
      badge.className = 'badge error';
      badge.textContent = 'error';
    }
  } catch {
    badge.className = 'badge error';
    badge.textContent = 'unreachable';
  }
}

document.getElementById('btnHealth').addEventListener('click', async () => {
  await Promise.allSettled([
    checkServiceHealth(document.getElementById('healthStatus'), apiCall, () => 'ok'),
    checkServiceHealth(
      document.getElementById('crawlerHealthStatus'),
      crawlerCall,
      (data) => (data.scraper_reachable ? 'ok' : 'no scraper'),
    ),
  ]);
});

// ─── Dynamic rows ─────────────────────────────────────────────────────────────
function addDynamicRow(containerId, placeholders) {
  const container = document.getElementById(containerId);
  const row = document.createElement('div');
  row.className = 'dynamic-row';
  row.innerHTML = placeholders.map(p =>
    `<input type="text" placeholder="${escapeHtml(p)}" />`
  ).join('') +
  `<button class="btn-remove" title="Remove">×</button>`;
  row.querySelector('.btn-remove').addEventListener('click', () => row.remove());
  container.appendChild(row);
}

function getRowValues(containerId) {
  const rows = document.querySelectorAll(`#${containerId} .dynamic-row`);
  return Array.from(rows).map(row =>
    Array.from(row.querySelectorAll('input')).map(i => i.value.trim())
  );
}

// Headers
document.getElementById('btnAddHeader').addEventListener('click', () =>
  addDynamicRow('headers-list', ['Header name', 'Value']));

// Cookies
document.getElementById('btnAddCookie').addEventListener('click', () =>
  addDynamicRow('cookies-list', ['name', 'value', 'domain', 'path']));

// Extract fields
document.getElementById('btnAddField').addEventListener('click', () =>
  addExtractField('extract-fields'));
document.getElementById('btnAddFieldBatch').addEventListener('click', () =>
  addExtractField('b-extract-fields'));

function addExtractField(containerId = 'extract-fields') {
  const container = document.getElementById(containerId);
  const row = document.createElement('div');
  row.className = 'dynamic-row extract-row';
  row.innerHTML = `
    <input type="text" placeholder="field name" style="flex:0 0 120px" />
    <input type="text" placeholder="selector" />
    <select style="flex:0 0 100px">
      <option value="text">text</option>
      <option value="html">html</option>
      <option value="href">href</option>
      <option value="src">src</option>
    </select>
    <label class="all-toggle" title="Return every match as a list instead of the first one">
      <input type="checkbox" />
      <span>all</span>
    </label>
    <button class="btn-remove" title="Remove row">×</button>`;
  row.querySelector('.btn-remove').addEventListener('click', () => row.remove());
  container.appendChild(row);
}

// ─── Camoufox premium fields collector ───────────────────────────────────────
// Returns flat camoufox fields for top-level spread into the request payload
// (humanize, block_webgl, and optionally fingerprint_profile/addons), or {} for
// non-camoufox. Prefix must match the tab's element-id prefix (s, b, se, pw).
function collectCamoufoxOpts(prefix) {
  const $ = (id) => document.getElementById(id);
  const engine = $(`${prefix}-browser-engine`)?.value;
  if (engine !== 'camoufox') return {};
  const opts = {
    humanize: !!$(`${prefix}-cf-humanize`)?.checked,
    block_webgl: !!$(`${prefix}-cf-block-webgl`)?.checked,
  };
  // '' = Auto -> omit the field so the server's CAMOUFOX_FINGERPRINT_PROFILE
  // applies. Sending 'auto' explicitly would mean the same thing, but omitting
  // it keeps the payload readable and matches how the headless select works.
  const fingerprint = $(`${prefix}-cf-fingerprint`)?.value;
  if (fingerprint) opts.fingerprint_profile = fingerprint;
  const addonsRaw = $(`${prefix}-cf-addons`)?.value.trim();
  if (addonsRaw) {
    const addons = addonsRaw.split(',').map(s => s.trim()).filter(Boolean);
    if (addons.length) opts.addons = addons;
  }
  return opts;
}

// ─── Build scrape payload ─────────────────────────────────────────────────────
function buildScrapePayload() {
  const url = normalizeUrl(document.getElementById('s-url').value);
  if (!url) { alert('URL is required'); return null; }

  const payload = {
    url,
    render: document.getElementById('s-render').checked,
    wait_until: document.getElementById('s-wait-until').value,
    device: document.getElementById('s-device').value,
    timeout_ms: Number(document.getElementById('s-timeout').value) || 30000,
    block_assets: document.getElementById('s-block-assets').checked,
    raw_html: document.getElementById('s-raw-html').checked,
    screenshot: document.getElementById('s-screenshot').checked,
    stealth: document.getElementById('s-stealth').checked,
  };

  // Browser engine — always included; chromium is the API default so sending
  // it explicitly is a no-op for existing behaviour.
  payload.browser_engine = document.getElementById('s-browser-engine')?.value || 'chromium';

  // Launch mode: '' = Auto -> omit the field so the server default applies.
  // Independent of warmup (which is session warm-up, not how the browser launches).
  const headlessSel = document.getElementById('s-headless')?.value;
  if (headlessSel === 'true' || headlessSel === 'false') {
    payload.headless = headlessSel === 'true';
  }

  Object.assign(payload, collectCamoufoxOpts('s'));

  // Viewport (and matching window.screen). Blank inputs => server default 1920x1080.
  const vpW = Number(document.getElementById('s-vp-width')?.value);
  const vpH = Number(document.getElementById('s-vp-height')?.value);
  if (vpW && vpH) payload.viewport = { width: vpW, height: vpH };

  // Optional per-request retry cap. Blank => server MAX_RETRIES default.
  const sMaxRetries = Number(document.getElementById('s-max-retries')?.value);
  if (sMaxRetries) payload.max_retries = sMaxRetries;

  // Markdown-family formats. Server unions these with the raw_html /
  // screenshot booleans, so we only list the markdown outputs here.
  const formats = [];
  if (document.getElementById('s-markdown').checked) formats.push('markdown');
  if (document.getElementById('s-fit-markdown').checked) formats.push('fit_markdown');
  if (formats.length) {
    payload.formats = formats;
    const mdOpts = {
      only_main_content: document.getElementById('s-md-only-main').checked,
      content_filter: document.getElementById('s-md-content-filter').value,
      citations: document.getElementById('s-md-citations').checked,
      ignore_links: document.getElementById('s-md-ignore-links').checked,
      ignore_images: document.getElementById('s-md-ignore-images').checked,
    };
    const instruction = document.getElementById('s-md-filter-instruction').value.trim();
    if (instruction) mdOpts.filter_instruction = instruction;
    payload.markdown_options = mdOpts;
  }

  const waitSelector = document.getElementById('s-wait-selector').value.trim();
  if (waitSelector) payload.wait_for_selector = waitSelector;

  // Proxy (type / pool / legacy geo / premium generator) — single source of
  // truth shared with the other tabs. Returns false after alerting on a missing
  // pool id; the Scrape tab has no "default (preset)" sentinel so null can't
  // happen here, but treat any falsy result as an abort.
  const proxy = collectProxy('s');
  if (!proxy) return null;
  Object.assign(payload, proxy);

  // Warmup — optional pre-navigation origin visit. Off by default.
  const warmup = collectWarmup('s');
  if (warmup) payload.warmup = warmup;

  // Session — server-side session id (created on Sessions tab). Overrides
  // device + proxy on the server when present.
  const sessionId = document.getElementById('s-session-id')?.value.trim();
  if (sessionId) payload.session_id = sessionId;

  // Headers
  const headerRows = getRowValues('headers-list');
  if (headerRows.length) {
    payload.headers = {};
    headerRows.forEach(([k, v]) => { if (k) payload.headers[k] = v; });
  }

  // Cookies
  const cookieRows = getRowValues('cookies-list');
  if (cookieRows.length) {
    payload.cookies = cookieRows
      .filter(([n]) => n)
      .map(([name, value, domain, path]) => ({
        name, value, domain: domain || undefined, path: path || undefined
      }));
  }

  // Extract
  if (!warnIfDeviceEngineClash('s-device', 's-browser-engine')) return null;
  if (!warnIfDeviceNeedsNoRender('s-device', 's-render')) return null;
  const extractType = document.getElementById('s-extract-type').value;
  if (extractType) {
    const fieldRows = document.querySelectorAll('#extract-fields .dynamic-row');
    const fields = {};
    let totalRows = 0;
    fieldRows.forEach(row => {
      totalRows++;
      const inputs = row.querySelectorAll('input[type="text"]');
      const name = inputs[0].value.trim();
      const selector = inputs[1].value.trim();
      const attr = row.querySelector('select').value;
      const all = row.querySelector('input[type="checkbox"]').checked;
      if (name && selector) {
        fields[name] = { selector, attr, all };
      }
    });
    if (totalRows > 0 && Object.keys(fields).length === 0) {
      alert('Extraction is enabled but no fields have both a name and a selector set. Please fill them in or disable extraction.');
      return null;
    }
    if (Object.keys(fields).length) {
      const rowContainer = readContainer('s-extract-container', fields);
      if (rowContainer === undefined) return null;
      payload.extract = { type: extractType, fields };
      if (rowContainer) payload.extract.container = rowContainer;
    }
  }

  return payload;
}

// ─── Scrape Page ──────────────────────────────────────────────────────────────
document.getElementById('btnScrape').addEventListener('click', async () => {
  const payload = buildScrapePayload();
  if (!payload) return;

  document.getElementById('scrape-result').innerHTML = '';
  setStatus('scrape-status', 'queued', 'Submitting...');

  const { data, ok } = await apiCall('/api/v1/scrape/page', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

  if (!ok || !data?.job_id) {
    setStatus('scrape-status', 'failed', 'Failed to submit job');
    showResult('scrape-result', data);
    return;
  }

  rememberSentPayload(data.job_id, payload);
  await loadServerConfig();
  pollJob(data.job_id, 'scrape-status', 'scrape-result', 1, 'btnCancelScrape');
});

// ─── Engine dropdown + Camoufox conditional visibility ───────────────────────
// Wires the browser-engine select for a given tab prefix.
// - Shows/hides the camoufox-opts panel when engine=camoufox
// - Disables the stealth checkbox (with tooltip) when engine≠chromium
function initEngineControls(prefix) {
  const $ = (id) => document.getElementById(id);
  const engineSel = $(`${prefix}-browser-engine`);
  const camoufoxOpts = $(`${prefix}-camoufox-opts`);
  const stealthCb = $(`${prefix}-stealth`);
  if (!engineSel) return;

  const sync = () => {
    const engine = engineSel.value;
    if (camoufoxOpts) {
      camoufoxOpts.style.display = engine === 'camoufox' ? '' : 'none';
    }
    if (stealthCb) {
      const isChromium = engine === 'chromium';
      stealthCb.disabled = !isChromium;
      stealthCb.parentElement.title = isChromium
        ? ''
        : 'Camoufox/Firefox use built-in anti-detect';
    }
  };

  engineSel.addEventListener('change', sync);
  sync();
}

initEngineControls('s');
initEngineControls('b');
initEngineControls('se');
// The Preset Builder had no engine selector at all, so every preset the wizard
// produced was Chromium — the two engines that need an anti-detect browser
// could only be written by editing the JSON by hand.
initEngineControls('pw');

// ─── Search ───────────────────────────────────────────────────────────────────
(() => {
  const scrapeCb = document.getElementById('se-scrape');
  const optsRow = document.getElementById('se-scrape-options-row');
  if (scrapeCb && optsRow) {
    const sync = () => { optsRow.style.display = scrapeCb.checked ? '' : 'none'; };
    scrapeCb.addEventListener('change', sync);
    sync();
  }
})();

function renderSearchResults(el, data) {
  el.innerHTML = '';
  if (!data || !Array.isArray(data.results)) {
    showResult('search-result', data);
    return;
  }
  const took = typeof data.took_ms === 'number'
    ? (data.took_ms >= 1000 ? `${(data.took_ms / 1000).toFixed(1)}s` : `${data.took_ms}ms`)
    : null;
  const summary = document.createElement('div');
  summary.style.cssText = 'margin:0 0 0.6rem;font-size:12px;color:var(--text-secondary)';
  summary.innerHTML = `<b style="color:var(--text-primary)">${data.count}</b> result${data.count === 1 ? '' : 's'} for “${escapeHtml(data.query)}”`
    + (took ? ` <span style="color:var(--text-muted)">in ${took}</span>` : '');
  el.appendChild(summary);

  if (Array.isArray(data.warnings) && data.warnings.length) {
    const w = document.createElement('div');
    w.style.cssText = 'margin:0 0 0.6rem;padding:0.4rem 0.6rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:11px;color:var(--color-orange,#b45309)';
    w.textContent = '⚠ ' + data.warnings.join(' · ');
    el.appendChild(w);
  }

  data.results.forEach((r) => {
    const card = document.createElement('div');
    card.style.cssText = 'margin-bottom:0.6rem;padding:0.5rem 0.7rem;border:1px solid var(--border);border-radius:var(--radius-sm)';
    const a = document.createElement('a');
    a.href = r.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.textContent = r.title || r.url;
    a.style.cssText = 'font-weight:600;color:var(--color-blue);text-decoration:none';
    card.appendChild(a);
    const urlLine = document.createElement('div');
    urlLine.style.cssText = 'font-size:11px;color:var(--text-muted);word-break:break-all;margin:0.15rem 0';
    urlLine.textContent = r.url;
    card.appendChild(urlLine);
    if (r.snippet) {
      const s = document.createElement('div');
      s.style.cssText = 'font-size:12px;color:var(--text-secondary)';
      s.textContent = r.snippet;
      card.appendChild(s);
    }
    if (r.scrape) {
      const det = document.createElement('details');
      det.style.cssText = 'margin-top:0.4rem';
      det.innerHTML = '<summary class="details-summary" style="cursor:pointer;font-size:11px;color:var(--text-secondary);list-style:none">▶ scrape response</summary>';
      const pre = document.createElement('pre');
      pre.style.cssText = 'max-height:300px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);padding:0.5rem;font-size:11px;border-radius:var(--radius-sm);white-space:pre-wrap;word-break:break-word';
      pre.innerHTML = syntaxHighlight(r.scrape);
      det.appendChild(pre);
      card.appendChild(det);
    }
    el.appendChild(card);
  });
}

// Fill the Search-tab Locale dropdown from the *selected engine's* preset
// locales (google_search_chromium / bing_search_chromium /
// yandex_search_camoufox — the exact variants ENGINES in src/api/search.py
// resolves each engine to). The list was a hardcoded us/uk/de/fr/ru/jp, so
// engine-specific locales — notably Yandex's region set (kz, by, ee, ua, …,
// each carrying its own `lr`) — were never selectable. Re-runs on engine
// change and when the scraper target changes.
// Mirrors ENGINES in src/api/search.py — keep the two in lockstep; adding or
// changing an entry on one side without the other silently breaks this.
const SEARCH_ENGINE_PRESET = {
  google: 'google_search_chromium',
  bing: 'bing_search_chromium',
  yandex: 'yandex_search_camoufox',
};
async function populateSearchLocales() {
  const engineSel = document.getElementById('se-engine');
  const localeSel = document.getElementById('se-locale');
  if (!engineSel || !localeSel) return;
  // Prefer the live selection; fall back to the persisted value. restoreState
  // runs before this and assigns se-locale against the static HTML options, so
  // an engine-specific locale (e.g. yandex `kz`) gets clobbered to '' — recover
  // it from saved state so the selection survives a reload.
  let want = localeSel.value;
  if (!want) {
    try { want = (JSON.parse(localStorage.getItem(STATE_KEY) || 'null')?.inputs || {})['se-locale'] || ''; }
    catch { /* no/invalid saved state */ }
  }
  const preset = SEARCH_ENGINE_PRESET[engineSel.value];
  if (!preset) {
    // The lockstep with ENGINES in src/api/search.py has broken: the backend
    // offers an engine this map does not know, so its locales silently stay
    // whatever the previous engine left in the <select>. Say so.
    console.warn(
      `populateSearchLocales: no preset mapped for engine "${engineSel.value}" — ` +
      `SEARCH_ENGINE_PRESET is out of lockstep with ENGINES in src/api/search.py; ` +
      `known engines: ${Object.keys(SEARCH_ENGINE_PRESET).join(', ')}`);
    return;
  }
  const { ok, data } = await apiCall(`/api/v1/presets/${encodeURIComponent(preset)}`);
  if (!ok || !data || !data.locales) return;
  const keys = Object.keys(data.locales);
  const def = data.default_locale || keys[0] || 'us';
  localeSel.innerHTML =
    `<option value="">default (${escapeHtml(def)})</option>` +
    keys.map(k => `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`).join('');
  if (want && keys.includes(want)) {
    localeSel.value = want;  // keep selection if the engine still offers it
    localeSel.dispatchEvent(new Event('change', { bubbles: true }));  // sync custom-select label
  }
}
// restoreState replays a synthetic change on every select[id]; skip that replay
// (the explicit init call below repopulates against the restored engine).
document.getElementById('se-engine')?.addEventListener('change', () => {
  if (_restoringState) return;
  populateSearchLocales();
});

document.getElementById('btnSearch').addEventListener('click', async () => {
  const query = document.getElementById('se-query').value.trim();
  if (!query) { alert('Query is required'); return; }
  const payload = { query, limit: Number(document.getElementById('se-limit').value) || 10 };
  payload.engine = document.getElementById('se-engine').value;
  const locale = document.getElementById('se-locale').value;
  if (locale) payload.locale = locale;

  const se = document.getElementById('se-browser-engine')?.value;
  if (se) payload.browser_engine = se;
  const seMaxRetries = Number(document.getElementById('se-max-retries')?.value);
  if (seMaxRetries) payload.max_retries = seMaxRetries;
  Object.assign(payload, collectCamoufoxOpts('se'));
  if (document.getElementById('se-scrape').checked) {
    payload.scrape = true;
    const opts = {};
    if (document.getElementById('se-so-raw-html').checked) opts.raw_html = true;
    if (document.getElementById('se-so-screenshot').checked) opts.screenshot = true;
    const fmts = [];
    if (document.getElementById('se-so-markdown').checked) fmts.push('markdown');
    if (document.getElementById('se-so-fit-markdown').checked) fmts.push('fit_markdown');
    if (fmts.length) opts.formats = fmts;
    if (Object.keys(opts).length) payload.scrape_options = opts;
  }
  const proxy = collectProxy('se');
  if (proxy === false) return;   // invalid (missing pool) — already alerted
  if (proxy) Object.assign(payload, proxy);

  // Warmup — optional pre-navigation origin visit. Off by default.
  const seWarmup = collectWarmup('se');
  if (seWarmup) payload.warmup = seWarmup;

  setStatus('search-status', 'queued', 'Searching...');
  document.getElementById('search-result').innerHTML = '<span class="placeholder">Searching…</span>';
  const { data, ok } = await apiCall('/api/v1/search', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  if (!ok) {
    setStatus('search-status', 'failed', 'Search failed');
    showResult('search-result', data);
    return;
  }
  setStatus('search-status', 'done', `${data.count} result(s)`);
  renderSearchResults(document.getElementById('search-result'), data);
});

// ─── Batch Scrape ─────────────────────────────────────────────────────────────
function buildBatchSharedPayload() {
  const proxy = collectProxy('b');
  if (proxy === false) return null;  // invalid (missing pool) — already alerted

  const payload = {
    render: document.getElementById('b-render').checked,
    wait_until: document.getElementById('b-wait-until').value,
    device: document.getElementById('b-device').value,
    timeout_ms: Number(document.getElementById('b-timeout').value) || 30000,
    block_assets: document.getElementById('b-block-assets').checked,
    raw_html: document.getElementById('b-raw-html').checked,
    screenshot: document.getElementById('b-screenshot').checked,
    stealth: document.getElementById('b-stealth').checked,
  };
  Object.assign(payload, proxy);

  // Warmup — optional pre-navigation origin visit. Off by default.
  const warmup = collectWarmup('b');
  if (warmup) payload.warmup = warmup;

  payload.browser_engine = document.getElementById('b-browser-engine')?.value || 'chromium';
  Object.assign(payload, collectCamoufoxOpts('b'));

  const bMaxRetries = Number(document.getElementById('b-max-retries')?.value);
  if (bMaxRetries) payload.max_retries = bMaxRetries;

  const waitSelector = document.getElementById('b-wait-selector').value.trim();
  if (waitSelector) payload.wait_for_selector = waitSelector;

  // Session — applied to every URL in the batch.
  const sessionId = document.getElementById('b-session-id')?.value.trim();
  if (sessionId) payload.session_id = sessionId;

  // Shared extract
  if (!warnIfDeviceEngineClash('b-device', 'b-browser-engine')) return null;
  if (!warnIfDeviceNeedsNoRender('b-device', 'b-render')) return null;
  const bExtractType = document.getElementById('b-extract-type').value;
  if (bExtractType) {
    const fieldRows = document.querySelectorAll('#b-extract-fields .dynamic-row');
    const fields = {};
    let totalRows = 0;
    fieldRows.forEach(row => {
      totalRows++;
      const inputs = row.querySelectorAll('input[type="text"]');
      const name = inputs[0].value.trim();
      const selector = inputs[1].value.trim();
      const attr = row.querySelector('select').value;
      const all = row.querySelector('input[type="checkbox"]').checked;
      if (name && selector) {
        fields[name] = { selector, attr, all };
      }
    });
    if (totalRows > 0 && Object.keys(fields).length === 0) {
      alert('Extraction is enabled but no fields have both a name and a selector set. Please fill them in or disable extraction.');
      return null;
    }
    if (Object.keys(fields).length) {
      const rowContainer = readContainer('b-extract-container', fields);
      if (rowContainer === undefined) return null;
      payload.extract = { type: bExtractType, fields };
      if (rowContainer) payload.extract.container = rowContainer;
    }
  }

  // Shared headers
  const headerRows = getRowValues('b-headers-list');
  if (headerRows.length) {
    payload.headers = {};
    headerRows.forEach(([k, v]) => { if (k) payload.headers[k] = v; });
  }

  // Shared cookies
  const cookieRows = getRowValues('b-cookies-list');
  if (cookieRows.length) {
    payload.cookies = cookieRows
      .filter(([n]) => n)
      .map(([name, value, domain, path]) => ({
        name, value, domain: domain || undefined, path: path || undefined
      }));
  }

  return payload;
}

renderProxyComponent('b', document.getElementById('b-proxy-component'));
renderWarmupComponent('b', document.getElementById('b-warmup-component'));
initProxyPool('b');

document.getElementById('btnCopyFromSingle').addEventListener('click', () => {
  const pairs = [
    ['s-render', 'b-render'],
    ['s-raw-html', 'b-raw-html'],
    ['s-screenshot', 'b-screenshot'],
    ['s-block-assets', 'b-block-assets'],
    ['s-stealth', 'b-stealth'],
    ['s-device', 'b-device'],
    ['s-wait-until', 'b-wait-until'],
    ['s-timeout', 'b-timeout'],
    ['s-wait-selector', 'b-wait-selector'],
    ['s-proxy-type', 'b-proxy-type'],
    ['s-proxy-pool', 'b-proxy-pool'],
    ['s-geo-country', 'b-geo-country'],
    ['s-geo-region', 'b-geo-region'],
    ['s-geo-city', 'b-geo-city'],
    ['s-extract-type', 'b-extract-type'],
    // Browser engine + Camoufox options
    ['s-browser-engine', 'b-browser-engine'],
    ['s-cf-humanize', 'b-cf-humanize'],
    ['s-cf-block-webgl', 'b-cf-block-webgl'],
    ['s-cf-fingerprint', 'b-cf-fingerprint'],
    ['s-cf-addons', 'b-cf-addons'],
  ];
  for (const [from, to] of pairs) {
    const src = document.getElementById(from);
    const dst = document.getElementById(to);
    if (!src || !dst) continue;
    if (src.type === 'checkbox') dst.checked = src.checked;
    else dst.value = src.value;
  }

  // Copy the extract field rows verbatim (name, selector, attr, all)
  const srcRows = document.querySelectorAll('#extract-fields .dynamic-row');
  const dstContainer = document.getElementById('b-extract-fields');
  dstContainer.innerHTML = '';
  srcRows.forEach(srcRow => {
    addExtractField('b-extract-fields');
    const newRow = dstContainer.querySelector('.dynamic-row:last-child');
    const srcInputs = srcRow.querySelectorAll('input[type="text"]');
    const dstInputs = newRow.querySelectorAll('input[type="text"]');
    dstInputs[0].value = srcInputs[0].value;
    dstInputs[1].value = srcInputs[1].value;
    newRow.querySelector('select').value = srcRow.querySelector('select').value;
    newRow.querySelector('input[type="checkbox"]').checked =
      srcRow.querySelector('input[type="checkbox"]').checked;
  });

  wireProxyPool('b');
});

document.getElementById('btnBatch').addEventListener('click', async () => {
  const raw = document.getElementById('b-urls').value;
  const urls = raw.split(/\r?\n/).map(normalizeUrl).filter(Boolean);

  if (!urls.length) { alert('Add at least one URL (one per line)'); return; }

  const shared = buildBatchSharedPayload();
  // A builder that refused (a bad device/engine or device/render pairing, a
  // container with a field that is not a column) returns null, and spreading
  // null yields `{url}` alone -- the batch would go out stripped of its proxy,
  // extraction and cookies instead of not going out at all. The single-page
  // handler has always checked; this one did not.
  if (!shared) return;
  const pages = urls.map(url => ({ ...shared, url }));

  document.getElementById('batch-result').innerHTML = '';
  setStatus('batch-status', 'queued', `Submitting ${pages.length} pages...`);

  const { data, ok } = await apiCall('/api/v1/scrape/pages', {
    method: 'POST',
    body: JSON.stringify({ pages }),
  });

  if (!ok || !data?.job_id) {
    setStatus('batch-status', 'failed', 'Failed to submit batch');
    showResult('batch-result', data);
    return;
  }

  rememberSentPayload(data.job_id, { pages });
  await loadServerConfig();
  pollJobBatch(data.job_id, 'batch-status', 'batch-result', pages.length, 'btnCancelBatch');
});

// ─── Jobs ─────────────────────────────────────────────────────────────────────
function updateJobCancelButton(jobId, status) {
  if (!jobId || !status) return;
  if (status === 'queued' || status === 'running') {
    setScrapeCancelButton('btnCancelJob', 'armed', jobId);
  } else {
    setScrapeCancelButton('btnCancelJob', 'done');
  }
}

document.getElementById('btnJobStatus').addEventListener('click', async () => {
  const jobId = document.getElementById('j-job-id').value.trim();
  if (!jobId) return;
  const { data } = await apiCall(`/api/v1/scrape/${jobId}`);
  showResult('jobs-result', data);
  if (data?.status) {
    addRecentJob(jobId, data.status);
    updateJobCancelButton(jobId, data.status);
  }
});

document.getElementById('btnJobResults').addEventListener('click', () => {
  const jobId = document.getElementById('j-job-id').value.trim();
  if (!jobId) return;
  lookupJobResults(jobId);
});

document.getElementById('btnClearJobs').addEventListener('click', () => {
  recentJobs = [];
  saveRecentJobs();
  renderRecentJobs();
});

// Render persisted jobs on load
renderRecentJobs();

// Reset MCP session whenever the target or its URL changes — sessions don't
// port between the two backends.
// Reveal the LLM filter instruction field only when the llm content filter
// is selected. Fires on restore too (restoreState re-dispatches `change`).
(() => {
  const sel = document.getElementById('s-md-content-filter');
  const row = document.getElementById('s-md-instruction-row');
  if (sel && row) {
    const sync = () => { row.style.display = sel.value === 'llm' ? '' : 'none'; };
    sel.addEventListener('change', sync);
    sync();
  }
})();

document.getElementById('scraperUrl').addEventListener('change', () => {
  mcpSessionId = null;
});
document.getElementById('crawlerUrl').addEventListener('change', () => {
  mcpSessionId = null;
});
document.getElementById('mcp-target').addEventListener('change', () => {
  mcpSessionId = null;
  const tools = document.getElementById('mcp-tools-list');
  if (tools) tools.innerHTML = '<span style="color:var(--text-muted)">Target changed — reload tools</span>';
});

// ─── Info tooltips (detached, fixed-positioned) ──────────────────────────────
// Wire one .info-icon (idempotent). Detaches its tooltip to <body> and shows it
// on hover/focus. Safe to call again for dynamically-rendered icons (e.g. the
// warmup component, which renders AFTER the initial load-time pass below).
function wireInfoIcon(icon) {
  if (!icon || icon.dataset.tipWired) return;
  const tooltip = icon.querySelector('.info-tooltip');
  if (!tooltip) return;
  icon.dataset.tipWired = '1';
  document.body.appendChild(tooltip);

  const show = () => {
    const rect = icon.getBoundingClientRect();
    tooltip.classList.add('visible');
    // Measure, then position
    const tipRect = tooltip.getBoundingClientRect();
    let top = rect.bottom + 8;
    let left = rect.right - tipRect.width;
    // Keep within viewport
    if (left < 8) left = 8;
    if (left + tipRect.width > window.innerWidth - 8) left = window.innerWidth - tipRect.width - 8;
    if (top + tipRect.height > window.innerHeight - 8) top = rect.top - tipRect.height - 8;
    tooltip.style.top = `${top}px`;
    tooltip.style.left = `${left}px`;
  };
  const hide = () => tooltip.classList.remove('visible');

  icon.addEventListener('mouseenter', show);
  icon.addEventListener('mouseleave', hide);
  icon.addEventListener('focus', show);
  icon.addEventListener('blur', hide);
}
function wireInfoIcons(root = document) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll('.info-icon').forEach(wireInfoIcon);
}
wireInfoIcons(document);

// ─── Proxy type dependent UI ──────────────────────────────────────────────────
const STATIC_PROXY_TYPES = new Set(['res_static', 'mobile', 'mobile_shared', 'dc_static']);

const BUY_PROXY_URLS = {
  res_rotating:  'https://app.cyberyozh.com/my-account/add-proxy/residential-rotating/',
  res_static:    'https://app.cyberyozh.com/my-account/add-proxy/residential/',
  mobile:        'https://app.cyberyozh.com/my-account/add-proxy/mobile/',
  mobile_shared: 'https://app.cyberyozh.com/my-account/add-proxy/mobile-shared/',
  dc_static:     'https://app.cyberyozh.com/my-account/add-proxy/datacenter/',
};

function renderBuyButton(proxyType) {
  const url = BUY_PROXY_URLS[proxyType];
  if (!url) return '';
  const label = proxyType.replace(/_/g, ' ');
  return `<a href="${url}" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:0.35rem;background:var(--neutral-900);color:var(--text-light);padding:0.5rem 0.9rem;border-radius:var(--radius-sm);text-decoration:none;font-size:13px;font-weight:500">Buy ${label} proxy ↗</a>`;
}

function ensureBuyButtonEl(fieldEl, id) {
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement('div');
    el.id = id;
    el.style.marginTop = '0.35rem';
    fieldEl.appendChild(el);
  }
  return el;
}

// One implementation for every proxy block. `prefix` is the element-id
// prefix: s (Scrape), b (Batch), c (Crawler scrape), cp (Crawl proxy),
// pw (Preset Builder wizard). Replaces four copy-pasted refresh functions.
async function wireProxyPool(prefix) {
  const $ = (suffix) => document.getElementById(`${prefix}-${suffix}`);
  const typeSel = $('proxy-type');
  if (!typeSel) return;            // tab not present in DOM
  const type = typeSel.value;
  const geoFields = $('geo-fields');
  const poolField = $('pool-id-field');
  const poolSelect = $('proxy-pool-select');
  const poolInput = $('proxy-pool');
  const hint = $('pool-id-hint');
  // All five sub-elements must exist for this prefix; bail if a block is
  // only partially in the DOM (e.g. a future block wired incrementally).
  if (!geoFields || !poolField || !poolSelect || !poolInput || !hint) return;
  const buyEl = ensureBuyButtonEl(poolField, `${prefix}-buy-proxy`);

  // '' is the Search tab's "default (preset)" sentinel — no override, so the
  // pool/geo controls are as irrelevant as they are for 'none'.
  if (type === 'none' || type === '') {
    geoFields.style.display = 'none';
    poolField.style.display = 'none';
    buyEl.innerHTML = '';
    return;
  }
  // prem_res_rotating uses the v2 gateway — no legacy pool id needed.
  if (type === 'prem_res_rotating') {
    poolField.style.display = 'none';
    buyEl.innerHTML = '';
    return;
  }
  poolField.style.display = '';
  // Both res_rotating (legacy) and prem_res_rotating (premium) target by geo, so
  // both reveal the geo row. The premium generator's own handler then manages
  // the region/city ⇄ zip swap within it.
  geoFields.style.display =
    (type === 'res_rotating' || type === 'prem_res_rotating') ? '' : 'none';

  hint.textContent = 'loading...';
  poolSelect.style.display = 'none';
  poolInput.style.display = '';
  poolSelect.innerHTML = '';
  buyEl.innerHTML = '';

  const { ok, data } = await apiCall(
    `/api/v1/proxies/available?proxy_type=${encodeURIComponent(type)}`
  );
  if (!ok || !data) { hint.textContent = '(failed to load)'; return; }
  if (!data.configured) {
    hint.textContent = '(CyberYozh API key not set — manual entry only)';
    return;
  }
  if (!data.items?.length) {
    hint.textContent = `(no purchased ${type.replace(/_/g, ' ')} proxies)`;
    poolSelect.style.display = 'none';
    poolInput.style.display = 'none';
    buyEl.innerHTML = renderBuyButton(type);
    return;
  }

  hint.textContent = `(${data.items.length} available)`;
  poolSelect.innerHTML = '<option value="">— select one —</option>' +
    data.items.map(p =>
      `<option value="${escapeHtml(p.id)}">${escapeHtml(p.id)} — ${escapeHtml(p.url || '(no url)')} ${p.expired ? '[expired]' : ''}</option>`
    ).join('');
  poolSelect.style.display = '';
  poolInput.style.display = 'none';
  poolInput.value = '';
}

// Attach the standard listeners (type change + select↔input sync + initial
// load) for a proxy block. Returns nothing; safe to call once per prefix.
function initProxyPool(prefix, { onChange } = {}) {
  const typeSel = document.getElementById(`${prefix}-proxy-type`);
  const sel = document.getElementById(`${prefix}-proxy-pool-select`);
  if (!typeSel || !sel) return;
  typeSel.addEventListener('change', () => {
    wireProxyPool(prefix);
    if (onChange) onChange();
  });
  sel.addEventListener('change', () => {
    document.getElementById(`${prefix}-proxy-pool`).value = sel.value;
  });
  // Initial load. wireProxyPool is async and intentionally not awaited;
  // onChange runs synchronously (current callbacks only read .value).
  wireProxyPool(prefix);
  if (onChange) onChange();
}

// ─── Unified proxy component factory ─────────────────────────────────────────
// PROXY_TYPES and PREM_IP_FILTER_FALLBACK are declared at the top of this file
// (they must be initialized before the first top-level renderProxyComponent()
// call in the Batch-tab init — a `const` here would be in its TDZ at that point).

// Render a complete proxy block into `container` for the given id prefix and
// wire all its conditional UI. Produces the exact ids the rest of app.js
// already reads (proxy-type / pool-id-field / proxy-pool-select / proxy-pool /
// pool-id-hint / geo-fields / geo-country / geo-region / geo-city) so
// wireProxyPool, collectProxy, loadCountries and restoreState keep working
// unchanged, plus a premium generator panel for prem_res_rotating.
// Fill a <select> from a v2 /geo endpoint. Best-effort: an error or wrong
// scraper target leaves the select with just the "— any —" option and never
// throws. Keeps the current value if it's still valid after the reload.
async function loadGeoSelect(sel, path, params, { value, label, code }) {
  if (!sel) return;
  const cur = sel.value;
  let items = [];
  try {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
      .join('&');
    const r = await apiCall(`${path}${qs ? `?${qs}` : ''}`);
    if (Array.isArray(r.data)) items = r.data;
  } catch { /* best-effort: leave the placeholder */ }
  sel.innerHTML = ['<option value="">— any —</option>'].concat(items.map(it => {
    const v = String(it[value] ?? '');
    const dc = (code != null && it[code] != null) ? ` data-code="${escapeHtml(String(it[code]))}"` : '';
    return `<option value="${escapeHtml(v)}"${dc}>${escapeHtml(String(it[label] ?? v))}</option>`;
  })).join('');
  if (cur && Array.from(sel.options).some(o => o.value === cur)) sel.value = cur;
}

function renderProxyComponent(prefix, container) {
  if (!container) return;
  const typeOptions = PROXY_TYPES
    .map(t => `<option value="${escapeHtml(t.value)}">${escapeHtml(t.label)}</option>`)
    .join('');

  // Stack direct children with the same vertical rhythm as the rest of the
  // form. The form's spacing comes from .panel's `gap` applied to its DIRECT
  // children; these rows are nested in the component, so they need their own
  // gap or they render squished against each other.
  container.style.display = 'flex';
  container.style.flexDirection = 'column';
  container.style.gap = '0.8rem';
  container.innerHTML = `
    <div class="field-row">
      <div class="field">
        <label>Proxy Type</label>
        <select id="${prefix}-proxy-type">${typeOptions}</select>
      </div>
      <div class="field" id="${prefix}-pool-id-field">
        <label>Pool ID <span id="${prefix}-pool-id-hint" style="color:var(--text-tertiary);font-weight:400"></span></label>
        <select id="${prefix}-proxy-pool-select" style="display:none"></select>
        <input id="${prefix}-proxy-pool" type="text" placeholder="optional (for rotating: pin to pool id)" />
      </div>
    </div>
    <div class="field-row" id="${prefix}-prem-top" style="display:none">
      <div class="field">
        <label>IP filter</label>
        <select id="${prefix}-prem-ipfilter"></select>
      </div>
      <div class="field">
        <label>Sub-user</label>
        <select id="${prefix}-prem-subuser"></select>
      </div>
    </div>
    <div class="field-row" id="${prefix}-geo-fields">
      <div class="field">
        <label>Country</label>
        <select id="${prefix}-geo-country"><option value="">— any —</option></select>
      </div>
      <div class="field" id="${prefix}-geo-region-field">
        <label>Region</label>
        <input id="${prefix}-geo-region" type="text" placeholder="New York" />
      </div>
      <div class="field" id="${prefix}-geo-city-field">
        <label>City</label>
        <input id="${prefix}-geo-city" type="text" placeholder="New York" />
      </div>
    </div>
    <div class="field-row" id="${prefix}-prem-geo" style="display:none">
      <div class="field" id="${prefix}-prem-region-field">
        <label>Region</label>
        <select id="${prefix}-prem-region"><option value="">— any —</option></select>
      </div>
      <div class="field" id="${prefix}-prem-city-field">
        <label>City</label>
        <select id="${prefix}-prem-city"><option value="">— any —</option></select>
      </div>
      <div class="field" id="${prefix}-prem-zip-field" style="display:none">
        <label>ZIP</label>
        <select id="${prefix}-prem-zip"><option value="">— any —</option></select>
      </div>
      <div class="field">
        <label>ISP <span style="color:var(--text-tertiary);font-weight:400">(optional)</span></label>
        <select id="${prefix}-prem-isp"><option value="">— any —</option></select>
      </div>
    </div>
    <div class="field-row checkboxes" id="${prefix}-prem-ziptoggle" style="display:none">
      <label><input type="checkbox" id="${prefix}-prem-zip-toggle" /> Target by ZIP <span style="color:var(--text-tertiary);font-weight:400">(switches Region/City → ZIP)</span></label>
    </div>
    <div class="field-row" id="${prefix}-prem-session-row" style="display:none">
      <div class="field">
        <label>Session</label>
        <div class="field-row checkboxes" style="padding-top:0.2rem">
          <label><input type="radio" name="${prefix}-prem-session" value="rotating" checked /> rotating</label>
          <label><input type="radio" name="${prefix}-prem-session" value="sticky" /> sticky</label>
        </div>
      </div>
      <div class="field" id="${prefix}-prem-rotation-field" style="display:none">
        <label>Rotation (min) <span style="color:var(--text-tertiary);font-weight:400">1–1440</span></label>
        <input id="${prefix}-prem-rotation" type="number" min="1" max="1440" placeholder="auto" />
      </div>
    </div>`;

  const $ = (suffix) => document.getElementById(`${prefix}-${suffix}`);
  const typeSel = $('proxy-type');
  const geoFields = $('geo-fields');
  const premTop = $('prem-top');
  const premGeo = $('prem-geo');
  const premZipToggleRow = $('prem-ziptoggle');
  const premSessionRow = $('prem-session-row');
  const regionField = $('geo-region-field');   // legacy free-text region
  const cityField = $('geo-city-field');        // legacy free-text city
  const premRegionField = $('prem-region-field');
  const premCityField = $('prem-city-field');
  const premZipField = $('prem-zip-field');
  const zipToggle = $('prem-zip-toggle');
  const rotationField = $('prem-rotation-field');

  const countrySel = $('geo-country');
  const premRegion = $('prem-region');
  const premCity = $('prem-city');
  const premZip = $('prem-zip');
  const premIsp = $('prem-isp');

  const isPremNow = () => typeSel.value === 'prem_res_rotating';
  const cc = () => (countrySel?.value || '').trim();
  const regionCode = () => premRegion?.selectedOptions?.[0]?.dataset?.code || '';
  const cityName = () => (premCity?.value || '').trim();

  // Cascade loaders (prem only). Region/city/zip/isp come from the v2 /geo
  // endpoints, scoped by the selections above them.
  const GEO = '/api/v2/prem-proxies/geo';
  // All /geo lookups are scoped by country — skip the fetch (which would 422)
  // until a country is chosen. region/city/isp stay optional ("— any —").
  const loadRegions = () => cc() ? loadGeoSelect(premRegion, `${GEO}/regions`, { country_code: cc() }, { value: 'name', label: 'name', code: 'code' }) : null;
  const loadCities  = () => cc() ? loadGeoSelect(premCity, `${GEO}/cities`, { country_code: cc(), region_code: regionCode() }, { value: 'name', label: 'name' }) : null;
  const loadIsps    = () => cc() ? loadGeoSelect(premIsp, `${GEO}/isps`, { country_code: cc(), city_name: cityName() }, { value: 'name', label: 'name' }) : null;
  const loadZips    = () => cc() ? loadGeoSelect(premZip, `${GEO}/zips`, { country_code: cc(), city_name: cityName() }, { value: 'zip', label: 'zip' }) : null;

  // ZIP toggle (prem only): region/city ⇄ zip; disable the hidden controls so a
  // stale value never leaks into collectProxy, and load zips when turned on.
  const syncZip = () => {
    const useZip = !!zipToggle.checked;
    if (premRegionField) premRegionField.style.display = useZip ? 'none' : '';
    if (premCityField) premCityField.style.display = useZip ? 'none' : '';
    if (premZipField) premZipField.style.display = useZip ? '' : 'none';
    if (premRegion) premRegion.disabled = useZip;
    if (premCity) premCity.disabled = useZip;
    if (premZip) premZip.disabled = !useZip;
    if (useZip && isPremNow() && cc()) loadZips();
  };

  const syncSession = () => {
    const sticky = container.querySelector(
      `input[name="${prefix}-prem-session"]:checked`)?.value === 'sticky';
    if (rotationField) rotationField.style.display = sticky ? '' : 'none';
  };

  // res_rotating → legacy free-text geo (Country + Region/City inputs).
  // prem_res_rotating → Country + cascading Region/City/ZIP/ISP dropdowns.
  // wireProxyPool also runs on the type change and owns geo-fields display for
  // res_rotating; we set the prem-only rows here so the two never disagree.
  const syncType = () => {
    const type = typeSel.value;
    const isPrem = type === 'prem_res_rotating';
    if (geoFields) geoFields.style.display = (type === 'res_rotating' || isPrem) ? '' : 'none';
    // For prem only the Country dropdown of geo-fields is used; its legacy
    // free-text region/city inputs are hidden in favour of the cascade.
    if (regionField) regionField.style.display = isPrem ? 'none' : '';
    if (cityField) cityField.style.display = isPrem ? 'none' : '';
    if (premTop) premTop.style.display = isPrem ? '' : 'none';
    if (premGeo) premGeo.style.display = isPrem ? '' : 'none';
    if (premZipToggleRow) premZipToggleRow.style.display = isPrem ? '' : 'none';
    if (premSessionRow) premSessionRow.style.display = isPrem ? '' : 'none';
    if (isPrem) {
      syncZip();
      // Populate the cascade for an already-selected country (e.g. on restore).
      if (cc()) { loadRegions(); loadIsps(); }
    }
  };

  // Cascade wiring (guarded so a shared country dropdown only cascades for prem).
  countrySel?.addEventListener('change', () => {
    if (!isPremNow()) return;
    loadRegions();
    if (premCity) premCity.innerHTML = '<option value="">— any —</option>';
    loadIsps();
    if (zipToggle?.checked) loadZips();
  });
  premRegion?.addEventListener('change', () => { if (isPremNow()) loadCities(); });
  premCity?.addEventListener('change', () => {
    if (!isPremNow()) return;
    loadIsps();
    if (zipToggle?.checked) loadZips();
  });

  typeSel.addEventListener('change', () => { syncType(); syncSession(); });
  if (zipToggle) zipToggle.addEventListener('change', syncZip);
  container.querySelectorAll(`input[name="${prefix}-prem-session"]`)
    .forEach(r => r.addEventListener('change', syncSession));

  syncType();
  syncSession();
}

// Populate every rendered prem dropdown from the sanitized v2 endpoints.
// Best-effort: a missing/erroring backend leaves dropdowns with safe defaults
// (sub-user/isp empty, ip_filter from the canonical enum) and never throws.
async function loadPremCatalogs() {
  const [subs, opts] = await Promise.all([
    apiCall('/api/v2/prem-proxies/sub-users')
      .then(r => (Array.isArray(r.data) ? r.data : [])).catch(() => []),
    apiCall('/api/v2/prem-proxies/session-options')
      .then(r => (r.data && typeof r.data === 'object' ? r.data : {})).catch(() => ({})),
  ]);

  // /session-options items are {value,label,suffix}; the fallback is plain
  // enum strings. Normalize both to {value,label} for the dropdown so options
  // show the human label (not "[object Object]") and submit the enum value.
  const ipFilters = (Array.isArray(opts.ip_filters) && opts.ip_filters.length
    ? opts.ip_filters
    : PREM_IP_FILTER_FALLBACK
  ).map(f => (typeof f === 'string'
    ? { value: f, label: f }
    : { value: f.value, label: f.label || f.value })
  ).filter(f => f.value);

  document.querySelectorAll('select[id$="-prem-subuser"]').forEach(sel => {
    const prev = sel.value;
    // Default = first sub-user (primary). Empty option lets the server fall
    // back to its own primary when no sub-users are configured.
    sel.innerHTML = subs.length
      ? subs.map((u, i) => {
          const label = u.login ? `${escapeHtml(u.login)}${u.is_primary ? ' (primary)' : ''}` : escapeHtml(u.id);
          return `<option value="${escapeHtml(u.id)}"${i === 0 ? ' selected' : ''}>${label}</option>`;
        }).join('')
      : '<option value="">— default —</option>';
    if (prev && subs.some(u => String(u.id) === prev)) sel.value = prev;
  });

  document.querySelectorAll('select[id$="-prem-ipfilter"]').forEach(sel => {
    const prev = sel.value;
    sel.innerHTML = ipFilters
      .map(f => `<option value="${escapeHtml(f.value)}">${escapeHtml(f.label)}</option>`)
      .join('');
    if (prev && ipFilters.some(f => f.value === prev)) sel.value = prev;
  });
}

// ─── Warmup component factory + collector ────────────────────────────────────
// Renders a minimal "Warmup" fieldset (checkbox + type select) into container.
// collectWarmup returns {type} when enabled, or null when disabled/absent.
function renderWarmupComponent(prefix, container) {
  if (!container) return;
  // A normal form section (matching the .panel rhythm), not a bordered fieldset,
  // so it reads like Proxy / Extraction. Own gap because these rows are nested
  // in this container, not direct children of .panel.
  container.style.display = 'flex';
  container.style.flexDirection = 'column';
  container.style.gap = '0.8rem';
  container.innerHTML = `
    <div style="display:flex;align-items:center;gap:0.5rem">
      <h3 style="margin:0">Warmup</h3>
      <span class="info-icon" tabindex="0">
        i
        <div class="info-tooltip">
          <div>Before fetching your URL, the browser first opens the site's <b>homepage</b> and dwells briefly, then navigates to the target — in the <b>same</b> browser context.</div>
          <div>This seeds cookies/session so anti-bot gates that only block a <i>cold</i> first hit (e.g. Yandex SmartCaptcha) let the warmed request through. Works on every engine and stacks with Max retries.</div>
          <div>Off by default. Dwell time is the server's <code>WARMUP_DWELL_MS</code> (2500ms).</div>
          <div><b>Reading the result</b>: <code>warnings</code> distinguishes two outcomes. <code>warmup_failed:</code> means the warmup navigation itself errored. <code>warmup_blocked:</code> means it navigated fine and was redirected to a known challenge path (today Google <code>/sorry/</code> and Yandex <code>/showcaptcha</code>) — the exit was already turned away <i>before</i> your URL was requested, so an empty result afterwards is the site refusing you, not a broken selector. Rotate the exit rather than reworking the recipe. <code>meta.applied_warmup</code> says which page was actually visited, and carries <code>blocked</code> as a boolean so you do not have to read the warning text.</div>
        </div>
      </span>
    </div>
    <div class="field-row checkboxes">
      <label><input type="checkbox" id="${prefix}-warmup-enable" /> Enable warmup <span style="color:var(--text-tertiary);font-weight:400">(visit a page first, then the target — same context)</span></label>
    </div>
    <div class="field-row">
      <div class="field" style="max-width:240px">
        <label>Type</label>
        <select id="${prefix}-warmup-type">
          <option value="homepage">homepage (target's origin)</option>
          <option value="custom">custom URL</option>
        </select>
      </div>
      <div class="field" id="${prefix}-warmup-url-field" style="display:none">
        <label>Warmup URL</label>
        <input id="${prefix}-warmup-url" type="text" placeholder="https://example.com/" />
      </div>
      <div class="field" style="max-width:180px">
        <label>Dwell (ms) <span style="color:var(--text-tertiary);font-weight:400">opt.</span></label>
        <input id="${prefix}-warmup-dwell" type="number" min="0" max="60000" placeholder="default 2500" />
      </div>
    </div>`;
  // The load-time tooltip pass already ran before this dynamic render, so wire
  // the freshly-injected info icon now (idempotent).
  wireInfoIcons(container);
  // Reveal the custom-URL field only for type=custom.
  const typeSel = document.getElementById(`${prefix}-warmup-type`);
  const urlField = document.getElementById(`${prefix}-warmup-url-field`);
  const syncWarmupType = () => {
    if (urlField) urlField.style.display = (typeSel && typeSel.value === 'custom') ? '' : 'none';
  };
  if (typeSel) typeSel.addEventListener('change', syncWarmupType);
  syncWarmupType();
}

function collectWarmup(prefix) {
  const en = document.getElementById(`${prefix}-warmup-enable`);
  if (!en || !en.checked) return null;
  const type = document.getElementById(`${prefix}-warmup-type`)?.value || 'homepage';
  const w = { type };
  if (type === 'custom') {
    const u = (document.getElementById(`${prefix}-warmup-url`)?.value || '').trim();
    if (u) w.url = u;
  }
  const d = (document.getElementById(`${prefix}-warmup-dwell`)?.value || '').trim();
  if (d) w.dwell_ms = Number(d);
  return w;
}

renderProxyComponent('s', document.getElementById('s-proxy-component'));
renderWarmupComponent('s', document.getElementById('s-warmup-component'));
initProxyPool('s');
// Render the Preset Builder proxy component eagerly so restoreState can restore
// its type/geo values; initProxyPool('pw') stays lazy (fired on first tab click)
// to avoid fetching available proxy pools before the user ever opens the tab.
renderProxyComponent('pw', document.getElementById('pw-proxy-component'));

// Read a proxy block into a {proxy_type, proxy_pool_id?, proxy_geo?,
// prem_proxy_options?} object. Returns null for the Search tab's "default
// (preset)" sentinel (empty type) so the caller omits proxy entirely; returns
// false on a validation failure (the user was already alerted) so the caller
// can abort.
function collectProxy(prefix) {
  const $ = (suffix) => document.getElementById(`${prefix}-${suffix}`);
  const val = (suffix) => ($(suffix)?.value || '').trim();
  const checked = (suffix) => !!$(suffix)?.checked;
  const radio = (name) =>
    document.querySelector(`input[name="${prefix}-${name}"]:checked`)?.value || '';

  const type = $('proxy-type').value;
  if (!type) return null;
  const proxy = { proxy_type: type };
  const poolId = val('proxy-pool');
  if (poolId) proxy.proxy_pool_id = poolId;
  // The v2 premium gateway is keyed on the API account, not a purchased pool —
  // so it (and 'none') are exempt from the legacy pool-id requirement.
  if (type !== 'none' && type !== 'prem_res_rotating' && !poolId) {
    alert(`Select a Pool ID for proxy type "${type}". If none available, use the "Buy" button to purchase one on CyberYozh.`);
    return false;
  }
  if (type === 'res_rotating') {
    const cc = val('geo-country');
    const region = val('geo-region');
    const city = val('geo-city');
    if (cc || region || city) {
      proxy.proxy_geo = {};
      if (cc) proxy.proxy_geo.country_code = cc.toUpperCase();
      if (region) proxy.proxy_geo.region = region;
      if (city) proxy.proxy_geo.city = city;
    }
  }
  if (type === 'prem_res_rotating') {
    const cc = val('geo-country');
    proxy.proxy_geo = cc ? { country_code: cc.toUpperCase() } : {};
    const prem = {};
    const su = val('prem-subuser'); if (su) prem.sub_user_id = su;
    const ipf = val('prem-ipfilter'); if (ipf) prem.ip_filter = ipf;
    const isp = val('prem-isp'); if (isp) prem.isp = isp;
    if (checked('prem-zip-toggle')) {
      // ZIP targeting is mutually exclusive with region/city (server enforces).
      const zip = val('prem-zip'); if (zip) prem.zip = zip;
    } else {
      const r = val('prem-region'); if (r) proxy.proxy_geo.region = r;
      const ci = val('prem-city'); if (ci) proxy.proxy_geo.city = ci;
    }
    const sess = radio('prem-session') || 'rotating';
    prem.session_type = sess;
    if (sess === 'sticky') {
      const rot = val('prem-rotation'); if (rot) prem.rotation_minutes = Number(rot);
    }
    proxy.prem_proxy_options = prem;
  }
  return proxy;
}

renderProxyComponent('se', document.getElementById('se-proxy-component'));
renderWarmupComponent('se', document.getElementById('se-warmup-component'));
// Search tab uses an empty-value sentinel so collectProxy returns null (= no proxy
// override, letting the google_search_chromium preset decide). Prepend it to the type select
// that renderProxyComponent just created.
(function() {
  const typeSel = document.getElementById('se-proxy-type');
  if (!typeSel) return;
  const sentinel = new Option('default (preset)', '');
  typeSel.insertBefore(sentinel, typeSel.firstChild);
  typeSel.value = '';  // select the sentinel by default
})();
initProxyPool('se');

// ─── MCP helpers ─────────────────────────────────────────────────────────────
function mcpTarget() {
  return document.getElementById('mcp-target')?.value || 'scraper';
}

async function mcpPost(body, sessionId) {
  const headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json, text/event-stream',
  };
  if (sessionId) headers['mcp-session-id'] = sessionId;

  // Crawler has CORS enabled — call directly. Scraper goes through the tester
  // proxy via x-scraper-target header (established pattern).
  let url;
  if (mcpTarget() === 'crawler') {
    url = `${crawlerUrl()}/mcp`;
  } else {
    url = '/proxy/mcp';
    headers['x-scraper-target'] = scraperUrl();
  }

  const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });
  const newSession = res.headers.get('mcp-session-id');
  const text = await res.text();

  let parsed = null;
  if (text.startsWith('data:')) {
    const lines = text.split('\n').filter(l => l.startsWith('data:'));
    for (const line of lines) {
      try { parsed = JSON.parse(line.slice(5).trim()); if (parsed?.result !== undefined || parsed?.error) break; }
      catch {}
    }
  } else {
    try { parsed = JSON.parse(text); } catch { parsed = text; }
  }

  return { parsed, newSession, rawText: text };
}

async function mcpInitSession() {
  const { parsed, newSession } = await mcpPost({
    jsonrpc: '2.0', id: 1,
    method: 'initialize',
    params: {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'scraper-tester', version: '1.0' },
    },
  }, null);

  const sid = newSession;
  if (sid) {
    // Send initialized notification
    await mcpPost({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }, sid);
  }
  return { sid, serverInfo: parsed };
}

// ─── MCP ──────────────────────────────────────────────────────────────────────
function renderToolSchema(tool) {
  const el = document.getElementById('mcp-tool-schema');
  if (!el) return;
  const rootSchema = tool.inputSchema;
  if (!rootSchema || !rootSchema.properties || !Object.keys(rootSchema.properties).length) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  const defs = rootSchema.$defs || rootSchema.definitions || {};

  const resolveRef = (ref) => {
    if (!ref) return null;
    const parts = ref.split('/').filter(Boolean);
    // Expect something like #/$defs/ScrapeRequest or #/definitions/ScrapeRequest
    const name = parts[parts.length - 1];
    return defs[name] || null;
  };

  const typeLabel = (p) => {
    if (!p) return 'any';
    if (p.$ref) return p.$ref.split('/').pop();
    if (Array.isArray(p.type)) return p.type.join('|');
    if (p.type === 'array') {
      const item = p.items || {};
      return `array<${typeLabel(item)}>`;
    }
    if (p.type) return p.type;
    if (p.anyOf) return p.anyOf.map(typeLabel).join(' | ');
    return 'any';
  };

  const renderProps = (schema, required, depth = 0) => {
    if (schema.$ref) schema = resolveRef(schema.$ref) || {};
    const props = schema.properties || {};
    const req = new Set(required || schema.required || []);
    return Object.entries(props).map(([name, p]) => {
      const resolved = p.$ref ? resolveRef(p.$ref) : null;
      const effective = resolved || p;
      const isRequired = req.has(name);
      const type = typeLabel(p);
      const dflt = p.default !== undefined
        ? ` <span style="color:var(--text-tertiary)">= ${escapeHtml(JSON.stringify(p.default))}</span>`
        : '';
      const desc = p.description || effective.description;
      const descHtml = desc
        ? `<div style="color:var(--text-secondary);margin-top:0.2rem">${escapeHtml(desc)}</div>`
        : '';
      const indent = depth * 16;

      // Recurse into objects (one level via $ref or inline) and into array items
      let nested = '';
      const objSchema = resolved || (effective.type === 'object' ? effective : null);
      if (objSchema && objSchema.properties) {
        nested = `<div style="margin-left:1rem;margin-top:0.3rem;padding-left:0.6rem;border-left:2px solid var(--border)">${renderProps(objSchema, objSchema.required, depth + 1)}</div>`;
      } else if (effective.type === 'array' && effective.items) {
        const item = effective.items.$ref ? resolveRef(effective.items.$ref) : effective.items;
        if (item && item.properties) {
          nested = `<div style="margin-left:1rem;margin-top:0.3rem;padding-left:0.6rem;border-left:2px solid var(--border)"><div style="color:var(--text-tertiary);font-size:11px;text-transform:uppercase;letter-spacing:0.05em">each item</div>${renderProps(item, item.required, depth + 1)}</div>`;
        }
      }

      return `
        <div style="padding:0.5rem 0;border-bottom:1px solid var(--border);margin-left:${indent}px">
          <div>
            <b>${escapeHtml(name)}</b>${isRequired ? '<span style="color:var(--color-red);font-weight:600">*</span>' : ''}
            <span style="color:var(--color-blue);font-family:var(--mono);font-size:12px">${escapeHtml(type)}</span>${dflt}
          </div>
          ${descHtml}
          ${nested}
        </div>`;
    }).join('');
  };

  const rows = renderProps(rootSchema, rootSchema.required);
  const count = Object.keys(rootSchema.properties).length;

  el.style.display = 'block';
  el.innerHTML = `
    <details open style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;padding:0.6rem 0.85rem;margin:0.4rem 0">
      <summary class="details-summary" style="cursor:pointer;list-style:none;display:flex;align-items:center;gap:0.5rem;font-size:12px;color:var(--text-secondary)">
        <span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span>
        <span>Schema (${count} top-level field${count === 1 ? '' : 's'})</span>
      </summary>
      <div style="margin-top:0.4rem;font-size:12px">${rows}</div>
    </details>`;
}

document.getElementById('btnListTools').addEventListener('click', async () => {
  const container = document.getElementById('mcp-tools-list');
  container.innerHTML = '<span style="color:var(--text-muted)">Initializing session...</span>';

  try {
    const { sid, serverInfo } = await mcpInitSession();
    mcpSessionId = sid;

    container.innerHTML = '<span style="color:var(--text-muted)">Loading tools...</span>';

    const { parsed } = await mcpPost({
      jsonrpc: '2.0', id: 2,
      method: 'tools/list',
      params: {},
    }, mcpSessionId);

    const tools = parsed?.result?.tools || [];

    if (!tools.length) {
      container.innerHTML = '<span style="color:var(--text-muted)">No tools found</span>';
      showResult('mcp-result', parsed);
      return;
    }

    container.innerHTML = `<span style="color:var(--success);font-size:12px">Session: ${escapeHtml(mcpSessionId || 'stateless')} — ${tools.length} tools</span>`;
    tools.forEach(tool => {
      const div = document.createElement('div');
      div.className = 'mcp-tool';
      div.innerHTML = `
        <div class="mcp-tool-name">${escapeHtml(tool.name)}</div>
        <div class="mcp-tool-desc">${escapeHtml(tool.description || '')}</div>`;
      div.addEventListener('click', () => {
        document.getElementById('mcp-tool-name').value = tool.name;
        renderToolSchema(tool);
        if (tool.inputSchema?.properties) {
          const template = {};
          Object.keys(tool.inputSchema.properties).forEach(k => {
            template[k] = tool.inputSchema.properties[k].default ?? '';
          });
          document.getElementById('mcp-tool-args').value = JSON.stringify(template, null, 2);
        }
      });
      container.appendChild(div);
    });
  } catch (e) {
    container.innerHTML = `<span style="color:var(--error)">${escapeHtml(e.message)}</span>`;
  }
});

document.getElementById('btnCallTool').addEventListener('click', async () => {
  const toolName = document.getElementById('mcp-tool-name').value.trim();
  if (!toolName) { alert('Enter tool name'); return; }

  let args = {};
  try {
    const raw = document.getElementById('mcp-tool-args').value.trim();
    if (raw) args = JSON.parse(raw);
  } catch { alert('Invalid JSON in arguments'); return; }

  showResult('mcp-result', '');
  document.getElementById('mcp-result').innerHTML =
    '<span style="color:var(--text-muted)">Calling tool...</span>';

  try {
    // Init session if not exists
    if (!mcpSessionId) {
      const { sid } = await mcpInitSession();
      mcpSessionId = sid;
    }

    const { parsed, rawText } = await mcpPost({
      jsonrpc: '2.0',
      id: Date.now(),
      method: 'tools/call',
      params: { name: toolName, arguments: args },
    }, mcpSessionId);

    showResult('mcp-result', parsed || rawText);
  } catch (e) {
    showResult('mcp-result', { error: e.message });
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// CRAWLER TAB
// ═══════════════════════════════════════════════════════════════════════════
function crawlerUrl() {
  return document.getElementById('crawlerUrl').value.replace(/\/$/, '');
}

async function crawlerCall(path, options = {}) {
  // Direct call to the crawler service (CORS-enabled). Bypasses the tester proxy
  // because EventSource can't send custom headers (we'd need x-crawler-target).
  const res = await fetch(`${crawlerUrl()}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  const text = await res.text();
  try { return { ok: res.ok, status: res.status, data: JSON.parse(text) }; }
  catch { return { ok: res.ok, status: res.status, data: text }; }
}

// ─── Map (fast URL discovery) ─────────────────────────────────────────────────
function renderMapResults(el, data) {
  el.innerHTML = '';
  if (!data || !Array.isArray(data.urls)) {
    showResult('map-result', data);
    return;
  }
  const stats = data.stats || {};
  const took = typeof data.took_ms === 'number'
    ? (data.took_ms >= 1000 ? `${(data.took_ms / 1000).toFixed(1)}s` : `${data.took_ms}ms`)
    : null;
  const summary = document.createElement('div');
  summary.style.cssText = 'margin:0 0 0.6rem;font-size:12px;color:var(--text-secondary)';
  summary.innerHTML = `<b style="color:var(--text-primary)">${data.count}</b> URL${data.count === 1 ? '' : 's'} `
    + (took ? `<span style="color:var(--text-muted)">in ${took}</span> ` : '')
    + `<span style="color:var(--text-muted)">(sitemap ${stats.from_sitemap ?? 0} · page ${stats.from_page ?? 0} · unique ${stats.unique_in_scope ?? 0}`
    + (stats.with_lastmod ? ` · dated ${stats.with_lastmod}` : '') + `)</span>`;
  el.appendChild(summary);

  if (Array.isArray(data.warnings) && data.warnings.length) {
    const w = document.createElement('div');
    w.style.cssText = 'margin:0 0 0.6rem;padding:0.4rem 0.6rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:11px;color:var(--color-orange,#b45309)';
    w.textContent = '⚠ ' + data.warnings.join(' · ');
    el.appendChild(w);
  }

  const list = document.createElement('div');
  list.style.cssText = 'max-height:520px;overflow:auto;border:1px solid var(--border);border-radius:var(--radius-sm)';
  const lastmod = data.lastmod || {};
  data.urls.forEach((u) => {
    const row = document.createElement('a');
    row.href = u; row.target = '_blank'; row.rel = 'noopener noreferrer';
    row.style.cssText = 'display:flex;justify-content:space-between;gap:0.6rem;padding:0.3rem 0.6rem;font-size:12px;color:var(--color-blue);text-decoration:none;border-bottom:1px solid var(--border);word-break:break-all';
    const link = document.createElement('span');
    link.textContent = u;
    row.appendChild(link);
    if (lastmod[u]) {
      const date = document.createElement('span');
      date.textContent = lastmod[u];
      date.style.cssText = 'flex:none;color:var(--text-muted);font-variant-numeric:tabular-nums';
      row.appendChild(date);
    }
    list.appendChild(row);
  });
  el.appendChild(list);
}

document.getElementById('btnMap').addEventListener('click', async () => {
  const seed = normalizeUrl(document.getElementById('mp-seed').value);
  if (!seed) { alert('Seed URL is required'); return; }
  const payload = {
    seed_url: seed,
    scope: { mode: document.getElementById('mp-scope').value },
    include_sitemap: document.getElementById('mp-sitemap').checked,
    include_page_links: document.getElementById('mp-page-links').checked,
    render: document.getElementById('mp-render').checked,
    limit: Number(document.getElementById('mp-limit').value) || 1000,
  };
  const search = document.getElementById('mp-search').value.trim();
  if (search) payload.search = search;

  // Recency (sitemap lastmod). Only one of published_after / recent_days is
  // meaningful — published_after wins server-side.
  const publishedAfter = document.getElementById('mp-published-after').value;
  if (publishedAfter) payload.published_after = publishedAfter;
  const recentDays = document.getElementById('mp-recent-days').value.trim();
  if (recentDays) payload.recent_days = Number(recentDays);
  const sort = document.getElementById('mp-sort').value;
  if (sort) payload.sort = sort;

  // Proxy (same convention as the Scrape/Crawler tabs).
  const mpProxy = collectProxy('mp');
  if (mpProxy === false) return;  // invalid (missing pool) — already alerted
  if (mpProxy) Object.assign(payload, mpProxy);

  setStatus('map-status', 'queued', 'Mapping...');
  document.getElementById('map-result').innerHTML = '<span class="placeholder">Mapping…</span>';
  const { data, ok } = await crawlerCall('/api/v1/map', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  if (!ok) {
    setStatus('map-status', 'failed', 'Map failed');
    showResult('map-result', data);
    return;
  }
  setStatus('map-status', 'done', `${data.count} URL(s)`);
  renderMapResults(document.getElementById('map-result'), data);
});

renderProxyComponent('mp', document.getElementById('mp-proxy-component'));
initProxyPool('mp');

// ─── Crawler dynamic rows ────────────────────────────────────────────────────
document.getElementById('btnAddHeaderCrawler').addEventListener('click', () =>
  addDynamicRow('c-headers-list', ['Header name', 'Value']));

document.getElementById('btnAddCookieCrawler').addEventListener('click', () =>
  addDynamicRow('c-cookies-list', ['name', 'value', 'domain', 'path']));

document.getElementById('btnAddFieldCrawler').addEventListener('click', () =>
  addExtractField('c-extract-fields'));

// ─── Crawler header presets ──────────────────────────────────────────────────
function applyHeaderPresetCrawler(presetKey) {
  const headers = HEADER_PRESETS[presetKey];
  if (!headers) return;
  document.getElementById('c-headers-list').innerHTML = '';
  Object.entries(headers).forEach(([k, v]) => {
    addDynamicRow('c-headers-list', ['Header name', 'Value']);
    const rows = document.querySelectorAll('#c-headers-list .dynamic-row');
    const last = rows[rows.length - 1];
    const inputs = last.querySelectorAll('input');
    inputs[0].value = k;
    inputs[1].value = v;
  });
}

document.getElementById('btnApplyPresetCrawler').addEventListener('click', () => {
  const val = document.getElementById('c-header-preset').value;
  if (val) applyHeaderPresetCrawler(val);
});

document.getElementById('btnClearHeadersCrawler').addEventListener('click', () => {
  document.getElementById('c-headers-list').innerHTML = '';
  document.getElementById('c-header-preset').value = '';
});

// ─── Crawler proxy pool ───────────────────────────────────────────────────────
function updateCrawlerProxyWarning() {
  const warn = document.getElementById('c-proxy-warning');
  if (!warn) return;
  warn.style.display = document.getElementById('c-proxy-type').value === 'none' ? '' : 'none';
}
renderProxyComponent('c', document.getElementById('c-proxy-component'));
initProxyPool('c', { onChange: updateCrawlerProxyWarning });

// ─── Crawl Proxy pool (independent block, cp-* prefix) ───────────────────────
function updateCrawlProxyWarning() {
  const warn = document.getElementById('cp-proxy-warning');
  if (!warn) return;
  warn.style.display = document.getElementById('cp-proxy-type').value === 'none' ? '' : 'none';
}
renderProxyComponent('cp', document.getElementById('cp-proxy-component'));
initProxyPool('cp', { onChange: updateCrawlProxyWarning });

// ─── Enable-scraping toggle: show/hide the scraping section ─────────────────
function applyCrawlerScrapingVisibility() {
  const section = document.getElementById('c-scraping-section');
  if (!section) return;
  // Restore the intended display:flex explicitly — setting '' after 'none'
  // would not bring back the inline style originally set in HTML.
  section.style.display = document.getElementById('c-enable-scraping').checked ? 'flex' : 'none';
}
document.getElementById('c-enable-scraping').addEventListener('change', applyCrawlerScrapingVisibility);
applyCrawlerScrapingVisibility();

// ─── Build Crawl payload ─────────────────────────────────────────────────────
function buildCrawlPayload() {
  if (!warnIfDeviceNeedsNoRender('c-device', 'c-render')) return null;
  const seed = normalizeUrl(document.getElementById('c-seed-url').value);
  if (!seed) { alert('Seed URL is required'); return null; }

  const scope = {
    mode: document.getElementById('c-scope-mode').value,
    max_depth: Number(document.getElementById('c-max-depth').value) || 3,
    max_pages: Number(document.getElementById('c-max-pages').value) || 500,
    per_domain_rps: Number(document.getElementById('c-rps').value) || 1.0,
    per_domain_concurrency: Number(document.getElementById('c-concurrency').value) || 1,
    include_patterns: document.getElementById('c-include').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
    exclude_patterns: document.getElementById('c-exclude').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
  };

  const cProxy = collectProxy('c');
  if (cProxy === false) return null;  // invalid (missing pool) — already alerted

  const scrape = {
    render: document.getElementById('c-render').checked,
    wait_until: document.getElementById('c-wait-until').value,
    device: document.getElementById('c-device').value,
    timeout_ms: Number(document.getElementById('c-timeout').value) || 30000,
    block_assets: document.getElementById('c-block-assets').checked,
    screenshot: document.getElementById('c-screenshot').checked,
    stealth: document.getElementById('c-stealth').checked,
  };
  Object.assign(scrape, cProxy);

  const waitSelector = document.getElementById('c-wait-selector').value.trim();
  if (waitSelector) scrape.wait_for_selector = waitSelector;

  // Session — attaches the crawler's per-page scrape to a server-side session.
  const sessionId = document.getElementById('c-session-id')?.value.trim();
  if (sessionId) scrape.session_id = sessionId;

  const extractType = document.getElementById('c-extract-type').value;
  if (extractType) {
    const fieldRows = document.querySelectorAll('#c-extract-fields .dynamic-row');
    const fields = {};
    let totalRows = 0;
    fieldRows.forEach(row => {
      totalRows++;
      const inputs = row.querySelectorAll('input[type="text"]');
      const name = inputs[0].value.trim();
      const selector = inputs[1].value.trim();
      const attr = row.querySelector('select').value;
      const all = row.querySelector('input[type="checkbox"]').checked;
      if (name && selector) fields[name] = { selector, attr, all };
    });
    if (totalRows > 0 && Object.keys(fields).length === 0) {
      alert('Extraction is enabled but no fields have both a name and a selector set. Please fill them in or disable extraction.');
      return null;
    }
    if (Object.keys(fields).length) {
      const rowContainer = readContainer('c-extract-container', fields);
      if (rowContainer === undefined) return null;
      scrape.extract = { type: extractType, fields };
      if (rowContainer) scrape.extract.container = rowContainer;
    }
  }

  const headerRows = getRowValues('c-headers-list');
  if (headerRows.length) {
    scrape.headers = {};
    headerRows.forEach(([k, v]) => { if (k) scrape.headers[k] = v; });
  }

  const cookieRows = getRowValues('c-cookies-list');
  if (cookieRows.length) {
    scrape.cookies = cookieRows.filter(([n]) => n).map(([name, value, domain, path]) => ({
      name, value, domain: domain || undefined, path: path || undefined,
    }));
  }

  // Separate crawl proxy (used when enable_scraping=false)
  const cpProxy = collectProxy('cp');
  if (cpProxy === false) return null;  // invalid (missing pool) — already alerted
  const crawl_proxy = cpProxy && cpProxy.proxy_type !== 'none' ? cpProxy : null;

  return {
    seed_url: seed,
    scope,
    scrape_options: scrape,
    crawl_proxy,
    enable_scraping: document.getElementById('c-enable-scraping').checked,
  };
}

// ─── Crawler state + SSE + rendering ─────────────────────────────────────────
let currentCrawlId = null;
let currentCrawlSource = null;
let crawlCancelState = 'ready';    // ready | soft-sent | hard-sent
const crawlPages = new Map();      // url → page record
const crawlPagesOrder = [];         // insertion order for rendering

function setCrawlCancelButton(state) {
  const btn = document.getElementById('btnCancelCrawl');
  crawlCancelState = state;
  btn.classList.remove('btn-primary');
  btn.classList.add('btn-secondary');
  if (state === 'ready' || state === 'done') {
    btn.disabled = true;
    btn.style.display = 'none';
    btn.textContent = 'Cancel';
  } else if (state === 'armed') {
    btn.disabled = false;
    btn.style.display = '';
    btn.textContent = 'Cancel';
  } else if (state === 'soft-sent') {
    btn.disabled = false;
    btn.style.display = '';
    btn.classList.remove('btn-secondary');
    btn.classList.add('btn-primary');
    btn.textContent = 'Force stop';
  } else if (state === 'hard-sent') {
    btn.disabled = true;
    btn.style.display = '';
    btn.textContent = 'Force-stopping…';
  }
}

function resetCrawlUI() {
  crawlPages.clear();
  crawlPagesOrder.length = 0;
  document.getElementById('crawl-result').innerHTML =
    '<span class="placeholder">Waiting for crawler...</span>';
}

function renderCrawlStats(stats, jobId, status) {
  const el = document.getElementById('crawl-status');
  el.className = `status-bar ${status === 'running' ? 'running' : status === 'done' ? 'done' : status === 'cancelled' ? 'failed' : 'queued'}`;
  const spinner = status === 'running' ? '<div class="spinner"></div>' : '';
  const cap = document.getElementById('c-max-pages').value || '?';
  el.innerHTML = `
    ${spinner}
    <div style="flex:1;display:flex;flex-direction:column;gap:4px">
      <div style="display:flex;gap:0.6rem;align-items:center;font-size:12px;flex-wrap:wrap">
        <span><b>${jobId || '—'}</b></span>
        <span>·</span>
        <span>status: <b>${status}</b></span>
        <span>·</span>
        <span>visited: <b>${stats.visited ?? 0}</b> / ${cap}</span>
        <span>·</span>
        <span>queued: <b>${stats.queued ?? 0}</b></span>
        <span>·</span>
        <span>failed: <b>${stats.failed ?? 0}</b></span>
        <span>·</span>
        <span>dedup: ${stats.dedup_skipped ?? 0}</span>
        <span>·</span>
        <span>out-of-scope: ${stats.out_of_scope ?? 0}</span>
        <span>·</span>
        <span>retries: ${stats.retries_total ?? 0}</span>
      </div>
    </div>`;
}

function buildSiteMapText() {
  // Group by parent_url, preserve insertion order under each parent.
  const childrenOf = new Map();
  const roots = [];
  for (const url of crawlPagesOrder) {
    const p = crawlPages.get(url);
    const parent = p.parent_url;
    if (!parent || !crawlPages.has(parent)) {
      roots.push(p);
    } else {
      if (!childrenOf.has(parent)) childrenOf.set(parent, []);
      childrenOf.get(parent).push(p);
    }
  }
  const lines = [];
  function render(p, prefix, branchPrefix) {
    const tag = p.error ? 'ERR' : (p.status_code ?? '—');
    lines.push(`${prefix}${p.url}  [d=${p.depth}, ${tag}, ${p.took_ms}ms]`);
    const kids = childrenOf.get(p.url) || [];
    kids.forEach((child, i) => {
      const last = i === kids.length - 1;
      render(child, branchPrefix + (last ? '└── ' : '├── '), branchPrefix + (last ? '    ' : '│   '));
    });
  }
  roots.forEach((root, i) => {
    const last = i === roots.length - 1;
    render(root, '', last ? '    ' : '│   ');
  });
  return lines.join('\n');
}

function renderSiteMapBlock(containerEl, openKeys) {
  const wrap = document.createElement('details');
  wrap.dataset.key = 'sitemap';
  // Default open on first render, remember user's toggle after
  if (openKeys.size === 0 || openKeys.has('sitemap')) wrap.setAttribute('open', '');
  wrap.style.cssText = 'margin-bottom:0.75rem';

  const summary = document.createElement('summary');
  summary.className = 'details-summary';
  summary.style.cssText = 'cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none';
  summary.innerHTML = `<span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span style="flex:1"><b style="color:var(--text-primary)">Site Map</b> (${crawlPagesOrder.length} pages)</span>`;

  const btnCopy = document.createElement('button');
  btnCopy.textContent = 'Copy';
  btnCopy.className = 'btn-secondary btn-sm';
  btnCopy.addEventListener('click', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(buildSiteMapText());
      btnCopy.textContent = 'Copied!';
      setTimeout(() => { btnCopy.textContent = 'Copy'; }, 1200);
    } catch {
      btnCopy.textContent = 'Failed';
    }
  });
  summary.appendChild(btnCopy);
  wrap.appendChild(summary);

  const pre = document.createElement('pre');
  pre.id = 'sitemap-text';
  pre.style.cssText = 'max-height:320px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);border-top:none;padding:0.75rem;font-size:11px;line-height:1.5;border-radius:0 0 var(--radius-sm) var(--radius-sm);color:var(--text-primary);white-space:pre;margin:0';
  pre.textContent = buildSiteMapText();
  wrap.appendChild(pre);
  containerEl.appendChild(wrap);
}

function renderCrawlPages(enableScraping) {
  const el = document.getElementById('crawl-result');
  if (!crawlPagesOrder.length) {
    el.innerHTML = '<span class="placeholder">No pages yet...</span>';
    return;
  }

  const openKeys = new Set(
    Array.from(el.querySelectorAll('details[open][data-key]')).map(d => d.dataset.key)
  );
  const prevScroll = el.scrollTop;

  el.innerHTML = '';
  renderSiteMapBlock(el, openKeys);

  // Compact mode (enable_scraping=false): single table
  if (!enableScraping) {
    const rows = crawlPagesOrder.map(url => {
      const p = crawlPages.get(url);
      const statusColor = p.error ? 'var(--color-red)' : (p.status_code && p.status_code < 400 ? 'var(--color-green)' : 'var(--color-red)');
      return `
        <tr>
          <td style="font-family:var(--mono);font-size:11px;max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(p.url)}">${escapeHtml(p.url)}</td>
          <td style="font-family:var(--mono);font-size:11px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-secondary)" title="${escapeHtml(p.parent_url || '')}">${escapeHtml(p.parent_url || '—')}</td>
          <td style="text-align:center">${p.depth}</td>
          <td style="text-align:center;color:${statusColor}"><b>${p.error ? 'ERR' : (p.status_code ?? '—')}</b></td>
          <td style="text-align:right;color:var(--text-secondary)">${p.took_ms}ms</td>
        </tr>`;
    }).join('');
    const tableWrap = document.createElement('div');
    tableWrap.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead>
          <tr style="background:var(--bg-secondary);color:var(--text-secondary)">
            <th style="text-align:left;padding:0.4rem 0.5rem">URL</th>
            <th style="text-align:left;padding:0.4rem 0.5rem">Parent</th>
            <th style="padding:0.4rem 0.5rem">Depth</th>
            <th style="padding:0.4rem 0.5rem">Status</th>
            <th style="text-align:right;padding:0.4rem 0.5rem">Took</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
    el.appendChild(tableWrap);
    el.scrollTop = prevScroll;
    return;
  }

  // Rich mode (enable_scraping=true): reuse showResult-style per-page blocks
  crawlPagesOrder.forEach((url, i) => {
    const p = crawlPages.get(url);
    const sr = p.scrape_response || {};
    const meta = sr.meta || {};
    const badge = document.createElement('div');
    badge.style.cssText = 'margin:0.75rem 0 0.4rem;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary)';
    const errBadge = p.error ? ` &nbsp;|&nbsp; <b style="color:var(--color-red)">ERROR</b>` : '';
    badge.innerHTML = `
      <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
        <b style="color:var(--text-primary)">#${i+1}</b>
        <span style="font-family:var(--mono);font-size:11px;max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(p.url)}">${escapeHtml(p.url)}</span>
        <span>depth: <b>${p.depth}</b></span>
        <span>status: <b style="color:var(--color-green)">${p.status_code ?? '—'}</b></span>
        <span>took: ${p.took_ms}ms</span>${errBadge}
      </div>
      <div style="margin-top:2px;font-size:11px">parent: <code>${escapeHtml(p.parent_url || '—')}</code></div>`;
    el.appendChild(badge);

    if (p.error) {
      const err = document.createElement('pre');
      err.style.cssText = 'color:var(--color-red);padding:0.5rem;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;margin:0 0 0.5rem 0';
      err.textContent = p.error;
      el.appendChild(err);
      return;
    }

    // Extracted data
    if (sr.data && typeof sr.data === 'object' && Object.keys(sr.data).length > 0) {
      const wrap = document.createElement('details');
      wrap.dataset.key = `data-${p.url}`;
      if (openKeys.has(wrap.dataset.key) || openKeys.size === 0) wrap.setAttribute('open', '');
      wrap.style.cssText = 'margin-bottom:0.5rem';
      const fieldCount = Object.keys(sr.data).length;
      wrap.innerHTML = `<summary class="details-summary" style="cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none"><span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span>Extracted Data (${fieldCount} field${fieldCount === 1 ? '' : 's'})</span></summary>`;
      const pre = document.createElement('pre');
      pre.style.cssText = 'max-height:300px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);border-top:none;padding:0.75rem;font-size:12px;border-radius:0 0 var(--radius-sm) var(--radius-sm);color:var(--text-primary);white-space:pre-wrap;word-break:break-word';
      pre.innerHTML = syntaxHighlight(sr.data);
      wrap.appendChild(pre);
      el.appendChild(wrap);
    }

    // Raw HTML (only if user requested it in c-raw-html)
    if (sr.raw_html && document.getElementById('c-raw-html').checked) {
      const wrap = document.createElement('details');
      wrap.dataset.key = `html-${p.url}`;
      if (openKeys.has(wrap.dataset.key)) wrap.setAttribute('open', '');
      wrap.style.cssText = 'margin-bottom:0.5rem';
      wrap.innerHTML = `<summary class="details-summary" style="cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none"><span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span>Raw HTML (${sr.raw_html.length} chars)</span></summary>`;
      const code = document.createElement('pre');
      code.style.cssText = 'max-height:300px;overflow:auto;background:var(--bg-primary);border:1px solid var(--border);border-top:none;padding:0.75rem;font-size:11px;border-radius:0 0 var(--radius-sm) var(--radius-sm);color:var(--text-primary)';
      code.textContent = sr.raw_html;
      wrap.appendChild(code);
      el.appendChild(wrap);
    }

    // Screenshot
    if (sr.screenshot_base64) {
      const wrap = document.createElement('details');
      wrap.dataset.key = `shot-${p.url}`;
      if (openKeys.has(wrap.dataset.key)) wrap.setAttribute('open', '');
      wrap.style.cssText = 'margin-bottom:0.5rem';
      wrap.innerHTML = `<summary class="details-summary" style="cursor:pointer;padding:0.5rem 0.75rem;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:0.5rem;list-style:none"><span class="details-chevron" style="display:inline-block;transition:transform 0.15s;font-size:10px">▶</span><span>Screenshot</span></summary>`;
      const img = document.createElement('img');
      img.src = `data:image/png;base64,${sr.screenshot_base64}`;
      img.style.cssText = 'width:100%;display:block;border:1px solid var(--border);border-top:none;border-radius:0 0 var(--radius-sm) var(--radius-sm)';
      wrap.appendChild(img);
      el.appendChild(wrap);
    }
  });
  el.scrollTop = prevScroll;
}

// Throttle re-renders so 500 pages don't tank the UI
let renderScheduled = false;
let renderEnableScraping = false;
function scheduleRender() {
  if (renderScheduled) return;
  renderScheduled = true;
  requestAnimationFrame(() => {
    renderScheduled = false;
    renderCrawlPages(renderEnableScraping);
  });
}

// ─── Submit crawl + consume SSE ──────────────────────────────────────────────
document.getElementById('btnCrawl').addEventListener('click', async () => {
  const payload = buildCrawlPayload();
  if (!payload) return;

  // Close previous stream if any
  if (currentCrawlSource) { try { currentCrawlSource.close(); } catch {} currentCrawlSource = null; }

  resetCrawlUI();
  renderEnableScraping = !!payload.enable_scraping;

  const el = document.getElementById('crawl-status');
  el.className = 'status-bar queued';
  el.innerHTML = '<div class="spinner"></div><span>Submitting crawl...</span>';

  const { ok, data, status } = await crawlerCall('/api/v1/crawl', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

  if (!ok || !data?.job_id) {
    el.className = 'status-bar failed';
    el.innerHTML = `<span>Failed to submit: ${escapeHtml(JSON.stringify(data).slice(0, 300))}</span>`;
    return;
  }

  currentCrawlId = data.job_id;
  setCrawlCancelButton('armed');
  renderCrawlStats({}, currentCrawlId, 'queued');

  // Open SSE stream directly to the crawler (CORS-enabled, EventSource can't send headers)
  const source = new EventSource(`${crawlerUrl()}/api/v1/crawl/${currentCrawlId}/events`);
  currentCrawlSource = source;

  source.addEventListener('stats', (e) => {
    try {
      const ev = JSON.parse(e.data);
      renderCrawlStats(ev.stats, currentCrawlId, 'running');
    } catch {}
  });

  source.addEventListener('page', (e) => {
    try {
      const ev = JSON.parse(e.data);
      const p = ev.page;
      if (!crawlPages.has(p.url)) crawlPagesOrder.push(p.url);
      crawlPages.set(p.url, p);
      scheduleRender();
    } catch {}
  });

  source.addEventListener('page_error', (e) => {
    try {
      const ev = JSON.parse(e.data);
      console.warn('crawl page_error', ev);
    } catch {}
  });

  source.addEventListener('error', (e) => {
    // EventSource-level transport error (connection dropped, CORS, etc).
    // Browser auto-reconnects — we only surface if the stream looks dead.
    if (source.readyState === EventSource.CLOSED) {
      const el = document.getElementById('crawl-status');
      el.className = 'status-bar failed';
      el.innerHTML = '<span>Stream closed (transport error)</span>';
    }
  });

  source.addEventListener('done', (e) => {
    try {
      const ev = JSON.parse(e.data);
      renderCrawlStats(ev.stats, currentCrawlId, ev.status || 'done');
    } catch {}
    source.close();
    currentCrawlSource = null;
    setCrawlCancelButton('done');
    scheduleRender();
  });

  source.addEventListener('cancelled', (e) => {
    try {
      const ev = JSON.parse(e.data);
      renderCrawlStats(ev.stats, currentCrawlId, 'cancelled');
    } catch {}
    source.close();
    currentCrawlSource = null;
    setCrawlCancelButton('done');
    scheduleRender();
  });
});

document.getElementById('btnCancelCrawl').addEventListener('click', async () => {
  if (!currentCrawlId) return;

  if (crawlCancelState === 'armed') {
    // First click — soft cancel. Workers stop dispatching new URLs, in-flight
    // requests drain. Button becomes "Force stop" for a second click.
    setCrawlCancelButton('soft-sent');
    await crawlerCall(`/api/v1/crawl/${currentCrawlId}?hard=false`, { method: 'DELETE' });
  } else if (crawlCancelState === 'soft-sent') {
    // Second click — hard cancel. Task.cancel() propagates through httpx and
    // drops the in-flight fetch. The scraper-side job is orphaned (no cancel
    // API upstream yet) but the crawler unblocks immediately.
    setCrawlCancelButton('hard-sent');
    await crawlerCall(`/api/v1/crawl/${currentCrawlId}?hard=true`, { method: 'DELETE' });
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// CUSTOM SELECT — replaces the native dropdown popup with a styled panel.
// Keeps the underlying <select> intact so buildPayload() and value reads work.
// ═══════════════════════════════════════════════════════════════════════════
function enhanceSelect(select) {
  if (select.dataset.enhanced) return;
  // Skip selects we don't want to enhance (opt-out via data-no-enhance)
  if (select.hasAttribute('data-no-enhance')) return;
  select.dataset.enhanced = '1';

  const wrap = document.createElement('div');
  wrap.className = 'custom-select';
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'custom-select-trigger';
  trigger.innerHTML = `
    <span class="custom-select-label"></span>
    <svg class="custom-select-chevron" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M5 7.5L10 12.5L15 7.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
  wrap.appendChild(trigger);

  const dropdown = document.createElement('ul');
  dropdown.className = 'custom-select-dropdown';
  dropdown.style.display = 'none';
  dropdown.setAttribute('role', 'listbox');
  wrap.appendChild(dropdown);

  const labelEl = trigger.querySelector('.custom-select-label');

  const esc = (s) => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

  function setLabel() {
    const opt = select.options[select.selectedIndex];
    const text = opt ? opt.textContent : '';
    const empty = !opt || opt.value === '' || text.startsWith('—');
    labelEl.textContent = '';
    if (opt && opt.dataset && opt.dataset.favicon) {
      const ic = document.createElement('img');
      ic.className = 'cs-favicon';
      ic.src = opt.dataset.favicon;
      ic.width = 16;
      ic.height = 16;
      ic.alt = '';
      ic.onerror = function () { this.style.display = 'none'; };
      labelEl.appendChild(ic);
    }
    labelEl.appendChild(document.createTextNode(text || '—'));
    labelEl.classList.toggle('placeholder', empty);
  }

  // Long lists (e.g. the ~250-country geo select) get an in-dropdown
  // filter. Short selects (device, wait, proxy type…) are left untouched.
  const CS_SEARCH_MIN = 12;

  function applyFilter(q) {
    const needle = q.trim().toLowerCase();
    dropdown.querySelectorAll('.custom-select-option').forEach(li => {
      li.style.display = (!needle || li.textContent.toLowerCase().includes(needle))
        ? '' : 'none';
    });
  }

  function rebuildOptions() {
    dropdown.innerHTML = '';
    const groupName = 'cs-' + (select.id || select.name || Math.random().toString(36).slice(2, 8));
    if (select.options.length >= CS_SEARCH_MIN) {
      const sli = document.createElement('li');
      sli.className = 'custom-select-search';
      const si = document.createElement('input');
      si.type = 'text';
      si.placeholder = 'search…';
      si.autocomplete = 'off';
      si.addEventListener('click', (e) => e.stopPropagation());
      si.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { closeDropdown(); trigger.focus(); return; }
        e.stopPropagation();
      });
      si.addEventListener('input', () => applyFilter(si.value));
      sli.appendChild(si);
      dropdown.appendChild(sli);
    }
    Array.from(select.options).forEach((opt, i) => {
      const li = document.createElement('li');
      li.className = 'custom-select-option';
      li.setAttribute('role', 'option');
      li.dataset.value = opt.value;
      const selected = opt.value === select.value;
      if (selected) li.setAttribute('aria-selected', 'true');
      const optId = `${groupName}-${i}`;
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = groupName;
      input.id = optId;
      input.value = opt.value;
      if (selected) input.checked = true;
      const label = document.createElement('label');
      label.setAttribute('for', optId);
      label.textContent = opt.textContent;
      li.appendChild(input);
      if (opt.dataset.favicon) {
        const ic = document.createElement('img');
        ic.className = 'cs-favicon';
        ic.src = opt.dataset.favicon;
        ic.width = 16;
        ic.height = 16;
        ic.loading = 'lazy';
        ic.alt = '';
        ic.onerror = function () { this.style.display = 'none'; };
        li.appendChild(ic);
      }
      li.appendChild(label);
      li.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (select.value !== opt.value) {
          select.value = opt.value;
          select.dispatchEvent(new Event('change', { bubbles: true }));
        }
        setLabel();
        closeDropdown();
      });
      dropdown.appendChild(li);
    });
  }

  // The dropdown is rendered `position: fixed` (anchored to the trigger via
  // getBoundingClientRect) rather than absolutely inside the wrapper, so it
  // escapes the scrollable `.two-col > *` panel that would otherwise clip it.
  // Flips above the trigger when there isn't room below.
  function positionDropdown() {
    // No destroy hook exists: if a panel re-render removed the trigger while we
    // were open, close so the scroll/resize listeners below are torn down
    // instead of leaking a closure over the detached node.
    if (!trigger.isConnected) { closeDropdown(); return; }
    const r = trigger.getBoundingClientRect();
    const gap = 8;
    const vh = window.innerHeight;
    // Trigger scrolled out of the viewport — close rather than leave the menu
    // floating detached from its (now off-screen) anchor.
    if (r.bottom <= 0 || r.top >= vh) { closeDropdown(); return; }
    dropdown.style.position = 'fixed';
    dropdown.style.left = r.left + 'px';
    dropdown.style.width = r.width + 'px';
    dropdown.style.right = 'auto';
    const spaceBelow = vh - r.bottom - gap;
    const spaceAbove = r.top - gap;
    const flipUp = spaceBelow < Math.min(440, dropdown.scrollHeight)
      && spaceAbove > spaceBelow;
    const maxH = Math.max(120, Math.min(440, flipUp ? spaceAbove : spaceBelow));
    dropdown.style.maxHeight = maxH + 'px';
    if (flipUp) {
      dropdown.style.top = 'auto';
      dropdown.style.bottom = (vh - r.top + gap) + 'px';
    } else {
      dropdown.style.bottom = 'auto';
      dropdown.style.top = (r.bottom + gap) + 'px';
    }
  }

  function openDropdown() {
    rebuildOptions();
    // Re-sync the trigger label from the live <select> value too. The label
    // otherwise only updates on `change`/mutation, so a programmatic
    // `select.value = x` set without a change event (e.g. a dropdown refresh)
    // would leave the label stale while rebuildOptions marks the real value —
    // the two would visibly disagree. Reading both from select.value here makes
    // the checkmark and the label provably consistent on every open.
    setLabel();
    if (!dropdown.children.length) return;
    wrap.classList.add('open');
    dropdown.style.display = '';
    positionDropdown();
    // Keep it anchored while the page or a parent panel scrolls/resizes.
    window.addEventListener('scroll', positionDropdown, true);
    window.addEventListener('resize', positionDropdown);
    // Focus the in-dropdown filter (long lists) so the user can type at once.
    const si = dropdown.querySelector('.custom-select-search input');
    if (si) requestAnimationFrame(() => si.focus());
    // Close other open custom selects
    document.querySelectorAll('.custom-select.open').forEach(el => {
      if (el !== wrap) el.dispatchEvent(new CustomEvent('cs:close'));
    });
  }
  function closeDropdown() {
    wrap.classList.remove('open');
    dropdown.style.display = 'none';
    window.removeEventListener('scroll', positionDropdown, true);
    window.removeEventListener('resize', positionDropdown);
  }
  wrap.addEventListener('cs:close', closeDropdown);

  trigger.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (select.disabled) return;
    wrap.classList.contains('open') ? closeDropdown() : openDropdown();
  });
  trigger.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); trigger.click(); }
    if (e.key === 'Escape') closeDropdown();
  });

  // Click outside — close
  document.addEventListener('click', (e) => {
    if (!wrap.contains(e.target)) closeDropdown();
  });

  // Mirror disabled state to trigger
  const syncDisabled = () => { trigger.disabled = select.disabled; };

  // Mirror select's own display (hide wrapper if existing code hides the select)
  const syncVisibility = () => {
    const d = select.style.display;
    wrap.style.display = (d === 'none') ? 'none' : '';
  };

  // External value changes (select.value = X programmatically) — refresh label
  select.addEventListener('change', () => { setLabel(); });

  // Observe options, style (display), disabled attr
  new MutationObserver(() => {
    syncDisabled();
    syncVisibility();
    setLabel();
  }).observe(select, {
    attributes: true,
    attributeFilter: ['style', 'disabled'],
    childList: true,
    subtree: true,
  });

  syncDisabled();
  syncVisibility();
  setLabel();
}

function enhanceAllSelects(root) {
  (root || document).querySelectorAll('select').forEach(enhanceSelect);
}

enhanceAllSelects();

// Catch dynamically inserted selects (+Add Field rows, etc.)
new MutationObserver((muts) => {
  muts.forEach(m => m.addedNodes.forEach(n => {
    if (n.nodeType !== 1) return;
    if (n.tagName === 'SELECT') enhanceSelect(n);
    if (n.querySelectorAll) n.querySelectorAll('select').forEach(enhanceSelect);
  }));
}).observe(document.body, { childList: true, subtree: true });

// ═══════════════════════════════════════════════════════════════════════════
// Persist tab + form values across F5 — localStorage-backed.
// Dev convenience only; cookies/headers persisted here are in clear text.
// ═══════════════════════════════════════════════════════════════════════════
const STATE_KEY = 'scraper-tester:state:v1';
const DYNAMIC_CONTAINERS = {
  'headers-list':     'kv',
  'cookies-list':     'cookie',
  'extract-fields':   'extract',
  'b-headers-list':   'kv',
  'b-cookies-list':   'cookie',
  'b-extract-fields': 'extract',
  'c-headers-list':   'kv',
  'c-cookies-list':   'cookie',
  'c-extract-fields': 'extract',
};
const _KV_PH = ['Header name', 'Value'];
const _COOKIE_PH = ['name', 'value', 'domain', 'path'];
let _stateSaveTimer = null;

function captureState() {
  const state = {
    activeTab: document.querySelector('.tab-btn.active')?.dataset.tab || null,
    inputs: {},
    dynamicRows: {},
  };
  document.querySelectorAll('input[id], select[id], textarea[id]').forEach(el => {
    if (!el.id) return;
    // Skip selects populated dynamically from APIs — restoring stale values
    // would conflict with the refresh fetches on page load (loadPremCatalogs
    // repopulates the prem sub-user / ip-filter lists; the geo cascade fills
    // region/city/isp/zip async from /geo, so a restored value would no-op
    // against an as-yet-empty list).
    if (el.id.endsWith('-proxy-pool-select') || el.id.endsWith('-geo-country') ||
        el.id.endsWith('-prem-subuser') || el.id.endsWith('-prem-ipfilter') ||
        el.id.endsWith('-prem-region') || el.id.endsWith('-prem-city') ||
        el.id.endsWith('-prem-isp') || el.id.endsWith('-prem-zip')) return;
    // Session pickers are repopulated from /api/v1/sessions on load; preserving
    // their value via restoreState would point at sessions that no longer exist.
    if (el.id === 's-session-id' || el.id === 'b-session-id' ||
        el.id === 'c-session-id' || el.id === 'sess-login-session-id') return;
    // Skip credential inputs — never persist passwords / TOTP secrets to localStorage.
    if (el.id.startsWith('sess-creds-')) return;
    // Skip ephemeral inputs
    if (el.id === 'mcp-tool-schema' || el.id === 'mcp-tool-name' || el.id === 'mcp-tool-args') return;
    if (el.id === 'j-job-id') return;
    // Skip inputs nested inside .dynamic-row — those get captured separately.
    if (el.closest('.dynamic-row')) return;
    if (el.type === 'checkbox') state.inputs[el.id] = el.checked;
    else state.inputs[el.id] = el.value;
  });
  Object.keys(DYNAMIC_CONTAINERS).forEach(cid => {
    const container = document.getElementById(cid);
    if (!container) return;
    const rows = Array.from(container.querySelectorAll('.dynamic-row')).map(row => ({
      texts: Array.from(row.querySelectorAll('input[type="text"]')).map(i => i.value),
      select: row.querySelector('select')?.value,
      checkbox: row.querySelector('input[type="checkbox"]')?.checked,
    }));
    if (rows.length) state.dynamicRows[cid] = rows;
  });
  return state;
}

let _stateSaveWarned = false;
function persistState() {
  try {
    localStorage.setItem(STATE_KEY, JSON.stringify(captureState()));
  } catch (e) {
    // QuotaExceededError is the common case — too much state (big HTML in
    // extract-field placeholders, thousands of dynamic rows). Warn ONCE so
    // we don't spam the console every keystroke.
    if (!_stateSaveWarned) {
      _stateSaveWarned = true;
      console.warn('scraper-tester: cannot persist UI state to localStorage:', e?.message || e);
    }
  }
}
function schedulePersist() {
  clearTimeout(_stateSaveTimer);
  _stateSaveTimer = setTimeout(persistState, 300);
}

function restoreState() {
  let state;
  try { state = JSON.parse(localStorage.getItem(STATE_KEY) || 'null'); } catch { return; }
  if (!state) return;
  _restoringState = true;

  Object.entries(state.inputs || {}).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') el.checked = !!value;
    else el.value = value;
  });

  Object.entries(state.dynamicRows || {}).forEach(([cid, rows]) => {
    const type = DYNAMIC_CONTAINERS[cid];
    const container = document.getElementById(cid);
    if (!type || !container) return;
    container.innerHTML = '';
    rows.forEach(row => {
      if (type === 'extract') {
        addExtractField(cid);
      } else {
        addDynamicRow(cid, type === 'kv' ? _KV_PH : _COOKIE_PH);
      }
      const last = container.querySelector('.dynamic-row:last-child');
      if (!last) return;
      const texts = last.querySelectorAll('input[type="text"]');
      (row.texts || []).forEach((v, i) => { if (texts[i]) texts[i].value = v; });
      const sel = last.querySelector('select');
      if (sel && row.select !== undefined) sel.value = row.select;
      const cb = last.querySelector('input[type="checkbox"]');
      if (cb && row.checkbox !== undefined) cb.checked = !!row.checkbox;
    });
  });

  // Fire change on selects / toggles so dependent refreshers re-run
  // (proxy pool visibility, Enable-scraping section, custom-select labels).
  ['s-proxy-type','b-proxy-type','c-proxy-type','cp-proxy-type','mp-proxy-type',
   's-extract-type','b-extract-type','c-extract-type',
   'c-enable-scraping','mcp-target']
    .forEach(id => {
      const el = document.getElementById(id);
      if (el) el.dispatchEvent(new Event('change', { bubbles: true }));
    });
  // Dispatch change on all enhanced selects so their custom-select label syncs.
  document.querySelectorAll('select[id]').forEach(el => {
    el.dispatchEvent(new Event('change', { bubbles: true }));
  });
  _restoringState = false;

  if (state.activeTab) {
    const btn = document.querySelector(`.tab-btn[data-tab="${state.activeTab}"]`);
    if (btn) btn.click();
  }
}

document.addEventListener('input', schedulePersist, true);
document.addEventListener('change', schedulePersist, true);
document.addEventListener('click', (e) => {
  if (e.target.closest('.tab-btn, .btn-remove, button[id^="btnAdd"], button[id^="btnClearHeaders"], button[id^="btnApplyPreset"]')) {
    schedulePersist();
  }
}, true);
window.addEventListener('beforeunload', persistState);

// ─── Presets ─────────────────────────────────────────────────────────────────
function pShow(data) { showResult('presets-result', data); }

// Module-level state for the presets library
let _presetsAll = [];   // full fetched list (unfiltered)
let pPage = 1;          // current 1-based page
const P_PAGE_SIZE = 10;

// Derive a favicon domain for a preset object.
// Priority: url_template host → default_locale domain → source heuristic.
function presetFaviconDomain(p) {
  // 1. url_template host; only trust it when the host portion has no {placeholder}
  // (e.g. https://www.amazon.{domain}/... must fall through to source → "amazon.com").
  if (p.url_template) {
    try {
      const m = String(p.url_template).match(/^[a-z]+:\/\/([^/?#]+)/i);
      const host = m && m[1];
      if (host && host.includes('.') && !host.includes('{') && !host.includes('}')) {
        return host;
      }
    } catch (_) {}
  }
  // 2. default_locale's domain (or first locale); if it's a bare TLD-ish token without a dot, skip
  const locs = p.locales || {};
  const loc = locs[p.default_locale] || Object.values(locs)[0];
  if (loc && loc.domain && String(loc.domain).includes('.')) {
    try { return new URL(/^https?:/.test(loc.domain) ? loc.domain : 'https://' + loc.domain).hostname; }
    catch (_) {}
  }
  // 3. source heuristic
  if (p.source) return p.source.includes('.') ? p.source : `${p.source}.com`;
  return '';
}

// Render the preset list from _presetsAll applying current search + kind filter + pagination.
function renderPresetList() {
  const search = (document.getElementById('p-search')?.value || '').toLowerCase().trim();
  const kind = document.getElementById('p-filter-kind')?.value || '';

  // Apply filters
  const filtered = _presetsAll.filter(p => {
    if (kind && p.kind !== kind) return false;
    if (search) {
      const name = String(p.name || '').toLowerCase();
      const source = String(p.source || '').toLowerCase();
      if (!name.includes(search) && !source.includes(search)) return false;
    }
    return true;
  });

  const totalPages = Math.max(1, Math.ceil(filtered.length / P_PAGE_SIZE));
  // Clamp page
  if (pPage > totalPages) pPage = totalPages;
  if (pPage < 1) pPage = 1;

  const start = (pPage - 1) * P_PAGE_SIZE;
  const pageItems = filtered.slice(start, start + P_PAGE_SIZE);

  const box = document.getElementById('presets-list');
  if (!filtered.length) {
    box.innerHTML = '<span class="placeholder">No presets match.</span>';
  } else {
    box.innerHTML = pageItems.map(p => {
      const isUser = p.kind === 'user';
      const domain = presetFaviconDomain(p);
      const faviconHtml = domain
        ? `<img class="p-favicon" src="https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32" width="16" height="16" loading="lazy" alt="" onerror="this.style.display='none'">`
        : '<span class="p-favicon-spacer"></span>';
      return `<div class="recent-job" style="display:flex;align-items:center;gap:0.5rem">
        <div style="flex:1;display:flex;align-items:center;gap:0">
          ${faviconHtml}<b>${escapeHtml(p.name)}</b>
          <span class="badge" style="margin-left:0.4rem">${escapeHtml(p.kind)}</span>
          <span style="color:var(--muted);margin-left:0.4rem">${escapeHtml(p.source)} · v${escapeHtml(String(p.version))}</span>
        </div>
        <button class="btn-secondary btn-sm" data-p-view="${escapeHtml(p.name)}">View</button>
        ${isUser ? `<button class="btn-secondary btn-sm" data-p-del="${escapeHtml(p.name)}">Delete</button>` : ''}
      </div>`;
    }).join('');
  }

  // Update pager
  const pager = document.getElementById('p-pager');
  const pageInfo = document.getElementById('p-pageinfo');
  const prevBtn = document.getElementById('p-prev');
  const nextBtn = document.getElementById('p-next');
  if (pager && pageInfo && prevBtn && nextBtn) {
    pageInfo.textContent = `${pPage} / ${totalPages} (${filtered.length})`;
    prevBtn.disabled = pPage <= 1;
    nextBtn.disabled = pPage >= totalPages;
    // Hide/neutralize pager when all items fit on one page
    pager.style.display = filtered.length <= P_PAGE_SIZE ? 'none' : '';
  }
}

// ─── Preset Builder wizard ───────────────────────────────────────────────
const pwState = {
  step: 1,
  sampleHtml: '',
  sampleUrl: '',
  scrapeDefaults: {},
  outputSchema: null,
};

function pwSetStep(n) {
  pwState.step = n;
  document.querySelectorAll('#pw-steps .wizard-step').forEach(s => {
    const sn = Number(s.dataset.step);
    s.classList.toggle('active', sn === n);
    s.classList.toggle('done', sn < n);
  });
  document.querySelectorAll('#tab-presets .wizard-pane').forEach(p => {
    p.classList.toggle('active', Number(p.dataset.pane) === n);
  });
  document.getElementById('pw-back').disabled = n === 1;
  document.getElementById('pw-next').style.display = n === 4 ? 'none' : '';
}

function pwReadFields() {
  const rows = document.querySelectorAll('#pw-fields .dynamic-row');
  const fields = {};
  rows.forEach(row => {
    const inputs = row.querySelectorAll('input[type="text"]');
    const name = inputs[0].value.trim();
    const selector = inputs[1].value.trim();
    const attr = row.querySelector('select').value;
    const all = row.querySelector('input[type="checkbox"]').checked;
    if (name && selector) fields[name] = { selector, attr, all };
  });
  const out = {
    type: document.getElementById('pw-extract-type').value,
    fields,
  };
  const rowContainer = document.getElementById('pw-extract-container').value.trim();
  if (rowContainer) out.container = rowContainer;
  return out;
}

function pwFillFields(instructions) {
  const container = document.getElementById('pw-fields');
  container.innerHTML = '';
  if (!instructions || !instructions.fields) return;
  document.getElementById('pw-extract-type').value = instructions.type || 'css';
  // Round-trips with pwReadFields: a wizard that showed the fields but not the
  // row they are scoped to would silently rewrite the recipe on save.
  document.getElementById('pw-extract-container').value = instructions.container || '';
  for (const [fname, fr] of Object.entries(instructions.fields)) {
    addExtractField('pw-fields');
    const row = container.lastElementChild;
    const inputs = row.querySelectorAll('input[type="text"]');
    inputs[0].value = fname;
    inputs[1].value = fr.selector || '';
    const attrSel = row.querySelector('select');
    const attr = fr.attr || 'text';
    if (![...attrSel.options].some(o => o.value === attr)) {
      attrSel.add(new Option(attr, attr));
    }
    attrSel.value = attr;
    row.querySelector('input[type="checkbox"]').checked = !!fr.all;
  }
}

// A fresh sample means a new preset: wipe everything downstream of step 1
// (method, generated fields/schema, name, source) so a prior build's values
// — restored from the F5-persist state — don't bleed into the new preset.
function pwResetBuild() {
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = v;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  set('pw-name', '');
  set('pw-source', '');
  set('pw-desc', '');
  set('pw-llm-model', '');
  set('pw-extract-type', 'css');
  set('pw-method', 'manual');
  const fields = document.getElementById('pw-fields');
  if (fields) fields.innerHTML = '';
  const schemaRows = document.getElementById('pw-schema-rows');
  if (schemaRows) schemaRows.innerHTML = '';
  pwState.outputSchema = null;
  if (typeof pwSyncMethod === 'function') pwSyncMethod();
  if (typeof schedulePersist === 'function') schedulePersist();
}

// Step 1: real scrape job → raw_html into pwState.sampleHtml
document.getElementById('pw-fetch').addEventListener('click', async () => {
  const pasted = document.getElementById('pw-sample-html').value.trim();
  const pwProxy = collectProxy('pw');
  if (pwProxy === false) return;  // invalid (missing pool) — already alerted
  // Before `scrapeDefaults`, not before the fetch: an impossible pairing saved
  // into a preset's defaults fails every later scrape that uses the preset,
  // not just this sample. The crawler and session forms cannot reach it —
  // neither offers an engine, so both run the default chromium.
  if (!warnIfDeviceEngineClash('pw-device', 'pw-browser-engine')) return;
  if (!warnIfDeviceNeedsNoRender('pw-device', 'pw-render')) return;

  // Persist scrape settings for Save (everything except concrete pool_id).
  pwState.scrapeDefaults = {
    device: document.getElementById('pw-device').value,
    render: document.getElementById('pw-render').checked,
    stealth: document.getElementById('pw-stealth').checked,
    block_assets: document.getElementById('pw-block-assets').checked,
    wait_until: document.getElementById('pw-wait-until').value,
    timeout_ms: Number(document.getElementById('pw-timeout').value) || 30000,
    browser_engine: document.getElementById('pw-browser-engine')?.value || 'chromium',
    // Camoufox fields (empty for other engines), so the sample fetch and the
    // saved preset both run on the engine the builder was pointed at —
    // scrapeDefaults is used for each.
    ...collectCamoufoxOpts('pw'),
  };
  const waitSel = document.getElementById('pw-wait-selector').value.trim();
  if (waitSel) pwState.scrapeDefaults.wait_for_selector = waitSel;
  if (pwProxy) {
    // Spread proxy fields into scrapeDefaults (proxy_type, proxy_geo?,
    // prem_proxy_options?) but strip proxy_pool_id — it's per-account, not
    // a portable preset default.
    const { proxy_pool_id: _skip, ...proxyDefaults } = pwProxy;
    Object.assign(pwState.scrapeDefaults, proxyDefaults);
  }

  if (pasted) {
    // Reset only once we're committed to a new build — never on a
    // mis-click that bails at the guards (that would silently destroy
    // and persist over an in-progress build).
    pwState.sampleUrl = '';
    pwResetBuild();
    pwState.sampleHtml = pasted;
    setStatus('pw-fetch-status', 'done', `Using pasted HTML (${pasted.length} chars)`);
    return;
  }
  const url = document.getElementById('pw-url').value.trim();
  if (!url) { alert('Sample URL (or pasted HTML) is required'); return; }
  // Committed to a new fetch: safe to reset prior build state now.
  pwState.sampleHtml = '';
  pwResetBuild();
  pwState.sampleUrl = url;

  const payload = {
    url,
    ...pwState.scrapeDefaults,
    raw_html: true,
    screenshot: document.getElementById('pw-screenshot').checked,
  };
  // Re-add pool_id to the actual scrape request (stripped from scrapeDefaults above).
  if (pwProxy && pwProxy.proxy_pool_id) payload.proxy_pool_id = pwProxy.proxy_pool_id;

  const fetchBtn = document.getElementById('pw-fetch');
  fetchBtn.disabled = true;
  try {
    setStatus('pw-fetch-status', 'queued', 'Submitting scrape…');
    const sub = await apiCall('/api/v1/scrape/page', {
      method: 'POST', body: JSON.stringify(payload),
    });
    if (!sub.ok || !sub.data?.job_id) {
      setStatus('pw-fetch-status', 'failed', 'Failed to submit scrape');
      pShow(sub.data);
      return;
    }
    const jobId = sub.data.job_id;
    for (let i = 0; i < 60; i++) {
      await new Promise(r => setTimeout(r, 2000));
      const { data } = await apiCall(`/api/v1/scrape/${jobId}`);
      if (!data || data.detail) {
        setStatus('pw-fetch-status', 'failed', 'Error polling scrape');
        pShow(data);
        return;
      }
      setStatus('pw-fetch-status', data.status, `Scrape ${data.status}…`);
      if (data.status === 'done') {
        const res = await apiCall(`/api/v1/scrape/${jobId}/results`);
        const html = res.data?.results?.[0]?.raw_html || '';
        if (!html) {
          setStatus('pw-fetch-status', 'failed', 'Scrape returned no raw_html');
          pShow(res.data);
          return;
        }
        pwState.sampleHtml = html;
        setStatus('pw-fetch-status', 'done', `Fetched sample (${html.length} chars)`);
        return;
      }
      if (data.status === 'failed' || data.status === 'cancelled') {
        setStatus('pw-fetch-status', 'failed', `Scrape ${data.status}`);
        pShow(data);
        return;
      }
    }
    setStatus('pw-fetch-status', 'failed', 'Timed out — check the Jobs tab');
  } finally {
    fetchBtn.disabled = false;
  }
});

// Step 2: method switch
function pwSyncMethod() {
  const m = document.getElementById('pw-method').value;
  document.getElementById('pw-ai-prompt').style.display = m === 'from_prompt' ? '' : 'none';
  document.getElementById('pw-ai-schema').style.display = m === 'from_schema' ? '' : 'none';
  document.getElementById('pw-ai-model-wrap').style.display = m === 'manual' ? 'none' : '';
}
document.getElementById('pw-method').addEventListener('change', pwSyncMethod);
document.getElementById('pw-add-field').addEventListener('click', () =>
  addExtractField('pw-fields'));
document.getElementById('pw-add-schema-row').addEventListener('click', () =>
  pwAddSchemaRow());

function pwAddSchemaRow() {
  const c = document.getElementById('pw-schema-rows');
  const row = document.createElement('div');
  row.className = 'dynamic-row';
  row.innerHTML = `
    <input type="text" placeholder="field name" style="flex:0 0 140px" />
    <select style="flex:0 0 120px">
      <option value="string">string</option>
      <option value="number">number</option>
      <option value="boolean">boolean</option>
      <option value="array">array</option>
    </select>
    <button class="btn-remove" title="Remove row">×</button>`;
  row.querySelector('.btn-remove').addEventListener('click', () => row.remove());
  c.appendChild(row);
}

function pwBuildSchema() {
  const schema = {};
  document.querySelectorAll('#pw-schema-rows .dynamic-row').forEach(row => {
    const name = row.querySelector('input').value.trim();
    const type = row.querySelector('select').value;
    if (name) schema[name] = type;
  });
  return schema;
}

document.getElementById('pw-generate').addEventListener('click', async () => {
  if (!pwState.sampleHtml) { alert('Fetch a sample first (step 1)'); return; }
  const mode = document.getElementById('pw-method').value;
  const body = {
    mode,
    sample_html: pwState.sampleHtml,
    llm_model: document.getElementById('pw-llm-model').value || undefined,
  };
  if (mode === 'from_prompt') {
    body.description = document.getElementById('pw-desc').value.trim();
    if (!body.description) { alert('Describe what to extract'); return; }
  } else if (mode === 'from_schema') {
    body.schema = pwBuildSchema();
    if (!Object.keys(body.schema).length) { alert('Add at least one schema field'); return; }
  } else {
    alert('Manual mode: just add fields below'); return;
  }
  pShow('Generating with AI…');
  const r = await apiCall('/api/v1/presets/preview', {
    method: 'POST', body: JSON.stringify(body),
  });
  if (!r.ok) { pShow(r.data); return; }
  pwState.outputSchema = r.data.output_schema || null;
  pwFillFields(r.data.parsing_instructions);
  pShow(r.data);
});

// Step 3: verify (always re-runs the pipeline with the edited table)
document.getElementById('pw-verify').addEventListener('click', async () => {
  if (!pwState.sampleHtml) { alert('Fetch a sample first (step 1)'); return; }
  const instructions = pwReadFields();
  if (!Object.keys(instructions.fields).length) {
    alert('Add at least one field with a name and a selector'); return;
  }
  const body = {
    mode: 'manual',
    sample_html: pwState.sampleHtml,
    parsing_instructions: instructions,
    output_schema: pwState.outputSchema || undefined,
    self_heal: document.getElementById('pw-self-heal').checked,
    llm_model: document.getElementById('pw-llm-model').value || undefined,
  };
  showResult('pw-verify-result', 'Running…');
  const r = await apiCall('/api/v1/presets/preview', {
    method: 'POST', body: JSON.stringify(body),
  });
  showResult('pw-verify-result', r.data);
});

// Step 4: save (deterministic create, no LLM)
document.getElementById('pw-save').addEventListener('click', async () => {
  // Backend requires name/source to match ^[a-z][a-z0-9_]*$. Slugify the
  // user's input (spaces, caps, a pasted URL, punctuation) instead of
  // POSTing it raw and failing with a cryptic 422.
  const slugify = (s) => String(s || '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '_').replace(/^[^a-z]+/, '').replace(/_+$/g, '');
  let name = slugify(document.getElementById('pw-name').value);
  if (!name) { pShow('name is required (letters, digits, underscores)'); return; }
  if (!name.startsWith('user_')) name = `user_${name}`;
  const source = slugify(document.getElementById('pw-source').value) || 'custom';
  const instructions = pwReadFields();
  const preset = {
    name,
    source,
    kind: 'user',
    request_defaults: pwState.scrapeDefaults,
    locales: {},
    default_locale: 'us',
    parsing_instructions: instructions,
    updated_at: Date.now() / 1000,
  };
  if (pwState.outputSchema) preset.output_schema = pwState.outputSchema;
  // Persist the sample URL as the preset's url_template: it records the real
  // site the preset was built from so the favicon (and server-side preset
  // scrape) come from the actual URL, not a guess from the `source` tag.
  if (pwState.sampleUrl) preset.url_template = pwState.sampleUrl;
  const r = await apiCall('/api/v1/presets', {
    method: 'POST', body: JSON.stringify(preset),
  });
  pShow(r.data);
  if (r.ok) {
    loadPresets();
    populateScrapePresetSelect();
    pwSetStep(1);
  }
});

// Nav
document.getElementById('pw-back').addEventListener('click', () => {
  if (pwState.step > 1) pwSetStep(pwState.step - 1);
});
document.getElementById('pw-next').addEventListener('click', () => {
  if (pwState.step === 1 && !pwState.sampleHtml) {
    alert('Fetch a sample (or paste HTML) before continuing'); return;
  }
  if (pwState.step < 4) pwSetStep(pwState.step + 1);
});

async function loadPresetModels() {
  const { data } = await apiCall('/api/v1/presets/llm-models');
  const models = (data && data.available) || [];
  const def = (data && data.default) || '';
  const sel = document.getElementById('pw-llm-model');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">— default —</option>' +
    models.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}${m === def ? ' (default)' : ''}</option>`).join('');
  if (prev) sel.value = prev;
}

async function loadPresets() {
  // Fetch all presets from server (no server-side kind filter — client does it)
  const { data } = await apiCall('/api/v1/presets');
  _presetsAll = (data && data.items) || [];
  renderPresetList();
}

document.getElementById('presets-list').addEventListener('click', async (e) => {
  const view = e.target.closest('[data-p-view]');
  const del = e.target.closest('[data-p-del]');
  if (view) {
    const { data } = await apiCall(`/api/v1/presets/${encodeURIComponent(view.dataset.pView)}`);
    pShow(data);
  } else if (del) {
    const name = del.dataset.pDel;
    if (!confirm(`Delete preset ${name}?`)) return;
    const r = await apiCall(`/api/v1/presets/${encodeURIComponent(name)}`, { method: 'DELETE' });
    pShow(r.ok ? { deleted: name } : r.data);
    loadPresets();
    populateScrapePresetSelect();
  }
});

document.getElementById('btnLoadPresets').addEventListener('click', loadPresets);
document.getElementById('p-filter-kind').addEventListener('change', () => { pPage = 1; renderPresetList(); });
document.getElementById('p-search').addEventListener('input', () => { pPage = 1; renderPresetList(); });
document.getElementById('p-prev').addEventListener('click', () => { pPage--; renderPresetList(); });
document.getElementById('p-next').addEventListener('click', () => { pPage++; renderPresetList(); });

let _presetsLoaded = false;
document.querySelector('.tab-btn[data-tab="presets"]').addEventListener('click', () => {
  if (_presetsLoaded) return;
  _presetsLoaded = true;
  loadPresets();
  loadPresetModels();
  initProxyPool('pw');
  pwSyncMethod();
  pwSetStep(1);
});

// ═══════════════════════════════════════════════════════════════════════════
// Sessions tab — server-side Playwright session create / login script / list.
// ═══════════════════════════════════════════════════════════════════════════

const LOGIN_OPS = [
  'goto', 'fill', 'click', 'wait_for_selector', 'wait_for_timeout',
  'press_key', 'type_text', 'hover',
];

function escapeAttr(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

function addLoginStep(initial) {
  const wrap = document.getElementById('sess-login-steps');
  if (!wrap) return;
  const step = initial || { op: 'goto' };
  const row = document.createElement('div');
  row.className = 'dynamic-row';
  row.innerHTML = `
    <select style="flex:0 0 160px">
      ${LOGIN_OPS.map(o => `<option value="${o}"${o === step.op ? ' selected' : ''}>${o}</option>`).join('')}
    </select>
    <input type="text" placeholder="selector" value="${escapeAttr(step.selector)}" />
    <input type="text" placeholder="value / url" value="${escapeAttr(step.value ?? step.url ?? '')}" />
    <input type="text" placeholder="key" value="${escapeAttr(step.key)}" style="flex:0 0 90px" />
    <input type="text" placeholder="ms / timeout_ms" value="${escapeAttr(step.ms ?? step.timeout_ms ?? '')}" style="flex:0 0 130px" />
    <button class="btn-remove" title="Remove">×</button>`;
  row.querySelector('.btn-remove').addEventListener('click', () => row.remove());
  wrap.appendChild(row);
}

function readLoginSteps() {
  return Array.from(document.querySelectorAll('#sess-login-steps .dynamic-row')).map(row => {
    const op = row.querySelector('select').value;
    const inputs = row.querySelectorAll('input[type="text"]');
    const selector = inputs[0].value.trim();
    const value = inputs[1].value.trim();
    const key = inputs[2].value.trim();
    const msRaw = inputs[3].value.trim();
    const step = { op };
    if (op === 'goto') {
      if (value) step.url = value;
    } else if (value !== '') {
      step.value = value;
    }
    if (selector) step.selector = selector;
    if (key) step.key = key;
    if (msRaw) {
      const ms = Number(msRaw);
      if (Number.isFinite(ms) && ms >= 0) {
        if (op === 'wait_for_timeout') step.ms = ms;
        else step.timeout_ms = ms;
      }
    }
    return step;
  });
}

function showSessOutput(elId, data) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.style.display = '';
  // Pull the screenshot (if any) out of the JSON dump and render it as an image
  // alongside — login failures ship a 50KB+ base64 PNG that is useless inside a
  // syntax-highlighted blob. The JSON dump keeps a placeholder so the operator
  // sees the field is set without scrolling through the encoded payload.
  let imgHtml = '';
  let dataForJson = data;
  if (data && typeof data === 'object' && typeof data.screenshot_b64 === 'string' && data.screenshot_b64) {
    const b64 = data.screenshot_b64;
    imgHtml = `<div style="margin-bottom:0.5rem">
      <img src="data:image/png;base64,${b64}" alt="login failure screenshot"
           style="max-width:100%;border:1px solid var(--border);border-radius:4px" />
    </div>`;
    dataForJson = { ...data, screenshot_b64: `<${Math.round(b64.length * 0.75)} bytes — rendered above>` };
  }
  el.innerHTML = imgHtml + `<pre>${syntaxHighlight(dataForJson)}</pre>`;
}

async function listSessions() {
  const { ok, data } = await apiCall('/api/v1/sessions');
  if (!ok || !data?.items) return [];
  return data.items;
}

function renderSessionList(sessions) {
  const el = document.getElementById('sess-list');
  if (!el) return;
  if (!sessions.length) {
    el.innerHTML = '<span class="placeholder">No sessions yet. Create one above.</span>';
    return;
  }
  const rows = sessions.map(s => {
    const expires = new Date(s.expires_at * 1000).toISOString();
    const proxy = s.proxy_type === 'none'
      ? 'none'
      : `${escapeHtml(s.proxy_type)}${s.proxy_pool_id ? ' / ' + escapeHtml(s.proxy_pool_id) : ''}`;
    const lastErr = s.last_error
      ? `<div style="color:var(--color-red);font-size:11px;margin-top:0.25rem">${escapeHtml(s.last_error)}</div>`
      : '';
    return `
      <div class="batch-page">
        <div class="batch-page-header">
          <span><code>${escapeHtml(s.session_id)}</code></span>
          <span class="badge ${s.status === 'ready' ? 'ok' : s.status === 'failed' ? 'error' : ''}">${escapeHtml(s.status)}</span>
          <span>device: <b>${escapeHtml(s.device)}</b></span>
          <span>proxy: ${proxy}</span>
          <span>bytes: <b>${Number(s.storage_state_bytes || 0).toLocaleString()}</b></span>
          <span>expires: <code>${escapeHtml(expires)}</code></span>
          <button class="btn-secondary btn-sm" data-copy-sess="${escapeAttr(s.session_id)}" style="margin-left:auto">Copy ID</button>
          <button class="btn-secondary btn-sm" data-del-sess="${escapeAttr(s.session_id)}">Delete</button>
        </div>
        ${lastErr}
      </div>`;
  }).join('');
  el.innerHTML = rows;
  el.querySelectorAll('[data-copy-sess]').forEach(b => b.addEventListener('click', () => {
    navigator.clipboard?.writeText(b.dataset.copySess).catch(() => {});
  }));
  el.querySelectorAll('[data-del-sess]').forEach(b => b.addEventListener('click', async () => {
    if (!confirm(`Delete session ${b.dataset.delSess}?`)) return;
    const { ok, data } = await apiCall(`/api/v1/sessions/${encodeURIComponent(b.dataset.delSess)}`, { method: 'DELETE' });
    if (!ok) {
      showSessOutput('sess-list', { error: `Failed to delete session: ${data?.detail || data?.message || 'Unknown error'}` });
      return;
    }
    refreshSessionsView();
  }));
}

// Cache of the latest sessions list; populated by populateSessionPickers and read
// by the picker-change handler so it can offer to copy proxy pins to the form.
let _lastSessions = [];

function _sessionPrefixFromPicker(sel) {
  // `<select id="s-session-id" data-session-picker>` -> "s"
  return sel.id.replace(/-session-id$/, '');
}

function _currentProxyIsCustomized(prefix) {
  const typeSel = document.getElementById(`${prefix}-proxy-type`);
  if (typeSel && typeSel.value && typeSel.value !== 'none') return true;
  const pool = document.getElementById(`${prefix}-proxy-pool`);
  if (pool && pool.value.trim()) return true;
  const country = document.getElementById(`${prefix}-geo-country`);
  if (country && country.value) return true;
  const region = document.getElementById(`${prefix}-geo-region`);
  if (region && region.value.trim()) return true;
  const city = document.getElementById(`${prefix}-geo-city`);
  if (city && city.value.trim()) return true;
  return false;
}

async function _waitForPoolSelectPopulated(prefix, timeoutMs = 3000) {
  const poolSelect = document.getElementById(`${prefix}-proxy-pool-select`);
  if (!poolSelect) return;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (poolSelect.options.length > 0) return;
    await new Promise(r => setTimeout(r, 40));
  }
}

async function applySessionProxyToForm(prefix, session) {
  // 1) proxy_type — dispatch change so the tab's refresh*ProxyPool() fires
  const typeSel = document.getElementById(`${prefix}-proxy-type`);
  if (typeSel) {
    typeSel.value = session.proxy_type || 'none';
    typeSel.dispatchEvent(new Event('change', { bubbles: true }));
  }
  // 2) Wait for the pool select to populate (or for the field to be hidden again on "none")
  await _waitForPoolSelectPopulated(prefix);

  // 3) pool_id
  const poolSelect = document.getElementById(`${prefix}-proxy-pool-select`);
  const poolInput = document.getElementById(`${prefix}-proxy-pool`);
  if (session.proxy_pool_id) {
    if (poolSelect) {
      const has = Array.from(poolSelect.options).some(o => o.value === session.proxy_pool_id);
      if (has) {
        poolSelect.value = session.proxy_pool_id;
        poolSelect.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    if (poolInput) poolInput.value = session.proxy_pool_id;
  } else {
    if (poolInput) poolInput.value = '';
    if (poolSelect) poolSelect.value = '';
  }

  // 4) Geo
  const geo = session.proxy_geo || {};
  const country = document.getElementById(`${prefix}-geo-country`);
  if (country) country.value = geo.country_code || '';
  const region = document.getElementById(`${prefix}-geo-region`);
  if (region) region.value = geo.region || '';
  const city = document.getElementById(`${prefix}-geo-city`);
  if (city) city.value = geo.city || '';
}

function _sessionProxyMatchesForm(prefix, session) {
  const typeSel = document.getElementById(`${prefix}-proxy-type`);
  const pool = document.getElementById(`${prefix}-proxy-pool`);
  const country = document.getElementById(`${prefix}-geo-country`);
  const region = document.getElementById(`${prefix}-geo-region`);
  const city = document.getElementById(`${prefix}-geo-city`);
  if (typeSel && typeSel.value !== (session.proxy_type || 'none')) return false;
  if (pool && (pool.value.trim() || '') !== (session.proxy_pool_id || '')) return false;
  const geo = session.proxy_geo || {};
  if (country && (country.value || '') !== (geo.country_code || '')) return false;
  if (region && (region.value.trim() || '') !== (geo.region || '')) return false;
  if (city && (city.value.trim() || '') !== (geo.city || '')) return false;
  return true;
}

async function _onSessionPickerChange(ev) {
  const sel = ev.currentTarget;
  const sid = sel.value;
  if (!sid) return;  // user picked "— none —"
  const session = _lastSessions.find(s => s.session_id === sid);
  if (!session) return;
  const prefix = _sessionPrefixFromPicker(sel);
  if (!prefix) return;
  if (_sessionProxyMatchesForm(prefix, session)) return;  // already aligned
  if (_currentProxyIsCustomized(prefix)) {
    const sessionDesc = `${session.proxy_type || 'none'}${session.proxy_pool_id ? ' / ' + session.proxy_pool_id.slice(0, 8) + '…' : ''}`;
    if (!confirm(`Session ${sid} is pinned to proxy ${sessionDesc}. Replace your current proxy fields with it?`)) {
      return;
    }
  }
  await applySessionProxyToForm(prefix, session);
}

document.querySelectorAll('select[data-session-picker]').forEach(sel => {
  sel.addEventListener('change', _onSessionPickerChange);
});

async function populateSessionPickers(sessions) {
  _lastSessions = sessions;
  // Scrape / Batch / Crawler pickers — preserve current selection, show "none" first.
  const opts = ['<option value="">— none —</option>']
    .concat(sessions.map(s => `<option value="${escapeAttr(s.session_id)}">${escapeHtml(s.session_id)} (${escapeHtml(s.status)})</option>`))
    .join('');
  document.querySelectorAll('select[data-session-picker]').forEach(sel => {
    const prev = sel.value;
    sel.innerHTML = opts;
    if (prev && sessions.some(s => s.session_id === prev)) sel.value = prev;
    else sel.value = '';
    if (sel.value !== prev) {
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
  // Cookie-injection session select — same shape as login form.
  const cookieSel = document.getElementById('sess-cookies-session-id');
  if (cookieSel) {
    const prev = cookieSel.value;
    cookieSel.innerHTML = sessions.length
      ? sessions.map(s => `<option value="${escapeAttr(s.session_id)}">${escapeHtml(s.session_id)} (${escapeHtml(s.status)})</option>`).join('')
      : '<option value="">— create a session first —</option>';
    if (prev && sessions.some(s => s.session_id === prev)) cookieSel.value = prev;
    else cookieSel.value = '';
  }
  // Login-form session select — sessions only, no "none".
  const loginSel = document.getElementById('sess-login-session-id');
  if (loginSel) {
    const prev = loginSel.value;
    loginSel.innerHTML = sessions.length
      ? sessions.map(s => `<option value="${escapeAttr(s.session_id)}">${escapeHtml(s.session_id)} (${escapeHtml(s.status)})</option>`).join('')
      : '<option value="">— create a session first —</option>';
    if (prev && sessions.some(s => s.session_id === prev)) loginSel.value = prev;
    else loginSel.value = '';
    if (loginSel.value !== prev) {
      loginSel.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
}

async function refreshSessionsView() {
  const sessions = await listSessions();
  renderSessionList(sessions);
  await populateSessionPickers(sessions);
}

// ─── Session → proxy-fields auto-fill ────────────────────────────────────────
// When a session is picked on Scrape / Batch / Crawler, copy its pinned
// device + proxy_type + pool_id + geo into the form. If the form already has
// non-default proxy state that differs, ask before overwriting — the scraper
// 422s any mismatch on submit anyway, so getting the form aligned up-front
// saves a round trip.

const _SESSION_TAB_PREFIX = {
  's-session-id': 's',
  'b-session-id': 'b',
  'c-session-id': 'c',
};

function _sessionGetVal(id) {
  const el = document.getElementById(id);
  return el ? (el.value || '').trim() : '';
}

function _proxyFieldsAtDefaults(prefix) {
  const device = _sessionGetVal(`${prefix}-device`);
  const isDefaultDevice = device === '' || device === 'desktop';
  return isDefaultDevice
    && _sessionGetVal(`${prefix}-proxy-type`) === 'none'
    && _sessionGetVal(`${prefix}-proxy-pool`) === ''
    && _sessionGetVal(`${prefix}-geo-country`) === ''
    && _sessionGetVal(`${prefix}-geo-region`) === ''
    && _sessionGetVal(`${prefix}-geo-city`) === '';
}

function _proxyMatchesSession(prefix, session) {
  const sessGeo = session.proxy_geo || {};
  return _sessionGetVal(`${prefix}-device`) === session.device
    && _sessionGetVal(`${prefix}-proxy-type`) === session.proxy_type
    && _sessionGetVal(`${prefix}-proxy-pool`) === (session.proxy_pool_id || '')
    && _sessionGetVal(`${prefix}-geo-country`) === (sessGeo.country_code || '')
    && _sessionGetVal(`${prefix}-geo-region`) === (sessGeo.region || '')
    && _sessionGetVal(`${prefix}-geo-city`) === (sessGeo.city || '');
}

function _describeSessionPin(session) {
  const parts = [`device=${session.device}`, `proxy_type=${session.proxy_type}`];
  if (session.proxy_pool_id) parts.push(`pool=${session.proxy_pool_id}`);
  const geo = session.proxy_geo || {};
  const geoBits = [];
  if (geo.country_code) geoBits.push(`country=${geo.country_code}`);
  if (geo.region) geoBits.push(`region=${geo.region}`);
  if (geo.city) geoBits.push(`city=${geo.city}`);
  if (geoBits.length) parts.push(`geo[${geoBits.join(', ')}]`);
  return parts.join(', ');
}

// Map tab-prefix to the corresponding refresh function so we can await the
// API-driven pool repopulation directly instead of polling. The functions
// already exist for the scrape/batch/crawler tab proxy blocks.
const _REFRESH_PROXY_POOL_BY_PREFIX = {
  's': () => (typeof refreshProxyPool === 'function' ? refreshProxyPool() : null),
  'b': () => (typeof refreshBatchProxyPool === 'function' ? refreshBatchProxyPool() : null),
  'c': () => (typeof refreshCrawlerProxyPool === 'function' ? refreshCrawlerProxyPool() : null),
};

async function _applySessionPinToProxyFields(prefix, session) {
  const setAndFire = (id, val) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = val;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };

  setAndFire(`${prefix}-device`, session.device);
  // proxy_type's change listener also runs the same refresh function, but the
  // event-dispatch path doesn't give us a Promise to await. Set the value
  // first, then explicitly await the named refresh to know the select is
  // populated before we try to pick the matching option.
  const proxyTypeEl = document.getElementById(`${prefix}-proxy-type`);
  if (proxyTypeEl) {
    proxyTypeEl.value = session.proxy_type;
    proxyTypeEl.dispatchEvent(new Event('change', { bubbles: true }));
  }
  const refresh = _REFRESH_PROXY_POOL_BY_PREFIX[prefix];
  if (refresh) {
    try {
      await refresh();
    } catch (_) {
      // Refresh failures are surfaced by the hint span; fall through to the
      // poll loop as a safety net.
    }
  }

  const poolSelect = document.getElementById(`${prefix}-proxy-pool-select`);
  const poolInput = document.getElementById(`${prefix}-proxy-pool`);
  const targetPool = session.proxy_pool_id || '';

  if (poolSelect && targetPool) {
    // Belt-and-braces poll loop. await refresh() already guarantees the
    // populate ran, but a sibling refresh dispatched from the change event
    // could race against it on slow paths — keep the poll as a fallback.
    for (let i = 0; i < 50; i++) {
      if (Array.from(poolSelect.options).some(o => o.value === targetPool)) break;
      await new Promise(r => setTimeout(r, 100));
    }
    if (Array.from(poolSelect.options).some(o => o.value === targetPool)) {
      poolSelect.value = targetPool;
      // Two dispatches: 'change' for the mirror handler that updates the
      // hidden text input, 'input' for any listener that reacts on input
      // (none today, but cheap insurance). The custom-select wrapper's
      // setLabel runs on 'change' and via its options-childList observer.
      poolSelect.dispatchEvent(new Event('change', { bubbles: true }));
      poolSelect.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (poolInput) {
      // Fall back to the text input if the select didn't get populated
      // (e.g. no API key, or the pool was deleted upstream).
      poolInput.value = targetPool;
    }
  } else if (poolInput) {
    poolInput.value = targetPool;
  }

  const geo = session.proxy_geo || {};
  const geoCountry = document.getElementById(`${prefix}-geo-country`);
  if (geoCountry) {
    geoCountry.value = geo.country_code || '';
    geoCountry.dispatchEvent(new Event('change', { bubbles: true }));
  }
  const geoRegion = document.getElementById(`${prefix}-geo-region`);
  if (geoRegion) geoRegion.value = geo.region || '';
  const geoCity = document.getElementById(`${prefix}-geo-city`);
  if (geoCity) geoCity.value = geo.city || '';
}

function _wireSessionPickerProxyAutofill() {
  document.querySelectorAll('select[data-session-picker]').forEach(sel => {
    if (sel._proxyAutofillWired) return;
    sel._proxyAutofillWired = true;
    sel.addEventListener('change', async () => {
      const sessionId = sel.value;
      if (!sessionId) return; // user cleared the picker
      const session = _lastSessions.find(s => s.session_id === sessionId);
      if (!session) return;
      const prefix = _SESSION_TAB_PREFIX[sel.id];
      if (!prefix) return;
      if (_proxyMatchesSession(prefix, session)) return; // already aligned
      if (!_proxyFieldsAtDefaults(prefix)) {
        const ok = confirm(
          `Session ${sessionId} is pinned to:\n  ${_describeSessionPin(session)}\n\n` +
          `Your current proxy settings differ. Replace them with the session's pins?\n\n` +
          `(The scraper will 422 a submit that diverges from the session pin.)`
        );
        if (!ok) return;
      }
      await _applySessionPinToProxyFields(prefix, session);
    });
  });
}

_wireSessionPickerProxyAutofill();

renderProxyComponent('sess', document.getElementById('sess-proxy-component'));
initProxyPool('sess');

document.getElementById('btnSessAddStep')?.addEventListener('click', () => addLoginStep());

document.getElementById('btnSessRefresh')?.addEventListener('click', refreshSessionsView);

document.getElementById('btnSessCreate')?.addEventListener('click', async () => {
  const device = document.getElementById('sess-device').value;
  const sessProxy = collectProxy('sess');
  if (sessProxy === false) return;  // invalid (missing pool) — already alerted
  const ttl = Number(document.getElementById('sess-ttl').value) || 86400;

  const body = { device, ttl_seconds: ttl };
  if (sessProxy) Object.assign(body, sessProxy);

  const { data, ok } = await apiCall('/api/v1/sessions', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  showSessOutput('sess-create-result', data);
  if (ok) refreshSessionsView();
});

// Translate the common browser-extension cookie export shapes (EditThisCookie /
// Cookie-Editor) into Playwright's storage_state cookies. The differences are
// small but every one of them would crash add_cookies if passed raw:
//   - expirationDate (float) → expires (int)
//   - sameSite lowercase ("lax") → Capitalized ("Lax")
//   - drop extension-only keys: hostOnly, session, storeId, id
function _normalizeBrowserCookies(input) {
  if (!Array.isArray(input)) {
    throw new Error('expected a JSON array of cookies');
  }
  const sameSiteMap = { strict: 'Strict', lax: 'Lax', none: 'None', no_restriction: 'None', unspecified: null };
  return input.map((raw, i) => {
    if (!raw || typeof raw !== 'object') {
      throw new Error(`cookie #${i} is not an object`);
    }
    const out = {};
    if (typeof raw.name !== 'string') throw new Error(`cookie #${i} missing name`);
    if (raw.value === undefined || raw.value === null) throw new Error(`cookie #${i} missing value`);
    out.name = raw.name;
    out.value = String(raw.value);
    if (raw.domain) out.domain = raw.domain;
    if (raw.path) out.path = raw.path;
    // expirationDate (extensions) vs expires (Playwright); drop session cookies.
    const exp = raw.expirationDate ?? raw.expires;
    if (typeof exp === 'number' && isFinite(exp) && exp > 0) {
      out.expires = Math.floor(exp);
    }
    if (typeof raw.httpOnly === 'boolean') out.httpOnly = raw.httpOnly;
    if (typeof raw.secure === 'boolean') out.secure = raw.secure;
    if (typeof raw.sameSite === 'string') {
      const ss = sameSiteMap[raw.sameSite.toLowerCase()];
      if (ss) out.sameSite = ss;
    }
    return out;
  });
}

document.getElementById('btnSessInjectCookies')?.addEventListener('click', async () => {
  const sessionId = document.getElementById('sess-cookies-session-id').value.trim();
  if (!sessionId) {
    alert('Pick a session first (create one above if the list is empty).');
    return;
  }
  const raw = document.getElementById('sess-cookies-json').value.trim();
  if (!raw) {
    alert('Paste a JSON array of cookies (export them from your browser).');
    return;
  }
  let cookies;
  try {
    const parsed = JSON.parse(raw);
    cookies = _normalizeBrowserCookies(parsed);
  } catch (e) {
    showSessOutput('sess-cookies-result', { error: `JSON / cookie format error: ${e.message}` });
    return;
  }
  const { ok, data } = await apiCall(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/cookies`,
    { method: 'POST', body: JSON.stringify({ cookies }) },
  );
  showSessOutput('sess-cookies-result', ok ? data : { error: data?.detail || data?.message || 'unknown error', raw: data });
  if (ok) {
    document.getElementById('sess-cookies-json').value = '';
    refreshSessionsView();
  }
});

document.getElementById('btnSessRunLogin')?.addEventListener('click', async () => {
  const sessionId = document.getElementById('sess-login-session-id').value.trim();
  if (!sessionId) {
    alert('Pick a session first (create one above if the list is empty).');
    return;
  }
  const steps = readLoginSteps();
  if (!steps.length) {
    alert('Add at least one login step.');
    return;
  }
  const script = { steps };
  const successSel = document.getElementById('sess-success-selector').value.trim();
  const successRe = document.getElementById('sess-success-url-regex').value.trim();
  if (successSel) script.success_selector = successSel;
  if (successRe) script.success_url_regex = successRe;

  const creds = {};
  const email = document.getElementById('sess-creds-email').value;
  const password = document.getElementById('sess-creds-password').value;
  const totp = document.getElementById('sess-creds-totp').value;
  if (email) creds.email = email;
  if (password) creds.password = password;
  if (totp) creds.totp_secret = totp;

  // `creds` is required server-side (may be empty if the login script doesn't
  // reference any $creds_* placeholders).
  const body = { script, creds };

  showSessOutput('sess-login-result', { status: 'running login script...' });
  const { data, ok } = await apiCall(`/api/v1/sessions/${encodeURIComponent(sessionId)}/login`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
  showSessOutput('sess-login-result', data);
  if (ok) {
    // Clear credential inputs from DOM after successful login — RAM-only, never persisted.
    document.getElementById('sess-creds-email').value = '';
    document.getElementById('sess-creds-password').value = '';
    const totp = document.getElementById('sess-creds-totp');
    if (totp) totp.value = '';
    refreshSessionsView();
  }
});

// Refresh the sessions tab whenever the user activates it.
document.querySelectorAll('.tab-btn[data-tab="sessions"]').forEach(btn => {
  btn.addEventListener('click', () => { refreshSessionsView(); });
});

// Initial load — populate the picker selects so the live UI shows current sessions.
// Best-effort; ignore failures (server down).
refreshSessionsView().catch(() => {});

restoreState();

// After restoreState the Scraper URL points at the real target — only now can
// the proxy countries and the preset dropdown load (these endpoints don't exist
// on a stock scraper / wrong host). Refresh them whenever the target changes too.
loadCountries();
loadPremCatalogs();
populateScrapePresetSelect();
populateSearchLocales();
document.getElementById('scraperUrl').addEventListener('change', () => {
  loadCountries();
  loadPremCatalogs();
  populateScrapePresetSelect();
  populateSearchLocales();
});

// Live GitHub star count on the header icon. Reads the public mirror
// (CyberYozh-data/yozh-scraper) — unauthenticated, so cache in localStorage to
// render instantly and stay under GitHub's 60 req/h/IP limit. Any failure
// (offline, rate-limited) silently leaves the icon without a count.
async function loadGithubStars() {
  const el = document.getElementById('ghStars');
  if (!el) return;
  const REPO = 'CyberYozh-data/yozh-scraper';
  const CACHE_KEY = 'ghStars:' + REPO;
  const TTL_MS = 6 * 60 * 60 * 1000;
  // Widen the icon into a pill only once a count is actually shown; until then
  // it stays a circle matching the sibling social icons.
  const show = (count) => {
    el.innerHTML = '<span class="gh-star">★</span> ' + count;
    el.hidden = false;
    el.parentElement.classList.add('gh-pill');
  };
  let fresh = false;
  try {
    const cached = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null');
    if (cached && typeof cached.count === 'number') {
      show(cached.count);
      fresh = Date.now() - cached.ts < TTL_MS;
    }
  } catch {}
  if (fresh) return;
  try {
    const resp = await fetch('https://api.github.com/repos/' + REPO, {
      headers: { Accept: 'application/vnd.github+json' },
    });
    if (!resp.ok) return;
    const { stargazers_count: count } = await resp.json();
    if (typeof count === 'number') {
      show(count);
      try { localStorage.setItem(CACHE_KEY, JSON.stringify({ count, ts: Date.now() })); } catch {}
    }
  } catch {}
}
loadGithubStars();
