"""A SERP whose every result links back to the search engine is not a SERP.

Bing wraps organic hrefs in bing.com/ck/a redirects, so `links` read straight
from href pointed at bing.com on every row — 100% populated, 100% useless, and
invisible to any fill-rate or row-count check. The preset now decodes the
wrapper, but the class of failure is not Bing-specific: any engine that starts
wrapping, or a selector that drifts onto internal navigation, produces the same
shape. This warns on the shape rather than on the engine.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from src.queue.scrape_runner import self_referential_link_warning as warn


class TestSelfReferentialLinks:
    def test_every_link_on_the_pages_own_host_is_reported(self):
        data = {"links": [
            "https://www.bing.com/ck/a?u=a1AAA",
            "https://www.bing.com/ck/a?u=a1BBB",
            "https://www.bing.com/ck/a?u=a1CCC",
        ]}

        out = warn(data, "https://www.bing.com/search?q=x")

        assert out is not None
        assert "bing.com" in out

    def test_real_destinations_are_not_reported(self):
        data = {"links": [
            "https://www.pcmag.com/picks/the-best-laptops",
            "https://www.cnet.com/tech/computing/best-laptop/",
            "https://www.tomshardware.com/laptops/best-laptops",
        ]}

        out = warn(data, "https://www.bing.com/search?q=x")

        assert out is None

    def test_a_single_self_link_among_real_ones_is_not_reported(self):
        """Engines legitimately link to themselves — 'more results', a cached
        copy. The signal is that there is nothing ELSE."""
        data = {"links": [
            "https://www.pcmag.com/picks/the-best-laptops",
            "https://www.cnet.com/tech/computing/best-laptop/",
            "https://www.bing.com/search?q=x&first=10",
        ]}

        out = warn(data, "https://www.bing.com/search?q=x")

        assert out is None

    def test_too_few_links_to_judge(self):
        """One or two rows is not evidence of anything; a product page that
        happens to link to itself must not trip this."""
        data = {"links": ["https://www.bing.com/ck/a?u=a1AAA"]}

        assert warn(data, "https://www.bing.com/search?q=x") is None

    def test_exactly_two_wrapper_shaped_links_is_still_too_few(self):
        """Two identical-path, same-host links are the wrapper SHAPE, but
        the row-count floor comes first: two rows is still not enough
        evidence either way, regardless of what shape they're in. Pins the
        `< 3` boundary itself, not just the "1 link" case above -- at
        exactly 2 the ratio check alone would actually fire (1 distinct
        path * 2 == 2 urls, satisfying "at most half distinct"), so this
        floor is load-bearing, not redundant with it."""
        data = {"links": [
            "https://www.bing.com/ck/a?u=a1AAA", "https://www.bing.com/ck/a?u=a1BBB",
        ]}

        assert warn(data, "https://www.bing.com/search?q=x") is None

    def test_host_comparison_is_case_insensitive_on_the_url_side(self):
        data = {"links": [
            "https://WWW.Bing.com/ck/a?u=a1AAA",
            "https://www.bing.com/ck/a?u=a1BBB",
            "https://www.bing.com/ck/a?u=a1CCC",
        ]}

        out = warn(data, "https://www.bing.com/search?q=x")
        assert out is not None

    def test_host_comparison_is_case_insensitive_on_the_final_url_side(self):
        data = {"links": [
            "https://www.bing.com/ck/a?u=a1AAA",
            "https://www.bing.com/ck/a?u=a1BBB",
            "https://www.bing.com/ck/a?u=a1CCC",
        ]}

        out = warn(data, "https://WWW.BING.com/search?q=x")
        assert out is not None

    def test_a_non_string_item_among_real_urls_is_skipped_not_a_crash(self):
        """A field's per-row list can legitimately hold `None` (a row with
        no link at all, in a column that otherwise holds strings) -- must be
        skipped, not passed to `.startswith()`."""
        data = {"links": [
            "https://www.bing.com/ck/a?u=a1AAA", None,
            "https://www.bing.com/ck/a?u=a1BBB",
            "https://www.bing.com/ck/a?u=a1CCC",
        ]}

        out = warn(data, "https://www.bing.com/search?q=x")
        assert out is not None

    def test_data_as_a_list_of_per_row_objects_is_handled(self):
        """Not every preset returns parallel arrays -- some emit one object
        per row. `rows = data if isinstance(data, list) else [data]` exists
        for exactly this shape; nothing before this test ever exercised
        `data` actually BEING a list of multiple row dicts."""
        data = [
            {"url": "https://www.bing.com/ck/a?u=a1AAA"},
            {"url": "https://www.bing.com/ck/a?u=a1BBB"},
            {"url": "https://www.bing.com/ck/a?u=a1CCC"},
        ]

        out = warn(data, "https://www.bing.com/search?q=x")
        assert out is not None

    def test_a_non_dict_row_in_a_list_of_rows_is_skipped_not_a_crash(self):
        """`isinstance(row, dict)` guards each row, not just each item: a
        list of per-row objects with a stray `None`/string entry (a
        malformed row, not a URL-bearing one) must be skipped rather than
        crashing on `.values()`."""
        data = [
            {"url": "https://www.bing.com/ck/a?u=a1AAA"},
            None,
            {"url": "https://www.bing.com/ck/a?u=a1BBB"},
            {"url": "https://www.bing.com/ck/a?u=a1CCC"},
        ]

        out = warn(data, "https://www.bing.com/search?q=x")
        assert out is not None

    def test_data_as_a_list_of_per_row_objects_with_real_destinations(self):
        data = [
            {"url": "https://www.pcmag.com/picks/the-best-laptops"},
            {"url": "https://www.cnet.com/tech/computing/best-laptop/"},
            {"url": "https://www.tomshardware.com/laptops/best-laptops"},
        ]

        out = warn(data, "https://www.bing.com/search?q=x")
        assert out is None

    def test_no_urls_at_all_is_silent(self):
        data = {"title": "A product", "price": "19.99"}

        assert warn(data, "https://shop.example/p/1") is None

    def test_a_single_object_row_is_handled(self):
        """Not every preset returns parallel arrays."""
        data = {"url": "https://shop.example/p/1", "title": "x"}

        assert warn(data, "https://shop.example/p/1") is None

    def test_no_final_url_means_nothing_to_compare_against(self):
        data = {"links": ["https://a.com/1", "https://a.com/2", "https://a.com/3"]}

        assert warn(data, None) is None

    def test_an_empty_netloc_final_url_is_silent_not_a_false_fire(self):
        """`about:blank` is a real Playwright `final_url` (e.g. a fetch that
        never navigated), and `urlsplit("http:///a?1").netloc == ""` despite
        `"http:///a?1".startswith("http://")` being True -- a malformed but
        collector-eligible URL. Without the explicit `own_host` emptiness
        check, an empty `final_url` host would match these malformed urls'
        equally-empty netlocs (both "") and pass the "all on-site" gate,
        turning `about:blank` into a false fire instead of the "not enough
        context to judge" case this actually is."""
        data = {"l": ["http:///a?1", "http:///a?2", "http:///a?3"]}

        assert warn(data, "about:blank") is None

    def test_a_site_linking_to_distinct_pages_within_itself_is_not_a_failure(self):
        """Fix-round-2 finding 2: crawling a shop's category page yields
        internal links by design, and three DISTINCT product pages on the
        shop's own host is precisely the shape of a real catalog listing
        (eBay's own /itm/<id>, Amazon's own /dp/<asin>) -- not evidence of a
        stuck redirect. The distinct-path signal now correctly recognizes
        that and stays silent, where an earlier version of this guard fired
        on host-alone and required a non-accusatory message to compensate.
        """
        data = {"links": [
            "https://shop.example/p/1", "https://shop.example/p/2",
            "https://shop.example/p/3",
        ]}

        out = warn(data, "https://shop.example/category")

        assert out is None

    def test_a_site_redirecting_through_one_path_on_itself_still_fires(self):
        """The sibling case that keeps the guard meaningful: three links on
        the shop's OWN host that all share the SAME path (e.g. a tracking or
        pagination redirect script) still look exactly like Bing's `ck/a` --
        no distinct destinations, regardless of whose host it is."""
        data = {"links": [
            "https://shop.example/out?to=1", "https://shop.example/out?to=2",
            "https://shop.example/out?to=3",
        ]}

        out = warn(data, "https://shop.example/category")

        assert out is not None
        assert "shop.example" in out


