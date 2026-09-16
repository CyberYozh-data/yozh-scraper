"""Business logic for preset management, generation, testing and preview.

All domain exceptions are plain Python exceptions — no HTTPException.
The API layer (src/api/presets.py) is a thin wrapper that catches these
per-route and maps them to HTTP responses.
"""
from __future__ import annotations

import logging
import time
from typing import Literal

import httpx
from pydantic import ValidationError

from src.presets.exceptions import PresetValidationError, SampleFetchError
from src.presets.llm.client import LLMError
from src.presets.llm.schema_gen import infer_schema
from src.presets.llm.selector_gen import generate_selectors
from src.presets.materializer import inject_url_base
from src.presets.models import ParsingInstructions, Preset
from src.presets.parser_pipeline import run as run_pipeline
from src.presets.requests import (
    PresetGenerateRequest,
    PresetPreviewRequest,
    PresetTestRequest,
)
from src.schemas import ProxyGeo, ScrapeRequest
from src.presets.store import (
    USER_PREFIX,
    PresetAlreadyExists,
    PresetNameInvalid,
    PresetNotFound,
    PresetStore,
)
from src.security.egress import EGRESS_BLOCKED_ERROR, EgressBlocked, assert_navigable
from src.settings import settings

log = logging.getLogger(__name__)

_SAMPLE_FETCH_TIMEOUT_S = 30.0
_MAX_REDIRECTS = 5


async def _assert_public_url(url: str) -> None:
    """SSRF guard for the sample fetch — an adapter over the shared policy.

    The name survives so `src/api/presets.py`'s SampleFetchError -> HTTP 400
    mapping is unchanged, but the predicate now lives in `src/security/egress`
    alongside the one the browser paths use. The local copy this replaces
    failed open on an empty resolver answer and raised `ValueError` (an HTTP
    500) on an unparseable one; both are fixed by the move, and the sample
    fetch can no longer drift away from the navigation guard.
    """
    try:
        await assert_navigable(url, resolve=True)
    except EgressBlocked as exc:
        raise SampleFetchError(EGRESS_BLOCKED_ERROR) from exc


