"""`query_relevance_warning`: did the page answer the query we asked?

The rule, the measurements and the calibration live in the function's
docstring. The fixtures here are the shapes recorded live on 2026-08-27 and
2026-09-04: Bing's dictionary SERP for "best" served to the `ru` exits (a dated
snippet carries the query's year, which a ratio-based first draft counted), the
Best Buy variant served to a `us` exit (one snippet lists laptops, which an
"anywhere on the page" second draft counted), a Honda forum page, and a real
laptop SERP.
"""
from __future__ import annotations

import pytest

from src.browser.runner import FetchResult
from src.queue.envelope import ScrapeOk
from src.queue.scrape_runner import query_relevance_warning, run_scrape

Q = "best laptop 2026"

DICTIONARY_SERP = {
    "titles": [
        "Перевод BEST с английского на русский: Cambridge Dictionary",
        "BEST Definition & Meaning - Merriam-Webster",
        "BEST | English meaning - Cambridge Dictionary",
        "Best - definition of best by The Free Dictionary",
        "best - Wiktionary, the free dictionary",
    ],
    "snippets": [
        "27 авг. 2026 г. · in the best of all possible worlds",
        "The meaning of BEST is excelling all others.", "", "", "",
    ],
}
HONDA_SERP = {
    "titles": ["The unofficial Honda Forum and Discussion Board",
               "Honda Scooters - The unofficial Honda Forum", "2000 Accord misfiring - Honda Accord Forum"],
    "snippets": ["Forums for Honda owners", "Scooter talk", "Misfire on cylinder 3"],
}
REAL_SERP = {
    "titles": ["The Best Laptops We've Tested (September 2026) - PCMag",
               "Best laptop 2026: our top picks", "Best Laptops of 2026 | WIRED"],
    "snippets": ["We test every laptop", "The best laptops you can buy in 2026", "Our favorite notebooks"],
    "links": ["https://a.example", "https://b.example", "https://c.example"],
}


class TestTheMeasuredShapes:
    def test_the_dictionary_page_for_best_is_flagged(self):
        note = query_relevance_warning(DICTIONARY_SERP, Q)
        assert note is not None
        assert note.startswith("serp_query_mismatch:")
        assert "1 of 2" in note, note                     # "best" is there, "laptop" nowhere
        for leak in ("Merriam", "Cambridge", "laptop", "2026"):
            assert leak not in note, "neither titles nor the query enter the warning"

    def test_a_wholly_unrelated_page_is_flagged(self):
        note = query_relevance_warning(HONDA_SERP, Q)
        assert note is not None and "2 of 2" in note

    def test_a_real_serp_for_the_query_is_silent(self):
        assert query_relevance_warning(REAL_SERP, Q) is None

    def test_a_one_word_product_query_naming_a_minority_of_rows_is_silent(self):
        """`google_shopping`/de for "laptop": under a third of the rows say the
        word, the rest are model names. A per-row majority rule flagged it; the
        page did answer the query."""
        models = ["Apple MacBook Neo", "Lenovo IdeaPad 5", "Dell XPS 13", "Acer Swift 16", "Asus Zenbook 14",
                  "HP OmniBook 5", "Samsung Galaxy Book"]
        data = {"titles": models + ["HP Laptop 15", "Lenovo Laptop V15", "MSI Gaming Laptop"],
                "prices": [1.0] * 10}
        assert query_relevance_warning(data, "laptop") is None

    def test_one_snippet_naming_the_product_does_not_rescue_a_page_about_something_else(self):
        """Bing `us`: the SERP for "best" -- Best Buy stores and dictionaries --
        whose first snippet lists "laptops" among what Best Buy sells. One row
        in ten is not an answer; three are."""
        titles = ["Best Buy | Official Online Store", "BEST Definition & Meaning - Merriam-Webster",
                  "Best Buy Store Locator", "Home | Best Home Furnishings", "Best - The Free Dictionary",
                  "1600 Center Rd - Best Buy", "BEST | Cambridge Dictionary", "BEST | Dictionary.com",
                  "USA TODAY 10BEST Readers' Choice Awards", "Best Buy Store Directory"]
        snippets = ["Shop Best Buy for electronics, computers, laptops, appliances & more"] + [""] * 9
        note = query_relevance_warning({"titles": titles, "snippets": snippets}, Q)
        assert note is not None and "1 of 2" in note
        snippets[1] = snippets[2] = "The best laptops we sell"
        assert query_relevance_warning({"titles": titles, "snippets": snippets}, Q) is None


