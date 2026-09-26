from __future__ import annotations

import json

import pytest

from src.presets.models import LocaleProfile, ParsingInstructions, Preset
from src.extract.models import FieldRule
from src.presets.store import BuiltInRegistry
from src.presets.materializer import (
    MaterializeError,
    SessionConflictError,
    PresetScrapeRequest,
    materialize,
)
from src.schemas import ScrapeRequest


def _amazon_preset(**overrides) -> Preset:
    base = {
        "name": "amazon_product",
        "source": "amazon",
        "kind": "builtin",
        "url_template": "https://www.amazon.{domain}/dp/{asin}",
        "request_defaults": {
            "device": "desktop",
            "proxy_type": "res_rotating",
            "stealth": True,
            "wait_until": "networkidle",
        },
        "locales": {
            "us": LocaleProfile(domain="com", country="US"),
            "de": LocaleProfile(domain="de", country="DE"),
        },
        "default_locale": "us",
        "parsing_instructions": ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector="#productTitle", required=True)},
        ),
        "updated_at": 1_700_000_000.0,
    }
    base.update(overrides)
    return Preset(**base)


def _builtin(name: str) -> Preset:
    return BuiltInRegistry().get(name)


class TestUrlTemplate:
    def test_fills_domain_and_params(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "B0CRTYZG5C"},
            locale="us",
        )
        scrape = materialize(preset, req)
        assert isinstance(scrape, ScrapeRequest)
        assert str(scrape.url) == "https://www.amazon.com/dp/B0CRTYZG5C"

    def test_locale_changes_domain(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "B0CRTYZG5C"},
            locale="de",
        )
        scrape = materialize(preset, req)
        assert str(scrape.url) == "https://www.amazon.de/dp/B0CRTYZG5C"

    def test_locale_lr_fills_template(self):
        """A locale's `lr` region code is exposed to url_template as {lr}."""
        preset = _amazon_preset(
            url_template="https://yandex.ru/search/?text={query}&lr={lr}",
            locales={
                "ru": LocaleProfile(domain="ru", country="RU", lr="225"),
                "us": LocaleProfile(domain="ru", country="RU", lr="84"),
            },
            default_locale="ru",
        )
        scrape = materialize(
            preset, PresetScrapeRequest(source="amazon_product", preset_params={"query": "x"}, locale="us")
        )
        assert str(scrape.url) == "https://yandex.ru/search/?text=x&lr=84"
        # proxy still routes through the locale country (RU), not the lr region.
        assert scrape.proxy_geo.country_code == "RU"

    def test_preset_param_overrides_locale_lr(self):
        preset = _amazon_preset(
            url_template="https://yandex.ru/search/?text={query}&lr={lr}",
            locales={"ru": LocaleProfile(domain="ru", country="RU", lr="225")},
            default_locale="ru",
        )
        scrape = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_product", preset_params={"query": "x", "lr": "10174"}, locale="ru"
            ),
        )
        assert str(scrape.url) == "https://yandex.ru/search/?text=x&lr=10174"

    def test_default_locale_used_when_unset(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}
        )
        scrape = materialize(preset, req)
        assert "amazon.com" in str(scrape.url)

    def test_missing_param_raises_materialize_error(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(source="amazon_product", locale="us")
        with pytest.raises(MaterializeError) as exc:
            materialize(preset, req)
        assert "asin" in str(exc.value)

    def test_unknown_locale_raises(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "X"},
            locale="zz",
        )
        with pytest.raises(MaterializeError) as exc:
            materialize(preset, req)
        assert "zz" in str(exc.value)

    def test_lang_and_country_template_vars(self):
        preset = _amazon_preset(
            url_template="https://www.google.{domain}/search?q={query}&gl={country}&hl={lang}",
            locales={"de": LocaleProfile(domain="de", country="DE")},
            default_locale="de",
        )
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"query": "kaffee"},
            locale="de",
        )
        scrape = materialize(preset, req)
        url = str(scrape.url)
        assert "google.de" in url
        assert "q=kaffee" in url
        assert "gl=de" in url  # country lowercased for gl=
        assert "hl=de" in url  # language derived from locale


class TestRequestDefaults:
    def test_request_defaults_applied(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert scrape.proxy_type == "res_rotating"
        assert scrape.wait_until == "networkidle"
        assert scrape.stealth is True

    def test_request_defaults_carry_max_retries(self):
        preset = _amazon_preset(
            request_defaults={"proxy_type": "res_rotating", "max_retries": 7}
        )
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert scrape.max_retries == 7

    def test_locale_sets_proxy_geo(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="de"
        )
        scrape = materialize(preset, req)
        assert scrape.proxy_geo is not None
        assert scrape.proxy_geo.country_code == "DE"

    def test_request_override_wins(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "X"},
            locale="us",
            request_override={"proxy_type": "none", "screenshot": True},
        )
        scrape = materialize(preset, req)
        assert scrape.proxy_type == "none"
        assert scrape.screenshot is True

    def test_unknown_request_default_key_raises_materialize_error(self):
        preset = _amazon_preset(
            request_defaults={"device": "desktop", "bogus_field": 123}
        )
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        with pytest.raises(MaterializeError):
            materialize(preset, req)


