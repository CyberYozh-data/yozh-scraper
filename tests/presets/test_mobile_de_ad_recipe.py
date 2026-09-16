"""Recipe guards for mobile_de_ad, pinned to the 2026-09-06 live capture.

There is no <h1> on the ad page: the title is the `vip-ad-title` node
(rendered twice, header and sticky bar). Key facts sit in
`vip-key-features-list-item-*` blocks whose value is an <h4>; the colour only
in the technical-data list as a <dt data-testid="color-item"> + <dd> pair.
"""
from __future__ import annotations

from src.extract.extractor import extract_fields
from src.presets.store import BuiltInRegistry


def _fact(key: str, label: str, value: str) -> str:
    return (f'<div data-testid="vip-key-features-list-item-{key}" class="_uwTMH"><div class="_xSIfI">'
            f'<div class="_UaCyj"><span><span>{label}</span></span></div><h4 class="_nDCZX _ZLqwg">{value}</h4></div></div>')


_PAGE = (
    '<html><body>'
    '<div data-testid="vip-ad-title" class="_KJvNm">BMW 525</div>'
    '<div data-testid="main-price-area"><span data-testid="vip-price-label" class="_hkzTQ">3.750&nbsp;€</span></div>'
    '<div data-testid="vip-key-features-box">'
    + _fact("mileage", "Kilometerstand", "256.000&nbsp;km")
    + _fact("power", "Leistung", "160&nbsp;kW&nbsp;(218&nbsp;PS)")
    + _fact("fuel", "Kraftstoffart", "Benzin")
    + _fact("transmission", "Getriebe", "Automatik")
    + _fact("firstRegistration", "Erstzulassung", "01/2008")
    + '<div data-testid="vip-key-features-seller-seller-name">PKW Augsburg IMS GmbH</div></div>'
    '<div data-testid="vip-technical-data-box"><dl class="_hvedS">'
    '<dt class="_OyeyS" data-testid="damageCondition-item">Fahrzeugzustand</dt><dd class="_GxGsO">Gebrauchtfahrzeug</dd>'
    '<dt class="_OyeyS" data-testid="color-item">Farbe</dt><dd class="_GxGsO">Grau Metallic</dd>'
    '<dt class="_OyeyS" data-testid="interiorColor-item">Innenausstattung</dt><dd class="_GxGsO">Schwarz</dd></dl></div>'
    '<div data-testid="vip-vehicle-description"><div data-testid="vip-vehicle-description-text">Hier bieten wir einen BMW 525i an:<br>2. Hand<br>Scheckheft gepflegt<ul><li>Armlehne</li><li>Elektr. Fensterheber</li></ul></div></div>'
    '<div data-testid="vip-ad-title" class="_sticky">BMW 525 (sticky bar)</div>'
    '</body></html>'
)


class TestMobileDeAdRecipe:
    def setup_method(self):
        self.preset = BuiltInRegistry().get("mobile_de_ad_chromium")

    def test_reads_every_fact_from_its_hook(self):
        data, warnings = extract_fields(_PAGE, self.preset.parsing_instructions)
        assert data == {
            "title": "BMW 525",
            "price": 3750.0,
            "mileage_km": 256000,
            "first_registration": "01/2008",
            "power_kw": 160,
            "fuel": "Benzin",
            "transmission": "Automatik",
            "color": "Grau Metallic",
            "seller": "PKW Augsburg IMS GmbH",
            # the live node carries ~30 <br> and 21 <li>; text_content() would glue them
            "description": "Hier bieten wir einen BMW 525i an: 2. Hand Scheckheft gepflegt Armlehne Elektr. Fensterheber",
        }
        assert warnings == []

    def test_colour_is_the_dd_right_after_its_dt(self):
        """`dt[data-testid='color-item'] + dd`: with the colour's own <dd> moved
        behind another row, the adjacent sibling is a <dt>, so nothing matches --
        a general-sibling combinator would have read the next colour-like <dd>."""
        page = _PAGE.replace(
            '<dt class="_OyeyS" data-testid="color-item">Farbe</dt><dd class="_GxGsO">Grau Metallic</dd>'
            '<dt class="_OyeyS" data-testid="interiorColor-item">Innenausstattung</dt><dd class="_GxGsO">Schwarz</dd>',
            '<dt class="_OyeyS" data-testid="color-item">Farbe</dt>'
            '<dt class="_OyeyS" data-testid="interiorColor-item">Innenausstattung</dt><dd class="_GxGsO">Schwarz</dd>'
            '<dd class="_GxGsO">Grau Metallic</dd>',
        )
        assert page != _PAGE
        data, _ = extract_fields(page, self.preset.parsing_instructions)
        assert data["color"] is None

    def test_optional_fields_declare_null(self):
        props = self.preset.output_schema["properties"]
        assert props["title"]["type"] == "string"
        for field in ("price", "mileage_km", "first_registration", "power_kw", "fuel", "transmission", "color", "seller", "description"):
            assert "null" in props[field]["type"], field
