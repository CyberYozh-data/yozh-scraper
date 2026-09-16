"""Pydantic models for scraping presets.

A `Preset` bundles three things under one name:
1. A request profile (proxy, device, wait strategy, headers) — `request_defaults`.
2. Per-locale routing (URL template + country/timezone fingerprint) — `locales`.
3. A parsing recipe (deterministic CSS/XPath + optional LLM fallback) —
   `parsing_instructions`, `output_schema`, `prompt_schema`, `llm_extract_prompt`.

`Preset` is the persisted on-disk JSON object (under `data/presets/<name>.json`
for user presets, bundled `src/presets/builtin/*.json` for built-ins). The
materializer (PR-2) turns a `(Preset, params, locale)` tuple into a regular
`ScrapeRequest` that the existing job queue accepts.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.browser.geo_profile import resolve_profile
from src.extract.models import ExtractRule, FieldRule, PostProcess

# Re-export so callers can `from src.presets.models import PostProcess` without
# threading through the extract package.
__all__ = [
    "FieldRule",
    "LocaleProfile",
    "ParserPlan",
    "ParsingInstructions",
    "Preset",
    "PresetKind",
    "PresetMeta",
    "PostProcess",
    "derive_locale_profile",
]


PresetKind = Literal["builtin", "user"]

# Same shape as ExtractRule. Aliasing avoids two parallel definitions while
# letting preset-related code import a name that describes intent.
ParsingInstructions = ExtractRule

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class LocaleProfile(BaseModel):
    """Per-locale request shaping.

    `domain` is the TLD-suffix used by `url_template` (e.g. `de` → `amazon.de`).
    `country` is the ISO 3166-1 alpha-2 **market** code: it fills the
    `url_template`'s `{country}`/`{lang}` placeholders (e.g. `gl=`/`hl=` on a
    search engine) and seeds `derive_locale_profile()`'s locale/timezone/
    accept_language lookup. It does NOT necessarily control the proxy exit —
    see `proxy_country`.

    `proxy_country` is the ISO 3166-1 alpha-2 code for the proxy **exit**
    (and, via `proxy_geo`, the browser's timezone/fingerprint — see
    `src/browser/runner.py`'s `resolve_profile(proxy_geo.country_code, ...)`).
    Defaults to `country` when unset, which is the common case where market
    and exit coincide. Set it explicitly when they must diverge: e.g. Google
    hard-blocks the proxy pool's US residential range with a 429 "unusual
    traffic" response, so `google_search`'s `us` locale wants the US market
    (`gl=us`) but must exit through a different country (GB).

    `locale`, `timezone`, `accept_language` may be omitted —
    `derive_locale_profile()` fills them from `src/browser/geo_profile.py`
    using `country` (the market) as the lookup key.
    """

    domain: str
    country: str
    proxy_country: str | None = None
    locale: str | None = None
    timezone: str | None = None
    accept_language: str | None = None
    # Optional search-engine region code exposed to `url_template` as `{lr}`.
    # Used by yandex_search (Yandex's own `lr` region id, e.g. 225=Russia,
    # 213=Moscow) to localise results independently of the proxy exit country.
    lr: str | None = None


def derive_locale_profile(loc: LocaleProfile) -> LocaleProfile:
    """Return a copy with locale/timezone/accept_language filled from
    `geo_profile.resolve_profile(country)` where the user left them blank.

    Unknown countries leave the optional fields as-is (which means `None`
    propagates and the caller can fall back to a default).
    """
    geo = resolve_profile(loc.country)
    if geo is None:
        return loc
    return loc.model_copy(
        update={
            "locale": loc.locale or geo.locale,
            "timezone": loc.timezone or geo.timezone_id,
            "accept_language": loc.accept_language or geo.accept_language,
        }
    )


class Preset(BaseModel):
    """A named scraping recipe persisted as a single JSON document."""

    name: str
    source: str
    description: str = ""
    kind: PresetKind = "user"
    url_template: str | None = None
    request_defaults: dict[str, Any] = Field(default_factory=dict)
    locales: dict[str, LocaleProfile] = Field(default_factory=dict)
    default_locale: str = "us"
    parsing_instructions: ParsingInstructions | None = None
    output_schema: dict[str, Any] | None = None
    self_heal: bool = True
    links_stay_on_site: bool = Field(
        default=False,
        description=(
            "The extracted URLs are the site's own pages -- query-addressed "
            "detail pages such as mobile.de's /fahrzeuge/details.html?id=... -- "
            "so the worker's self-referential-links guard must not read them as "
            "an unwrapped redirect. Leave unset for engines and marketplaces "
            "whose results link out or give every row its own path."
        ),
    )
    prompt_schema: dict[str, Any] | None = None
    llm_extract_prompt: str | None = None
    version: int = 1
    updated_at: float

    @field_validator("name", "source")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError(
                f"must be lowercase snake_case, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _default_locale_known(self) -> Preset:
        if self.locales and self.default_locale not in self.locales:
            raise ValueError(
                f"default_locale={self.default_locale!r} is not in "
                f"locales={list(self.locales)}"
            )
        return self


class PresetMeta(BaseModel):
    """Echo of which preset / locale / version was applied to a scrape.

    Returned on `ScrapeResponse.meta` so callers can confirm what the server
    actually used after preset materialization.
    """

    name: str
    source: str
    locale: str | None = None
    version: int = 1
    query: str | None = Field(
        default=None,
        description=(
            "The search query the URL was rendered with: the preset's `{query}` "
            "placeholder, as a string. The worker judges the parsed page against "
            "it (the serp_query_mismatch warning). None for presets without that "
            "placeholder or without a `titles` list to judge, and for stored "
            "requests that predate this field."
        ),
    )
    links_stay_on_site: bool = Field(
        default=False,
        description=(
            "Echo of the preset's `links_stay_on_site`: the worker skips the "
            "self-referential-links guard for a preset whose results are the "
            "site's own pages."
        ),
    )


class ParserPlan(BaseModel):
    """LLM-side parsing config carried alongside ScrapeRequest.extract.

    The deterministic selectors travel in ScrapeRequest.extract; this carries
    the bits the worker needs for self-heal / AI-only extraction. `llm_model`
    is None when no LLM is configured, which disables every LLM step. The
    preset identity lets the worker persist self-healed selectors back to a
    user preset (built-ins are read-only and only logged).
    """

    self_heal: bool = False
    llm_model: str | None = None
    output_schema: dict[str, Any] | None = None
    llm_extract_prompt: str | None = None
    preset_name: str | None = None
    preset_kind: PresetKind | None = None
    preset_stamp: str | None = Field(
        default=None,
        description=(
            "Fingerprint of the stored preset AS THIS JOB READ IT. The worker "
            "writes healed selectors only while the file still matches, so an "
            "edit its owner saved during the minutes this job spent rendering "
            "is not overwritten by a language model's guess. None on a "
            "built-in (never written) and on a job enqueued before this "
            "field existed, both of which keep the old unconditional write."
        ),
    )
    instructions_from_override: bool = Field(
        default=False,
        description=(
            "True when the request replaced the preset's parsing_instructions "
            "with its own `parsing_override`. A self-heal on that contract "
            "describes the caller's fields, not the preset's, so it is never "
            "persisted to the preset."
        ),
    )
    materializer_injected: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Which post_process args the materializer filled in for THIS "
            "request (op name -> field names), e.g. {'urljoin': ['urls']}. "
            "Lets a self-heal strip exactly these back to empty before "
            "persisting to a user preset, so the next materialize() call "
            "re-injects a fresh, request-appropriate value instead of the "
            "one baked in here."
        ),
    )
