from __future__ import annotations

import logging
from typing import get_args

import pytest

from src.api.search import ENGINES, _engine_override, _parse_results, build_search
from src.presets.store import PresetStore
from src.schemas import (
    PremProxyOptions,
    ProxyGeo,
    ScrapeMeta,
    ScrapeResponse,
    SearchEngine,
    SearchRequest,
    WarmupOptions,
)


def test_engines_cover_search_engine_literal():
    # Guards ENGINES against drifting from the SearchEngine Literal: pydantic
    # rejects unknown engines with a 422, so ENGINES[req.engine] can only
    # KeyError if someone extends the Literal without adding a profile here.
    assert set(ENGINES) == set(get_args(SearchEngine))


def _resp(*, data=None, request_id="r", url="https://www.google.com/search"):
    return ScrapeResponse(
        request_id=request_id,
        took_ms=1,
        meta=ScrapeMeta(url=url, device="desktop", proxy_type="none"),
        data=data,
    )


def _block(hveid, title, href, snippet):
    return (
        f'<div data-hveid="{hveid}"><h3>{title}</h3>'
        f'<a href="{href}">link</a>'
        f'<div class="VwiC3b">{snippet}</div></div>'
    )


_SERP_DATA = {
    "result_blocks": [
        _block("1", "First", "https://a.com/1", "snippet one"),
        _block("2", "Second", "https://b.com/2", "snippet two"),
        # duplicate URL -> deduped
        _block("3", "First again", "https://a.com/1", "dupe"),
        # no <h3> -> not an organic result -> skipped
        '<div data-hveid="4"><a href="https://ads.com/x">ad</a></div>',
    ]
}


class _Runner:
    """Fake run_job: 1st call = SERP, later calls = per-result scrapes."""

    def __init__(self, serp_data, scrape_responses=None):
        self.serp_data = serp_data
        self.scrape_responses = scrape_responses or []
        self.calls = []

    async def __call__(self, pages):
        self.calls.append(pages)
        if len(self.calls) == 1:
            return [_resp(data=self.serp_data)]
        return self.scrape_responses


@pytest.fixture
def store():
    return PresetStore()


