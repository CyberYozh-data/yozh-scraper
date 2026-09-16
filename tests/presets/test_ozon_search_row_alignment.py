"""Row-alignment guards for the ozon_search selectors.

Ozon's class names are build hashes (g3p_21, ag5_12_6-a0 ...) that rotate
between releases, so the recipe anchors on the `data-widget="tileGridDesktop"`
grid, the `tile-root` card class, the `tile-clickable-element` anchor class and
the typography classes. The shapes pinned below are verbatim from the live
captures of 2026-09-06 (24 and 8 tiles):

  * every card carries TWO `a.tile-clickable-element` anchors to the same
    product -- one around the image, one around the title -- and both hrefs
    end in Ozon's per-session `?at=` attribution token;
  * prices carry U+2009 THIN SPACES as thousands separators ("55 119 ₽"),
    which parse_price reads as grouping since the same day's extractor fix;
  * a card without reviews has no rating and no review count (1 of 8 on the
    settled-early capture), and the optional values live in sibling spans of
    the same typography class the rating uses.
"""
from __future__ import annotations

import pytest

from src.extract.extractor import extract_fields
from src.presets.store import BuiltInRegistry

THIN = "\u2009"


def _tile(title: str, href: str, price: str, *, old_price: str | None = None,
          rating: str | None = None, reviews: str | None = None) -> str:
    """One `tile-root` card in the live shape: image anchor first, then the
    price block, then the title anchor, then the rating/review row."""
    price_block = f'<span class="c35_6_0-a1 tsHeadline500Medium c35_6_0-b2">{price}{THIN}₽</span>'
    if old_price is not None:
        price_block += f'<span class="c35_6_0-a1 tsBodyControl400Small c35_6_0-b">{old_price}{THIN}₽</span>'
    score = ""
    if rating is not None:
        score = (
            '<div class="g0q_21 c7w1_8_3-a"><svg></svg>'
            f'<span class="tsBodyControl300XSmall" style="color:var(--textPremium);">{rating}</span><svg></svg>'
            f'<span class="tsBodyControl300XSmall" style="color:var(--textSecondary);">{reviews}&nbsp;отзывов</span></div>'
        )
    return (
        '<div data-index="0" class="tile-root gp1_21 hj9_21 hk_21">'
        f'<a data-prerender="true" target="_blank" href="{href}" rel="noopener" class="q4b1_5_9-a tile-clickable-element gp7_21 p7g_21">'
        '<div class="pg4_21"><img class="gp9_21 g9p_21 b95_4_3-a"></div></a>'
        f'<div class="pg1_21"><div class="g0q_21 g1q_21 c35_6_0-a"><div class="c35_6_0-a0">{price_block}</div></div>'
        '<div class="ea5_7_7-a">'
        f'<a target="_blank" href="{href}" rel="noopener" class="q4b1_5_9-a tile-clickable-element qg1_21">'
        f'<div class="bq03_9_2-a g0q_21"><span class="tsBody500Medium">{title}</span></div></a></div>'
        f'{score}</div></div>'
    )


def _page(tiles: list[str]) -> str:
    return (
        '<html><body><div data-widget="searchResultsSort"></div>'
        f'<div data-widget="tileGridDesktop"><div class="widget-search-result-container">{"".join(tiles)}</div></div>'
        '</body></html>'
    )


_HREF_A = "/product/huawei-matebook-d14-noutbuk-14-5013249796/?at=1tOk1Z2uxxHSDAH2dwlV1WASOM-VKifk"
_HREF_B = "/product/veltron-noutbuk-15-6-2145601987/?at=Xq0k1Z2uxx"


