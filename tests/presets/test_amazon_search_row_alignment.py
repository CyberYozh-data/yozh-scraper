"""Guards for the amazon_search row-alignment fix.

extract_fields matches each field's selector against the whole document
independently and returns flat parallel arrays; consumers zip them by index.
Any field matching a different number of nodes per card misaligns the
response — no exception, just wrong data lined up under the wrong product.
The `row_alignment_mismatch` guard catches the subset where the lengths end
up unequal; compensating errors within one field still slip through.

The invariant that makes null-in-slot work is *container presence*, not
selector optionality: every field anchors to a per-card container Amazon
renders on every card, then digs the value out of that container's html. A
selector that matches nothing does NOT yield null — it shrinks the array
(pinned below in `test_missing_price_recipe_container_shrinks_the_array`).

pydantic's default `extra="ignore"` means a typo'd JSON key (e.g.
`post_proccess`, `atr`) vanishes silently instead of failing validation, so
these tests don't just check the preset parses — they pin the exact
selector/attr/post_process configuration on the loaded `FieldRule` objects,
and exercise `extract_fields` against card fixtures that mirror real Amazon
markup (verified against live amazon.co.uk / amazon.de captures 2026-07-17).
"""
from __future__ import annotations

import json
import pathlib
from html import escape as html_escape

from src.extract.extractor import extract_fields
from src.extract.models import ExtractRule, FieldRule
from src.presets.materializer import PresetScrapeRequest, materialize
from src.presets.models import Preset
from src.presets.store import DEFAULT_BUILTIN_DIR


def _load(name: str) -> Preset:
    path = DEFAULT_BUILTIN_DIR / f"{name}.json"
    return Preset(**json.loads(path.read_text(encoding="utf-8")))


def _ops(rule) -> list[tuple[str, list]]:
    return [(step.op, step.args) for step in rule.post_process]


def _price(text: str) -> str:
    """Amazon's real price markup: the authoritative string lives in the
    screen-reader-only `.a-offscreen` node, mirrored by an aria-hidden
    presentational copy split across symbol/whole/fraction spans."""
    return (
        '<span class="a-price" data-a-size="xl" data-a-color="base">'
        f'<span class="a-offscreen">{text}</span>'
        f'<span aria-hidden="true">{text}</span>'
        "</span>"
    )


def _amazon_card(*, asin: str, title: str, price_html: str = "", rating_html: str = "") -> str:
    """Mirror of the real card shape: the h2 lives *inside* the title-recipe
    anchor (verified on live amazon.co.uk/.de — 60/60 and 22/22 cards)."""
    return f"""
    <div data-component-type="s-search-result" data-asin="{asin}">
      <div data-cy="title-recipe" class="a-section s-title-instructions-style">
        <a class="a-link-normal s-line-clamp-4" href="/dp/{asin}/ref=sr_1">
          <h2 aria-label="{title}" class="a-size-base-plus"><span>{title}</span></h2>
        </a>
      </div>
      <div data-cy="price-recipe">{price_html}</div>
      <div data-cy="asin-faceout-container">{rating_html}</div>
    </div>
    """


def _amazon_page(cards: list[str]) -> str:
    return f"<html><body><div class='s-main-slot'>{''.join(cards)}</div></body></html>"