class TestParsing:
    def test_parsing_instructions_become_extract(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert scrape.extract is not None
        assert "title" in scrape.extract.fields

    def test_parsing_override_replaces_preset(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "X"},
            locale="us",
            parsing_override=ParsingInstructions(
                type="css",
                fields={"custom": FieldRule(selector=".x")},
            ),
        )
        scrape = materialize(preset, req)
        assert "custom" in scrape.extract.fields
        assert "title" not in scrape.extract.fields

    def test_preset_meta_attached(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert scrape.preset_meta is not None
        assert scrape.preset_meta.name == "amazon_product"
        assert scrape.preset_meta.source == "amazon"
        assert scrape.preset_meta.locale == "us"


class TestUrlEncoding:
    def test_path_param_uses_percent_encoding_not_plus(self):
        preset = _amazon_preset(
            url_template="https://www.amazon.{domain}/s/{term}"
        )
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"term": "red shoes"},
            locale="us",
        )
        scrape = materialize(preset, req)
        # space in a PATH segment must be %20, never +
        assert "/s/red%20shoes" in str(scrape.url)
        assert "+" not in str(scrape.url)

    def test_query_param_uses_plus_encoding(self):
        preset = _amazon_preset(
            url_template="https://www.google.{domain}/search?q={query}",
            locales={"us": LocaleProfile(domain="com", country="US")},
            default_locale="us",
        )
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"query": "red shoes"},
            locale="us",
        )
        scrape = materialize(preset, req)
        # space in QUERY may be + (application/x-www-form-urlencoded)
        assert "q=red+shoes" in str(scrape.url)


class TestProxyGeoOverride:
    def test_proxy_geo_from_request_defaults_logs(self, caplog):
        import logging

        preset = _amazon_preset(
            request_defaults={
                "device": "desktop",
                "proxy_geo": {"country_code": "NL"},
            }
        )
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="de"
        )
        with caplog.at_level(logging.INFO):
            scrape = materialize(preset, req)
        # escape hatch honored ...
        assert scrape.proxy_geo.country_code == "NL"
        # ... but not silently
        assert any(
            "proxy_geo" in r.message and "NL" in r.message and "DE" in r.message
            for r in caplog.records
        )


class TestProxyCountry:
    """LocaleProfile.proxy_country decouples the proxy exit country from the
    locale's market country (LocaleProfile.country). Some markets must not be
    fetched through their own country's proxy exit (Google 429s the US
    residential range), so a locale can pin a market (gl=/hl=) while exiting
    through a different country.
    """

    def test_unset_proxy_country_falls_back_to_market_country(self):
        preset = _amazon_preset()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="de"
        )
        scrape = materialize(preset, req)
        assert scrape.proxy_geo.country_code == "DE"

    def test_proxy_country_decouples_exit_from_market(self):
        preset = _amazon_preset(
            url_template="https://www.google.{domain}/search?q={query}&gl={country}&hl={lang}",
            locales={
                "us": LocaleProfile(domain="com", country="US", proxy_country="GB"),
            },
            default_locale="us",
        )
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"query": "x"}, locale="us"
        )
        scrape = materialize(preset, req)
        # Exit country follows the override ...
        assert scrape.proxy_geo.country_code == "GB"
        # ... but the market (gl=/hl=, and the domain) still reflects the
        # locale's country, not the proxy exit. This is the assertion that
        # would catch a naive fix that repoints the market too.
        url = str(scrape.url)
        assert "google.com" in url
        assert "gl=us" in url
        assert "hl=en" in url

    def test_explicit_proxy_geo_overrides_proxy_country(self):
        """The request_defaults/request_override escape hatch still wins,
        even when the locale also sets proxy_country."""
        preset = _amazon_preset(
            request_defaults={
                "device": "desktop",
                "proxy_geo": {"country_code": "NL"},
            },
            locales={
                "us": LocaleProfile(domain="com", country="US", proxy_country="GB"),
            },
            default_locale="us",
        )
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert scrape.proxy_geo.country_code == "NL"

    def test_derived_fields_follow_market_not_exit_on_non_degenerate_pair(self):
        """US/GB (the shipped google_* pair) is degenerate on every axis
        `derive_locale_profile()`/`_price_locale_for()` touch: both give
        hl=en and price locale "us", so a naive refactor that repoints
        either at the proxy exit instead of the market would still pass
        every US/GB-based assertion above. DE market / GB exit is not
        degenerate: it pins hl=, price-locale, and proxy exit to three
        different, independently-checkable values."""
        from src.extract.models import PostProcess

        preset = _amazon_preset(
            url_template="https://www.google.{domain}/search?q={query}&gl={country}&hl={lang}",
            locales={
                "de": LocaleProfile(domain="de", country="DE", proxy_country="GB"),
            },
            default_locale="de",
            parsing_instructions=ParsingInstructions(
                type="css",
                fields={
                    "price": FieldRule(
                        selector=".price",
                        post_process=[PostProcess(op="parse_price")],
                    ),
                },
            ),
        )
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"query": "x"}, locale="de"
        )
        scrape = materialize(preset, req)
        # market (DE) drives the derived language, not the exit (GB) — a
        # flip to en would silently mis-render hl= for every non-English
        # market that has a proxy_country override.
        assert "hl=de" in str(scrape.url)
        # market drives price locale too: a flip to "us" would parse
        # "12,34 €" as 1234.0 instead of 12.34.
        assert scrape.extract.fields["price"].post_process[0].args == ["eu"]
        # exit still follows proxy_country.
        assert scrape.proxy_geo.country_code == "GB"


