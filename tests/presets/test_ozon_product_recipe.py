"""Recipe guards for ozon_product, pinned to the 2026-09-06 live capture.

Ozon renders TWO prices in its `webPrice` widget -- the Ozon-card price first
(tsHeadline600Large, "С банками") and the ordinary one after it
(tsHeadline500Medium, "С другими банками") -- and both carry U+2009 thin
spaces as thousands separators. `price` must be the ordinary one, not the
first number on the page. The score widget prints "5 • 3 отзыва" in one node.
"""
from __future__ import annotations

from src.extract.extractor import extract_fields
from src.presets.store import BuiltInRegistry

THIN = "\u2009"


def _page(*, card_price: str | None = f"193{THIN}812", price: str = f"208{THIN}646",
          score: str = "5 • 3 отзыва", out_of_stock: bool = False) -> str:
    card = (f'<span class="tsHeadline600Large">{card_price}{THIN}₽</span> '
            '<span class="pdp_b3h tsBody400Small">С банками</span>') if card_price else ""
    oos = '<div data-widget="webOutOfStock"><div>Этот товар закончился</div></div>' if out_of_stock else ""
    return (
        '<html><body>'
        '<div data-widget="webProductHeading"><h1 class="pdp_bf8 tsHeadline550Medium">MSI A17 AI Игровой ноутбук 17.30"</h1></div>'
        f'<div data-widget="webPrice" class="pdp_bh7"><div class="pdp_b6h"><div class="pdp_b2h">{card}</div>'
        f'<div class="pdp_ib pdp_ib3"><span class="pdp_ib0 tsHeadline500Medium">{price}{THIN}₽</span>'
        '<span class="pdp_b0i tsBody400Small">С другими банками</span></div></div></div>'
        f'<div data-widget="webSingleProductScore"><a href="/product/x/reviews/"><div class="tsBodyControl500Medium">{score}</div></a></div>'
        '<button data-widget="webDetailSKU"><div class="tsBodyControl400Small">Артикул: 3972904544</div></button>'
        f'{oos}</body></html>'
    )


class TestOzonProductRecipe:
    def setup_method(self):
        self.preset = BuiltInRegistry().get("ozon_product_camoufox")

    def test_reads_the_ordinary_price_not_the_card_price(self):
        data, warnings = extract_fields(_page(), self.preset.parsing_instructions)
        assert data["title"] == 'MSI A17 AI Игровой ноутбук 17.30"'
        assert data["price"] == 208646.0
        assert data["price_ozon_card"] == 193812.0
        assert data["rating"] == 5.0
        assert data["review_count"] == 3
        assert data["sku"] == "3972904544"
        assert data["out_of_stock"] is None
        assert warnings == []

    def test_a_page_without_the_card_price_leaves_that_field_null(self):
        data, _ = extract_fields(_page(card_price=None), self.preset.parsing_instructions)
        assert data["price"] == 208646.0
        assert data["price_ozon_card"] is None

    def test_out_of_stock_marker_follows_the_widget(self):
        data, _ = extract_fields(_page(out_of_stock=True), self.preset.parsing_instructions)
        assert data["out_of_stock"] == "webOutOfStock"

    def test_the_sold_out_redirect_page_still_names_the_product(self):
        """Live shape (capture 2026-09-06): the id URL redirected to a search
        for the product's name, no heading widget, `webOutOfStock` carrying the
        name and the last price, "Товар закончился" in a textBlock."""
        page = (
            '<html><body><div data-widget="fulltextResultsHeader"><div>По вашему запросу товаров сейчас нет.<br> Но мы нашли похожие</div></div>'
            '<div data-widget="webOutOfStock" class="pdp_s7"><div class="pdp_eb0"><div class="c35_5_2-a0">'
            f'<span class="c35_5_2-a1 tsHeadline400Small c35_5_2-b2">205{THIN}390{THIN}₽</span></div>'
            '<div class="bq03_9_1-a"><span class="tsBody400Small">MSI A17 AI B2HWFKG-048XRU Игровой ноутбук 17.30"</span></div></div></div>'
            '<div data-widget="textBlock"><span class="tsHeadline500Medium">Товар закончился</span></div>'
            '<div data-widget="searchResultsSort"></div></body></html>'
        )
        data, warnings = extract_fields(page, self.preset.parsing_instructions)
        assert data["title"] == 'MSI A17 AI B2HWFKG-048XRU Игровой ноутбук 17.30"'
        assert data["price"] is None
        assert data["price_last"] == 205390.0
        assert data["out_of_stock"] == "webOutOfStock"
        assert not [w for w in warnings if "required" in w]

    def test_in_stock_page_has_no_last_price(self):
        data, _ = extract_fields(_page(), self.preset.parsing_instructions)
        assert data["price_last"] is None

    def test_fractional_rating_and_grouped_review_count(self):
        data, _ = extract_fields(_page(score=f"4.8 • 1{THIN}724 отзыва"), self.preset.parsing_instructions)
        assert data["rating"] == 4.8
        assert data["review_count"] == 1724

    def test_optional_fields_declare_null(self):
        props = self.preset.output_schema["properties"]
        assert props["title"]["type"] == "string"
        for field in ("price", "price_ozon_card", "price_last", "rating", "review_count", "sku", "out_of_stock"):
            assert "null" in props[field]["type"], field