class TestWhatCountsAsATerm:
    def test_short_function_words_are_not_terms(self):
        # "a", "of", "in" cannot decide relevance; only "laptop" does.
        assert query_relevance_warning(REAL_SERP, "a laptop of in") is None
        note = query_relevance_warning(HONDA_SERP, "a laptop of in")
        assert note is not None and "1 of 1" in note

    def test_a_pure_number_is_not_a_term(self):
        data = {"titles": ["Laptop deals", "Laptop reviews", "Laptop sale"]}
        assert query_relevance_warning(data, "laptop 2027") is None
        assert query_relevance_warning(HONDA_SERP, "2026") is None

    def test_operator_tokens_are_constraints_not_words(self):
        assert query_relevance_warning(REAL_SERP, "site:pcmag.com laptop") is None
        assert query_relevance_warning(REAL_SERP, "inurl:reviews intitle:best laptop") is None
        note = query_relevance_warning(HONDA_SERP, "site:pcmag.com laptop")
        assert note is not None and "1 of 1" in note
        assert query_relevance_warning(HONDA_SERP, "site:example.com") is None

    def test_an_excluded_word_is_not_required(self):
        """`python -snake` asks for pages WITHOUT snakes; a correct SERP has none."""
        python = {"titles": ["Python tutorial", "Python docs", "Python guide"]}
        assert query_relevance_warning(python, "python -snake") is None
        note = query_relevance_warning(HONDA_SERP, "python -snake")
        assert note is not None and "1 of 1" in note
        note = query_relevance_warning(HONDA_SERP, "python-snake")
        assert note is not None and "2 of 2" in note, "a hyphenated compound is two words, not an exclusion"

    def test_a_quoted_exclusion_goes_as_a_whole(self):
        python = {"titles": ["Python tutorial", "Python docs", "Python guide"]}
        assert query_relevance_warning(python, 'python -"snake oil"') is None
        note = query_relevance_warning(HONDA_SERP, 'python -"snake oil"')
        assert note is not None and "1 of 1" in note

    def test_a_query_with_alternatives_is_not_judged(self):
        """The rule requires every term; `python OR rust` requires either."""
        assert query_relevance_warning(HONDA_SERP, "python OR rust") is None
        assert query_relevance_warning(HONDA_SERP, "python | rust") is None
        assert query_relevance_warning(HONDA_SERP, "python AND rust") is None
        note = query_relevance_warning(HONDA_SERP, "python or rust")
        assert note is not None and "2 of 2" in note, "lowercase `or` is a word, not the operator"

    def test_a_word_before_a_colon_is_still_a_word(self):
        note = query_relevance_warning(HONDA_SERP, "Python: tutorial")
        assert note is not None and "2 of 2" in note

    def test_words_of_unsegmented_scripts_are_not_judged(self):
        """`\\w{3,}` cannot cut terms out of a Japanese or Chinese query -- the
        whole query is one token whose prefix matches nothing on a real page --
        so those words are left alone (`jp` is a shipped locale of the search
        presets). A Latin word alongside is still judged on its own."""
        jp = {"titles": ["【2026年】ノートパソコンのおすすめ20選", "おすすめノートパソコン比較",
                         "ノートパソコン 人気ランキング"]}
        assert query_relevance_warning(jp, "ノートパソコンおすすめ2026") is None
        assert query_relevance_warning(HONDA_SERP, "最好的笔记本电脑2026") is None
        assert query_relevance_warning(HONDA_SERP, "ノートパソコン laptop") is not None
        assert query_relevance_warning(REAL_SERP, "ノートパソコン laptop") is None

    def test_at_most_a_dozen_terms_are_judged(self):
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
                 "hotel", "india", "juliet", "kilo", "lima", "mike"]
        page = {"titles": [" ".join(words[:12])] * 3}
        assert query_relevance_warning(page, " ".join(words)) is None, "the 13th is not judged"
        page = {"titles": [" ".join(words[:11] + words[12:])] * 3}
        note = query_relevance_warning(page, " ".join(words))
        assert note is not None and "1 of 12" in note, "the 12th is"

    def test_cyrillic_terms_match_by_prefix_so_morphology_does_not_fail_them(self):
        data = {"titles": ["Лучшие ноутбуки 2026 года — рейтинг", "Ноутбук для работы: топ моделей"],
                "snippets": ["Обзор лучших ноутбуков", "Какой ноутбук выбрать"], "x": ["a", "b", "c"]}
        assert query_relevance_warning(data, "лучший ноутбук 2026") is None

    def test_an_english_plural_in_the_query_matches_the_singular_on_the_page(self):
        data = {"titles": ["Laptop deals", "Laptop reviews", "Laptop sale"]}
        assert query_relevance_warning(data, "laptops") is None

    def test_matching_is_case_insensitive(self):
        data = {"titles": ["LAPTOP DEALS 2026", "BEST LAPTOPS", "MORE LAPTOPS"]}
        assert query_relevance_warning(data, Q) is None

    def test_a_single_term_query_is_judged_on_that_term(self):
        assert query_relevance_warning(HONDA_SERP, "laptop") is not None
        assert query_relevance_warning(REAL_SERP, "laptop") is None


