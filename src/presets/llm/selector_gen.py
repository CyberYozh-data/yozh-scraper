"""LLM-generated CSS/XPath selectors.

Given a cleaned page and a desired field schema, ask the model for a
ParsingInstructions JSON (the same deterministic DSL the worker runs). Used
by /presets/generate (from_schema) and by self-heal when a built-in's
selectors stop matching.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from src.presets.llm.client import LLMError, complete
from src.presets.llm.htmlclean import clean_html
from src.presets.llm.jsonparse import parse_json
from src.presets.models import ParsingInstructions

_SYSTEM = (
    "You generate web-scraping selectors. Given an HTML excerpt and a target "
    "field schema, output ONLY a JSON object matching this shape: "
    '{"type":"css"|"xpath","fields":{"<name>":{"selector":"<expr>",'
    '"attr":"text"|"html"|"<attr>","all":bool,"required":bool,'
    '"post_process":[{"op":"...","args":[...]}]}}}. '
    "Prefer stable selectors (ids, data-* attributes) over positional ones. "
    "Use post_process ops (parse_price, parse_int, regex, strip) to coerce "
    "values. Output JSON only, no prose, no code fences."
)


_ROW_SCOPED = (
    "Every selector you return is evaluated RELATIVE TO A ROW, not to the "
    "document: the row is `{container}`. Write each selector as if the row "
    "were the document root. Start XPath with './/' (or '.' for the row "
    "itself); never with '/' or '//', never use '..' or the ancestor, parent, "
    "preceding, following or following-sibling axes, and no '|' unions. For "
    "CSS do not use the sibling combinators '+' or '~'. A selector that "
    "leaves its row will be rejected."
)


async def generate_selectors(
    page_html: str,
    schema: dict[str, Any],
    model: str,
    *,
    max_tokens: int | None = None,
    container: str | None = None,
) -> ParsingInstructions:
    system = _SYSTEM
    if container:
        # Without this the model has no way to know, and every heal of a
        # row-scoped recipe comes back document-scoped and is refused -- which
        # would have quietly traded self-heal away for any recipe that adopts
        # `container`.
        system = f"{_SYSTEM} {_ROW_SCOPED.format(container=container)}"
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"Target field schema:\n{schema}\n\n"
                f"HTML:\n{clean_html(page_html)}"
            ),
        },
    ]
    raw = await complete(model, messages, max_tokens=max_tokens)
    obj = parse_json(raw)
    try:
        return ParsingInstructions.model_validate(obj)
    except ValidationError as exc:
        raise LLMError(
            f"LLM did not return valid ParsingInstructions: {exc}"
        ) from exc
