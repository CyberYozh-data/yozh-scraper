"""Guards for the ebay_search selectors after eBay's s-card relayout.

eBay renamed the SRP card markup from `su-item-card__*` (shipped by PR #58) to
`s-card__*`. The old selectors matched nothing, so every field came back empty
while the fetch itself looked healthy: HTTP 200, a 2.2 MB page, positive layout
markers present, `data == {}`. Verified on live captures 2026-07-26, fetched
through the preset itself so the URL carries its `&_sop=12` newest-first sort
the URL carries its `&_sop=12` newest-first sort — ebay.com and ebay.co.uk
?_nkw=iphone+13&_sop=12, 60 cards each;
`su-item-card__title` occurred 0 times and `s-card__title` 91 times.

extract_fields matches each selector against the whole document and returns
flat parallel arrays that consumers zip by index, so the fields must agree on
which rows they describe. Two shapes in eBay's live markup break naive
selectors and are pinned below:

  * each card carries TWO `a.s-card__link` anchors — one wrapping the image in
    `.su-card-container__media`, one wrapping the title in
    `.su-card-container__header`. An unscoped `a.s-card__link` returns 120 urls
    for 60 cards;
  * `.su-card-container__attributes` holds one `.s-card__price` per price line,
    and cards with a strikethrough original price carry two — 100 prices for
    60 cards. Anchoring on `__attributes__primary` and digging the first price
    out of its html keeps exactly one slot per card.
"""
from __future__ import annotations

import json

from src.extract.extractor import extract_fields
from src.presets.models import Preset
from src.presets.store import DEFAULT_BUILTIN_DIR

RIVER = ".srp-river-results li.s-card--horizontal"


def _load(name: str) -> Preset:
    path = DEFAULT_BUILTIN_DIR / f"{name}.json"
    return Preset(**json.loads(path.read_text(encoding="utf-8")))


def _ops(rule) -> list[tuple[str, list]]:
    return [(step.op, step.args) for step in rule.post_process]


_CLIPPED_SPAN = '<span class="clipped">Opens in a new window or tab</span>'
# Live shape, ebay.co.uk 2026-09-05, one card in sixty (`s-card--overflow`): a
# second, EMPTY styled span follows the title span. A titles selector on
# `span.su-styled-text` reads it as a 61st title and shifts every column after it.
_OVERFLOW_SPAN = '<span class="su-styled-text default"></span>'


def _title_block(title: str, trailing: str = _CLIPPED_SPAN) -> str:
    """Verbatim live shape: the visible title span is followed by a
    screen-reader-only `.clipped` span carrying "Opens in a new window or tab",
    which is why titles read the inner `span.su-styled-text` and not the
    `.s-card__title` container's text."""
    return (
        '<div role="heading" aria-level="3" class="s-card__title">'
        f'<span class="su-styled-text primary default">{title}</span>'
        f"{trailing}</div>"
    )


def _ebay_card(
    *,
    item_id: str,
    title: str,
    price_rows: str = "",
    with_media_link: bool = True,
    overflow: bool = False,
) -> str:
    media = (
        '<div class="su-card-container__media"><div class="su-image">'
        f'<a class="s-card__link image-treatment" href="https://www.ebay.com/itm/{item_id}?img=1">'
        f'<img class="s-card__image" alt="{title}">'
        "</a></div></div>"
        if with_media_link
        else ""
    )
    return f"""
    <li class="s-card s-card--horizontal" data-listingid="{item_id}">
      <div class="su-card-container su-card-container--horizontal">
        {media}
        <div class="su-card-container__content">
          <div class="su-card-container__header">
            <a class="s-card__link" target="_blank"
               href="https://www.ebay.com/itm/{item_id}?_skw=iphone+13">
              {_title_block(title, trailing=_OVERFLOW_SPAN if overflow else _CLIPPED_SPAN)}
            </a>
            <div class="s-card__subtitle-row">
              <div class="s-card__subtitle">
                <span class="su-styled-text secondary default">Pre-Owned</span>
              </div>
            </div>
            <div class="s-card__product-reviews"><div class="s-card__reviews">
              <a href="https://www.ebay.com/p/4049279857?iid={item_id}">reviews</a>
            </div></div>
          </div>
          <div class="su-card-container__attributes">
            <div class="su-card-container__attributes__primary">{price_rows}</div>
          </div>
        </div>
      </div>
    </li>
    """