class TestGoogleBuiltinPresetsExitViaGB:
    """The shipped google_search / google_shopping presets flag the `us`
    locale's proxy_country as GB: Google hard-blocks the proxy pool's US
    residential exit range (429 'unusual traffic') while still wanting the
    US market (gl=us)."""

    @pytest.mark.parametrize(
        "preset_file", ["google_search_chromium.json", "google_shopping_chromium.json"]
    )
    def test_us_locale_resolves_to_gb_exit_with_us_market(self, preset_file):
        import json
        import pathlib

        raw = json.loads(
            pathlib.Path(f"src/presets/builtin/{preset_file}").read_text()
        )
        preset = Preset(**raw)
        scrape = materialize(
            preset,
            PresetScrapeRequest(
                source=preset.name,
                preset_params={"query": "test"},
                locale="us",
            ),
        )
        assert scrape.proxy_geo.country_code == "GB"
        url = str(scrape.url)
        assert "google.com" in url
        assert "gl=us" in url


class TestAiOnlyPreset:
    def test_ai_only_preset_has_no_extract(self):
        preset = _amazon_preset(
            parsing_instructions=None,
            llm_extract_prompt="Extract product as JSON",
        )
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert scrape.extract is None
        # raw_html is forced on so PR-3 LLM step has something to work with
        assert scrape.raw_html is True


class TestParsingOverrideIsMarkedOnThePlan:
    """The worker refuses to persist a self-heal whose contract was the
    request's own `parsing_override` (audit 2026-09-03, H-14); the flag it
    reads is set here, and only here."""

    def test_the_plan_says_where_the_instructions_came_from(self):
        preset = _amazon_preset()
        override = ParsingInstructions(type="css", fields={"anything": FieldRule(selector="h1", required=True)})
        req = materialize(preset, PresetScrapeRequest(source="amazon_product", preset_params={"asin": "B0"}, parsing_override=override))
        assert req.parser_plan.instructions_from_override is True
        plain = materialize(preset, PresetScrapeRequest(source="amazon_product", preset_params={"asin": "B0"}))
        assert plain.parser_plan.instructions_from_override is False


class TestParsePriceLocaleInjection:
    def _preset_with_price(self, **over):
        from src.extract.models import PostProcess

        return _amazon_preset(
            parsing_instructions=ParsingInstructions(
                type="css",
                fields={
                    "price": FieldRule(
                        selector=".a-offscreen",
                        post_process=[PostProcess(op="parse_price")],
                    ),
                    "price_eu_explicit": FieldRule(
                        selector=".x",
                        post_process=[PostProcess(op="parse_price", args=["eu"])],
                    ),
                    "rating": FieldRule(
                        selector=".r",
                        post_process=[PostProcess(op="parse_float")],
                    ),
                },
            ),
            **over,
        )

    def test_us_locale_injects_us(self):
        preset = self._preset_with_price()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        pp = scrape.extract.fields["price"].post_process[0]
        assert pp.op == "parse_price"
        assert pp.args == ["us"]

    def test_de_locale_injects_eu(self):
        preset = self._preset_with_price()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="de"
        )
        scrape = materialize(preset, req)
        assert scrape.extract.fields["price"].post_process[0].args == ["eu"]

    def test_explicit_args_preserved(self):
        preset = self._preset_with_price()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        # author set ["eu"] explicitly — must not be overwritten by US locale
        assert scrape.extract.fields["price_eu_explicit"].post_process[0].args == ["eu"]

    def test_non_parse_price_ops_untouched(self):
        preset = self._preset_with_price()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="de"
        )
        scrape = materialize(preset, req)
        assert scrape.extract.fields["rating"].post_process[0].op == "parse_float"
        assert scrape.extract.fields["rating"].post_process[0].args == []

    def test_parsing_override_also_gets_locale(self):
        preset = self._preset_with_price()
        from src.extract.models import PostProcess

        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "X"},
            locale="de",
            parsing_override=ParsingInstructions(
                type="css",
                fields={
                    "p": FieldRule(
                        selector=".p",
                        post_process=[PostProcess(op="parse_price")],
                    )
                },
            ),
        )
        scrape = materialize(preset, req)
        assert scrape.extract.fields["p"].post_process[0].args == ["eu"]


