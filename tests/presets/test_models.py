from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.extract.models import ExtractRule, FieldRule, PostProcess
from src.presets.models import (
    LocaleProfile,
    ParsingInstructions,
    Preset,
    PresetMeta,
    derive_locale_profile,
)


def _minimal_preset(**overrides) -> Preset:
    base = dict(
        name="amazon_product",
        source="amazon",
        kind="builtin",
        request_defaults={"device": "desktop", "proxy_type": "res_rotating"},
        default_locale="us",
        locales={
            "us": LocaleProfile(domain="com", country="US"),
        },
        updated_at=1_700_000_000.0,
    )
    base.update(overrides)
    return Preset(**base)


class TestParsingInstructions:
    def test_is_alias_of_extract_rule(self):
        instr = ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector="h1")},
        )
        assert isinstance(instr, ExtractRule)


class TestLocaleProfile:
    def test_minimal_fields(self):
        loc = LocaleProfile(domain="de", country="DE")
        assert loc.domain == "de"
        assert loc.country == "DE"
        assert loc.locale is None

    def test_proxy_country_defaults_to_none(self):
        """Unset proxy_country means 'exit through the market country' —
        the materializer falls back to `country` when this is None."""
        loc = LocaleProfile(domain="com", country="US")
        assert loc.proxy_country is None

    def test_proxy_country_can_diverge_from_market_country(self):
        """A locale can pin the market (gl=/hl=) to one country while
        exiting through a different one, e.g. Google's US residential
        proxy range is hard-blocked so the `us` locale must exit via GB."""
        loc = LocaleProfile(domain="com", country="US", proxy_country="GB")
        assert loc.country == "US"
        assert loc.proxy_country == "GB"

    def test_explicit_override_kept(self):
        loc = LocaleProfile(
            domain="com",
            country="US",
            locale="es-US",
            timezone="America/Los_Angeles",
            accept_language="es-US,es;q=0.9",
        )
        assert loc.locale == "es-US"
        assert loc.timezone == "America/Los_Angeles"


class TestDeriveLocaleProfile:
    def test_fills_from_geo_profile(self):
        loc = LocaleProfile(domain="de", country="DE")
        derived = derive_locale_profile(loc)
        assert derived.locale == "de-DE"
        assert derived.timezone == "Europe/Berlin"
        assert "de-DE" in (derived.accept_language or "")

    def test_explicit_values_take_precedence(self):
        loc = LocaleProfile(domain="com", country="US", locale="fr-CA")
        derived = derive_locale_profile(loc)
        assert derived.locale == "fr-CA"
        # timezone still filled from country fallback
        assert derived.timezone is not None

    def test_unknown_country_leaves_optional_fields_none(self):
        loc = LocaleProfile(domain="zz", country="ZZ")
        derived = derive_locale_profile(loc)
        assert derived.locale is None
        assert derived.timezone is None
        assert derived.accept_language is None


class TestPreset:
    def test_minimal_valid_preset(self):
        p = _minimal_preset()
        assert p.kind == "builtin"
        assert p.version == 1
        assert p.self_heal is True

    def test_preset_with_parsing_instructions(self):
        p = _minimal_preset(
            parsing_instructions=ParsingInstructions(
                type="css",
                fields={
                    "title": FieldRule(selector="#productTitle"),
                    "price": FieldRule(
                        selector=".a-price",
                        post_process=[PostProcess(op="parse_price")],
                    ),
                },
            ),
        )
        assert p.parsing_instructions is not None
        assert "title" in p.parsing_instructions.fields

    def test_name_must_be_snake_case(self):
        with pytest.raises(ValidationError):
            _minimal_preset(name="Amazon Product")
        with pytest.raises(ValidationError):
            _minimal_preset(name="amazon-product")

    def test_default_locale_must_exist_in_locales(self):
        with pytest.raises(ValidationError):
            _minimal_preset(default_locale="de")  # only 'us' is registered

    def test_roundtrip_json(self):
        original = _minimal_preset(
            parsing_instructions=ParsingInstructions(
                type="css",
                fields={"title": FieldRule(selector="h1")},
            ),
            llm_extract_prompt=None,
            output_schema={"title": {"type": "string"}},
        )
        dumped = original.model_dump(mode="json")
        loaded = Preset.model_validate(dumped)
        assert loaded == original
        # JSON-string round-trip too
        loaded2 = Preset.model_validate(json.loads(json.dumps(dumped)))
        assert loaded2 == original

    def test_ai_only_preset_has_no_instructions(self):
        p = _minimal_preset(
            parsing_instructions=None,
            llm_extract_prompt="Extract product info as JSON",
            output_schema={"title": {"type": "string"}},
        )
        assert p.parsing_instructions is None
        assert p.llm_extract_prompt is not None


class TestPresetMeta:
    def test_basic_fields(self):
        meta = PresetMeta(
            name="amazon_product",
            source="amazon",
            locale="us",
            version=3,
        )
        assert meta.name == "amazon_product"
        assert meta.version == 3

    def test_a_stored_meta_from_before_the_query_field_still_validates(self):
        """Stored page dicts cross deploy boundaries; an older one has no `query`."""
        assert PresetMeta(name="n", source="s", locale=None, version=3).query is None