class TestExactHostDistinctPathBoundary:
    """Fix-round-4 finding 2: the boundary was `distinct_paths * 2 <=
    urls` ("at most half distinct"), which turned out to be the wrong
    magnitude -- two ordinary, benign HTML shapes sit at exactly a 2x
    duplication factor (eBay-shaped cards linking their destination twice;
    a listing mixed with a `?page=N` pagination bar), and a "half" boundary
    fired on both (see TestBenignDuplicationStaysHealthy below). The rule
    is now "at most a QUARTER of the urls have a distinct path"
    (`distinct_paths * 4 <= urls`) -- pinned exactly at both sides of that
    line, since the previous boundary shipped with neither side pinned (the
    `>1 -> >2` mutant survived an entire round because of it)."""

    def test_exactly_a_quarter_distinct_paths_still_fires(self):
        # 8 urls, 2 distinct paths: 2*4 == 8 -> fires (boundary inclusive).
        data = {"urls": [
            "https://x.example/a?i=1", "https://x.example/a?i=2",
            "https://x.example/a?i=3", "https://x.example/a?i=4",
            "https://x.example/b?i=5", "https://x.example/b?i=6",
            "https://x.example/b?i=7", "https://x.example/b?i=8",
        ]}
        out = warn(data, "https://x.example/search?q=y")
        assert out is not None

    def test_just_over_a_quarter_distinct_paths_is_silent(self):
        # 8 urls, 3 distinct paths: 3*4 > 8 -> silent.
        data = {"urls": [
            "https://x.example/a?i=1", "https://x.example/a?i=2",
            "https://x.example/a?i=3",
            "https://x.example/b?i=4", "https://x.example/b?i=5",
            "https://x.example/b?i=6",
            "https://x.example/c?i=7", "https://x.example/c?i=8",
        ]}
        out = warn(data, "https://x.example/search?q=y")
        assert out is None

    def test_two_distinct_paths_still_goes_through_the_ratio_not_the_floor(self):
        """The absolute floor is `n_distinct <= 1` -- exactly 2 distinct
        paths must still be judged by the RATIO, not waved through as "small
        enough" on its own. 6 urls, 2 distinct paths (3 each): 2*4 > 6 ->
        silent. (A floor accidentally widened to `<= 2` would skip the ratio
        entirely here and fire regardless of url count.)"""
        data = {"urls": [
            "https://x.example/a?i=1", "https://x.example/a?i=2",
            "https://x.example/a?i=3",
            "https://x.example/b?i=4", "https://x.example/b?i=5",
            "https://x.example/b?i=6",
        ]}
        out = warn(data, "https://x.example/search?q=y")
        assert out is None

    def test_a_ratio_between_a_quarter_and_a_third_is_silent(self):
        """Pins the *4 multiplier itself, not just the boundary it draws:
        10 urls, 3 distinct paths is ratio 0.3 -- above a quarter (silent)
        but below a third (a looser multiplier would wrongly fire here)."""
        data = {"urls": [
            "https://x.example/a?i=1", "https://x.example/a?i=2",
            "https://x.example/a?i=3", "https://x.example/a?i=4",
            "https://x.example/b?i=5", "https://x.example/b?i=6",
            "https://x.example/b?i=7",
            "https://x.example/c?i=8", "https://x.example/c?i=9",
            "https://x.example/c?i=10",
        ]}
        out = warn(data, "https://x.example/search?q=y")
        assert out is None


