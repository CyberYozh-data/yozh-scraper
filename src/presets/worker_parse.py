"""Worker-side entry point for preset parsing.

The worker subprocess calls `apply()` after rendering. It bridges the
serialized job dict (extract + parser_plan as plain dicts) to the parser
pipeline, and persists self-healed selectors back to user presets
(built-ins are read-only — logged, not written). Extracted here rather than
inlined in worker_pool so it is unit-testable without spawning a process.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from src.extract.extractor import extract_fields
from src.extract.models import ExtractRule
from src.presets.materializer import strip_materializer_injected
from src.presets.models import ParserPlan, ParsingInstructions
from src.presets.parser_pipeline import run as run_pipeline
from src.presets.store import (
    PresetChangedSinceRead,
    PresetLockUnavailable,
    PresetStore,
)

log = logging.getLogger(__name__)


def _get_store() -> PresetStore:
    return PresetStore()


def _persist_self_heal(
    preset_name: str,
    healed: ParsingInstructions,
    stamp: str | None,
) -> str | None:
    """Write healed selectors back, unless the preset moved under us.

    `stamp` is what the file looked like when THIS JOB read it, minutes ago
    (src/api/scrape_preset.py). The person who owns the preset may have saved
    their own selectors since; theirs is the edit that must survive, because
    ours is a guess by a language model and the next scrape simply heals
    again from what they wrote. None -- a built-in, or a job enqueued before
    the field existed -- keeps the old unconditional write.
    """
    try:
        store = _get_store()
        preset = store.get(preset_name)
        updated = preset.model_copy(
            update={
                "parsing_instructions": healed,
                "version": preset.version + 1,
                "updated_at": time.time(),
            }
        )
        store.update(preset_name, updated, if_stamp=stamp, lock_timeout_s=0.0)
        log.info(
            "self-heal persisted: preset=%s new_version=%d",
            preset_name,
            updated.version,
        )
        return None
    except PresetChangedSinceRead:
        log.info(
            "self-heal not persisted: preset=%s changed while the job ran; "
            "the stored version is kept and this request used the healed "
            "selectors only",
            preset_name,
        )
        return "self_heal_persist_skipped: the preset changed while the job ran"
    except PresetLockUnavailable:
        log.info(
            "self-heal not persisted: preset=%s is being written by somebody "
            "else; this request used the healed selectors only",
            preset_name,
        )
        return (
            "self_heal_persist_skipped: another writer held the preset"
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Persistence is best-effort: the scrape already succeeded, a failed
        # write must not fail the response.
        log.warning("self-heal persist failed for %s: %s", preset_name, exc)
        return f"self_heal_persist_failed: {exc}"


async def apply(
    page_html: str,
    extract_dict: dict[str, Any] | None,
    plan_dict: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not extract_dict and not plan_dict:
        return None, []
    if not page_html:
        return None, []

    instructions = (
        ExtractRule.model_validate(extract_dict) if extract_dict else None
    )

    # No preset plan -> raw /scrape path: behave exactly like the old direct
    # extract so non-preset callers are unaffected.
    if not plan_dict:
        if instructions is None:
            return None, []
        data, warnings = extract_fields(page_html, instructions)
        return data, [str(warning) for warning in warnings]

    plan = ParserPlan.model_validate(plan_dict)
    result = await run_pipeline(
        page_html,
        instructions,
        self_heal=plan.self_heal,
        llm_model=plan.llm_model,
        output_schema=plan.output_schema,
        llm_extract_prompt=plan.llm_extract_prompt,
    )
    warnings = [str(warning) for warning in result.warnings]

    if result.mode == "self_healed" and result.healed_instructions:
        if plan.instructions_from_override:
            # The contract the heal satisfied is this request's own
            # `parsing_override`, not the preset's: persisting it would
            # replace the shared preset's fields for every other caller
            # (audit 2026-09-03, H-14).
            note = (
                "self_heal_not_persisted: parsed with a per-request "
                "parsing_override; healed selectors used for this request only"
            )
            log.info("%s (preset=%s)", note, plan.preset_name)
            warnings.append(note)
        elif plan.preset_kind == "user" and plan.preset_name:
            # Strip whatever THIS request's materialize() call injected (a
            # price locale, a urljoin base) before it can be written to a user
            # preset — `result.healed_instructions` copies post_process
            # verbatim from the already-materialized instructions, so left
            # alone it would freeze this one request's locale/URL into the
            # preset forever. See strip_materializer_injected's docstring.
            healed_for_persist = strip_materializer_injected(
                result.healed_instructions, plan.materializer_injected
            )
            # Called straight, with no await inside it: `within_deadline` can
            # only cancel this coroutine where it yields, and a best-effort
            # write must not be able to take the parsed page down with it
            # (src/queue/scrape_runner.py). It does not wait for the lock, so
            # it costs one file write.
            note = _persist_self_heal(
                plan.preset_name, healed_for_persist, plan.preset_stamp
            )
            if note:
                warnings.append(note)
        elif plan.preset_name:
            log.info(
                "self-heal recovered built-in preset %s but built-in presets "
                "are read-only; not persisting (regenerated selectors used "
                "for this request only)",
                plan.preset_name,
            )

    return result.data, warnings