class TestAmazonSearchSelectors:
    def setup_method(self):
        self.preset = _load("amazon_search_chromium")
        self.fields = self.preset.parsing_instructions.fields

    def test_titles_anchored_to_title_recipe_container(self):
        # The h2 is the only node in the title-recipe guaranteed to exist:
        # 104/104 live cards carry exactly one (see TestAmazonSearchTitleAnchoring
        # for the capture breakdown). `h2 span` happens to match 1:1 on today's
        # markup too, but a title rendered as bare text inside the h2 would match
        # nothing, shrink the array and misalign every later row — titles is the
        # reference array the other three columns are zipped against, so it is
        # the one field that must never lose a slot.
        rule = self.fields["titles"]
        assert rule.selector == (
            "div[data-component-type='s-search-result'] [data-cy='title-recipe'] h2"
        )
        assert rule.required is True

    def test_titles_read_text_not_aria_label(self):
        # attr MUST stay `text`. aria-label looks like a cleaner source, but
        # Amazon prefixes it with a localized ad marker that never appears in
        # the visible text ("Sponsored Ad – " / "Gesponserte Anzeige – ") on
        # 24 of 104 live cards — 23% of every title silently corrupted.
        # Pinned functionally in TestAmazonSearchTitleAnchoring.
        assert self.fields["titles"].attr == "text"

    def test_urls_anchored_to_title_recipe_anchor(self):
        rule = self.fields["urls"]
        assert rule.selector == (
            "div[data-component-type='s-search-result'] [data-cy='title-recipe'] > a[href]"
        )
        assert rule.attr == "href"
        assert rule.all is True

    def test_urls_unwraps_the_sponsored_redirect_then_resolves_absolute(self):
        # Fix round 1: unwrap_param recovers the real destination Amazon
        # embeds inline in a sponsored /sspa/click redirect's own 
        # query param (passing any other value through unchanged -- which is
        # what lets one pipeline handle organic and sponsored hrefs
        # together), THEN urljoin resolves whatever comes out of that
        # absolute. Round 0 nulled the sponsored row's url instead, which
        # closed the relative-url defect but discarded the destination.
        assert _ops(self.fields["urls"]) == [
            ("unwrap_param", ["url"]),
            ("urljoin", []),
        ]

    def test_sponsored_marker_shares_the_urls_selector_and_length(self):
        # Same selector/attr/all as urls, so the marker is always exactly as
        # long as urls -- it can never itself desync the row-alignment guard
        # (see test_preset_engine_variants -> its length always matches urls
        # because both are the SAME node set, only the post_process differs).
        rule = self.fields["sponsored"]
        assert rule.selector == self.fields["urls"].selector
        assert rule.attr == "href"
        assert rule.all is True
        # Fix round 2 (finding 5): an always-matching alternation, not a
        # plain "(sspa/click)" -- the pattern always finds something (the
        # literal text, or a zero-width empty match), so the field never
        # legitimately nulls and reads truthy ("sspa/click") vs empty ("")
        # rather than value vs null. See TestAmazonSearchSponsoredMarkerOp.
        # `(?s)` is load-bearing, not decoration: `.` does not cross a
        # newline by default, so an href carrying one BEFORE the wrapper
        # makes the negative-lookahead branch succeed at position 0 and the
        # marker report "organic" while `unwrap_param` (whose own pattern is
        # not `.`-based) still unwraps the row -- the two fields disagreeing,
        # which this preset's description says cannot happen. See
        # test_a_newline_before_the_wrapper_still_marks_the_row_sponsored.
        assert _ops(rule) == [("regex", ["(?s)sspa/click|(?!.*sspa/click)"])]

    def test_output_schema_item_types_are_honest_about_which_fields_null(self):
        """Fix-round-2 finding 3, now guarded (fix-round-3 finding 6):
        reverting `prices`/`ratings` back to a bare `{"type": "number"}` --
        losing the `null` this preset's own row-alignment invariant
        documents as by-design (measured: 5/176 null prices, 15/176 null
        ratings in the 2026-08-27 audit) -- left the suite fully green
        before this test existed. `output_schema` is handed to the self-heal
        LLM as the target contract (parser_pipeline.run -> generate_selectors),
        so a false non-nullable claim steers a heal toward exactly the
        per-row selector shape this preset forbids. `urls`/`titles` stay
        plain `string` (they no longer null by design at all); `sponsored`
        is `string | null` since 2026-09-05 -- its regex has always matched
        on the pages measured (fix-round-2 finding 5), but a regex CAN null a
        row, and the schema now says so for every such column (see
        test_recipe_nullable_columns_declare_null)."""
        props = self.preset.output_schema["properties"]
        assert props["prices"]["items"]["type"] == ["number", "null"]
        assert props["ratings"]["items"]["type"] == ["number", "null"]
        assert props["urls"]["items"]["type"] == "string"
        assert props["titles"]["items"]["type"] == "string"
        assert props["sponsored"]["items"]["type"] == ["string", "null"]

    def test_prices_anchored_to_container_but_read_from_a_offscreen(self):
        # The container anchor is what guarantees the slot; the a-offscreen
        # anchor inside it is what guarantees the *value*. Reading the
        # container's text and taking the first number instead lets a deal
        # countdown ("Ends in 12:25:11") or a "20% off" badge win.
        rule = self.fields["prices"]
        assert rule.selector == (
            "div[data-component-type='s-search-result'] [data-cy='price-recipe']"
        )
        assert rule.attr == "html"
        assert rule.all is True
        assert _ops(rule) == [
            ("regex", ["a-offscreen[^>]*>[^<]*?(\\d[\\d.,]*\\d|\\d)"]),
            ("parse_price", []),
        ]

    def test_prices_locale_left_empty_for_materializer(self):
        # MUST stay empty: the materializer injects the market's tag per
        # request and a self-heal strips it back (strip_materializer_injected);
        # the value no longer changes any parse, the separator is read from
        # the text.
        assert self.fields["prices"].post_process[-1].args == []

    def test_ratings_anchored_to_faceout_container_via_html(self):
        rule = self.fields["ratings"]
        assert rule.selector == (
            "div[data-component-type='s-search-result'] [data-cy='asin-faceout-container']"
        )
        assert rule.attr == "html"
        assert rule.all is True
        assert _ops(rule) == [
            ("regex", ["a-icon-alt[^>]*>(?:\\s|<[a-zA-Z][^>]*>)*(\\d[.,]\\d)"]),
            ("parse_float", []),
        ]

    def test_ratings_regex_is_bounded_not_greedy(self):
        # A greedy [\d.,]+ would glue adjacent digits ("4.7" + "4.7" ->
        # 4747.0). The shipped pattern captures exactly one digit either
        # side of the separator.
        pattern = self.fields["ratings"].post_process[0].args[0]
        assert pattern.endswith("(\\d[.,]\\d)")
        assert "[\\d.,]+" not in pattern

    def test_ratings_keeps_parse_float_not_parse_price(self):
        # parse_float reads "4,7" as 4.7 since 2026-09-05 (a one-digit tail is
        # a decimal in every locale), so the replace ,->. step that used to fix
        # the amazon.de all-4.0 bug is gone; parse_price would re-introduce
        # the labelled-price parsing this field does not need.
        ops = _ops(self.fields["ratings"])
        assert ("parse_float", []) in ops
        assert not any(op == "parse_price" for op, _ in ops)

    def test_version_bumped(self):
        assert self.preset.version >= 2

    def test_self_heal_still_enabled(self):
        assert self.preset.self_heal is True