class TestTheTwoSidesAreWrittenByDifferentPeople:
    """Review finding, 2026-09-08 (Раиль).

    The caller types the query and the site writes the page, so the same word
    reaches the two sides spelled differently. Each of these was a false
    `serp_query_mismatch` on a page that answered the question perfectly --
    the diacritic pair flagged BOTH terms. Folding runs on both sides.

    Replayed over every recorded audit afterwards: exactly the same 18 records
    are flagged as before, so the corpus the threshold was calibrated on is
    unmoved.
    """

    @pytest.mark.parametrize("query,titles", [
        # ё on one side, е on the other -- both spellings are ordinary Russian
        ("ёлка искусственная", ["Елка искусственная 180 см", "Елка литая premium",
                                "Елка искусственная с подсветкой", "Елка настольная"]),
        ("елка искусственная", ["Ёлка искусственная 180 см", "Ёлка литая premium",
                                "Ёлка искусственная с подсветкой", "Ёлка настольная"]),
        # Latin diacritics are decoration over the same letter
        ("café münchen", ["Cafe Munchen Zentrum", "Cafe Munchen Altstadt",
                          "Cafe Munchen Bar", "Cafe Munchen Bistro"]),
        ("cafe munchen", ["Café München Zentrum", "Café München Altstadt",
                          "Café München Bar", "Café München Bistro"]),
    ])
    def test_a_spelling_difference_is_not_a_different_query(self, query, titles):
        assert query_relevance_warning({"titles": titles}, query) is None

    def test_a_word_with_a_digit_is_not_a_term(self):
        """`16gb` typed, `16 GB` written: the glued token appears in no row and
        read as an unanswered term. The number never tells a right page from a
        wrong one -- the words around it do."""
        titles = ["Ноутбук ASUS 16 GB RAM", "Ноутбук Lenovo 16 GB",
                  "Ноутбук HP 16 GB SSD", "Ноутбук Acer 16 GB"]
        assert query_relevance_warning({"titles": titles}, "ноутбук 16gb") is None

    def test_folding_does_not_merge_и_and_й(self):
        """NFKD decomposes `й` into `и` + breve. Stripping that would make
        `мой` match `мои`, which is a different word -- so Cyrillic is folded
        by the ё/е rule alone."""
        from src.queue.scrape_runner import _query_stems
        assert _query_stems("мой") == ["мой"]
        assert _query_stems("мои") == ["мои"]

    def test_a_genuinely_wrong_page_still_warns_through_the_folding(self):
        """The point of the guard survives: folding must not silence it."""
        out = query_relevance_warning(
            {"titles": ["LEGO Store official", "Berlin Steel company",
                        "YouTube Help center", "Google"]},
            "best laptop 2026",
        )
        assert out is not None and "serp_query_mismatch" in out


