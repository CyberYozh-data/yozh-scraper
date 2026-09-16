"""`amazon_product.rating`: the star text is localised.

amazon.de, 2026-09-04 audit and 2026-09-05 probe: `#acrPopover .a-icon-alt` reads
"4,2 von 5 Sternen" and the rule -- a regex for "out of" -- returned null on 3
of 4 `de` runs with data while the `us` twin was fine. `amazon_search` had
already solved the same text with a locale-free number match; the product
preset was left behind. One twin is enough: the twins' parsing_instructions are
pinned identical in test_preset_engine_variants.
"""
from __future__ import annotations

import pytest

from src.extract.extractor import extract_fields
from src.presets.store import BuiltInRegistry


def _page(alt: str) -> str:
    return (
        "<html><body><span id='acrPopover' class='reviewCountTextLinkedHistogram'>"
        f"<span class='a-icon-alt'>{alt}</span></span>"
        "<span id='productTitle'>Thing</span></body></html>"
    )


@pytest.mark.parametrize("alt, expected", [
    ("4.2 out of 5 stars", 4.2),        # us, uk
    ("4,2 von 5 Sternen", 4.2),         # de
    ("4,2 sur 5 étoiles", 4.2),         # fr
    ("5つ星のうち4.2", 4.2),             # jp: the maximum comes first
    ("5.0 out of 5 stars", 5.0),
])
def test_rating_parses_in_every_shipped_locale(alt, expected):
    preset = BuiltInRegistry().get("amazon_product_chromium")
    data, warnings = extract_fields(_page(alt), preset.parsing_instructions)
    assert data["rating"] == expected, (alt, data["rating"], warnings)