class TestMultiPathWrapperIsStillCaught:
    """The concrete case a naive `> 1 distinct path -> silent` boundary
    missed: a redirect wrapper that uses MORE than one endpoint. Modelled on
    Google mixing organic `/url?q=...` redirects with ad `/aclk?sa=...`
    ones, sized like a realistic SERP (comfortably inside the quarter
    boundary: 2 distinct paths over 12 urls, ratio ~0.17) -- two paths
    total, but every single result is still a dead end."""

    def test_two_path_wrapper_fires(self):
        data = {"urls": [
            f"https://www.google.com/url?q={i}" for i in range(9)
        ] + [
            "https://www.google.com/aclk?sa=1", "https://www.google.com/aclk?sa=2",
            "https://www.google.com/aclk?sa=3",
        ]}
        out = warn(data, "https://www.google.com/search?q=laptop")
        assert out is not None
        assert "google.com" in out


class TestBenignDuplicationStaysHealthy:
    """Fix-round-4 finding 2's concrete counter-examples: ordinary HTML
    shapes that duplicate each destination roughly 2x, which is exactly the
    ratio a "half" boundary fired on. Neither is a redirect wrapper -- both
    have real, useful, distinct destinations; the duplication is a
    markup/pagination artifact, not evidence of a stuck unwrap."""

    def test_each_card_linking_its_destination_twice_stays_silent(self):
        """eBay-shaped: 30 real distinct /itm/<id> destinations, each one
        linked twice (title anchor + image anchor) -- 60 urls, 30 distinct
        paths, ratio 0.5."""
        data = {"urls": [
            f"https://www.ebay.com/itm/{100000 + i}"
            for i in range(30)
            for _ in range(2)
        ]}
        out = warn(data, "https://www.ebay.com/sch/i.html?_nkw=laptop")
        assert out is None

    def test_pagination_links_mixed_with_real_results_stays_silent(self):
        """10 real distinct product links plus a 12-link `?page=N`
        pagination bar (all sharing ONE path) -- 22 urls, 11 distinct paths,
        ratio 0.5."""
        data = {"urls": (
            [f"https://shop.example/p/{i}" for i in range(10)]
            + [f"https://shop.example/list?page={i}" for i in range(12)]
        )}
        out = warn(data, "https://shop.example/list?page=1")
        assert out is None