class TestUrlBaseInjection:
    """`urljoin`'s base is the materialized request URL itself -- the same
    per-locale-derived value the preset is about to be fetched with. This is
    how the fix works within the constraint that extract_fields never sees a
    page URL (it takes only page_html; see src/extract/extractor.py): rather
    than threading the *navigated* URL all the way down through worker_parse
    -> parser_pipeline -> extract_fields, the materializer injects the URL it
    already computed for the fetch, at the one place both are in scope
    together -- exactly the pattern `_inject_price_locale` established for
    parse_price's locale arg."""

    def _preset_with_url(self, **over):
        from src.extract.models import PostProcess

        return _amazon_preset(
            url_template="https://www.amazon.{domain}/s?k={query}",
            parsing_instructions=ParsingInstructions(
                type="css",
                fields={
                    "urls": FieldRule(
                        selector="a",
                        attr="href",
                        all=True,
                        post_process=[PostProcess(op="urljoin")],
                    ),
                    "urls_explicit_base": FieldRule(
                        selector="a",
                        attr="href",
                        all=True,
                        post_process=[
                            PostProcess(op="urljoin", args=["https://pinned.example"])
                        ],
                    ),
                },
            ),
            **over,
        )

    def test_empty_args_get_the_materialized_url_injected(self):
        preset = self._preset_with_url()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"query": "x"}, locale="us"
        )
        scrape = materialize(preset, req)
        pp = scrape.extract.fields["urls"].post_process[0]
        assert pp.op == "urljoin"
        assert pp.args == ["https://www.amazon.com/s?k=x"]

    def test_different_locale_injects_its_own_domain(self):
        preset = self._preset_with_url()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"query": "x"}, locale="de"
        )
        scrape = materialize(preset, req)
        assert scrape.extract.fields["urls"].post_process[0].args == [
            "https://www.amazon.de/s?k=x"
        ]

    def test_explicit_base_is_not_overwritten(self):
        preset = self._preset_with_url()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"query": "x"}, locale="de"
        )
        scrape = materialize(preset, req)
        assert scrape.extract.fields["urls_explicit_base"].post_process[0].args == [
            "https://pinned.example"
        ]


class TestMaterializerInjectedTracking:
    """Fix-round-1 finding 3: a self-heal must not freeze THIS request's
    injected locale/URL into a user preset forever -- the next request
    (different locale, different domain) would silently resolve against a
    stale, wrong value. The fix needs to know, after the fact, exactly which
    (op, field) pairs were actually injected (as opposed to author-set
    explicit args, indistinguishable from injected ones by content alone once
    both have round-tripped through the same PostProcess.args list) --
    `ParserPlan.materializer_injected` is where that gets recorded. See
    tests/presets/test_worker_parse.py for the strip-before-persist half."""

    def _preset_with_url_and_price(self, **over):
        from src.extract.models import PostProcess

        return _amazon_preset(
            url_template="https://www.amazon.{domain}/s?k={query}",
            parsing_instructions=ParsingInstructions(
                type="css",
                fields={
                    "urls": FieldRule(
                        selector="a", attr="href", all=True,
                        post_process=[PostProcess(op="urljoin")],
                    ),
                    "urls_explicit_base": FieldRule(
                        selector="a", attr="href", all=True,
                        post_process=[
                            PostProcess(op="urljoin", args=["https://pinned.example"])
                        ],
                    ),
                    "price": FieldRule(
                        selector=".a-offscreen",
                        post_process=[PostProcess(op="parse_price")],
                    ),
                    "price_eu_explicit": FieldRule(
                        selector=".x",
                        post_process=[PostProcess(op="parse_price", args=["eu"])],
                    ),
                },
            ),
            **over,
        )

    def test_injected_urljoin_field_is_recorded(self):
        preset = self._preset_with_url_and_price()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"query": "x"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert scrape.parser_plan.materializer_injected.get("urljoin") == ["urls"]

    def test_explicit_base_field_is_not_recorded_as_injected(self):
        preset = self._preset_with_url_and_price()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"query": "x"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert "urls_explicit_base" not in scrape.parser_plan.materializer_injected.get(
            "urljoin", []
        )

    def test_injected_price_locale_field_is_recorded_too(self):
        preset = self._preset_with_url_and_price()
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"query": "x"}, locale="de"
        )
        scrape = materialize(preset, req)
        assert scrape.parser_plan.materializer_injected.get("parse_price") == ["price"]
        assert "price_eu_explicit" not in scrape.parser_plan.materializer_injected.get(
            "parse_price", []
        )

    def test_no_injection_at_all_records_an_empty_mapping(self):
        preset = _amazon_preset()  # title only, no urljoin/parse_price fields
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        assert scrape.parser_plan.materializer_injected == {}


