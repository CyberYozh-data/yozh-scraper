"""Three-tier parsing: deterministic -> self-heal -> AI-only.

1. Run the preset's CSS/XPath instructions. If every required field is
   populated, done (cheap path, no LLM).
2. If required fields are missing and self_heal + an LLM model are
   available, ask the model to regenerate selectors from the live HTML and
   re-extract. On success the healed instructions are returned so the caller
   can persist them.
3. If there are no instructions at all (AI-only preset) and an LLM model +
   schema/prompt are available, extract directly with the model.

This module does no I/O: persistence of healed instructions is the caller's
job (the worker), which keeps the pipeline unit-testable without a store.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from src.extract.extractor import extract_fields
from src.presets.llm.client import LLMError
from src.presets.llm.extract import llm_extract
from src.presets.llm.selector_gen import generate_selectors
from src.presets.models import ParsingInstructions

log = logging.getLogger(__name__)

Mode = Literal["deterministic", "self_healed", "llm_extracted", "none"]


@dataclass
class ParserResult:
    data: dict[str, Any] | None
    warnings: list[str] = field(default_factory=list)
    mode: Mode = "none"
    healed_instructions: ParsingInstructions | None = None


def _extracted_nothing(value: Any) -> bool:
    """Whether a field came back with no usable value.

    A list of nothing but nulls is empty in every sense that matters, but it is
    truthy — so a container-anchored field (the row-alignment pattern: anchor on
    an element that is present even when the value is not, then dig the value
    out of it) satisfied `required` no matter how badly the inner selectors had
    drifted. That silently disabled self-heal for exactly the presets built to
    survive drift.
    """
    if isinstance(value, list):
        return not any(item not in (None, "") for item in value)
    return not value


def _missing_required(
    instructions: ParsingInstructions, data: dict[str, Any]
) -> list[str]:
    return [
        name
        for name, rule in instructions.fields.items()
        if rule.required and _extracted_nothing(data.get(name))
    ]


def _missing_required_names(instructions: ParsingInstructions) -> list[str]:
    return [name for name, rule in instructions.fields.items() if rule.required]


def _schema_from(instructions: ParsingInstructions) -> dict[str, str]:
    # Coarse schema hint for the selector regenerator: just the field names.
    return {name: "string" for name in instructions.fields}


def _under_original_contract(
    original: ParsingInstructions, healed: ParsingInstructions
) -> ParsingInstructions | None:
    """The healed SELECTORS, everything else from the preset. Or None.

    Grading the heal against the preset is only half the job: what gets written
    to a user preset is a whole `parsing_instructions` replacement, so returning
    the model's object hands it the field set and every extraction knob too.
    Measured before this existed: one successful heal persisted
    `required: false`, and on the NEXT drift `_missing_required` came back
    empty, self-heal never ran again, and the caller got `{"title": None}`
    labelled deterministic with no warnings at all.

    Restoring only `required` was still not enough (caught in review): `all`,
    `attr` and `post_process` are what turn matched nodes into the shape a
    consumer reads. `amazon_search.urls` is attr="href" all=True, `prices`
    carries parse_price — and a model asked for selectors answers with bare
    text scalars, so the heal would quietly return one label where a list of
    coerced numbers belongs. The heal contributes a selector (and the dialect
    that selector is written in); the preset keeps the rest.

    Returns `(plan, None)`, or `(None, reason)`. The reason matters: an
    INCOMPLETE plan is still usable for this one request, while one that
    escapes the recipe's container is not usable at all — running it would
    answer with unscoped columns and label them `self_healed`.

    Returns no plan when the healed plan does not cover every original field —
    persisting that would truncate the preset permanently, and a version bump
    makes it look deliberate. Fields the model invented are dropped: fine to
    extract for one request, but nobody agreed to add them to a preset.
    """
    if not set(original.fields) <= set(healed.fields):
        if original.container is not None:
            # An incomplete plan is normally still usable for this one
            # request, run bare. For a SCOPED recipe that means running it
            # without the container -- the same unscoped answer under the
            # `self_healed` label that the escape branch below refuses, just
            # reached through the other door. And it cannot simply be
            # re-scoped: the fields are columns of one row set, so a heal that
            # covers only some of them says nothing about keeping the rest
            # aligned. The AI-only tier is still available.
            return None, "escapes_container"
        return None, "incomplete"
    merged = healed.model_copy(
        update={
            "container": original.container,
            # The container is the ORIGINAL's, so it speaks the original's
            # language. Taking the rule `type` from the heal would hand an
            # XPath container to cssselect and quietly return empty columns;
            # the per-field dialects below still travel with their selectors.
            "type": original.type if original.container else healed.type,
            "fields": {
                name: rule.model_copy(
                    update={
                        "selector": healed.fields[name].selector,
                        # The per-field dialect travels WITH the selector: an
                        # xpath expression under the preset's `css` would match
                        # nothing at all. Resolved to the heal's EFFECTIVE
                        # dialect rather than copied: the model usually states
                        # the language once on the rule, and the rule type here
                        # belongs to the original's container, so an unstated
                        # field dialect would silently become the container's.
                        "type": healed.fields[name].type or healed.type,
                    }
                )
                for name, rule in original.fields.items()
            }
        }
    )
    # `model_copy` does NOT re-run validators, and this is the one place a rule
    # is assembled programmatically. The generator is never told about
    # `container` (see `llm/selector_gen.py`), so a heal always returns a
    # document-scoped selector, which the merge then re-attaches to the
    # original's container: measured, `//h3` under a container yields the first
    # row's value repeated once per row, with no warning, and for a user preset
    # that gets PERSISTED. Refusing the heal keeps the working recipe.
    try:
        return ParsingInstructions.model_validate(merged.model_dump()), None
    except ValidationError:
        log.warning(
            "self-heal rejected: the healed selectors do not stay inside the "
            "recipe's container"
        )
        return None, "escapes_container"


async def run(  # pylint: disable=too-many-return-statements
    page_html: str,
    instructions: ParsingInstructions | None,
    *,
    self_heal: bool,
    llm_model: str | None,
    output_schema: dict[str, Any] | None,
    llm_extract_prompt: str | None,
) -> ParserResult:
    if instructions is not None:
        data, warnings = extract_fields(page_html, instructions)
        if not _missing_required(instructions, data):
            return ParserResult(data=data, warnings=warnings, mode="deterministic")

        if self_heal and llm_model:
            # The ask and the grade come from different places: the model is
            # given `output_schema` when the preset has one, while the heal is
            # graded on the instructions' required names. A preset whose schema
            # omits a required field can therefore never pass — and because a
            # schema is set, the pipeline then pays for a full llm_extract on
            # every request, forever, with nothing saying why. All ten built-ins
            # are aligned today; a hand-written preset need not be.
            uncovered = (
                sorted(set(_missing_required_names(instructions)) - set(output_schema))
                if output_schema else []
            )
            if uncovered:
                log.warning(
                    "self-heal: preset requires %s but its output_schema does not "
                    "name them, so no regenerated plan can satisfy the grade",
                    ", ".join(uncovered),
                )
            try:
                healed = await generate_selectors(
                    page_html,
                    output_schema or _schema_from(instructions),
                    llm_model,
                    container=instructions.container,
                )
            except LLMError as exc:
                log.warning("self-heal generation failed: %s", exc)
                return ParserResult(
                    data=data,
                    warnings=[*warnings, f"self_heal_failed: {exc}"],
                    mode="deterministic",
                )
            # Extracted with the RESTORED plan, so the data the caller gets is
            # the data the persisted preset will produce. Graded on it for the
            # same reason: with the model's bare selectors a price field looks
            # filled because the raw label is a non-empty string, while under
            # the preset's parse_price — the coercion that actually ships — it
            # is None, and the heal has recovered nothing.
            persistable, refusal = _under_original_contract(instructions, healed)
            if refusal == "escapes_container":
                # Not merely unpersistable: running the bare heal would drop
                # the container and answer with unscoped columns under the
                # `self_healed` label, which is the misalignment the container
                # exists to prevent, now wearing a badge that says it is fixed.
                #
                # Falls THROUGH rather than returning, matching the sibling
                # `self_heal_did_not_recover` path 15 lines below. An early
                # return also skipped the AI-only tier, which uses no selectors
                # at all and therefore cannot escape a container -- so a
                # recipe that adopted `container` would have lost both repair
                # mechanisms at once.
                warnings = [
                    *warnings,
                    "self_heal_refused: the healed selectors leave the "
                    "recipe's row scope",
                ]
            else:
                plan_for_this_request = persistable if persistable is not None else healed
                healed_data, healed_warnings = extract_fields(page_html, plan_for_this_request)
                # Graded against the ORIGINAL instructions, not the healed ones.
                # `healed` arrives from the model with its own `required` flags, so
                # judging it by those let it grade its own homework: a plan that
                # marks nothing required trivially "recovers", gets labelled
                # self_healed, and — for a user preset — is then written over the
                # selectors that were working. The preset's contract is the only one
                # the caller ever agreed to.
                if not _missing_required(instructions, healed_data):
                    notes = [*warnings, *healed_warnings, "self_healed"]
                    if persistable is None:
                        # Usable for THIS request, never written to the preset: a
                        # plan missing fields would truncate it for every later
                        # scrape, and the caller asked for a repair, not a rewrite.
                        notes.append(
                            "self_heal_not_persisted: the healed plan does not cover "
                            + ", ".join(sorted(set(instructions.fields) - set(healed.fields)))
                        )
                    return ParserResult(
                        data=healed_data,
                        warnings=notes,
                        mode="self_healed",
                        healed_instructions=persistable,
                    )
                warnings = [*warnings, "self_heal_did_not_recover"]

        # Deterministic parser ran (some required fields may be empty). Only
        # fall through to AI-only if it's actually configured; otherwise this
        # is still a deterministic result, just an incomplete one.
        if not (llm_model and (output_schema or llm_extract_prompt)):
            return ParserResult(
                data=data, warnings=warnings, mode="deterministic"
            )

    if llm_model and (output_schema or llm_extract_prompt):
        try:
            extracted = await llm_extract(
                page_html, output_schema, llm_extract_prompt, llm_model
            )
        except LLMError as exc:
            log.warning("AI-only extraction failed: %s", exc)
            return ParserResult(
                data=None,
                warnings=[f"llm_extract_failed: {exc}"],
                mode="none",
            )
        return ParserResult(
            data=extracted, warnings=["llm_extracted"], mode="llm_extracted"
        )

    return ParserResult(data=None, warnings=["no_parser"], mode="none")