class TestMarketplaceShapedDataStaysHealthy:
    """Fix-round-3 finding 3: there is no preset-name exemption any more --
    `self_referential_link_warning` takes no `preset_name` argument at all.
    A marketplace's own search results (many urls, each a distinct path, on
    the search page's own EXACT host) are silent because of that shape
    alone, the same way for a named preset, an unrecognized one, or none —
    there is nothing left to name."""

    def test_amazon_shaped_distinct_paths_are_silent(self):
        data = {"urls": [
            "https://www.amazon.de/dp/A1", "https://www.amazon.de/dp/A2",
            "https://www.amazon.de/dp/A3",
        ]}
        assert warn(data, "https://www.amazon.de/s?k=laptop") is None

    def test_ebay_shaped_distinct_paths_are_silent(self):
        data = {"urls": [
            "https://www.ebay.com/itm/1", "https://www.ebay.com/itm/2",
            "https://www.ebay.com/itm/3",
        ]}
        assert warn(data, "https://www.ebay.com/sch/i.html?_nkw=laptop") is None

    def test_an_unnamed_marketplace_shaped_page_is_silent_too(self):
        """The raw `/scrape` + inline `extract` path: no `preset_meta` at
        all, still correctly silent on distinct-path data because the
        property never needed a name to begin with."""
        data = {"urls": [
            "https://shop.example/dp/A1", "https://shop.example/dp/A2",
            "https://shop.example/dp/A3",
        ]}
        assert warn(data, "https://shop.example/s?k=x") is None


def _real_yandex_search_run():
    """The 2026-08-27 dual-engine audit record for yandex_search_camoufox
    (research/preset_audit_dual_engine_2026_08_27.json), moscow locale, run
    0. Measured composition of its 27 links: 11 on `yabs.yandex.ru`, 2 on
    `yandex.ru` (the page's OWN EXACT host), 1 on `market.yandex.ru`, and
    13 on other third-party sites (citilink.ru, mvideo.ru, ozon.ru,
    dns-shop.ru, wildberries, ...) -- 14 genuinely off-host in total (13
    third-party + market.yandex.ru). 26 of the 27 paths are distinct. This
    is NOT the run with the most on-host ad redirects -- yandex_search_
    chromium's moscow runs 0 and 1 each carry 14 `yabs.yandex.ru` links,
    more than this one's 11; it is used here only because it was the
    fixture already on hand when the (since-retracted) yandex claim was
    first written, and re-measuring it in place keeps the correction
    anchored to the same record rather than swapping in a new one."""
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "research" / "preset_audit_dual_engine_2026_08_27.json"
    )
    runs = json.loads(path.read_text(encoding="utf-8"))
    for run in runs:
        if run.get("preset") == "yandex_search_camoufox" and run.get("locale") == "moscow" and run.get("run") == 0:
            links = (run.get("data") or {}).get("links") or []
            final_url = (run.get("meta") or {}).get("final_url")
            return links, final_url
    raise AssertionError("expected audit run not found")