class TestAmazonSearchExtraction:
    """Functional check: arrays stay one-slot-per-card even when a card is
    missing its rating or price, instead of shrinking and misaligning."""

    def setup_method(self):
        self.preset = _load("amazon_search_chromium")

    def test_missing_rating_and_price_do_not_shrink_arrays(self):
        html = _amazon_page(
            [
                _amazon_card(
                    asin="A1",
                    title="Widget One",
                    price_html=_price("$19.99"),
                    rating_html='<span class="a-icon-alt">4.5 out of 5 stars</span>',
                ),
                # unrated product: faceout container present, no icon-alt span
                _amazon_card(asin="A2", title="Widget Two", price_html=_price("$9.99")),
                # out-of-stock: price container present but empty
                _amazon_card(
                    asin="A3",
                    title="Widget Three",
                    rating_html='<span class="a-icon-alt">3.0 out of 5 stars</span>',
                ),
                _amazon_card(
                    asin="A4",
                    title="Widget Four",
                    price_html=_price("$5.49"),
                    rating_html='<span class="a-icon-alt">5.0 out of 5 stars</span>',
                ),
            ]
        )
        data, warnings = extract_fields(html, self.preset.parsing_instructions)

        assert len(data["titles"]) == 4
        assert len(data["urls"]) == 4
        assert len(data["prices"]) == 4
        assert len(data["ratings"]) == 4
        assert len(data["sponsored"]) == 4

        assert data["titles"] == ["Widget One", "Widget Two", "Widget Three", "Widget Four"]
        # None of these hrefs carry a sponsored "url=" wrapper, so
        # unwrap_param passes them through unchanged -- unmaterialized here,
        # so urljoin has no base and leaves them relative too.
        assert data["urls"] == [
            "/dp/A1/ref=sr_1",
            "/dp/A2/ref=sr_1",
            "/dp/A3/ref=sr_1",
            "/dp/A4/ref=sr_1",
        ]
        assert data["prices"] == [19.99, 9.99, None, 5.49]
        assert data["ratings"] == [4.5, None, 3.0, 5.0]
        # No sponsored rows on this page at all -- an empty string (truthy
        # vs empty), not null (fix round 2, finding 5), so this column never
        # trips the silent-null guard regardless of row count.
        assert data["sponsored"] == ["", "", "", ""]
        # One DOCUMENTED warning, not a regression of the behaviour this test
        # actually pins (missing rating/price don't shrink arrays): urljoin
        # has no base, since this call bypasses materialize(), the only
        # thing that ever fills it in (see TestUrlBaseInjection in
        # test_materializer.py).
        assert len(warnings) == 1
        assert "urljoin" in warnings[0]

    def test_missing_price_recipe_container_shrinks_the_array(self):
        """Pins the invariant the whole fix depends on.

        Null-in-slot is NOT a property of the selector being optional — a
        selector that matches nothing shrinks the array and misaligns every
        later row. It only works because the anchored container is present
        on every card. If this test ever starts failing because the arrays
        came back equal, extract_fields grew per-row alignment and the
        container-anchor requirement can be relaxed; until then, never point
        a field at an element that exists only when its value does.

        The shrink is no longer silent: the `row_alignment_mismatch` guard
        sees the unequal lengths and warns. That warning is the safety net,
        not the fix — it reports the corruption rather than preventing it.
        """
        html = _amazon_page(
            [
                _amazon_card(asin="A1", title="Has price", price_html=_price("$19.99")),
                # card WITHOUT a price-recipe container at all
                """
                <div data-component-type="s-search-result" data-asin="A2">
                  <div data-cy="title-recipe">
                    <a href="/dp/A2/ref=sr_1"><h2><span>No price container</span></h2></a>
                  </div>
                  <div data-cy="asin-faceout-container"></div>
                </div>
                """,
            ]
        )
        data, warnings = extract_fields(html, self.preset.parsing_instructions)

        assert len(data["titles"]) == 2
        assert len(data["urls"]) == 2
        # The array SHRANK — this is the misalignment failure mode.
        assert len(data["prices"]) == 1
        assert data["prices"] == [19.99]
        # Wrong, but not silent: the length guard flags the shrunk field.
        # Plus the same documented warning as the test above (no
        # materialize() -> urljoin has no base) -- unrelated to the shrink
        # this test actually pins. `sponsored` contributes nothing here: an
        # empty-string marker on both (non-sponsored) rows never trips the
        # silent-null guard (fix round 2, finding 5).
        assert data["sponsored"] == ["", ""]
        assert len(warnings) == 2
        assert any(w.startswith("row_alignment_mismatch:") for w in warnings)
        assert any("urljoin" in w for w in warnings)
        alignment_warning = next(w for w in warnings if w.startswith("row_alignment_mismatch:"))
        assert "'prices'=1" in alignment_warning