class TestOzonSearchSelectors:
    def setup_method(self):
        self.preset = BuiltInRegistry().get("ozon_search_camoufox")
        self.fields = self.preset.parsing_instructions.fields

    def test_urls_read_the_title_anchor_not_the_image_anchor(self):
        html = _page([_tile("A", _HREF_A, f"55{THIN}119", rating="5.0", reviews="6"),
                      _tile("B", _HREF_B, f"37{THIN}187", rating="4.8", reviews="332")])
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        # two anchors per card, one url per card
        assert data["urls"] == [_HREF_A.split("?")[0], _HREF_B.split("?")[0]]
        assert data["titles"] == ["A", "B"]

    def test_urls_pipeline_is_token_strip_then_urljoin(self):
        assert [(step.op, step.args) for step in self.fields["urls"].post_process] == [
            ("regex", ["^([^?#]+)"]), ("urljoin", [])]

    def test_prices_parse_thin_space_grouping(self):
        html = _page([_tile("A", _HREF_A, f"55{THIN}119", old_price=f"69{THIN}959", rating="5.0", reviews="6"),
                      _tile("B", _HREF_B, f"1{THIN}234{THIN}567")])
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        # the current price is the tsHeadline500Medium span; the struck old
        # price (tsBodyControl400Small) must not win, and the thin spaces are
        # grouping, not a stop
        assert data["prices"] == [55119.0, 1234567.0]

    def test_a_card_without_reviews_keeps_its_slot(self):
        html = _page([_tile("A", _HREF_A, "100", rating="5.0", reviews="6"),
                      _tile("B", _HREF_B, "200"),
                      _tile("C", _HREF_A, "300", rating="4.8", reviews=f"1{THIN}724")])
        data, warnings = extract_fields(html, self.preset.parsing_instructions)
        assert data["titles"] == ["A", "B", "C"]
        assert data["ratings"] == [5.0, None, 4.8]
        assert data["review_counts"] == [6, None, 1724]
        assert len({len(data[k]) for k in ("titles", "urls", "prices", "ratings", "review_counts")}) == 1
        assert not [w for w in warnings if "nulled every value" in w]


class TestATileWithoutATitleLeavesEveryColumn:
    """Review finding, 2026-09-08 (Раиль).

    `titles` and `urls` require the title span; `prices`, `ratings` and
    `review_counts` selected every `.tile-root`. A tile rendered without a
    title therefore shortened the first two columns while the last three kept
    their slot, and every row after it paired with the previous tile's price.
    Reproduced before the fix: titles 2, prices 3, and the third title zipped
    against the second tile's price -- a real product shown at another
    product's price, silently.
    """

    def _titleless(self, href: str, price: str) -> str:
        return (
            '<div class="tile-root">'
            f'<a href="{href}" class="tile-clickable-element"><img></a>'
            f'<div class="pg1_21"><div class="c35_6_0-a">'
            f'<span class="c35_6_0-a1 tsHeadline500Medium">{price}{THIN}\u20bd</span></div>'
            '<div class="g0q_21 c7w1_8_3-a"><span class="tsBodyControl300XSmall">4.8</span>'
            '<span class="tsBodyControl300XSmall">332&nbsp;\u043e\u0442\u0437\u044b\u0432\u0430</span>'
            '</div></div></div>'
        )

    @pytest.mark.parametrize("name", ["ozon_search_camoufox", "ozon_search_chromium"])
    def test_every_column_keeps_the_same_length(self, name):
        preset = BuiltInRegistry().get(name)
        html = _page([
            _tile("A", _HREF_A, f"55{THIN}119", rating="5.0", reviews="6"),
            self._titleless(_HREF_B, f"37{THIN}187"),
            _tile("C", _HREF_A, f"27{THIN}736", rating="4.8", reviews="332"),
        ])
        data, _ = extract_fields(html, preset.parsing_instructions)
        lengths = {c: len(data[c]) for c in ("titles", "urls", "prices", "ratings", "review_counts")}
        assert len(set(lengths.values())) == 1, lengths
        assert lengths["titles"] == 2, lengths

    @pytest.mark.parametrize("name", ["ozon_search_camoufox", "ozon_search_chromium"])
    def test_the_surviving_rows_keep_their_own_prices(self, name):
        preset = BuiltInRegistry().get(name)
        html = _page([
            _tile("A", _HREF_A, f"55{THIN}119", rating="5.0", reviews="6"),
            self._titleless(_HREF_B, f"37{THIN}187"),
            _tile("C", _HREF_A, f"27{THIN}736", rating="4.8", reviews="332"),
        ])
        data, _ = extract_fields(html, preset.parsing_instructions)
        assert list(zip(data["titles"], data["prices"])) == [("A", 55119.0), ("C", 27736.0)]