class TestYandexWasCorrectlySilentAllAlong:
    """CORRECTION: an earlier round of this fix claimed yandex_search
    returned "27/27 yabs.yandex.ru" ad redirects and that the guard's
    silence there was a false negative needing a subdomain-aware host
    comparison to fix. That claim was never measured against the record
    and was wrong, and a first attempt at retracting it here introduced
    two MORE unmeasured numbers (an inconsistent "16 off-host" and an
    unverified "most on-host ad redirects of any run") plus a causal
    explanation that was wrong in the guard's own terms. Re-measured
    directly (see `_real_yandex_search_run`'s docstring for the full
    composition and its sourcing caveat) and asserted below so these
    numbers can't drift from the record silently again:

    27 links = 11 `yabs.yandex.ru` + 2 `yandex.ru` (the page's own EXACT
    host) + 1 `market.yandex.ru` + 13 other third-party. Under the shipped
    exact-host comparison, `yabs.yandex.ru` is NOT the same host as
    `yandex.ru` -- so `on_own` is 2, not 11, and 25 of the 27 urls fail the
    "is every url on this exact host" gate. That is the actual reason this
    run is silent: it never reaches the distinct-path logic at all. There
    never was a yandex false negative to fix, and this test exists to pin
    that the real record stays silent -- not to claim any defect was
    caught, here or in the original (retracted) framing.
    """

    def test_the_measured_composition_matches_what_the_docstrings_claim(self):
        """Makes the prose above self-checking: if the audit file the
        numbers were read from ever changes, or was mis-read again, this
        fails instead of a docstring quietly going stale."""
        from urllib.parse import urlsplit

        links, final_url = _real_yandex_search_run()
        own_host = urlsplit(final_url).netloc.lower()
        hosts = [urlsplit(u).netloc.lower() for u in links]

        assert own_host == "yandex.ru"
        assert len(links) == 27
        assert hosts.count("yabs.yandex.ru") == 11
        assert hosts.count(own_host) == 2
        assert len({urlsplit(u).path for u in links}) == 26

    def test_the_real_record_is_silent(self):
        links, final_url = _real_yandex_search_run()
        assert len(links) >= 3
        assert warn({"links": links}, final_url) is None


def test_a_synthetic_all_wrapper_page_on_its_own_exact_host_still_fires():
    """Not a reproduction of any observed preset defect (see the correction
    above) -- a plain, hand-built worst case for the exact-host rule: EVERY
    result stays on the search page's own host, crowded onto one path, only
    the query differing. If a real engine ever regressed to this shape in
    full, the guard must still catch it."""
    data = {"links": [
        "https://example-search.test/count?id=1",
        "https://example-search.test/count?id=2",
        "https://example-search.test/count?id=3",
        "https://example-search.test/count?id=4",
    ]}
    out = warn(data, "https://example-search.test/search?q=laptop")
    assert out is not None
    assert "example-search.test" in out


def test_a_regression_in_amazon_searchs_own_unwrap_would_be_caught():
    """Finding 3's concrete demonstration, run through amazon_search_
    chromium's ACTUAL shipped `urls` post_process pipeline (unwrap_param +
    urljoin), not a hand-rolled guard call: if a future Amazon format change
    dropped the `url=` parameter from sponsored redirects, `unwrap_param`'s
    deliberate pass-through-on-no-match behaviour (correct and necessary for
    organic rows) means every sponsored row would stay wrapped --
    `https://www.amazon.de/sspa/click?...` -- and still resolve absolute via
    `urljoin`. With no preset-name exemption at all, the guard fires on
    exactly the preset whose own unwrap introduced this risk.
    """
    import json as _json

    from src.extract.extractor import extract_fields
    from src.extract.models import ExtractRule, FieldRule
    from src.presets.materializer import PresetScrapeRequest, materialize
    from src.presets.models import Preset
    from src.presets.store import DEFAULT_BUILTIN_DIR

    preset = Preset(**_json.loads(
        (DEFAULT_BUILTIN_DIR / "amazon_search_chromium.json").read_text(encoding="utf-8")
    ))
    scrape = materialize(
        preset,
        PresetScrapeRequest(
            source="amazon_search_chromium", preset_params={"query": "laptop"}, locale="de"
        ),
    )
    ops = scrape.extract.fields["urls"].post_process

    # Every row is the sponsored-redirect shape but WITHOUT a `url=`
    # parameter -- exactly what a format change dropping it would produce.
    html = "".join(
        f'<a href="/sspa/click?ie=UTF8&amp;spc=X{i}">x</a>' for i in range(5)
    )
    rule = ExtractRule(
        type="css",
        fields={"urls": FieldRule(selector="a", attr="href", all=True, post_process=ops)},
    )
    data, _ = extract_fields(html, rule)

    assert all(u.startswith("https://www.amazon.de/sspa/click") for u in data["urls"])

    out = warn({"urls": data["urls"]}, "https://www.amazon.de/s?k=laptop")
    assert out is not None
    assert "amazon.de" in out