class TestBuildSearch:
    @pytest.mark.asyncio
    async def test_parses_blocks_dedups_and_skips_non_organic(self, store):
        runner = _Runner(_SERP_DATA)
        res = await build_search(
            SearchRequest(query="hello"), store=store, run_job=runner
        )
        assert [r.url for r in res.results] == ["https://a.com/1", "https://b.com/2"]
        assert res.results[0].title == "First"
        assert res.results[0].snippet == "snippet one"
        assert res.count == 2

    @pytest.mark.asyncio
    async def test_serp_request_targets_google_with_query(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(SearchRequest(query="open source"), store=store, run_job=runner)
        serp_pages = runner.calls[0]
        assert len(serp_pages) == 1
        assert "google" in str(serp_pages[0].url)
        assert "open" in str(serp_pages[0].url)  # query encoded into the URL

    @pytest.mark.asyncio
    async def test_limit_caps_results(self, store):
        runner = _Runner(_SERP_DATA)
        res = await build_search(
            SearchRequest(query="x", limit=1), store=store, run_job=runner
        )
        assert res.count == 1

    @pytest.mark.asyncio
    async def test_no_scrape_leaves_scrape_none(self, store):
        runner = _Runner(_SERP_DATA)
        res = await build_search(SearchRequest(query="x"), store=store, run_job=runner)
        assert all(r.scrape is None for r in res.results)
        assert len(runner.calls) == 1  # no second job

    @pytest.mark.asyncio
    async def test_scrape_attaches_responses(self, store):
        scrapes = [
            _resp(request_id="s1", url="https://a.com/1"),
            _resp(request_id="s2", url="https://b.com/2"),
        ]
        runner = _Runner(_SERP_DATA, scrape_responses=scrapes)
        res = await build_search(
            SearchRequest(query="x", scrape=True), store=store, run_job=runner
        )
        assert len(runner.calls) == 2
        assert res.results[0].scrape.request_id == "s1"
        assert res.results[1].scrape.request_id == "s2"
        # scrape options inject url per result
        scrape_pages = runner.calls[1]
        assert {str(p.url) for p in scrape_pages} == {"https://a.com/1", "https://b.com/2"}

    @pytest.mark.asyncio
    async def test_empty_serp_yields_no_results(self, store):
        runner = _Runner({"result_blocks": []})
        res = await build_search(SearchRequest(query="x"), store=store, run_job=runner)
        assert res.count == 0
        assert res.results == []

    @pytest.mark.asyncio
    async def test_serp_unavailable_emits_warning(self, store):
        async def empty_run(pages):
            return []  # job didn't complete / produced nothing

        res = await build_search(SearchRequest(query="x"), store=store, run_job=empty_run)
        assert res.count == 0
        assert any("serp_unavailable" in w for w in res.warnings)

    @pytest.mark.asyncio
    async def test_partial_scrape_responses_leave_rest_none(self, store):
        # Scrape stage returns fewer responses than results (e.g. timeout).
        runner = _Runner(_SERP_DATA, scrape_responses=[_resp(request_id="s1", url="https://a.com/1")])
        res = await build_search(
            SearchRequest(query="x", scrape=True), store=store, run_job=runner
        )
        assert res.results[0].scrape.request_id == "s1"
        assert res.results[1].scrape is None  # no response -> stays None, no error

    @pytest.mark.asyncio
    async def test_none_padded_scrape_responses_warn_incomplete(self, store, caplog):
        # Mid-job timeout: the queue returns a full-length list padded with
        # None placeholders (the runner preallocates [None] * len(pages)),
        # so the incomplete signal must count attached responses, not lengths.
        runner = _Runner(
            _SERP_DATA,
            scrape_responses=[_resp(request_id="s1", url="https://a.com/1"), None],
        )
        with caplog.at_level(logging.WARNING):
            res = await build_search(
                SearchRequest(query="x", scrape=True), store=store, run_job=runner
            )
        assert res.results[0].scrape.request_id == "s1"
        assert res.results[1].scrape is None
        assert any("scrape_incomplete" in w for w in res.warnings)
        assert any("search scrape incomplete" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_internal_fields_stripped_from_scrape_options(self, store):
        runner = _Runner(_SERP_DATA, scrape_responses=[_resp(), _resp()])
        await build_search(
            SearchRequest(query="x", scrape=True, scrape_options={"parser_plan": {"x": 1}, "raw_html": True}),
            store=store,
            run_job=runner,
        )
        for page in runner.calls[1]:
            assert page.parser_plan is None
            assert page.raw_html is True


def _bing_block(title, href, snippet):
    return (
        f'<li class="b_algo"><h2><a href="{href}">{title}</a></h2>'
        f'<div class="b_caption"><p>{snippet}</p></div></li>'
    )


def _yandex_block(title, href, snippet):
    return (
        f'<li class="serp-item"><div class="OrganicTitle">'
        f'<a class="OrganicTitle-Link" href="{href}">'
        f'<span class="OrganicTitle-LinkText">{title}</span></a></div>'
        f'<div class="OrganicText"><span class="TextContainer">{snippet}</span></div></li>'
    )


class TestMultiEngineParse:
    def test_parse_bing_block(self):
        data = {"result_blocks": [_bing_block("First", "https://a.com/1", "snip one")]}
        res = _parse_results(data, ENGINES["bing"])
        assert [r.url for r in res] == ["https://a.com/1"]
        assert res[0].title == "First"
        assert res[0].snippet == "snip one"

    def test_parse_yandex_block(self):
        data = {"result_blocks": [_yandex_block("First", "https://a.com/1", "snip one")]}
        res = _parse_results(data, ENGINES["yandex"])
        assert [r.url for r in res] == ["https://a.com/1"]
        assert res[0].title == "First"
        assert res[0].snippet == "snip one"

    def test_block_without_title_is_skipped(self):
        # A Bing block with no <h2> is not an organic result.
        data = {"result_blocks": ['<li class="b_algo"><a href="https://ads.com">ad</a></li>']}
        assert _parse_results(data, ENGINES["bing"]) == []

    def test_google_profile_still_parses_h3_blocks(self):
        res = _parse_results(_SERP_DATA, ENGINES["google"])
        assert [r.url for r in res] == ["https://a.com/1", "https://b.com/2"]


def _bing_ck(real_url):
    """Build a Bing /ck/a redirect wrapping `real_url` like the live SERP does."""
    import base64

    u = "a1" + base64.urlsafe_b64encode(real_url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc123&u={u}"


class TestBingRedirect:
    def test_unwrap_decodes_real_url(self):
        from src.api.search import _unwrap_redirect

        assert (
            _unwrap_redirect(_bing_ck("https://example.com/page?x=1"))
            == "https://example.com/page?x=1"
        )

    def test_unwrap_passes_through_plain_url(self):
        from src.api.search import _unwrap_redirect

        assert _unwrap_redirect("https://example.com/x") == "https://example.com/x"

    def test_unwrap_rejects_lookalike_host_and_non_http(self):
        from src.api.search import _unwrap_redirect
        import base64

        # Lookalike host must not be treated as Bing.
        evil = "https://bing.com.evil.com/ck/a?u=a1" + base64.urlsafe_b64encode(
            b"https://real.com"
        ).decode().rstrip("=")
        assert _unwrap_redirect(evil) == evil
        # A non-http(s) payload falls back to the original URL.
        js = _bing_ck("javascript:alert(1)")
        assert _unwrap_redirect(js) == js

    def test_bing_block_yields_unwrapped_url(self):
        block = (
            f'<li class="b_algo"><h2><a href="{_bing_ck("https://real.com/p")}">T</a>'
            f'</h2><div class="b_caption"><p>s</p></div></li>'
        )
        res = _parse_results({"result_blocks": [block]}, ENGINES["bing"])
        assert res[0].url == "https://real.com/p"


class TestYandexRedirect:
    """_unwrap_redirect handles yabs.yandex.ru click-tracking links."""

    def test_yabs_with_url_param_is_unwrapped(self):
        from src.api.search import _unwrap_redirect

        yabs = "https://yabs.yandex.ru/count/ABC?q=buy+proxy&url=https%3A%2F%2Fdataimpulse.com%2F"
        out = _unwrap_redirect(yabs)
        assert out == "https://dataimpulse.com/"

    def test_yabs_with_u_param_is_unwrapped(self):
        from src.api.search import _unwrap_redirect

        yabs = "https://yabs.yandex.ru/count/XYZ?u=https%3A%2F%2Fexample.com%2F"
        out = _unwrap_redirect(yabs)
        assert out == "https://example.com/"

    def test_yabs_opaque_blob_passthrough(self):
        from src.api.search import _unwrap_redirect

        # No decodable url= or u= param — pass through unchanged rather than dropping.
        yabs = "https://yabs.yandex.ru/count/OPAQUEONLY"
        assert _unwrap_redirect(yabs) == yabs

    def test_yabs_etext_only_passthrough(self):
        from src.api.search import _unwrap_redirect

        # etext= is opaque and not followed; result is unchanged.
        yabs = "https://yabs.yandex.ru/count/BLOB?etext=abc123"
        assert _unwrap_redirect(yabs) == yabs

    def test_yabs_non_http_payload_passthrough(self):
        from src.api.search import _unwrap_redirect

        # A crafted url= with a non-http(s) scheme must not be returned.
        yabs = "https://yabs.yandex.ru/count/X?url=javascript%3Aalert%281%29"
        assert _unwrap_redirect(yabs) == yabs

    def test_plain_https_link_passes_through(self):
        from src.api.search import _unwrap_redirect

        assert _unwrap_redirect("https://hidemy.name/") == "https://hidemy.name/"

    def test_subdomain_count_path_unwrapped(self):
        from src.api.search import _unwrap_redirect

        # *.yandex.ru with /count/ in path should also be handled.
        yabs = "https://an.yandex.ru/count/BLOB?url=https%3A%2F%2Ftarget.com%2F"
        assert _unwrap_redirect(yabs) == "https://target.com/"


class TestEngineRouting:
    @pytest.mark.asyncio
    async def test_default_engine_is_google(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(SearchRequest(query="x"), store=store, run_job=runner)
        assert "google" in str(runner.calls[0][0].url)

    @pytest.mark.asyncio
    async def test_engine_bing_uses_bing_preset(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(query="x", engine="bing"), store=store, run_job=runner
        )
        assert "bing" in str(runner.calls[0][0].url)

    @pytest.mark.asyncio
    async def test_engine_yandex_uses_yandex_preset(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(query="x", engine="yandex"), store=store, run_job=runner
        )
        assert "yandex" in str(runner.calls[0][0].url)

    @pytest.mark.asyncio
    async def test_blocked_engine_surfaces_warning(self, store):
        # A blocked SERP (no organic blocks + captcha warning) surfaces the
        # warning through build_search for a non-default engine too.
        async def run(pages):
            return [
                ScrapeResponse(
                    request_id="r",
                    took_ms=1,
                    meta=ScrapeMeta(
                        url="https://www.bing.com/search",
                        device="desktop",
                        proxy_type="none",
                    ),
                    data={"result_blocks": []},
                    warnings=["Captcha/block detected by heuristic"],
                )
            ]

        res = await build_search(
            SearchRequest(query="x", engine="bing"), store=store, run_job=run
        )
        assert res.count == 0
        assert any("Captcha" in w for w in res.warnings)


def test_google_search_runs_the_camoufox_twin_on_dot_com():
    """Pinned on the map itself, not on a word in a description (review on #136:
    renaming `WALLED` would have let the route slide back to the refused engine
    with everything green). Measured 2026-09-18: camoufox + www.google.com is
    the combination that passes; chromium and every ccTLD are refused."""
    from urllib.parse import urlsplit

    from src.presets.materializer import PresetScrapeRequest, materialize

    assert ENGINES["google"].preset == "google_search_camoufox"
    preset = PresetStore().get("google_search_camoufox")
    assert preset.request_defaults["browser_engine"] == "camoufox"
    for locale in preset.locales:
        scrape = materialize(preset, PresetScrapeRequest(
            source=preset.name, preset_params={"query": "x"}, locale=locale))
        assert urlsplit(str(scrape.url)).hostname == "www.google.com", locale


def _store_with_serp_proxy(store, proxy_type: str):
    """The SERP preset, with `proxy_type` forced to a value that is neither the
    schema default nor any override under test, so inheritance is observable.

    Wraps rather than writes: the builtin files stay untouched and the preset
    keeps every other field it ships with.
    """
    name = ENGINES["google"].preset
    patched = store.get(name).model_copy(
        update={"request_defaults": {**store.get(name).request_defaults,
                                     "proxy_type": proxy_type}}
    )

    class _PatchedStore:
        def __getattr__(self, item):
            return getattr(store, item)

        def get(self, preset_name: str):
            return patched if preset_name == name else store.get(preset_name)

    return _PatchedStore()


class TestSearchProxy:
    @pytest.mark.asyncio
    async def test_no_override_keeps_preset_proxy(self, store):
        # Default-off: with no proxy fields, the SERP job inherits whatever the
        # SERP preset declares. The preset's own value cannot be used as the
        # expectation any more: since 2026-09-18 it is `none`, which is also
        # `ScrapeRequest`'s schema default, so an assertion against it passes
        # even when the inheritance is deleted (measured). The preset is given a
        # value that is neither the default nor the override under test, which is
        # the only shape that can fail.
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(query="x"),
            store=_store_with_serp_proxy(store, "res_rotating"),
            run_job=runner,
        )
        assert runner.calls[0][0].proxy_type == "res_rotating"

    @pytest.mark.asyncio
    async def test_override_applied_to_serp(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(
                query="x",
                proxy_type="mobile",
                proxy_pool_id="pool-123",
                proxy_geo=ProxyGeo(country_code="DE"),
            ),
            store=store,
            run_job=runner,
        )
        serp = runner.calls[0][0]
        assert serp.proxy_type == "mobile"
        assert serp.proxy_pool_id == "pool-123"
        assert serp.proxy_geo.country_code == "DE"

    @pytest.mark.asyncio
    async def test_override_can_disable_preset_proxy(self, store):
        # Explicit "none" overrides whatever the preset declares (distinct from
        # "field omitted", which keeps the preset default). The preset must
        # declare something OTHER than "none" for this to be able to fail --
        # against the shipped `none` the assertion holds even with the override
        # stripped out, because "none" is the schema default too.
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(query="x", proxy_type="none"),
            store=_store_with_serp_proxy(store, "res_rotating"),
            run_job=runner,
        )
        assert runner.calls[0][0].proxy_type == "none"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["google", "bing"])
    async def test_empty_proxy_geo_keeps_locale_default(self, store, engine):
        # An all-None proxy_geo must NOT wipe the materializer's locale-derived
        # geo: it has to behave exactly as if proxy_geo were omitted. Both paths
        # derive one -- google's preset has no exit and pins the MARKET country,
        # bing's exits through the pool and pins the EXIT country -- so the
        # default engine is covered as well as a pooled one.
        omitted, empty = _Runner(_SERP_DATA), _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(query="x", engine=engine), store=store, run_job=omitted
        )
        await build_search(
            SearchRequest(query="x", engine=engine, proxy_geo=ProxyGeo()),
            store=store,
            run_job=empty,
        )
        expected = omitted.calls[0][0].proxy_geo
        assert expected is not None and expected.country_code  # a real default
        assert empty.calls[0][0].proxy_geo == expected

    @pytest.mark.asyncio
    async def test_override_applied_to_result_scrapes(self, store):
        runner = _Runner(_SERP_DATA, scrape_responses=[_resp(), _resp()])
        await build_search(
            SearchRequest(
                query="x",
                scrape=True,
                proxy_type="mobile",
                proxy_pool_id="pool-123",
            ),
            store=store,
            run_job=runner,
        )
        for page in runner.calls[1]:
            assert page.proxy_type == "mobile"
            assert page.proxy_pool_id == "pool-123"