class TestAmazonSearchPriceValueTraps:
    """The container anchor guarantees the slot; these pin that it did not
    cost us the *value*. Reading the container's text and taking the first
    number is what the old `(\\d[\\d.,]*)` regex did."""

    def setup_method(self):
        self.preset = _load("amazon_search_chromium")

    def _prices(self, price_html: str):
        html = _amazon_page([_amazon_card(asin="A1", title="T", price_html=price_html)])
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        return data["prices"]

    def test_numeric_badge_before_price_does_not_win(self):
        # "20% off" badge adjacent to the price -> first-number-wins took 20.
        assert self._prices('<span>20% off</span>' + _price("$19.99")) == [19.99]

    def test_deal_countdown_timer_does_not_win(self):
        # Verbatim shape from a live amazon.co.uk capture (2026-07-17): 2 of
        # 60 cards carried a deal badge whose countdown clock made the old
        # text regex return 12.0 instead of £10.99 / £25.49, silently.
        deal = (
            '<div class="a-row"><a href="/deals"><span class="a-badge" data-a-badge-type="deal">'
            '<span class="a-badge-label"><span class="a-badge-label-inner">'
            '<span class="a-badge-text">Ends in </span>'
            '<span class="dealBadge-countdown-timer" data-target-time="2026-07-17T22:59:59Z">'
            "12:25:11</span></span></span></span></a></div>"
            '<div class="a-row">'
            '<span id="price-link" class="aok-offscreen">Price, product page</span>'
            '<a href="/dp/A1">' + _price("£10.99") + "</a></div>"
        )
        assert self._prices(deal) == [10.99]

    def test_trailing_separator_does_not_multiply_by_100(self):
        # "$19.99." -> parse_price sees two dots, treats both as thousands
        # separators -> 1999.0. The regex must hand it a digit-terminated
        # number.
        assert self._prices(_price("$19.99.")) == [19.99]

    def test_current_price_wins_over_strikethrough_list_price(self):
        # 36 of 60 live UK cards carry 3 a-offscreen nodes (price, "RRP: £x",
        # £x). Document order puts the current price first.
        both = _price("£10.99") + (
            '<span class="a-price a-text-price" data-a-strike="true">'
            '<span class="a-offscreen">RRP: £14.99</span></span>'
        )
        assert self._prices(both) == [10.99]

    def test_empty_price_container_yields_null_not_a_stray_number(self):
        assert self._prices("") == [None]


class TestAmazonSearchRatingMarkupShapes:
    """`attr: html` serialises whatever lxml parsed, so the rating regex must
    not assume minified, un-nested markup."""

    def setup_method(self):
        self.preset = _load("amazon_search_chromium")

    def _ratings(self, rating_html: str):
        html = _amazon_page([_amazon_card(asin="A1", title="T", rating_html=rating_html)])
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        return data["ratings"]

    def test_minified_markup(self):
        assert self._ratings('<span class="a-icon-alt">4.5 out of 5 stars</span>') == [4.5]

    def test_pretty_printed_markup(self):
        # Whitespace between the tag close and the digits used to yield None
        # for the whole column — and ratings isn't `required`, so
        # _missing_required never fires and self-heal never engages.
        assert self._ratings('<span class="a-icon-alt">\n  4.5 out of 5 stars</span>') == [4.5]

    def test_nested_inline_markup(self):
        assert self._ratings(
            '<span class="a-icon-alt"><span>4.5 out of 5 stars</span></span>'
        ) == [4.5]

    def test_pretty_printed_and_nested_together(self):
        assert self._ratings(
            '<span class="a-icon-alt">\n  <span>\n    4.5 out of 5 stars\n  </span>\n</span>'
        ) == [4.5]

    def test_de_comma_rating_parses_correctly_not_truncated_to_4(self):
        # Pre-existing bug: regex "([\d.]+)" captured just "4" from
        # "4,9 von 5 Sternen" -> every DE rating came back 4.0.
        assert self._ratings('<span class="a-icon-alt">4,9 von 5 Sternen</span>') == [4.9]

    def test_non_rating_icon_alt_badge_is_skipped_not_fatal(self):
        # DELIBERATE, not accidental: re.search retries the pattern at the
        # next "a-icon-alt" occurrence, so a Prime/badge icon-alt without a
        # rating in it does not swallow the card's real rating. Do not
        # "simplify" this into a first-occurrence-only match.
        assert self._ratings(
            '<span class="a-icon-alt">Amazon Prime</span>'
            '<span class="a-icon-alt">4.2 out of 5 stars</span>'
        ) == [4.2]

    def test_closing_tag_does_not_leak_into_neighbouring_number(self):
        # The tag-skip must only cross *opening* tags. If it could cross
        # "</span>" an empty icon-alt would grab the adjacent price digits.
        assert self._ratings(
            '<span class="a-icon-alt"></span><span class="a-price">4.99</span>'
        ) == [None]

    def test_duplicate_accessibility_text_does_not_glue_digits(self):
        html = _amazon_page(
            [
                _amazon_card(
                    asin="C1",
                    title="Gadget",
                    price_html=_price("$1,299.99"),
                    rating_html=(
                        '<span class="a-icon-alt">4.7 out of 5 stars</span>'
                        '<span aria-hidden="true">4.7 out of 5 stars</span>'
                    ),
                )
            ]
        )
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        assert data["ratings"] == [4.7]
        # thousands-separator price also parses cleanly under the default
        # (us) locale used when calling extract_fields directly.
        assert data["prices"] == [1299.99]