@pytest.mark.asyncio
async def test_the_queue_actually_emits_the_warning():
    """The wiring, not just the helper.

    Removing the call site left the whole suite green, which is how a feature
    ships switched off. This drives a real `run_scrape` and reads `warnings`
    off the response the caller would receive.
    """
    from src.browser.runner import FetchResult
    from src.queue.envelope import ScrapeOk
    from src.queue.scrape_runner import run_scrape

    html = (
        '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1AAA">a</a></h2></li>'
        '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1BBB">b</a></h2></li>'
        '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1CCC">c</a></h2></li>'
    )

    class _Runner:
        async def resolve_proxy(self, proxy):
            return proxy, None

        async def fetch(self, **_kw):
            return FetchResult(
                html=html, final_url="https://www.bing.com/search?q=x",
                status_code=200, screenshot_b64=None, ok=True, error=None,
            )

    out = await run_scrape(
        _Runner(), "req_selflink",
        {
            "url": "https://www.bing.com/search?q=x", "device": "desktop",
            "proxy_type": "none",
            "extract": {"type": "css", "fields": {
                "links": {"selector": "li.b_algo h2 a", "attr": "href", "all": True}}},
        },
        None,
    )

    assert isinstance(out, ScrapeOk)
    assert any("all_extracted_links_self_referential" in w for w in out.result.warnings), (
        out.result.warnings
    )


@pytest.mark.asyncio
async def test_the_queue_stays_silent_on_marketplace_shaped_results():
    """The wiring for the distinct-path property itself: a real `run_scrape`
    whose extracted urls are all on the page's own host but each a distinct
    path (the correct shape for a marketplace's own search) must not raise
    the self-referential warning. No `preset_meta` at all -- the property
    needs no name to work, including through the real queue path.
    """
    from src.browser.runner import FetchResult
    from src.queue.envelope import ScrapeOk
    from src.queue.scrape_runner import run_scrape

    html = (
        '<a href="https://www.amazon.de/dp/A1">a</a>'
        '<a href="https://www.amazon.de/dp/A2">b</a>'
        '<a href="https://www.amazon.de/dp/A3">c</a>'
    )

    class _Runner:
        async def resolve_proxy(self, proxy):
            return proxy, None

        async def fetch(self, **_kw):
            return FetchResult(
                html=html, final_url="https://www.amazon.de/s?k=laptop",
                status_code=200, screenshot_b64=None, ok=True, error=None,
            )

    out = await run_scrape(
        _Runner(), "req_marketplace",
        {
            "url": "https://www.amazon.de/s?k=laptop", "device": "desktop",
            "proxy_type": "none",
            "extract": {"type": "css", "fields": {
                "urls": {"selector": "a", "attr": "href", "all": True}}},
        },
        None,
    )

    assert isinstance(out, ScrapeOk)
    assert not any(
        "all_extracted_links_self_referential" in w for w in out.result.warnings
    ), out.result.warnings


def test_the_mobile_de_listing_shape_fires_which_is_why_the_preset_declares_links_stay_on_site():
    """mobile.de's search (2026-09-06): 24 of 24 listings are the site's own
    `/fahrzeuge/details.html?id=<ad id>&…` -- one path, only the query
    differing, exactly the wrapper shape this guard looks for. The guard is
    right to fire on the URL alone; the preset carries `links_stay_on_site`
    so the worker does not ask it (tests/queue/test_resolve_redirects_wiring.py)."""
    data = {"urls": [
        f"https://suchen.mobile.de/fahrzeuge/details.html?id={391420794 + n}" for n in range(24)
    ]}
    out = warn(data, "https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true")
    assert out is not None
    assert "suchen.mobile.de" in out
