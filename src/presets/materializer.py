"""Turn a (Preset, PresetScrapeRequest) pair into a plain ScrapeRequest.

This runs on the API layer (B1 in the design): the preset is fully expanded
into a normal ScrapeRequest *before* it enters the existing job queue, so the
worker stays preset-agnostic. The deterministic parser already runs in the
worker via ScrapeRequest.extract — no separate parser channel is needed for
PR-2. Self-heal / AI-only execution is PR-3 and only carried as metadata here.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote, quote_plus

from pydantic import BaseModel, Field, ValidationError

from src.presets.models import (
    ParserPlan,
    ParsingInstructions,
    Preset,
    PresetMeta,
    derive_locale_profile,
)
from src.schemas import ScrapeRequest

log = logging.getLogger(__name__)

# What ScrapeRequest resolves an absent proxy_type to -- read from the schema so
# the two cannot drift.
_DEFAULT_PROXY_TYPE = ScrapeRequest.model_fields["proxy_type"].default  # pylint: disable=unsubscriptable-object

_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Countries that write prices with a comma decimal separator ("12,34").
# `parse_price` reads the separator from the text since 2026-09-05, so the
# arg this set feeds changes no parse; the injection stays only so stored
# presets and older callers keep validating. Explicit args on a step still
# win. Removing the whole path is a follow-up.
_COMMA_DECIMAL_COUNTRIES = frozenset(
    {
        "DE", "FR", "ES", "IT", "NL", "BE", "AT", "PT", "FI", "SE", "NO",
        "DK", "PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "LT", "LV",
        "EE", "GR", "RU", "UA", "TR", "BR", "AR", "CL", "CO", "ID", "VN",
    }
)


def _price_locale_for(country: str) -> str:
    return "eu" if country.upper() in _COMMA_DECIMAL_COUNTRIES else "us"


def _inject_price_locale(
    instructions: "ParsingInstructions", price_locale: str
) -> tuple["ParsingInstructions", list[str]]:
    """Fill empty parse_price args with the market's locale tag (accepted and
    ignored by the op). Explicit args set by the preset author are left untouched.

    Returns the (possibly unchanged) instructions AND the field names that
    were actually injected -- the caller records this on `ParserPlan.
    materializer_injected` so a later self-heal can strip exactly these
    values back to empty before persisting (see `strip_materializer_injected`
    below); a self-heal that persisted this request's injected locale would
    freeze it into the preset forever, silently wrong for every other market.
    """
    new_fields = {}
    injected_fields: list[str] = []
    for fname, frule in instructions.fields.items():
        new_pp = []
        field_injected = False
        for step in frule.post_process:
            if step.op == "parse_price" and not step.args:
                new_pp.append(step.model_copy(update={"args": [price_locale]}))
                field_injected = True
            else:
                new_pp.append(step)
        if field_injected:
            injected_fields.append(fname)
        new_fields[fname] = (
            frule.model_copy(update={"post_process": new_pp})
            if new_pp != list(frule.post_process)
            else frule
        )
    if not injected_fields:
        return instructions, []
    return instructions.model_copy(update={"fields": new_fields}), injected_fields


def inject_url_base(
    instructions: "ParsingInstructions", base_url: str
) -> tuple["ParsingInstructions", list[str]]:
    """Fill empty `urljoin` args with the URL this request is about to fetch.

    extract_fields (src/extract/extractor.py) takes only page_html — it is
    never given the page's URL, navigated or otherwise — so a relative href
    (Amazon serves every search-result href relative) cannot be made absolute
    inside the extractor itself. Threading the *navigated* URL all the way
    down through worker_parse -> parser_pipeline -> extract_fields would touch
    three modules' signatures and the self-heal path for a fix scoped to one
    preset. Instead this mirrors `_inject_price_locale`: the materializer
    already computes the exact URL the fetch is about to use (`url`, above,
    built from the same locale/domain the preset resolves), and injects it as
    `urljoin`'s base at the one place both are in scope together. Explicit
    args set by the preset author are left untouched.

    Public (no leading underscore): `src/presets/service.py`'s preview()/
    test() also call this directly, with `req.sample_url` as the base, so
    those paths predict the same absolute urls production will return
    instead of silently no-op'ing where a URL was available all along.

    Returns the (possibly unchanged) instructions AND the field names that
    were actually injected -- see `_inject_price_locale`'s docstring for why
    the caller needs this (the self-heal-freezes-the-base bug).
    """
    new_fields = {}
    injected_fields: list[str] = []
    for fname, frule in instructions.fields.items():
        new_pp = []
        field_injected = False
        for step in frule.post_process:
            if step.op == "urljoin" and not step.args:
                new_pp.append(step.model_copy(update={"args": [base_url]}))
                field_injected = True
            else:
                new_pp.append(step)
        if field_injected:
            injected_fields.append(fname)
        new_fields[fname] = (
            frule.model_copy(update={"post_process": new_pp})
            if new_pp != list(frule.post_process)
            else frule
        )
    if not injected_fields:
        return instructions, []
    return instructions.model_copy(update={"fields": new_fields}), injected_fields


def strip_materializer_injected(
    instructions: "ParsingInstructions", injected: dict[str, list[str]]
) -> "ParsingInstructions":
    """Undo `_inject_price_locale`/`inject_url_base` before a self-heal is
    persisted to a user preset.

    `_under_original_contract` (parser_pipeline.py) builds the instructions a
    self-heal writes back by copying `post_process` VERBATIM from the
    already-materialized instructions -- which, for any op an `_inject_*`
    helper filled in for THIS ONE request, means the value baked in belongs
    to this request's locale/URL, not to the preset. Persisting it as-is
    freezes that one request's value forever: the next materialize() call
    sees non-empty args and skips injection (by design -- an author's
    explicit args must never be overwritten), so every later request, in
    every locale, silently resolves against a stale, request-specific value
    -- a plausible-looking absolute URL on the wrong domain, or a price
    locale that stops matching the market. If a user preset's `url_template`
    ever carried a credential in its query string, that credential would be
    the thing frozen in and served back by the unauthenticated
    `GET /api/v1/presets/{name}`.

    `injected` names exactly the (op -> [field, ...]) pairs a materialize()
    call actually filled in, which is the only source that can tell an
    injected value apart from an author's explicit one -- they are
    indistinguishable by content alone once both have round-tripped through
    the same `PostProcess.args` list. Blanking those specific args back to
    `[]` restores the "not yet injected" state the preset's author actually
    wrote, so the very next materialize() call (for whatever locale/URL that
    request uses) re-injects a fresh, correct value.
    """
    if not injected:
        return instructions
    new_fields = {}
    changed = False
    for fname, frule in instructions.fields.items():
        target_ops = {op for op, fields in injected.items() if fname in fields}
        if not target_ops:
            new_fields[fname] = frule
            continue
        new_pp = [
            step.model_copy(update={"args": []}) if step.op in target_ops else step
            for step in frule.post_process
        ]
        if new_pp != list(frule.post_process):
            changed = True
            new_fields[fname] = frule.model_copy(update={"post_process": new_pp})
        else:
            new_fields[fname] = frule
    if not changed:
        return instructions
    return instructions.model_copy(update={"fields": new_fields})


class MaterializeError(ValueError):
    """Preset could not be turned into a valid ScrapeRequest (missing
    template params, unknown locale, request_defaults that don't fit
    ScrapeRequest). API maps this to HTTP 400."""


class SessionConflictError(MaterializeError):
    """session_id given via both the field and request_override with different values.

    API maps this to HTTP 422 (vs 400 for other MaterializeError).
    """


class LLMConfig(BaseModel):
    model: str = "anthropic/claude-haiku-4-5-20251001"
    temperature: float = 0.0
    max_tokens: int = 4000


class PresetScrapeRequest(BaseModel):
    source: str = Field(..., description="Preset name to apply.")
    preset_params: dict[str, Any] = Field(
        default_factory=dict,
        description="Values for url_template placeholders, e.g. {'asin': 'B0..'}.",
    )
    locale: str | None = Field(
        default=None,
        description="Locale key from the preset's `locales`; preset default if unset.",
    )
    parsing_override: ParsingInstructions | None = Field(
        default=None,
        description="Replace the preset's parsing_instructions for this call.",
    )
    schema_override: dict[str, Any] | None = Field(
        default=None,
        description="User JSON schema; consumed by the LLM path (PR-3).",
    )
    self_heal: bool | None = Field(
        default=None,
        description="Override preset.self_heal for this call (PR-3).",
    )
    llm: LLMConfig | None = Field(
        default=None,
        description="LLM model/params for self-heal or AI-only extraction (PR-3).",
    )
    request_override: dict[str, Any] | None = Field(
        default=None,
        description="Advanced: override any ScrapeRequest field after the preset.",
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Run this preset scrape inside a previously authenticated "
            "server-side session. Validated against the *materialized* "
            "request's device/proxy/geo: 404 if unknown, 410 if expired, "
            "422 if incompatible. If also given via request_override, the "
            "two must be equal (else 422)."
        ),
    )


def _placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def _extracts_result_rows(instructions: ParsingInstructions | None) -> bool:
    """The query-relevance rule was calibrated on result rows: a `titles` list.

    A schema without one -- facets, navigation, a caller's parsing_override, an
    AI-only preset -- is not judged, whatever its URL asked for.
    """
    if instructions is None:
        return False
    rule = instructions.fields.get("titles")
    return rule is not None and rule.all


def _render_template(template: str, variables: dict[str, str]) -> str:
    """Substitute {placeholders} with context-aware percent-encoding.

    Placeholders before the first '?' are URL path segments and use strict
    RFC-3986 encoding (space -> %20). Placeholders in the query string use
    application/x-www-form-urlencoded (space -> +). Values are raw here and
    encoded per-position so a single quoting strategy can't corrupt a path.
    """
    missing = sorted(_placeholders(template) - variables.keys())
    if missing:
        raise MaterializeError(f"missing template params: {missing}")

    query_start = template.find("?")

    def _sub(match: re.Match[str]) -> str:
        raw = str(variables[match.group(1)])
        in_query = query_start != -1 and match.start() > query_start
        return quote_plus(raw) if in_query else quote(raw, safe="")

    return _PLACEHOLDER.sub(_sub, template)


def materialize(
    preset: Preset, req: PresetScrapeRequest, *, preset_stamp: str | None = None
) -> ScrapeRequest:
    locale_key = req.locale or preset.default_locale
    locale = preset.locales.get(locale_key)
    if locale is None:
        raise MaterializeError(
            f"unknown locale {locale_key!r}; preset offers "
            f"{sorted(preset.locales)}"
        )
    locale = derive_locale_profile(locale)

    lang = (
        locale.locale.split("-")[0].lower()
        if locale.locale
        else locale.country.lower()
    )
    template_vars: dict[str, str] = {
        "domain": locale.domain,
        "country": locale.country.lower(),
        "lang": lang,
    }
    if locale.lr is not None:
        template_vars["lr"] = locale.lr
    # preset_params last so a caller can override a locale-derived value
    # (e.g. pass a custom Yandex `lr` region).
    for key, value in req.preset_params.items():
        template_vars[key] = str(value)

    if not preset.url_template:
        raise MaterializeError(f"preset {preset.name!r} has no url_template")
    url = _render_template(preset.url_template, template_vars)

    merged: dict[str, Any] = dict(preset.request_defaults)
    if req.request_override:
        # `spoof_os` and `fingerprint_profile` are two ways to name the same
        # thing, so a caller who states one of them means to REPLACE whichever
        # the preset stated — not to be told the two disagree. Without this,
        # `spoof_os` on a preset carrying `fingerprint_profile` (both Camoufox
        # builtins do) is a 400 for what is plainly an override.
        for stated, superseded in (
            ("spoof_os", "fingerprint_profile"),
            ("fingerprint_profile", "spoof_os"),
        ):
            if stated in req.request_override and superseded not in req.request_override:
                merged.pop(superseded, None)
        merged.update(req.request_override)

    unknown = set(merged) - set(ScrapeRequest.model_fields)
    # url/extract/proxy_geo/preset_meta are set by us, not via request_defaults
    unknown -= {"url", "extract", "proxy_geo", "preset_meta"}
    if unknown:
        raise MaterializeError(
            f"request_defaults/request_override has keys not on ScrapeRequest: "
            f"{sorted(unknown)}"
        )

    # Reject an ambiguous request rather than silently preferring one
    # session_id channel (first-class field vs request_override) over the
    # other.
    override_sid = merged.get("session_id")
    if (
        req.session_id is not None
        and override_sid is not None
        and req.session_id != override_sid
    ):
        raise SessionConflictError(
            f"session_id conflict: request field {req.session_id!r} != "
            f"request_defaults/request_override {override_sid!r}"
        )
    resolved_sid = req.session_id if req.session_id is not None else override_sid
    if resolved_sid is not None:
        merged["session_id"] = resolved_sid

    direct = merged.get("proxy_type", _DEFAULT_PROXY_TYPE) == "none"
    if "proxy_geo" not in merged:
        # `proxy_country` is an EXIT override -- where to leave from when the
        # market's own range is refused. With no proxy there is no exit to
        # steer, so on the direct path the market's own country is the pin
        # (review on #136: `us` with proxy_country GB announced
        # en-GB/Europe/London over an Armenian address). The pin itself stays:
        # without one Chromium does not fall back to "the host" but to its
        # device default, en-US/America/New_York, and a direct preset's other
        # locales would all collapse onto it.
        if direct and locale.proxy_country and locale.proxy_country != locale.country:
            log.info(
                "preset %r: locale exit override proxy_country=%s does not apply "
                "on the direct path; pinning the market country %s",
                preset.name, locale.proxy_country, locale.country,
            )
        merged["proxy_geo"] = {
            "country_code": locale.country if direct else (locale.proxy_country or locale.country)
        }
    else:
        # Escape hatch: a preset/request can pin proxy_geo independent of the
        # locale's country. Legitimate but easy to get wrong, so make the
        # decoupling visible rather than silent.
        override_geo = merged["proxy_geo"]
        override_cc = (
            override_geo.get("country_code")
            if isinstance(override_geo, dict)
            else getattr(override_geo, "country_code", None)
        )
        log.info(
            "preset %r: proxy_geo pinned to %s by request_defaults/override, "
            "decoupled from the locale's %s %s (market country %s)",
            preset.name,
            override_cc,
            "market" if direct else "exit",
            locale.country if direct else (locale.proxy_country or locale.country),
            locale.country,
        )

    instructions = req.parsing_override or preset.parsing_instructions
    # Records exactly which (op, field) pairs this call injected -- carried on
    # ParserPlan so a later self-heal can strip them back to empty before
    # persisting (see strip_materializer_injected's docstring for why: an
    # injected value is specific to THIS request's locale/URL, and freezing
    # it into a user preset silently breaks every other locale/URL forever).
    materializer_injected: dict[str, list[str]] = {}
    if instructions is not None:
        instructions, price_injected = _inject_price_locale(
            instructions, _price_locale_for(locale.country)
        )
        if price_injected:
            materializer_injected["parse_price"] = price_injected
        instructions, url_injected = inject_url_base(instructions, url)
        if url_injected:
            materializer_injected["urljoin"] = url_injected
    else:
        # AI-only preset: no deterministic parser. Force raw_html so the PR-3
        # LLM step has the page to work with.
        merged.setdefault("raw_html", True)

    preset_meta = PresetMeta(
        name=preset.name,
        source=preset.source,
        locale=locale_key,
        version=preset.version,
        # The worker judges the parsed page against this (query_relevance_warning):
        # a search, by its URL, that extracts result rows.
        query=(
            template_vars["query"]
            if "query" in _placeholders(preset.url_template) and _extracts_result_rows(instructions)
            else None
        ),
        links_stay_on_site=preset.links_stay_on_site,
    )

    self_heal = req.self_heal if req.self_heal is not None else preset.self_heal
    parser_plan = ParserPlan(
        self_heal=self_heal,
        # No llm config -> model None -> every LLM step is disabled.
        llm_model=req.llm.model if req.llm else None,
        output_schema=req.schema_override or preset.output_schema,
        llm_extract_prompt=preset.llm_extract_prompt,
        preset_name=preset.name,
        preset_kind=preset.kind,
        preset_stamp=preset_stamp,
        instructions_from_override=req.parsing_override is not None,
        materializer_injected=materializer_injected,
    )

    try:
        return ScrapeRequest(
            url=url,
            extract=instructions,
            preset_meta=preset_meta,
            parser_plan=parser_plan,
            **merged,
        )
    except ValidationError as exc:
        raise MaterializeError(
            f"preset {preset.name!r} produced an invalid ScrapeRequest: {exc}"
        ) from exc