class TestSearchTiming:
    @pytest.mark.asyncio
    async def test_response_includes_took_ms(self, store):
        runner = _Runner(_SERP_DATA)
        res = await build_search(SearchRequest(query="x"), store=store, run_job=runner)
        assert isinstance(res.took_ms, int)
        assert res.took_ms >= 0


class TestSearchEngineOverride:
    """browser_engine + Camoufox premium fields flow through the SERP and result scrapes."""

    def test_engine_override_set_when_engine_provided(self):
        override = _engine_override(SearchRequest(query="x", browser_engine="firefox"))
        assert override["browser_engine"] == "firefox"

    def test_engine_override_empty_when_engine_unset(self):
        # None default -> preset engine wins; override must be empty.
        override = _engine_override(SearchRequest(query="x"))
        assert "browser_engine" not in override

    def test_engine_override_camoufox_opts_included_when_set(self):
        override = _engine_override(
            SearchRequest(
                query="x",
                browser_engine="camoufox",
                humanize=True,
                block_webgl=True,
                spoof_os="windows",
                addons=["ublock"],
            )
        )
        assert override["browser_engine"] == "camoufox"
        assert override["humanize"] is True
        assert override["block_webgl"] is True
        assert override["spoof_os"] == "windows"
        assert override["addons"] == ["ublock"]

    def test_engine_override_camoufox_defaults_not_included(self):
        # Default-off fields must NOT pollute the override dict (preset wins).
        override = _engine_override(SearchRequest(query="x", browser_engine="camoufox"))
        assert "humanize" not in override
        assert "block_webgl" not in override
        assert "spoof_os" not in override
        assert "addons" not in override

    @pytest.mark.asyncio
    async def test_browser_engine_override_applied_to_serp(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(query="x", browser_engine="firefox"),
            store=store,
            run_job=runner,
        )
        serp = runner.calls[0][0]
        assert serp.browser_engine == "firefox"

    @pytest.mark.asyncio
    async def test_no_browser_engine_keeps_preset_engine(self, store):
        # Default (None) must NOT inject browser_engine into the override, so
        # the preset's own engine setting (e.g. camoufox for yandex) survives.
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(query="x", engine="yandex"),
            store=store,
            run_job=runner,
        )
        serp = runner.calls[0][0]
        # The yandex_search preset defaults to camoufox; the override must not
        # have clobbered it with chromium (the old hard-coded default).
        assert serp.browser_engine == "camoufox"

    @pytest.mark.asyncio
    async def test_browser_engine_override_applied_to_result_scrapes(self, store):
        runner = _Runner(_SERP_DATA, scrape_responses=[_resp(), _resp()])
        await build_search(
            SearchRequest(query="x", scrape=True, browser_engine="firefox"),
            store=store,
            run_job=runner,
        )
        for page in runner.calls[1]:
            assert page.browser_engine == "firefox"