class TestParserPlan:
    def test_plan_carries_preset_self_heal_and_identity(self):
        preset = _amazon_preset(self_heal=True, version=4)
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "X"},
            locale="us",
            llm={"model": "openai/gpt-5.4-mini"},
        )
        scrape = materialize(preset, req)
        assert scrape.parser_plan is not None
        plan = scrape.parser_plan
        assert plan.self_heal is True
        assert plan.llm_model == "openai/gpt-5.4-mini"
        assert plan.preset_name == "amazon_product"
        assert plan.preset_kind == "builtin"

    def test_request_self_heal_overrides_preset(self):
        preset = _amazon_preset(self_heal=True)
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "X"},
            locale="us",
            self_heal=False,
            llm={"model": "m"},
        )
        scrape = materialize(preset, req)
        assert scrape.parser_plan.self_heal is False

    def test_no_llm_means_llm_model_none(self):
        preset = _amazon_preset(self_heal=True)
        req = PresetScrapeRequest(
            source="amazon_product", preset_params={"asin": "X"}, locale="us"
        )
        scrape = materialize(preset, req)
        # self_heal in preset but no llm config -> model None disables LLM
        assert scrape.parser_plan.llm_model is None

    def test_schema_override_lands_in_plan(self):
        preset = _amazon_preset(output_schema={"a": {"type": "string"}})
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "X"},
            locale="us",
            schema_override={"b": {"type": "number"}},
            llm={"model": "m"},
        )
        scrape = materialize(preset, req)
        assert scrape.parser_plan.output_schema == {"b": {"type": "number"}}

    def test_ai_only_prompt_in_plan(self):
        preset = _amazon_preset(
            parsing_instructions=None,
            llm_extract_prompt="grab title",
        )
        req = PresetScrapeRequest(
            source="amazon_product",
            preset_params={"asin": "X"},
            locale="us",
            llm={"model": "m"},
        )
        scrape = materialize(preset, req)
        assert scrape.parser_plan.llm_extract_prompt == "grab title"


class TestMaterializeSessionId:
    def test_session_id_field_propagates(self):
        sr = materialize(
            _amazon_preset(),
            PresetScrapeRequest(
                source="amazon_product",
                preset_params={"asin": "X"},
                session_id="sess_abc",
            ),
        )
        assert sr.session_id == "sess_abc"

    def test_no_session_id_is_none(self):
        sr = materialize(
            _amazon_preset(),
            PresetScrapeRequest(source="amazon_product", preset_params={"asin": "X"}),
        )
        assert sr.session_id is None

    def test_session_id_via_request_override_propagates(self):
        sr = materialize(
            _amazon_preset(),
            PresetScrapeRequest(
                source="amazon_product",
                preset_params={"asin": "X"},
                request_override={"session_id": "sess_ovr"},
            ),
        )
        assert sr.session_id == "sess_ovr"

    def test_field_and_override_equal_ok(self):
        sr = materialize(
            _amazon_preset(),
            PresetScrapeRequest(
                source="amazon_product",
                preset_params={"asin": "X"},
                session_id="sess_same",
                request_override={"session_id": "sess_same"},
            ),
        )
        assert sr.session_id == "sess_same"

    def test_field_and_override_diverge_raises(self):
        with pytest.raises(SessionConflictError, match="session_id conflict"):
            materialize(
                _amazon_preset(),
                PresetScrapeRequest(
                    source="amazon_product",
                    preset_params={"asin": "X"},
                    session_id="sess_field",
                    request_override={"session_id": "sess_override"},
                ),
            )
        assert issubclass(SessionConflictError, MaterializeError)