class TestAmazonSearchTitleAnchoring:
    """Why `titles` reads the h2's text, and what that does and does not cost.

    Measured 2026-07-17 over three live captures — amazon.co.uk ?k=coffee+maker
    (60 cards), amazon.de ?k=kaffee (22), amazon.co.uk ?k=laptop+backpack... (22,
    titles up to 200 chars) — 104 cards total:

      * 104/104 carry exactly one h2 under [data-cy='title-recipe'];
      * 104/104 hold exactly one span inside that h2;
      * 104/104 carry an aria-label on the h2;
      *   0/104 use Amazon's a-truncate widget inside the h2 — titles are
          clamped with CSS (`a-link-normal s-line-clamp-4`), not by JS.

    So `attr: text` over the h2 returns each title exactly once on real cards.
    The two seemingly-cleaner alternatives are both measurably worse, and are
    pinned below so they are not "fixed" back in:

      * aria-label carries a localized ad prefix on 24/104 cards (23%);
      * `.a-truncate-full` is absent from every live title h2, so reading it
        would return None for 100% of cards.
    """

    def setup_method(self):
        self.preset = _load("amazon_search_chromium")

    def _titles(self, h2_inner: str):
        html = _amazon_page(
            [
                f"""
                <div data-component-type="s-search-result" data-asin="A1">
                  <div data-cy="title-recipe">
                    <a href="/dp/A1/ref=sr_1">{h2_inner}</a>
                  </div>
                  <div data-cy="price-recipe">
                    <span class="a-price"><span class="a-offscreen">$1.00</span></span>
                  </div>
                  <div data-cy="asin-faceout-container">
                    <span class="a-icon-alt">4.0 out of 5 stars</span>
                  </div>
                </div>
                """
            ]
        )
        data, warnings = extract_fields(html, self.preset.parsing_instructions)
        for key in ("titles", "urls", "prices", "ratings", "sponsored"):
            assert len(data[key]) == 1, key
        # One DOCUMENTED warning unrelated to title anchoring (what this
        # helper actually tests): unmaterialized -> urljoin has no base.
        # `sponsored` never contributes a warning at all (fix round 2): its
        # always-matching regex yields "" for this non-sponsored card, never
        # null, so there is nothing for the silent-null guard to see.
        assert len(warnings) == 1
        assert "urljoin" in warnings[0]
        assert data["sponsored"] == [""]
        return data["titles"]

    def test_live_card_shape_reads_the_title_exactly_once(self):
        # The shape 104/104 live cards actually have.
        assert self._titles(
            '<h2 aria-label="Real Title" class="a-size-base-plus"><span>Real Title</span></h2>'
        ) == ["Real Title"]

    def test_sponsored_ad_prefix_stays_out_of_the_title(self):
        """The reason `titles` reads text and not the h2's aria-label.

        Verbatim live shape: on sponsored cards Amazon prepends a localized
        "Sponsored Ad – " (co.uk) / "Gesponserte Anzeige – " (de) to aria-label
        only — the visible text is clean. 24 of 104 live cards are sponsored,
        so attr='aria-label' would ship an ad marker glued to 23% of titles,
        in a different language per domain.
        """
        assert self._titles(
            '<h2 aria-label="Sponsored Ad – Breville Barista Max Espresso Machine" '
            'class="a-size-base-plus"><span>Breville Barista Max Espresso Machine</span></h2>'
        ) == ["Breville Barista Max Espresso Machine"]

    def test_truncate_wrapper_as_live_renders_it_does_not_double(self):
        """Amazon's a-truncate widget, copied verbatim from the live captures.

        It appears 77 times across the three captures (sponsored brand banners
        and carousels) — never inside a search-result card, but this pins what
        `attr: text` would do if it moved there. In the served HTML
        `.a-truncate-cut` is always present-but-EMPTY and carries `a-hidden`
        (77/77 wrappers), so it contributes no text and the title reads once.
        """
        assert self._titles(
            '<h2 aria-label="Real Title"><span class="a-truncate" '
            'data-a-max-rows="2" data-a-overflow-marker="&hellip;">'
            '<span class="a-truncate-full">Real Title</span>'
            '<span class="a-truncate-cut a-hidden" aria-hidden="true"></span>'
            "</span></h2>"
        ) == ["Real Title"]

    def test_activated_truncate_widget_would_double_the_title(self):
        """KNOWN BOUNDARY — documented, not currently reachable.

        If Amazon's a-truncate JS ever runs against a title h2, it moves the
        full text offscreen and writes a visible cut copy into
        `.a-truncate-cut`. Both halves then carry the string and `text_content`
        concatenates them, so the title comes back doubled — silently, with no
        warning, because the h2 still matches exactly once. Amazon serves the
        wrapper minified (no whitespace between the two spans, verbatim above),
        so the copies are glued rather than spaced: the seam word "TitleReal"
        below is what a consumer would actually receive.

        This needs TWO independent changes to bite: Amazon must switch titles
        from CSS line-clamp to the JS widget (0/104 cards today), AND the
        widget must run in our render (0/77 wrappers are activated in the HTML
        we fetch at wait_until='domcontentloaded'). Note the damage is confined
        to the row's own slot — alignment holds — which is why this is pinned
        as a boundary rather than worked around: every available workaround
        (aria-label, `.a-truncate-full`, an `h2 > span` union) trades this
        unobserved value bug for a *structural* one on live cards. If this test
        ever starts failing because the title came back once, the widget shape
        changed and this guard can go.
        """
        assert self._titles(
            "<h2><span class=\"a-truncate\">"
            '<span class="a-truncate-full a-offscreen">Real Title</span>'
            '<span class="a-truncate-cut" aria-hidden="true">Real Title</span>'
            "</span></h2>"
        ) == ["Real TitleReal Title"]