def _price_row(text: str) -> str:
    return (
        '<div class="s-card__attribute-row">'
        f'<span class="su-styled-text primary bold large-1 s-card__price">{text}</span>'
        "</div>"
    )


def _ebay_page(cards: list[str]) -> str:
    return (
        "<html><body><ul class='srp-results srp-river-results'>"
        f"{''.join(cards)}</ul></body></html>"
    )


class TestEbaySearchSelectors:
    def setup_method(self):
        self.preset = _load("ebay_search_chromium")
        self.fields = self.preset.parsing_instructions.fields

    def test_titles_read_the_inner_styled_text_span(self):
        # Reading `.s-card__title` itself concatenates the clipped
        # "Opens in a new window or tab" onto every title; `.primary` because an
        # overflow card carries a second, empty styled span (2026-09-05).
        rule = self.fields["titles"]
        assert rule.selector == (
            f"{RIVER} .su-card-container__header .s-card__title > span.su-styled-text.primary"
        )
        assert rule.all is True
        assert rule.required is True

    def test_urls_scoped_to_the_header_anchor_as_a_direct_child(self):
        # `>` excludes the review anchor nested deeper in the header, and the
        # header scope excludes the media anchor — both are real live nodes.
        rule = self.fields["urls"]
        assert rule.selector == f"{RIVER} .su-card-container__header > a.s-card__link"
        assert rule.attr == "href"
        assert rule.all is True

    def test_prices_anchored_on_the_primary_attributes_container(self):
        # The container is present on every card, so a card with no price
        # yields null-in-slot instead of shrinking the array.
        rule = self.fields["prices"]
        assert rule.selector == f"{RIVER} .su-card-container__attributes__primary"
        assert rule.attr == "html"
        assert rule.all is True
        assert _ops(rule) == [
            (
                "regex",
                ['class="[^"]*s-card__price[\\s"][^>]*>(?:\\s|<[a-zA-Z][^>]*>)*([^<]+)'],
            ),
            ("strip_tags", []),
            ("strip", []),
        ]

    def test_price_regex_bounds_the_class_and_crosses_nested_tags(self):
        # Two silent-wrong-value traps, both one markup change away:
        #   * a pattern that cannot cross a nested opening tag does not fail to
        #     null — re.search retries at the NEXT s-card__price, which is the
        #     strikethrough was-price on 20/60 (US) and 14/60 (UK) live cards;
        #   * an unbounded class match lets a sibling whose class merely STARTS
        #     with s-card__price (e.g. s-card__price-note) win on document order.
        pattern = self.fields["prices"].post_process[0].args[0]
        assert pattern.startswith('class="[^"]*s-card__price[\\s"]')
        assert "(?:\\s|<[a-zA-Z][^>]*>)*" in pattern

    def test_version_bumped(self):
        assert self.preset.version >= 3

    def test_self_heal_still_enabled(self):
        assert self.preset.self_heal is True