def test_yandex_preset_uses_prem_and_warmup():
    import json, pathlib
    from src.presets.models import Preset
    from src.presets.materializer import materialize, PresetScrapeRequest

    raw = json.loads(
        pathlib.Path("src/presets/builtin/yandex_search_camoufox.json").read_text()
    )
    preset = Preset(**raw)
    out = materialize(
        preset,
        PresetScrapeRequest(
            source="yandex_search_camoufox",
            locale="ru",
            preset_params={"query": "купить ноутбук"},
        ),
    )
    assert out.proxy_type == "prem_res_rotating"
    assert out.prem_proxy_options.ip_filter == "quality-security"
    assert out.warmup.type == "homepage"
    assert out.proxy_geo.country_code == "RU"
    assert out.browser_engine == "camoufox"
    # Yandex serves a transparent JS "browser check" interstitial
    # (/showcaptchafast) that self-resolves and redirects to the real SERP a
    # few seconds after domcontentloaded fires. Without wait_for_selector we
    # snapshot the interstitial instead of the SERP. Preset/LocaleProfile use
    # pydantic's default extra="ignore", so e.g. a misplaced key (outside
    # request_defaults) would vanish silently instead of raising — this
    # assertion is what stands between the fix and a silent regression.
    #
    # The streaming problem (one live capture carried 2 of 18 results) is
    # handled by wait_until='load', not by a stricter selector: a `.Pager`
    # anchor fails closed on a SERP that fits on one page and renders no pager,
    # costing a rotation of three premium exits and the task ceiling.
    assert out.wait_for_selector == "li.serp-item"
    assert out.wait_until == "load"


class TestFingerprintProfile:
    """A preset carries the profile through `request_defaults`, no code needed.

    `request_defaults` is a free dict validated against ScrapeRequest's field
    names, so the only thing that can go wrong is the name — and that fails as
    a MaterializeError naming the key rather than as a silently ignored option.
    """

    def test_a_preset_can_pin_the_fingerprint_profile(self):
        preset = _amazon_preset(
            request_defaults={
                "browser_engine": "camoufox",
                "fingerprint_profile": "windows_on_host",
            }
        )

        req = materialize(preset, PresetScrapeRequest(source="amazon_product",
                                                      preset_params={"asin": "B0"}))

        assert req.fingerprint_profile == "windows_on_host"

    def test_a_caller_can_override_the_presets_profile(self):
        preset = _amazon_preset(
            request_defaults={
                "browser_engine": "camoufox",
                "fingerprint_profile": "windows_on_host",
            }
        )

        req = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_product", preset_params={"asin": "B0"},
                request_override={"fingerprint_profile": "random"},
            ),
        )

        assert req.fingerprint_profile == "random"

    def test_a_misspelled_key_is_refused_rather_than_ignored(self):
        preset = _amazon_preset(request_defaults={"fingerprint_profil": "host"})

        with pytest.raises(MaterializeError, match="fingerprint_profil"):
            materialize(preset, PresetScrapeRequest(source="amazon_product",
                                                    preset_params={"asin": "B0"}))

    def test_a_caller_spoof_os_supersedes_the_presets_profile(self):
        """The regression this class exists for.

        Both Camoufox builtins state `fingerprint_profile`, and the merge put it
        in the same ScrapeRequest as a caller's `spoof_os`. The conflict
        validator then compared the two literals, so `spoof_os='windows'` beside
        `fingerprint_profile='windows_on_host'` — the same OS — became a 400 on
        /search and /scrape/preset for requests that worked before this branch.

        A caller naming one channel means to replace the other, not to argue
        with it.
        """
        preset = _amazon_preset(
            request_defaults={
                "browser_engine": "camoufox",
                "fingerprint_profile": "windows_on_host",
            }
        )

        for os_name in ("windows", "macos", "linux"):
            req = materialize(
                preset,
                PresetScrapeRequest(
                    source="amazon_product", preset_params={"asin": "B0"},
                    request_override={"spoof_os": os_name},
                ),
            )
            assert req.spoof_os == os_name
            assert req.fingerprint_profile is None, (
                "the preset's profile must step aside, not collide"
            )

    def test_a_caller_profile_supersedes_the_presets_spoof_os(self):
        """The same rule in the other direction, for presets written before
        profiles existed."""
        preset = _amazon_preset(
            request_defaults={"browser_engine": "camoufox", "spoof_os": "macos"}
        )

        req = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_product", preset_params={"asin": "B0"},
                request_override={"fingerprint_profile": "windows_on_host"},
            ),
        )

        assert req.fingerprint_profile == "windows_on_host"
        assert req.spoof_os is None

    def test_a_caller_stating_both_keeps_both(self):
        """Superseding is for the channel the caller did NOT state."""
        preset = _amazon_preset(
            request_defaults={
                "browser_engine": "camoufox",
                "fingerprint_profile": "windows_on_host",
            }
        )

        req = materialize(
            preset,
            PresetScrapeRequest(
                source="amazon_product", preset_params={"asin": "B0"},
                request_override={"spoof_os": "linux", "fingerprint_profile": "linux"},
            ),
        )

        assert req.spoof_os == "linux"
        assert req.fingerprint_profile == "linux"