class TestAmazonSearchUrlsAbsoluteAndSponsoredMarked:
    """MEASURED 2026-08-27: five successful audit runs returned 76/76 relative
    `urls` (`/dp/ASIN/ref=...`), and 6 of 22 rows in three of five runs were a
    `/sspa/click?...&url=%2F...` sponsored redirect sitting in the SAME
    `div[data-component-type='s-search-result']` container as organic rows —
    indistinguishable except by that href shape.

    FIXED 2026-08-28 (round 0): `urljoin` resolved organic urls absolute and
    `null_if_regex` nulled the sponsored row's url in place. Closed the
    relative-url defect, but only HALF the sponsored one: deleting the url
    identified a sponsored row without recovering its destination, so the row
    still carried its title/price into the organic list unlabelled, and its
    None was indistinguishable from a drifted selector.

    FIX ROUND 1 2026-08-28 (review finding 2): Amazon's own sponsored
    redirect carries the real destination INLINE —
    `/sspa/click?...&url=%2FAcer-Aspire-Laptop-Ryzen-Netzteil%2Fdp%2F...` —
    so `urls` now UNWRAPS it (percent-decodes the `url` query param via the
    new `unwrap_param` op; any value without that param passes through
    UNCHANGED, which is what lets one pipeline handle organic and sponsored
    hrefs together) and resolves the recovered path absolute the same way as
    every organic url. A parallel `sponsored` field — same selector, same
    length — carries the marker `"sspa/click"` exactly on the rows that were
    unwrapped, so a caller can still filter organic-only. All 48 real
    sponsored hrefs from the 2026-08-27 audit are checked against the actual
    shipped pipeline in TestAmazonSearchSponsoredUnwrapAgainstRealAuditCaptures
    below.

    FIX ROUND 2 2026-08-28 (review finding 5): `sponsored` originally read
    `None` for an organic row -- byte-identical to what a caller would see if
    Amazon renamed `sspa/click` and the marker died outright, and a false
    `_warn_on_silent_nulls` trip on a genuinely ad-free page (every row
    legitimately null is indistinguishable from "the pipeline broke"). The
    regex is now an always-matching alternation
    (`sspa/click|(?!.*sspa/click)`): the literal text on a sponsored row, an
    empty string `""` on an organic one -- never `None` -- so the field reads
    truthy-vs-empty and never trips the silent-null guard regardless of how
    many rows are (or aren't) sponsored.

    LIVE-VERIFIED 2026-08-28 against real amazon.de/amazon.co.uk captures:
    amazon.de sometimes serves results under an English-UI `/-/en/` path
    prefix, putting that prefix in front of the sponsored redirect too
    (`/-/en/sspa/click?...`) — round 0's anchored `^/sspa/click` pattern
    MISSED every sponsored row under this prefix (see
    test_sponsored_href_under_a_locale_path_prefix_still_unwraps_and_marks);
    neither `unwrap_param` (matches `url=` anywhere) nor the `sponsored`
    marker's regex (matches `sspa/click` anywhere) depend on where in the
    path the wrapper sits, so this shape needs no special-casing this time.
    """

    # Verbatim shapes from the audit record (research/preset_audit... 2026-08-27).
    ORGANIC_HREF = (
        "/HP-Display-Graphics-Microsoft-enthalten/dp/B0F8L98RLY/ref=sr_1_3"
        "?dib=eyJ2IjoiMSJ9"
    )
    SPONSORED_HREF = (
        "/sspa/click?ie=UTF8&spc=MTo3Nzk4MTkzMTM5MDgyNDAyOjE3ODc4MjU1NTY6c3BfYXRm"
        "&url=%2FAcer-Aspire-Laptop-Ryzen-Netzteil%2Fdp%2FB0EXAMPLE1"
    )
    # The real destination Amazon embeds in SPONSORED_HREF's `url=` param,
    # percent-decoded -- what unwrap_param must recover.
    SPONSORED_UNWRAPPED = "/Acer-Aspire-Laptop-Ryzen-Netzteil/dp/B0EXAMPLE1"

    def _card(self, *, asin: str, title: str, href: str) -> str:
        return f"""
        <div data-component-type="s-search-result" data-asin="{asin}">
          <div data-cy="title-recipe" class="a-section s-title-instructions-style">
            <a class="a-link-normal s-line-clamp-4" href="{href}">
              <h2 aria-label="{title}" class="a-size-base-plus"><span>{title}</span></h2>
            </a>
          </div>
          <div data-cy="price-recipe">{_price("$19.99")}</div>
          <div data-cy="asin-faceout-container"></div>
        </div>
        """

    def _page(self) -> str:
        return _amazon_page(
            [
                self._card(asin="ORG1", title="HP Laptop", href=self.ORGANIC_HREF),
                self._card(
                    asin="SPON1", title="Acer Laptop Sponsored", href=self.SPONSORED_HREF
                ),
            ]
        )

    def test_materialized_organic_and_sponsored_urls_are_both_absolute(self):
        preset = _load("amazon_search_chromium")
        scrape = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_search_chromium",
                preset_params={"query": "laptop"},
                locale="de",
            ),
        )

        data, warnings = extract_fields(self._page(), scrape.extract)

        assert (
            len(data["titles"]) == len(data["urls"]) == len(data["prices"])
            == len(data["sponsored"]) == 2
        )

        assert data["urls"][0] == "https://www.amazon.de" + self.ORGANIC_HREF
        # The sponsored row now carries a REAL, followable destination --
        # not None -- recovered from its own wrapper.
        assert data["urls"][1] == "https://www.amazon.de" + self.SPONSORED_UNWRAPPED
        assert all(u.startswith("https://www.amazon.de/") for u in data["urls"])
        assert not any("/sspa/click" in u for u in data["urls"])

        # Marked, not dropped: sponsored[1] is non-null exactly where the
        # unwrap happened; sponsored[0] (organic) is null.
        assert data["sponsored"] == ["", "sspa/click"]

        # Every OTHER field is untouched -- titles[1]/prices[1] still
        # genuinely describe row 2, same as always; only urls/sponsored
        # differ from a plain organic row.
        assert data["titles"] == ["HP Laptop", "Acer Laptop Sponsored"]
        assert data["prices"] == [19.99, 19.99]
        assert warnings == []

    def test_a_newline_before_the_wrapper_still_marks_the_row_sponsored(self):
        """Final-review finding: `.` does not cross a newline. Without `(?s)`
        the alternation's second branch, `(?!.*sspa/click)`, succeeds at
        position 0 of an href whose wrapper sits after a newline -- because
        `.*` cannot reach the wrapper to refute it -- so `sponsored` reports
        `""` (organic) on a row `unwrap_param` DID unwrap, since unwrap's own
        `(?:^|[?&])url=` pattern uses no `.` and is unaffected. The two fields
        then disagree, which this preset's description states cannot happen.
        HTML attribute values may legally contain newlines, and the marker is
        the only thing telling a caller a row is an ad."""
        href = "\n" + self.SPONSORED_HREF
        preset = _load("amazon_search_chromium")
        scrape = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_search_chromium",
                preset_params={"query": "laptop"},
                locale="de",
            ),
        )
        page = _amazon_page(
            [self._card(asin="SPON1", title="Acer Laptop Sponsored", href=href)]
        )
        data, _ = extract_fields(page, scrape.extract)

        # The row WAS unwrapped ...
        assert data["urls"] == ["https://www.amazon.de" + self.SPONSORED_UNWRAPPED]
        # ... so the marker must say so too.
        assert data["sponsored"] == ["sspa/click"]

    def test_uk_locale_resolves_against_co_uk_not_com(self):
        preset = _load("amazon_search_chromium")
        scrape = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_search_chromium",
                preset_params={"query": "laptop"},
                locale="uk",
            ),
        )
        data, _ = extract_fields(self._page(), scrape.extract)
        assert data["urls"] == [
            "https://www.amazon.co.uk" + self.ORGANIC_HREF,
            "https://www.amazon.co.uk" + self.SPONSORED_UNWRAPPED,
        ]

    def test_camoufox_twin_behaves_identically(self):
        preset = _load("amazon_search_camoufox")
        scrape = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_search_camoufox",
                preset_params={"query": "laptop"},
                locale="de",
            ),
        )
        data, _ = extract_fields(self._page(), scrape.extract)
        assert data["urls"] == [
            "https://www.amazon.de" + self.ORGANIC_HREF,
            "https://www.amazon.de" + self.SPONSORED_UNWRAPPED,
        ]
        assert data["sponsored"] == ["", "sspa/click"]

    def test_unmaterialized_extraction_recovers_the_url_but_urljoin_warns(self):
        """`unwrap_param` needs no base at all -- unlike `urljoin`, calling
        extract_fields directly (bypassing materialize()) still recovers the
        real RELATIVE destination for a sponsored row. `urljoin` itself has
        no base to resolve against here, so (fix-round-1 finding 4) it now
        WARNS rather than silently staying relative -- unlike parse_price's
        "us" default, there is no sensible default transform for a URL with
        no base, so silence would be a "succeeded but useless" result with
        nothing telling the caller why urls came back relative."""
        preset = _load("amazon_search_chromium")
        data, warnings = extract_fields(self._page(), preset.parsing_instructions)
        assert data["urls"][0] == self.ORGANIC_HREF
        assert data["urls"][1] == self.SPONSORED_UNWRAPPED
        assert data["sponsored"] == ["", "sspa/click"]
        # Exactly one warning: urljoin has no base. The sponsored marker does
        # NOT warn here (unlike the single-card fixtures elsewhere in this
        # file) because this page has one sponsored AND one organic row, so
        # the column is not 100% null.
        assert len(warnings) == 1
        assert "urljoin" in warnings[0]

    def test_sponsored_href_under_a_locale_path_prefix_still_unwraps_and_marks(self):
        """Live capture 2026-08-28: amazon.de served its results under an
        English-UI `/-/en/` path prefix, so the sponsored redirect was
        `/-/en/sspa/click?...` rather than a bare `/sspa/click?...` at the
        start of the href. Round 0's anchored `^/sspa/click` pattern MISSED
        every one of the 6 sponsored rows on that page. Neither
        `unwrap_param` nor the `sponsored` marker anchor to the start of the
        href, so this shape needs no special-casing."""
        organic = "/-/en/HP-Display-Graphics-Windows-Keyboard/dp/B0F8L98RLY/ref=sr_1_3"
        sponsored = (
            "/-/en/sspa/click?ie=UTF8&spc=MToyNjY0MjYwOTcxNDY5MzM0"
            "&url=%2FAcer-Aspire-Laptop-Ryzen-Supply%2Fdp%2FB0836QR869"
        )
        page = _amazon_page(
            [
                self._card(asin="ORG2", title="HP Laptop EN-UI", href=organic),
                self._card(asin="SPON2", title="Acer Laptop Sponsored EN-UI", href=sponsored),
            ]
        )
        preset = _load("amazon_search_chromium")
        scrape = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_search_chromium", preset_params={"query": "laptop"}, locale="de"
            ),
        )

        data, warnings = extract_fields(page, scrape.extract)

        assert len(data["titles"]) == len(data["urls"]) == len(data["sponsored"]) == 2
        assert data["urls"][0] == "https://www.amazon.de" + organic
        assert (
            data["urls"][1]
            == "https://www.amazon.de/Acer-Aspire-Laptop-Ryzen-Supply/dp/B0836QR869"
        )
        assert not any("/sspa/click" in u for u in data["urls"])
        assert data["sponsored"] == ["", "sspa/click"]
        assert warnings == []