class TestWhatCountsAsARow:
    def test_a_scalar_field_echoing_the_query_cannot_rescue_the_page(self):
        """The measured Bing shape: our query in the page's own `<title>` above
        someone else's results. A preset that extracts that title must not
        turn the guard off."""
        data = {"page_title": "best laptop 2026 - Search", **HONDA_SERP}
        assert query_relevance_warning(data, Q) is not None

    def test_markup_cells_are_not_text(self):
        """The search presets extract `result_blocks` with `attr: html`; its
        tags and hrefs would answer any query term that is also a markup token,
        and its text is already in the text columns."""
        blocks = ['<li class="b_algo best-laptop-2026"><a href="https://x.example/best/laptop/2026">Honda</a></li>'] * 3
        assert query_relevance_warning({**HONDA_SERP, "result_blocks": blocks}, Q) is not None
        assert query_relevance_warning({"result_blocks": blocks}, Q) is None, "markup alone is not a row"

    def test_a_numeric_column_does_not_make_a_one_title_page_a_full_one(self):
        """`prices` is float after `parse_price`; `row_alignment_mismatch`
        already reports the length gap. One title is under the floor."""
        data = {"prices": [1.0, 2.0, 3.0, 4.0], "titles": ["Honda forum"]}
        assert query_relevance_warning(data, Q) is None
        data = {"titles": [None, None, None, "Honda forum"]}
        assert query_relevance_warning(data, Q) is None

    def test_a_urls_query_string_does_not_answer_for_the_page(self):
        """`amazon_search`/`ebay_search` echo the search in every result href
        (`?keywords=laptop`, `_nkw=laptop`); the path's slug still counts."""
        echo = ["https://www.amazon.com/dp/B0/ref=sr_1?keywords=best+laptop+2026"] * 3
        assert query_relevance_warning({**HONDA_SERP, "urls": echo}, Q) is not None
        relative = ["/dp/B0/ref=sr_1?k=best+laptop+2026#reviews"] * 3
        assert query_relevance_warning({**HONDA_SERP, "urls": relative}, Q) is not None
        slug = ["https://x.example/best-laptop-2026?ref=abc"] * 3
        assert query_relevance_warning({"titles": ["A", "B", "C"], "urls": slug}, Q) is None

    def test_every_list_column_counts_including_links(self):
        data = {"titles": ["A", "B", "C"], "links": ["https://x.example/best-laptop-2026"] * 3}
        assert query_relevance_warning(data, Q) is None


class TestWhenThereIsNothingToJudge:
    @pytest.mark.parametrize("query", [None, "", "   ", "ab", "https://x"])
    def test_no_usable_query_is_silent(self, query):
        assert query_relevance_warning(HONDA_SERP, query) is None

    @pytest.mark.parametrize("data", [None, {}, {"titles": []}, {"titles": [None, None]}, "not a dict", [1, 2]])
    def test_no_extracted_text_is_silent(self, data):
        assert query_relevance_warning(data, Q) is None

    def test_too_few_rows_to_judge(self):
        """One or two rows can be legitimately terse; the shape measured is a
        FULL page of someone else's results. Three rows is the floor."""
        assert query_relevance_warning({"titles": ["Honda forum", "UPS tracking"]}, Q) is None
        assert query_relevance_warning({"titles": ["Honda forum", "UPS tracking", "Netflix"]}, Q) is not None

    def test_non_string_values_are_skipped_not_a_crash(self):
        data = {"titles": [1, {"a": 2}, "Honda", None, "UPS", "Netflix"], "n": 5}
        assert query_relevance_warning(data, Q) is not None


HONDA_HTML = "".join(f'<li class="b_algo"><h2><a href="https://x{i}.example">{t}</a></h2></li>'
                     for i, t in enumerate(HONDA_SERP["titles"]))


class _Runner:
    async def resolve_proxy(self, proxy):
        return proxy, None

    async def fetch(self, **_kw):
        return FetchResult(html=HONDA_HTML, final_url="https://www.bing.com/search?q=x",
                           status_code=200, screenshot_b64=None, ok=True, error=None)


def _request(**meta):
    return {
        "url": "https://www.bing.com/search?q=x", "device": "desktop", "proxy_type": "none",
        "extract": {"type": "css", "fields": {"titles": {"selector": "li.b_algo h2 a", "all": True}}},
        "preset_meta": {"name": "bing_search_chromium", "source": "bing", "locale": "ru", "version": 4, **meta},
    }


@pytest.mark.asyncio
async def test_the_queue_emits_the_warning_for_the_query_the_materializer_echoed():
    """The wiring: the query reaches the worker as `preset_meta.query` and the
    verdict lands in the warnings the caller receives. Removing the call site
    leaves the helper tests green."""
    out = await run_scrape(_Runner(), "req_relevance", _request(query=Q), None)
    assert isinstance(out, ScrapeOk), out
    assert any(w.startswith("serp_query_mismatch:") for w in out.result.warnings), out.result.warnings


@pytest.mark.asyncio
async def test_the_same_page_from_a_preset_without_a_query_is_never_judged():
    out = await run_scrape(_Runner(), "req_noquery", _request(), None)
    assert isinstance(out, ScrapeOk), out
    assert not any("serp_query_mismatch" in w for w in out.result.warnings), out.result.warnings