class TestBingSearchUnwrapsItsLinks:
    """`links` must not ship Bing's click-tracking wrapper.

    Read straight from href, every organic link is
    `https://www.bing.com/ck/a?...&u=a1<base64url>` — pointing at bing.com on
    every row. The field is 100% populated and carries no destination, which no
    fill-rate, row-count or status check can see. Verified live after the fix:
    6/6 rows on us and 5/5 on de resolved to real hosts (pcmag, cnet, nytimes,
    pcwelt, chip), 0 still on bing.com.
    """

    @staticmethod
    def _links_field():
        import json
        import pathlib

        raw = json.loads(pathlib.Path("src/presets/builtin/bing_search_chromium.json").read_text())
        return Preset(**raw).parsing_instructions.fields["links"]

    def test_every_shape_a_link_arrives_in_ends_as_a_destination_or_null(self):
        """Pinned on BEHAVIOUR, not on the op list.

        This used to assert `regex` ran before `base64_decode`. That pinned one
        implementation of the property rather than the property, and the
        implementation had to change: uBlock Origin — which Camoufox loads on
        every launch — rewrites Bing's organic hrefs to their destination before
        we see them, so the field now meets two shapes and the regex nulled the
        sanitized one. What must stay true is only this table.
        """
        from src.extract.extractor import _apply_post_process

        steps = self._links_field().post_process
        wrapped = (
            "https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly93d3cucGNtYWcuY29t&ntb=1"
        )
        cases = {
            # wrapped by Bing -> decoded
            wrapped: "https://www.pcmag.com",
            # already sanitized by uBO -> passed through untouched
            "https://www.pcmag.com/picks/the-best-laptops":
                "https://www.pcmag.com/picks/the-best-laptops",
            # an ad wrapper (`u=` with no `a1`) -> nulled, never shipped as a link
            "https://www.bing.com/aclk?ld=e8&u=aHR0cHM6Ly9hZHMuZXhhbXBsZQ&ntb=1": None,
            # a wrapper whose payload does not decode -> nulled
            "https://www.bing.com/ck/a?!&&p=x&u=a1!!!not-base64!!!&ntb=1": None,
        }
        for href, expected in cases.items():
            assert _apply_post_process(href, steps, "links", [], set()) == expected, href


class TestResolveRedirectsReachesTheRequest:
    """`resolve_redirects` is a ScrapeRequest field, so a preset's
    request_defaults can turn it on and the materializer must carry it through
    untouched — the google_search presets rely on exactly that to get
    followable links back out of Google's `/goto?url=` stubs."""

    def test_google_search_asks_for_its_links_to_be_resolved(self):
        for name in ("google_search_chromium", "google_search_camoufox"):
            req = materialize(_builtin(name), PresetScrapeRequest(source=name, locale="de", preset_params={"query": "x"}))
            assert req.resolve_redirects == ["links"], name

    def test_a_preset_that_says_nothing_gets_none(self):
        req = materialize(_builtin("bing_search_chromium"), PresetScrapeRequest(source="bing_search_chromium", locale="us", preset_params={"query": "x"}))
        assert req.resolve_redirects is None


class TestPresetMetaCarriesLinksStayOnSite:
    """mobile.de's listings are the site's own query-addressed pages on one
    path; the worker skips the self-referential-links guard on the preset's
    say-so, which has to travel with the plan."""

    def test_a_declaring_preset_is_echoed(self):
        req = materialize(_builtin("mobile_de_search_chromium"),
                          PresetScrapeRequest(source="mobile_de_search_chromium", preset_params={"make_id": "3500", "model_id": "20"}))
        assert req.preset_meta.links_stay_on_site is True

    def test_the_default_is_off(self):
        req = materialize(_builtin("bing_search_chromium"),
                          PresetScrapeRequest(source="bing_search_chromium", preset_params={"query": "x"}))
        assert req.preset_meta.links_stay_on_site is False