class TestAmazonSearchSponsoredUnwrapAgainstRealAuditCaptures:
    """Fix-round-1 review: verify the unwrap against the REAL hrefs recorded
    in the 2026-08-27 dual-engine audit, not a hand-written fixture. Loads
    every amazon_search run's `urls` from
    research/preset_audit_dual_engine_2026_08_27.json, filters to the ones
    that are the sponsored `/sspa/click` shape, and runs each through the
    actual shipped `urls` post_process pipeline (read from the preset file,
    not re-typed here, so a future edit to the pipeline is caught by this
    test rather than silently going unchecked).
    """

    def _real_sponsored_hrefs(self) -> list[str]:
        path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "research" / "preset_audit_dual_engine_2026_08_27.json"
        )
        runs = json.loads(path.read_text(encoding="utf-8"))
        hrefs = []
        for run in runs:
            if "amazon_search" not in run.get("preset", ""):
                continue
            for url in (run.get("data") or {}).get("urls") or []:
                if url and "sspa/click" in url:
                    hrefs.append(url)
        return hrefs

    def test_the_fixture_itself_has_the_expected_48_sponsored_hrefs(self):
        # Pins the input so a change to the audit file is noticed, rather
        # than silently shrinking what the next test actually checks.
        assert len(self._real_sponsored_hrefs()) == 48

    def test_every_real_sponsored_href_unwraps_to_a_plausible_product_path(self):
        preset = _load("amazon_search_chromium")
        ops = preset.parsing_instructions.fields["urls"].post_process
        hrefs = self._real_sponsored_hrefs()
        assert hrefs, "fixture loader returned nothing -- see the pinned-count test"

        for href in hrefs:
            html = f'<a href="{html_escape(href)}">x</a>'
            rule = ExtractRule(
                type="css",
                fields={"url": FieldRule(selector="a", attr="href", post_process=ops)},
            )
            data, _ = extract_fields(html, rule)
            out = data["url"]
            # No base injected here (this checks the UNWRAP, which needs
            # none) -- urljoin's own no-base behaviour is covered elsewhere.
            assert out is not None, href
            assert out.startswith("/"), (href, out)
            assert "/dp/" in out, (href, out)
            # The wrapper itself must be gone -- a half-unwrap that left the
            # /sspa/click prefix attached would still be useless.
            assert "sspa/click" not in out, (href, out)