class TestEbaySearchExtraction:
    def setup_method(self):
        self.preset = _load("ebay_search_chromium")

    def _extract(self, cards):
        return extract_fields(_ebay_page(cards), self.preset.parsing_instructions)

    def test_three_cards_yield_three_aligned_rows(self):
        data, warnings = self._extract(
            [
                _ebay_card(
                    item_id="111", title="Apple iPhone 13 Pro",
                    price_rows=_price_row("$479.99"),
                ),
                _ebay_card(
                    item_id="222", title="Apple iPhone 8", price_rows=_price_row("$94.99")
                ),
                _ebay_card(
                    item_id="333", title="Apple iPhone 14",
                    price_rows=_price_row("$339.99"),
                ),
            ]
        )
        assert data["titles"] == [
            "Apple iPhone 13 Pro",
            "Apple iPhone 8",
            "Apple iPhone 14",
        ]
        assert data["urls"] == [
            "https://www.ebay.com/itm/111?_skw=iphone+13",
            "https://www.ebay.com/itm/222?_skw=iphone+13",
            "https://www.ebay.com/itm/333?_skw=iphone+13",
        ]
        assert data["prices"] == ["$479.99", "$94.99", "$339.99"]
        assert not warnings

    def test_card_without_a_price_keeps_its_slot(self):
        data, warnings = self._extract(
            [
                _ebay_card(item_id="111", title="Has price", price_rows=_price_row("$479.99")),
                _ebay_card(item_id="222", title="No price"),
                _ebay_card(item_id="333", title="Also priced", price_rows=_price_row("$12.00")),
            ]
        )
        assert len(data["titles"]) == 3
        assert len(data["urls"]) == 3
        assert data["prices"] == ["$479.99", None, "$12.00"]
        assert not warnings

    def test_an_overflow_cards_empty_second_span_is_not_a_title(self):
        """ebay.co.uk `laptop`, 2026-09-05: 61 titles against 60 prices and
        urls, one card carrying an empty second `span.su-styled-text`. The
        title is the `primary` span; the empty one is not a row."""
        data, warnings = self._extract(
            [
                _ebay_card(item_id="111", title="First", price_rows=_price_row("$1.00")),
                _ebay_card(item_id="222", title="Overflow", price_rows=_price_row("$2.00"), overflow=True),
                _ebay_card(item_id="333", title="Third", price_rows=_price_row("$3.00")),
            ]
        )
        assert data["titles"] == ["First", "Overflow", "Third"]
        assert len(data["prices"]) == len(data["urls"]) == 3
        assert not warnings

    def test_media_anchor_does_not_double_the_urls(self):
        # The image link is a real second a.s-card__link on every live card.
        data, _ = self._extract(
            [_ebay_card(item_id="111", title="One", price_rows=_price_row("$1.00"))]
        )
        assert data["urls"] == ["https://www.ebay.com/itm/111?_skw=iphone+13"]

    def test_cards_missing_the_media_block_stay_aligned(self):
        data, warnings = self._extract(
            [
                _ebay_card(item_id="111", title="With image", price_rows=_price_row("$1.00")),
                _ebay_card(
                    item_id="222", title="No image", price_rows=_price_row("$2.00"),
                    with_media_link=False,
                ),
            ]
        )
        assert data["urls"] == [
            "https://www.ebay.com/itm/111?_skw=iphone+13",
            "https://www.ebay.com/itm/222?_skw=iphone+13",
        ]
        assert not warnings


class TestEbaySearchValueTraps:
    def setup_method(self):
        self.preset = _load("ebay_search_chromium")

    def _one(self, card: str):
        data, _ = extract_fields(_ebay_page([card]), self.preset.parsing_instructions)
        return data

    def test_clipped_screen_reader_suffix_stays_out_of_the_title(self):
        data = self._one(
            _ebay_card(item_id="111", title="Apple iPhone 13", price_rows=_price_row("$1.00"))
        )
        assert data["titles"] == ["Apple iPhone 13"]

    def test_strikethrough_original_price_does_not_win(self):
        # 40 of 100 live price nodes are a second, was-price line. Document
        # order puts the current price first, and the regex stops at it.
        data = self._one(
            _ebay_card(
                item_id="111",
                title="Discounted",
                price_rows=_price_row("$479.99") + _price_row("$599.99"),
            )
        )
        assert data["prices"] == ["$479.99"]

    def test_non_price_attribute_row_does_not_leak_into_the_price(self):
        # "or Best Offer" sits in its own attribute row without the price
        # class; reading the container's text would return it.
        best_offer = (
            '<div class="s-card__attribute-row">'
            '<span class="su-styled-text secondary large">or Best Offer</span></div>'
        )
        data = self._one(
            _ebay_card(
                item_id="111", title="Auction", price_rows=_price_row("$20.00") + best_offer
            )
        )
        assert data["prices"] == ["$20.00"]

    def test_empty_attributes_container_yields_null_not_markup(self):
        data = self._one(_ebay_card(item_id="111", title="No attrs", price_rows=""))
        assert data["prices"] == [None]