class TestSearchWarmup:
    """warmup and prem_proxy_options flow from SearchRequest into the SERP ScrapeRequest."""

    @pytest.mark.asyncio
    async def test_warmup_forwarded_to_serp(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(query="x", warmup=WarmupOptions(type="homepage", dwell_ms=500)),
            store=store,
            run_job=runner,
        )
        serp = runner.calls[0][0]
        assert serp.warmup is not None
        assert serp.warmup.type == "homepage"
        assert serp.warmup.dwell_ms == 500

    @pytest.mark.asyncio
    async def test_warmup_inherited_from_preset_when_unset(self, store):
        # google_search ships warmup {type: homepage}; with no request-level warmup
        # the SERP job inherits it — omitting the field is not the same as disabling.
        runner = _Runner(_SERP_DATA)
        await build_search(SearchRequest(query="x"), store=store, run_job=runner)
        serp = runner.calls[0][0]
        assert serp.warmup is not None
        assert serp.warmup.type == "homepage"

    @pytest.mark.asyncio
    async def test_prem_proxy_options_forwarded_to_serp(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(
            SearchRequest(
                query="x",
                proxy_type="prem_res_rotating",
                prem_proxy_options=PremProxyOptions(ip_filter="max-speed-security"),
            ),
            store=store,
            run_job=runner,
        )
        serp = runner.calls[0][0]
        assert serp.prem_proxy_options is not None
        assert serp.prem_proxy_options.ip_filter == "max-speed-security"

    @pytest.mark.asyncio
    async def test_prem_proxy_options_absent_when_unset(self, store):
        runner = _Runner(_SERP_DATA)
        await build_search(SearchRequest(query="x"), store=store, run_job=runner)
        serp = runner.calls[0][0]
        assert serp.prem_proxy_options is None


class TestUnsupportedEngineOptionsAfterTheSerp:
    """The per-result ScrapeRequest is built AFTER the SERP has been paid for.

    `scrape_options` is caller-supplied and is merged with the panel's engine
    only at that point, so an unsupported pair (camoufox + session_id, say)
    first raises there. Uncaught, a raw pydantic ValidationError escapes the
    route — there is no exception handler for it in `src/`, so it is a 500 for
    what is a bad request, with the SERP launch, proxy bandwidth and preset run
    already spent.
    """

    @pytest.mark.parametrize("bad_options", [
        {"render": False},
        {"session_id": "sess_abc"},
        {"cookies": [{"name": "a", "value": "b", "domain": "e.com", "path": "/"}]},
    ])
    @pytest.mark.asyncio
    async def test_it_is_a_client_error_the_route_can_map(self, bad_options):
        from src.api.search import _scrape_result_pages
        from src.presets.materializer import MaterializeError
        from src.schemas import SearchRequest, SearchResult

        req = SearchRequest(query="q", engine="google", scrape=True,
                            scrape_options=bad_options)
        results = [SearchResult(url="https://e.com/1")]

        async def never_called(_reqs):
            raise AssertionError("the SERP scrape must not be attempted")

        with pytest.raises(MaterializeError) as exc:
            await _scrape_result_pages(
                req, results, {}, {"browser_engine": "camoufox"},
                never_called, [],
            )

        assert "camoufox" in str(exc.value)


def test_engine_override_forwards_the_fingerprint_profile():
    override = _engine_override(
        SearchRequest(query="x", browser_engine="camoufox",
                      fingerprint_profile="windows_on_host")
    )

    assert override["fingerprint_profile"] == "windows_on_host"


def test_engine_override_omits_an_unset_fingerprint_profile():
    """An unset field must not overwrite the SERP preset's own value."""
    override = _engine_override(SearchRequest(query="x", browser_engine="camoufox"))

    assert "fingerprint_profile" not in override


def _google_block(title: str, href: str, snippet: str) -> str:
    """One `div.tF2Cxc` as Google ships it since 2026-08: the organic href is a
    RELATIVE `/goto?url=` stub, so the block carries no http anchor at all."""
    return (
        f'<div class="tF2Cxc"><div class="yuRUbf"><a href="{href}"><h3>{title}</h3>'
        f'<cite>https://a.example › p</cite></a></div>'
        f'<div class="VwiC3b" data-sncf="1">{snippet}</div></div>'
    )


class TestGoogleResultsTakeTheResolvedLinksColumn:
    def test_the_resolved_link_is_the_url(self):
        data = {
            "result_blocks": [_google_block("First", "/goto?url=CAES1", "s1"),
                              _google_block("Second", "/goto?url=CAES2", "s2")],
            "links": ["https://a.example/1", "https://b.example/2"],
        }
        res = _parse_results(data, ENGINES["google"])
        assert [(r.title, r.url) for r in res] == [("First", "https://a.example/1"), ("Second", "https://b.example/2")]

    def test_a_stub_that_did_not_resolve_drops_its_row(self):
        """The aligned column is authoritative: a raw `/goto` stub is not a
        destination, and inventing one from the block would be worse."""
        data = {
            "result_blocks": [_google_block("First", "/goto?url=CAES1", "s1"),
                              _google_block("Second", "/goto?url=CAES2", "s2")],
            "links": ["https://a.example/1", "/goto?url=CAES2"],
        }
        res = _parse_results(data, ENGINES["google"])
        assert [(r.title, r.url) for r in res] == [("First", "https://a.example/1")]

    def test_a_row_the_preset_nulled_stays_dropped_even_if_the_block_has_an_anchor(self):
        """The bing shape the first version of this rule got wrong. bing_search's
        `links` pipeline nulls anything still on bing.com — an ad, an undecodable
        wrapper — and the block still carries the raw `/ck/a` anchor. Falling
        back to that anchor resurrected, re-decoded, exactly the URL the preset
        author had excluded. A None in an aligned column means "not a result"."""
        block = ('<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9hZHMuZXhhbXBsZQ&ntb=1">'
                 'Excluded</a></h2><div class="b_caption"><p class="b_lineclamp2">s</p></div></li>')
        data = {"result_blocks": [block, block.replace("Excluded", "Kept")],
                "links": [None, "https://kept.example/"]}
        res = _parse_results(data, ENGINES["bing"])
        assert [(r.title, r.url) for r in res] == [("Kept", "https://kept.example/")]

    def test_a_misaligned_links_column_is_ignored_rather_than_zipped_wrong(self):
        data = {
            "result_blocks": [_google_block("First", "/goto?url=CAES1", "s1"),
                              _google_block("Second", "/goto?url=CAES2", "s2")],
            "links": ["https://a.example/1"],
        }
        assert _parse_results(data, ENGINES["google"]) == []

    def test_without_a_links_column_google_blocks_yield_nothing(self):
        """Pins what `/api/v1/search?engine=google` returned before this: every
        block dropped, because `_first_http_link` finds no http anchor in a
        block whose only href is the stub."""
        data = {"result_blocks": [_google_block("First", "/goto?url=CAES1", "s1")]}
        assert _parse_results(data, ENGINES["google"]) == []