class TestAmazonSearchPriceLocaleIntegration:
    """End-to-end guard that the empty parse_price args really do get the
    correct locale injected per-domain, rather than a hardcoded one."""

    def _materialized(self, locale: str):
        preset = _load("amazon_search_chromium")
        return materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_search_chromium", preset_params={"query": "kaffee"}, locale=locale
            ),
        )

    def test_de_locale_injects_eu_not_us(self):
        scrape = self._materialized("de")
        price_ops = scrape.extract.fields["prices"].post_process
        assert price_ops[-1].op == "parse_price"
        assert price_ops[-1].args == ["eu"]

    def test_de_price_parses_as_899_not_89900(self):
        scrape = self._materialized("de")
        html = _amazon_page(
            [_amazon_card(asin="D1", title="Kaffeemaschine", price_html=_price("899,00 €"))]
        )
        data, _ = extract_fields(html, scrape.extract)
        assert data["prices"] == [899.0]

    def test_de_thousands_price_parses_as_1234_56(self):
        scrape = self._materialized("de")
        html = _amazon_page(
            [_amazon_card(asin="D2", title="Vollautomat", price_html=_price("1.234,56 €"))]
        )
        data, _ = extract_fields(html, scrape.extract)
        assert data["prices"] == [1234.56]

    def test_us_locale_injects_us(self):
        scrape = self._materialized("us")
        price_ops = scrape.extract.fields["prices"].post_process
        assert price_ops[-1].args == ["us"]
