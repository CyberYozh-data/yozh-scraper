"""Row-alignment guards for the mobile_de_search selectors.

mobile.de's class names are build hashes (_NzmyG, _CoixB ...), so the recipe
anchors on the `data-testid` hooks inside the per-listing card, which is the
`<a href=…/fahrzeuge/details.html?id=…>` that wraps the whole listing (title,
price and attribute line all sit inside it -- 24 of 24 cards on the
2026-09-06 capture). Two live shapes are pinned:

  * the price label carries a non-breaking space before the euro sign
    ("3.750&nbsp;€") and dot-grouped thousands;
  * a sponsored card carries a `data-testid="sponsored-badge"` node (4 of 24),
    and the marker column must stay as long as the others on every page.
"""
from __future__ import annotations

import pytest

from src.extract.extractor import extract_fields
from src.presets.store import BuiltInRegistry


def _card(n: int, title: str, price: str, attrs: str, *, sponsored: bool = False) -> str:
    badge = '<span class="_YhjQt" data-testid="sponsored-badge">Gesponsert</span>' if sponsored else ""
    return (
        f'<div data-testid="result-listing-{n}" class="_OChAv _ihMir">'
        f'<a class="_CoixB _tniRU" data-testid="result-listing-{n}-link" target="_blank" type="unstyled" '
        f'href="https://suchen.mobile.de/fahrzeuge/details.html?id={n}&vc=Car&ms=3500%3B20%3B%3B%3B&s=Car&sb=rel&od=up">'
        f'<div class="_NzmyG"><div data-testid="result-listing-{n}-title" class="_jTFxm _zMwgG"><h2 class="_mbOMT">'
        f'<span class="_UXJMC" data-testid="listing-title-card-view">{title}</span></h2>{badge}</div>'
        f'<div class="_vVLF _PaeKX" data-testid="result-listing-{n}-price-section"><div data-testid="main-price-label"><div>'
        f'<span class="_RPvxW" data-testid="price-label">{price}&nbsp;€</span></div></div></div>'
        f'<div data-testid="listing-details" class="_kZQEY"><div data-testid="listing-details-attributes" class="_avGEA">{attrs}</div>'
        '<div data-testid="seller-info" class="_hbQmN">Händler</div></div></div></a></div>'
    )


def _page(cards: list[str]) -> str:
    return f'<html><body><div data-testid="result-list" class="_XjRCm">{"".join(cards)}</div></body></html>'


_ATTRS = "EZ 01/2008 • 256.000&nbsp;km • 160&nbsp;kW&nbsp;(218&nbsp;PS) • Benzin"


class TestMobileDeSearchSelectors:
    def setup_method(self):
        self.preset = BuiltInRegistry().get("mobile_de_search_chromium")

    def test_one_slot_per_card_across_every_column(self):
        html = _page([_card(1, "BMW 525", "3.750", _ATTRS, sponsored=True),
                      _card(2, "BMW 525d Touring", "17.900", "EZ 10/2016 • 113.106&nbsp;km • Diesel"),
                      _card(3, "BMW 525i", "25.990", "Unfallfrei • EZ 10/2017 • 107.956&nbsp;km • Diesel")])
        data, warnings = extract_fields(html, self.preset.parsing_instructions)
        assert data["titles"] == ["BMW 525", "BMW 525d Touring", "BMW 525i"]
        # the per-session tail (searchId/refId/ref=srp on the live page) is dropped
        assert data["urls"] == [f"https://suchen.mobile.de/fahrzeuge/details.html?id={n}" for n in (1, 2, 3)]
        assert data["prices"] == [3750.0, 17900.0, 25990.0]
        assert data["details"] == ["EZ 01/2008 • 256.000 km • 160 kW (218 PS) • Benzin",
                                   "EZ 10/2016 • 113.106 km • Diesel",
                                   "Unfallfrei • EZ 10/2017 • 107.956 km • Diesel"]
        assert data["sponsored"] == ["sponsored-badge", "", ""]
        assert not [w for w in warnings if "nulled every value" in w]

    def test_ad_free_page_reads_as_empty_markers_without_a_silent_null_warning(self):
        html = _page([_card(1, "BMW 525", "3.750", _ATTRS), _card(2, "BMW 525", "4.000", _ATTRS)])
        data, warnings = extract_fields(html, self.preset.parsing_instructions)
        assert data["sponsored"] == ["", ""]
        assert warnings == []

    def test_a_card_without_a_euro_price_keeps_a_null_slot(self):
        html = _page([_card(1, "BMW 525", "3.750", _ATTRS), _card(2, "BMW 525", "Preis auf Anfrage", _ATTRS).replace("Preis auf Anfrage&nbsp;€", "Preis auf Anfrage")])
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        assert data["prices"] == [3750.0, None]
        assert len(data["titles"]) == 2


class TestOnlyTheResultListCounts:
    """Review finding, 2026-09-08 (Раиль).

    The card anchor was matched anywhere in the document. mobile.de also links
    to `details.html` from a "similar vehicles" rail and from promo blocks, so
    those would have become extra rows -- polluting the result set, and
    misaligning it outright for any such link that carries no title node,
    which is exactly the shape a compact rail uses. Scoping every column to
    `[data-testid='result-list']` is a guard: on the 2026-09-06 capture all 24
    anchors already sat inside the list, so nothing there changes.
    """

    SIMILAR_RAIL = (
        '<section data-testid="similar-vehicles"><a '
        'href="https://suchen.mobile.de/fahrzeuge/details.html?id=999&vc=Car">'
        '<div data-testid="main-price-label"><span data-testid="price-label">9.999&nbsp;\u20ac</span></div>'
        '</a></section>'
    )

    @pytest.mark.parametrize("name", ["mobile_de_search_chromium", "mobile_de_search_camoufox"])
    def test_a_similar_vehicles_link_outside_the_list_is_not_a_row(self, name):
        preset = BuiltInRegistry().get(name)
        page = (
            '<html><body>'
            f'<div data-testid="result-list" class="_XjRCm">{_card(1, "BMW 525", "3.750", _ATTRS)}'
            f'{_card(2, "BMW 525d", "17.900", _ATTRS)}</div>'
            f'{self.SIMILAR_RAIL}'
            '</body></html>'
        )
        data, _ = extract_fields(page, preset.parsing_instructions)
        lengths = {c: len(data[c]) for c in ("titles", "urls", "prices", "details", "sponsored")}
        assert set(lengths.values()) == {2}, lengths
        assert all("id=999" not in u for u in data["urls"]), data["urls"]
        assert 9999.0 not in data["prices"], data["prices"]