class TestEbaySearchPriceMarkupShapes:
    """`attr: html` serialises whatever lxml parsed, so the price regex must not
    assume a flat, single-price, exactly-classed node.

    The dangerous property here is that a failed match is NOT null: re.search
    retries at the next `s-card__price` occurrence, and on 20/60 (US) and 14/60
    (UK) live cards that next occurrence is the strikethrough was-price. A
    pattern that cannot cross a nested tag therefore silently reports a real
    but WRONG price, which no consumer can distinguish from a correct one.
    """

    def setup_method(self):
        self.preset = _load("ebay_search_chromium")

    def _prices(self, price_rows: str):
        html = _ebay_page([_ebay_card(item_id="111", title="T", price_rows=price_rows)])
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        return data["prices"]

    def test_nested_tag_inside_the_price_does_not_fall_through_to_the_was_price(self):
        current = (
            '<div class="s-card__attribute-row">'
            '<span class="su-styled-text s-card__price"><b>$479.99</b></span></div>'
        )
        assert self._prices(current + _price_row("$599.99")) == ["$479.99"]

    def test_pretty_printed_price_markup(self):
        current = (
            '<div class="s-card__attribute-row">'
            '<span class="su-styled-text s-card__price">\n   $479.99</span></div>'
        )
        assert self._prices(current + _price_row("$599.99")) == ["$479.99"]

    def test_prefix_class_sibling_does_not_win_on_document_order(self):
        note = (
            '<div class="s-card__attribute-row">'
            '<span class="s-card__price-note">Was: $999.00</span></div>'
        )
        assert self._prices(note + _price_row("$479.99")) == ["$479.99"]

    def test_entities_are_unescaped_like_attr_text_would_have(self):
        assert self._prices(_price_row("Tom &amp; Jerry $5")) == ["Tom & Jerry $5"]


class TestEbaySearchContainerInvariant:
    """Pins the invariant the whole design rests on, mirroring
    `test_missing_price_recipe_container_shrinks_the_array` for amazon_search.

    Null-in-slot is a property of the anchored CONTAINER being present, not of
    the field being optional. If this ever starts passing with equal lengths,
    extract_fields grew per-row alignment and the container-anchor requirement
    can be relaxed; until then, never point a field at an element that exists
    only when its value does.
    """

    def setup_method(self):
        self.preset = _load("ebay_search_chromium")

    def test_card_without_the_attributes_container_shrinks_the_array(self):
        no_attrs = """
        <li class="s-card s-card--horizontal" data-listingid="222">
          <div class="su-card-container su-card-container--horizontal">
            <div class="su-card-container__content">
              <div class="su-card-container__header">
                <a class="s-card__link" href="https://www.ebay.com/itm/222">
                  <div class="s-card__title">
                    <span class="su-styled-text primary default">No attributes</span>
                  </div>
                </a>
              </div>
            </div>
          </div>
        </li>
        """
        html = _ebay_page(
            [
                _ebay_card(
                    item_id="111", title="Has price", price_rows=_price_row("$1.00")
                ),
                no_attrs,
            ]
        )
        data, warnings = extract_fields(html, self.preset.parsing_instructions)

        assert len(data["titles"]) == 2
        assert len(data["urls"]) == 2
        assert data["prices"] == ["$1.00"]
        assert len(warnings) == 1
        assert warnings[0].startswith("row_alignment_mismatch:")


class TestEbaySearchNonItemCards:
    """Documents a deliberate behaviour change, not an oversight.

    The su-card layout filtered titles and urls through `[href*='/itm/']`, which
    excluded eBay's "Shop on eBay" dummy and /sch/ related-search suggestions.
    Re-applying that filter to one field alone would shrink that field and
    misalign every later row — the exact bug this preset was rewritten to end —
    and cssselect 1.4.0 cannot express the card-level `:has()` predicate that
    would filter all three together.

    Neither shape occurred in the 120 live cards captured 2026-07-26, so this
    pins what a consumer would receive if one appears: a fully-aligned row whose
    url is not an item link. Filter on the url downstream.
    """

    def setup_method(self):
        self.preset = _load("ebay_search_chromium")

    def test_related_search_card_is_emitted_as_an_aligned_row(self):
        suggestion = """
        <li class="s-card s-card--horizontal">
          <div class="su-card-container su-card-container--horizontal">
            <div class="su-card-container__content">
              <div class="su-card-container__header">
                <a class="s-card__link" href="https://www.ebay.com/sch/i.html?_nkw=iphone">
                  <div class="s-card__title">
                    <span class="su-styled-text primary default">Shop on eBay</span>
                  </div>
                </a>
              </div>
              <div class="su-card-container__attributes">
                <div class="su-card-container__attributes__primary"></div>
              </div>
            </div>
          </div>
        </li>
        """
        html = _ebay_page(
            [
                _ebay_card(
                    item_id="111", title="Real listing", price_rows=_price_row("$1.00")
                ),
                suggestion,
            ]
        )
        data, warnings = extract_fields(html, self.preset.parsing_instructions)

        assert data["titles"] == ["Real listing", "Shop on eBay"]
        assert data["urls"][1] == "https://www.ebay.com/sch/i.html?_nkw=iphone"
        assert data["prices"] == ["$1.00", None]
        assert not warnings