async def _fetch_sample_html(url: str) -> str:
    """Fetch a sample page for preset generation/testing.

    Redirects are followed manually so every hop is SSRF-revalidated.
    Raises SampleFetchError on any network/SSRF failure.
    """
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=_SAMPLE_FETCH_TIMEOUT_S, follow_redirects=False
        ) as http:
            for _ in range(_MAX_REDIRECTS + 1):
                await _assert_public_url(current)
                resp = await http.get(current, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        break
                    current = str(httpx.URL(current).join(location))
                    continue
                resp.raise_for_status()
                return resp.text
        raise SampleFetchError("too many redirects fetching sample")
    except SampleFetchError:
        raise
    except httpx.HTTPError as exc:
        raise SampleFetchError(f"sample fetch failed: {exc}") from exc


class PresetService:
    def list_presets(
        self,
        kind: Literal["builtin", "user"] | None,
        source: str | None,
        store: PresetStore,
    ) -> list[Preset]:
        return store.list(kind=kind, source=source)

    def list_llm_models(self) -> dict:
        available: list[str] = []
        if settings.openai_api_key:
            available += ["openai/gpt-5.4-mini", "openai/gpt-5.1"]
        if settings.anthropic_api_key:
            available += ["anthropic/claude-haiku-4-5-20251001"]
        if settings.gemini_api_key:
            available += ["gemini/gemini-2.0-flash"]
        if settings.openrouter_api_key:
            available += ["openrouter/auto"]
        if settings.custom_llm_base_url:
            available += ["custom/default"]
        return {"default": settings.default_llm_model, "available": available}

    def get(self, name: str, store: PresetStore) -> Preset:
        return store.get(name)

    def create(self, preset: Preset, store: PresetStore) -> Preset:
        _validate_request_defaults(preset)
        return store.create(preset)

    def update(self, name: str, preset: Preset, store: PresetStore) -> Preset:
        # Existence first, so a missing target still answers 404 rather than
        # 422: the name is about the target, the profile is about the payload,
        # and a caller testing for existence by status code should not have to
        # read the body. (`create` validates first -- there the name error and
        # the profile error are both about the payload it just sent.)
        #
        # `exists`, never `get`: `get` deserialises, and `_read_preset_file`
        # returns None for a file whose JSON is broken or whose shape an older
        # version wrote -- so reading it here would answer 404 for a preset that
        # is sitting right there, and PUT is exactly how such a file gets
        # repaired.
        if not store.builtin.exists(name) and not store.user.exists(name):
            raise PresetNotFound(name)
        _validate_request_defaults(preset)
        return store.update(name, preset)

    def delete(self, name: str, store: PresetStore) -> None:
        store.delete(name)

    async def _get_sample_html(
        self,
        req: PresetGenerateRequest | PresetTestRequest | PresetPreviewRequest,
    ) -> str:
        if req.sample_html:
            return req.sample_html
        if req.sample_url:
            return await _fetch_sample_html(req.sample_url)
        raise PresetValidationError("sample_url or sample_html is required")

    async def generate(
        self, req: PresetGenerateRequest, store: PresetStore
    ) -> Preset:
        if req.mode == "manual":
            if not req.preset:
                raise PresetValidationError("manual mode requires 'preset'")
            try:
                preset = Preset.model_validate(req.preset)
            except ValidationError as exc:
                raise PresetValidationError(str(exc)) from exc
            # The third write path: this one stores a caller-supplied preset
            # whole, so without the guard an impossible profile refused by
            # `POST /presets` walks in through here.
            _validate_request_defaults(preset)
            return store.create(preset)

        if not req.source:
            raise PresetValidationError(f"{req.mode} mode requires 'source'")

        # Reject doomed names before fetching/LLM so we don't burn tokens.
        if store.builtin.exists(req.name):
            raise PresetAlreadyExists(req.name)
        if not req.name.startswith(USER_PREFIX):
            raise PresetNameInvalid(
                f"user-defined preset name must start with {USER_PREFIX!r}: "
                f"got {req.name!r}"
            )
        if store.user.exists(req.name):
            raise PresetAlreadyExists(req.name)

        html = await self._get_sample_html(req)
        model = req.llm_model or settings.default_llm_model

        try:
            if req.mode == "from_prompt":
                if not req.description:
                    raise PresetValidationError(
                        "from_prompt mode requires 'description'"
                    )
                out_schema = await infer_schema(html, req.description, model)
            else:  # from_schema
                if not req.schema_:
                    raise PresetValidationError("from_schema mode requires 'schema'")
                out_schema = req.schema_
            instructions = await generate_selectors(html, out_schema, model)
        except LLMError as exc:
            log.warning("preset generate LLM call failed model=%s: %s", model, exc)
            raise

        preset = Preset(
            name=req.name,
            source=req.source,
            kind="user",
            request_defaults={},
            locales={},
            default_locale="us",
            parsing_instructions=instructions,
            output_schema=out_schema,
            updated_at=time.time(),
        )
        return store.create(preset)

    async def test(
        self, name: str, req: PresetTestRequest, store: PresetStore
    ) -> dict:
        preset = store.get(name)
        html = await self._get_sample_html(req)
        instructions = preset.parsing_instructions
        # Same base a real scrape's materialize() would inject (see
        # materializer.inject_url_base) -- without this, testing a preset
        # whose urls use `urljoin` reports them relative even though the
        # sample was fetched from a known URL, and production returns them
        # absolute. `sample_html`-only calls have no URL to inject, and
        # correctly fall through to urljoin's own no-base warning.
        if instructions is not None and req.sample_url:
            instructions, _ = inject_url_base(instructions, req.sample_url)
        result = await run_pipeline(
            html,
            instructions,
            self_heal=req.self_heal,
            llm_model=req.llm_model,
            output_schema=preset.output_schema,
            llm_extract_prompt=preset.llm_extract_prompt,
        )
        return {
            "extracted": result.data,
            "warnings": result.warnings,
            "mode": result.mode,
        }

    async def preview(self, req: PresetPreviewRequest) -> dict:
        html = await self._get_sample_html(req)
        output_schema = req.output_schema
        instructions = None

        if req.parsing_instructions is not None:
            try:
                instructions = ParsingInstructions.model_validate(
                    req.parsing_instructions
                )
            except ValidationError as exc:
                raise PresetValidationError(str(exc)) from exc
        else:
            if req.mode == "manual":
                raise PresetValidationError(
                    "manual mode requires 'parsing_instructions'"
                )
            model = req.llm_model or settings.default_llm_model
            try:
                if req.mode == "from_prompt":
                    if not req.description:
                        raise PresetValidationError(
                            "from_prompt mode requires 'description'"
                        )
                    output_schema = await infer_schema(html, req.description, model)
                else:  # from_schema
                    if not req.schema_:
                        raise PresetValidationError(
                            "from_schema mode requires 'schema'"
                        )
                    output_schema = req.schema_
                instructions = await generate_selectors(html, output_schema, model)
            except LLMError as exc:
                log.warning(
                    "preset preview LLM call failed model=%s: %s", model, exc
                )
                raise

        # Same reasoning as PresetService.test(): predict what a real
        # materialize() would inject for `urljoin`'s base, so preview does
        # not report relative urls for instructions that will resolve
        # absolute in production. Injected into a SEPARATE variable, never
        # into `instructions` itself: `instructions` is what gets returned
        # below as `parsing_instructions`, which exists precisely so a caller
        # can persist it (the only way to retrieve LLM-generated instructions
        # in from_prompt/from_schema mode) -- reassigning `instructions` to
        # the injected copy would echo `sample_url` (which may itself carry a
        # credential in its query string) back in the response, and a
        # preview -> create round-trip would freeze THIS request's base into
        # the stored preset forever, exactly the bug self-heal persistence
        # was just fixed for, one file away.
        pipeline_instructions = instructions
        if pipeline_instructions is not None and req.sample_url:
            pipeline_instructions, _ = inject_url_base(
                pipeline_instructions, req.sample_url
            )

        result = await run_pipeline(
            html,
            pipeline_instructions,
            self_heal=req.self_heal,
            llm_model=req.llm_model,
            output_schema=output_schema,
            llm_extract_prompt=req.llm_extract_prompt,
        )
        return {
            "parsing_instructions": (
                instructions.model_dump(mode="json") if instructions else None
            ),
            "output_schema": output_schema,
            "extracted": result.data,
            "warnings": result.warnings,
            "mode": result.mode,
        }


preset_service = PresetService()


# A url is required to build the request and is never used: only the request
# profile is under test. `.invalid` is reserved by RFC 2606, so the string
# cannot collide with a real target even if it reached a log.
_DEFAULTS_PROBE_URL = "https://preset-defaults.invalid/"

# `materialize()` passes these to `ScrapeRequest` as explicit keyword arguments
# (materializer.py), so the same key in `request_defaults` is a TypeError there
# -- not a MaterializeError, so not a 400 either, but a 500 on every scrape off
# the preset. `proxy_geo` is deliberately absent: it reaches the request through
# the merged dict, and a preset may legitimately pin it.
_MATERIALIZER_OWNED_KEYS = ("url", "extract", "preset_meta", "parser_plan")


def _validate_request_defaults(preset: Preset) -> None:
    """Refuse a saved preset the scraper could never run.

    `request_defaults` is a free `dict[str, Any]` and went to the store
    unexamined, while `ScrapeRequest`'s cross-field rules only run when
    `materializer.py` builds the request at scrape time. So an impossible
    profile -- `device='mobile'` with `browser_engine='firefox'`, a
    camoufox-only knob on chromium, conflicting `spoof_os` -- saved cleanly and
    then failed EVERY run off that preset, which is the worst place to learn it:
    the recipe looks wrong while the profile is what is broken. The
    materializer's own check compares key NAMES against
    `ScrapeRequest.model_fields` and never the values.

    A dry construction rather than a full `materialize()`: materialising needs a
    locale and whatever params the `url_template` wants, so it would refuse
    legitimate presets. Unknown keys stay the materializer's business -- pydantic
    ignores them here, so this adds no new refusal for them. One profile IS newly
    unsaveable: `spoof_os` together with a conflicting `fingerprint_profile`,
    which `/search` can still make work by overriding one of the two. It is
    broken for every `/scrape/preset` call that does not override, so refusing it
    is the lesser surprise.

    Here rather than on the `Preset` model itself, because `_read_preset_file`
    drops a preset whose model refuses to validate: a model-level rule would
    make an already-stored bad preset vanish from `GET /presets` -- unfixable
    instead of correctable -- and could fail the self-heal write in
    `worker_parse.py`, which must never be blocked by a legacy profile.
    """
    defaults = dict(preset.request_defaults)
    owned = [key for key in _MATERIALIZER_OWNED_KEYS if key in defaults]
    if owned:
        log.warning("preset %r refused: request_defaults sets %s", preset.name, owned)
        raise PresetValidationError(
            f"preset {preset.name!r} request_defaults may not set "
            f"{owned}: the scraper supplies those per request"
        )
    # `model_validate` of a dict, not `ScrapeRequest(url=..., **defaults)`: the
    # kwargs form raises TypeError (not ValidationError) on a duplicate key, and
    # a TypeError here is a 500 on a save that used to succeed.
    try:
        ScrapeRequest.model_validate({**defaults, "url": _DEFAULTS_PROBE_URL})
    except ValidationError as exc:
        log.warning(
            "preset %r refused: request_defaults invalid (%s)",
            preset.name, _summarise(exc),
        )
        raise PresetValidationError(
            f"preset {preset.name!r} request_defaults would not build a valid "
            f"scrape request: {_summarise(exc)}"
        ) from exc

    # `LocaleProfile.country` is a free string and the materializer derives
    # `proxy_geo.country_code` from it, where ProxyGeo demands two letters. A
    # three-letter country saved cleanly and then killed every scrape.
    for name, locale in preset.locales.items():
        try:
            ProxyGeo(country_code=locale.country)
        except ValidationError as exc:
            raise PresetValidationError(
                f"preset {preset.name!r} locale {name!r} country "
                f"{locale.country!r} is not usable as proxy_geo.country_code: "
                f"{_summarise(exc)}"
            ) from exc


def _summarise(exc: ValidationError) -> str:
    """Field and reason, never the value.

    pydantic's `str(exc)` carries `input_value=`, which here is the caller's own
    `request_defaults` -- proxy pool ids, headers and cookies included. The
    policy this repo settled on after a name-based denylist failed open
    (`utils/redaction.py`) is to mask the values and keep the names.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in exc.errors()
    )