class TestPresetMetaCarriesTheQuery:
    """`preset_meta.query` is how the worker learns what the caller asked for
    after the page is parsed -- the query-relevance guard reads it."""

    def test_the_rendered_query_is_carried(self):
        req = materialize(_builtin("bing_search_chromium"),
                          PresetScrapeRequest(source="bing_search_chromium", locale="ru", preset_params={"query": "best laptop 2026"}))
        assert req.preset_meta.query == "best laptop 2026"

    def test_the_query_is_the_string_the_url_was_rendered_from(self):
        req = materialize(_builtin("bing_search_chromium"),
                          PresetScrapeRequest(source="bing_search_chromium", preset_params={"query": 2026}))
        assert req.preset_meta.query == "2026"

    def test_only_a_schema_with_result_rows_is_judged(self):
        """The rule was calibrated on a `titles` list. Facets or navigation
        links -- from a caller's parsing_override or a user preset's own
        schema -- are not result rows, whatever the URL asked for."""
        facets = ParsingInstructions(type="css", fields={"facets": FieldRule(selector="a", all=True)})
        req = materialize(_builtin("bing_search_chromium"),
                          PresetScrapeRequest(source="bing_search_chromium", preset_params={"query": "x"}, parsing_override=facets))
        assert req.preset_meta.query is None
        user_preset = _amazon_preset(url_template="https://www.amazon.{domain}/s?k={query}", parsing_instructions=facets)
        req = materialize(user_preset, PresetScrapeRequest(source="amazon_product", preset_params={"query": "x"}))
        assert req.preset_meta.query is None
        rows = ParsingInstructions(type="css", fields={"titles": FieldRule(selector="h2", all=True)})
        req = materialize(_builtin("bing_search_chromium"),
                          PresetScrapeRequest(source="bing_search_chromium", preset_params={"query": "x"}, parsing_override=rows))
        assert req.preset_meta.query == "x", "an override that extracts result rows is judged"

    def test_a_preset_without_a_query_placeholder_carries_none(self):
        """Placeholder-driven, not param-driven: a stray `query` param on a
        product preset was never applied, so it is not what the page answers."""
        req = materialize(_amazon_preset(), PresetScrapeRequest(source="amazon_product", preset_params={"asin": "B0", "query": "ignored"}))
        assert req.preset_meta.query is None


class TestTheExitOverrideNeedsAnExit:
    """`proxy_country` steers the EXIT. On the direct path (proxy_type none, or
    absent -- the schema default) there is none, so the market country is the
    pin (review on #136: `us` with proxy_country GB announced
    en-GB/Europe/London over an Armenian address). The pin itself stays: without
    it Chromium falls back to its device default, en-US/America/New_York, and a
    direct preset's other locales would lose their identity."""

    @staticmethod
    def _preset(proxy_type):
        defaults = {"device": "desktop"} if proxy_type is None else {"device": "desktop", "proxy_type": proxy_type}
        return _amazon_preset(
            request_defaults=defaults,
            locales={
                "us": LocaleProfile(domain="com", country="US", proxy_country="GB"),
                "de": LocaleProfile(domain="de", country="DE"),
            },
        )

    @staticmethod
    def _geo(preset, locale, override=None):
        req = PresetScrapeRequest(source="amazon_product", preset_params={"asin": "X"}, locale=locale,
                                  request_override=override)
        scrape = materialize(preset, req)
        return scrape.proxy_geo.country_code if scrape.proxy_geo else None

    @pytest.mark.parametrize("proxy_type", ["none", None])
    def test_the_direct_path_pins_the_market_not_the_exit_override(self, proxy_type):
        assert self._geo(self._preset(proxy_type), "us") == "US"

    @pytest.mark.parametrize("proxy_type", ["none", None])
    def test_a_direct_locale_without_an_override_keeps_its_identity(self, proxy_type):
        assert self._geo(self._preset(proxy_type), "de") == "DE"

    def test_a_proxied_preset_still_takes_the_override(self):
        assert self._geo(self._preset("prem_res_rotating"), "us") == "GB"

    def test_overriding_a_direct_preset_onto_a_proxy_brings_the_override_back(self):
        assert self._geo(self._preset("none"), "us", {"proxy_type": "prem_res_rotating"}) == "GB"

    def test_an_explicit_geo_on_the_direct_path_is_kept(self):
        assert self._geo(self._preset("none"), "us", {"proxy_geo": {"country_code": "NL"}}) == "NL"

    @pytest.mark.parametrize("name, expected", [
        ("google_search_camoufox", "US"), ("google_shopping_camoufox", "US"),
        ("google_search_chromium", "GB"), ("google_shopping_chromium", "GB"),
    ])
    def test_the_google_twins(self, name, expected):
        preset = _builtin(name)
        scrape = materialize(preset, PresetScrapeRequest(source=preset.name, preset_params={"query": "t"}, locale="us"))
        assert scrape.proxy_geo.country_code == expected
        assert "gl=us" in str(scrape.url)

