"""Guard that every builtin preset validates. `_read_preset_file` swallows
validation errors and drops the preset from the registry, so a malformed builtin
would silently vanish rather than fail CI; this catches that."""
from __future__ import annotations

import glob
import json
import os
import time

import pytest

from src.presets.models import Preset
from src.presets.store import DEFAULT_BUILTIN_DIR
from src.queue.scrape_runner import affordable_attempt_timeout_ms, attempt_slack_s
from src.settings import Settings, settings

BUILTIN_FILES = sorted(glob.glob(os.path.join(str(DEFAULT_BUILTIN_DIR), "*.json")))


def test_builtin_dir_not_empty():
    assert BUILTIN_FILES, "no builtin presets found"


@pytest.mark.parametrize("path", BUILTIN_FILES, ids=lambda p: os.path.basename(p))
def test_builtin_preset_validates(path):
    with open(path, encoding="utf-8") as fh:
        Preset(**json.load(fh))


# (preset base name, field, declared item/property type) for every builtin field
# MEASURED to come back null on a self-healing preset. `output_schema` is handed
# to the self-heal LLM as the target contract (parser_pipeline.run ->
# generate_selectors), so a schema claiming a field can never be null steers a
# heal against the field's only observed behaviour. Reverting any of these to a
# bare scalar type left the suite fully green before this test existed -- the
# same gap `amazon_search`'s own schema test was written to close.
NULLABLE_BY_MEASUREMENT = [
    # headline null 4/4 in BOTH audit records: a plain `strip` field that the
    # page simply does not always carry -- the one shape the recipe cannot tell.
    ("linkedin_profile", "headline", ["string", "null"]),
]

# An `all: true` column whose pipeline carries an op that nulls IN PLACE (a
# regex with no match, a number that does not parse, `null_if_regex` by
# design) keeps a row as null rather than shifting the ones after it, so its
# items are nullable by construction -- the shape measured on bing (de,
# 2026-09-04) and declared for every such column from the recipe, not from a
# per-field measurement that guards only what someone already looked at.
_NULLING_OPS = {"regex", "null_if_regex", "parse_int", "parse_float", "parse_price", "base64_decode"}


def _recipe_nullable_columns():
    for path in BUILTIN_FILES:
        with open(path, encoding="utf-8") as fh:
            preset = json.load(fh)
        for name, rule in preset["parsing_instructions"]["fields"].items():
            if rule.get("all") and any(step["op"] in _NULLING_OPS for step in rule.get("post_process") or []):
                yield pytest.param(path, name, id=f"{os.path.basename(path)}:{name}")


@pytest.mark.parametrize("path,field", list(_recipe_nullable_columns()))
def test_recipe_nullable_columns_declare_null(path, field):
    with open(path, encoding="utf-8") as fh:
        prop = json.load(fh)["output_schema"]["properties"][field]
    assert prop["type"] == "array", (field, prop)
    declared = prop["items"]["type"]
    assert isinstance(declared, list) and "null" in declared, f"{os.path.basename(path)}.{field} items: {declared!r}"


@pytest.mark.parametrize("base,field,expected", NULLABLE_BY_MEASUREMENT)
@pytest.mark.parametrize("engine", ["chromium", "camoufox"])
def test_measured_nullable_fields_declare_null(base, field, expected, engine):
    path = os.path.join(str(DEFAULT_BUILTIN_DIR), f"{base}_{engine}.json")
    with open(path, encoding="utf-8") as fh:
        preset = json.load(fh)
    assert preset["self_heal"] is True, "this guard only matters on a healing preset"
    prop = preset["output_schema"]["properties"][field]
    declared = prop["items"]["type"] if prop.get("type") == "array" else prop["type"]
    assert declared == expected


@pytest.mark.parametrize("path", BUILTIN_FILES, ids=lambda p: os.path.basename(p))
def test_builtin_updated_at_is_not_in_the_future(path):
    """A stamp later than now is a typo, never a fact -- these are hand-edited
    constants recording when a recipe last changed, and a preset cannot have
    been updated after the moment it is read.

    Written because the fix wave that added it shipped `1787961600.0` on six
    presets, which is 2026-08-29 00:00 UTC: a day in the FUTURE, on a branch
    whose whole subject was wrong numbers. It survived the entire suite, and
    the review caught it by hand. This is the check that would not have needed
    a human."""
    with open(path, encoding="utf-8") as fh:
        updated_at = json.load(fh)["updated_at"]
    now = time.time()
    assert updated_at <= now, (
        f"{os.path.basename(path)} claims updated_at={updated_at} "
        f"({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(updated_at))}), "
        f"which is in the future (now {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))})"
    )


# The seconds a task spends before its first navigation (the session lock,
# proxy resolution, a browser context): 3.8-10.3 s across the 792 shortened
# first attempts in the recorded audits, 4.4-4.5 s measured live on k12 on
# 2026-09-05 when 55 s with a warmup still came back "shortened to 54048ms".
BEFORE_FIRST_NAVIGATION_S = 10.0


@pytest.mark.parametrize("path", BUILTIN_FILES, ids=os.path.basename)
def test_builtin_timeout_fits_the_first_attempt(path, monkeypatch):
    """`timeout_ms` is spent PER PHASE (warmup, goto, wait_for_selector), and the
    first attempt gets the page-task ceiling minus the attempt slack minus the
    seconds before its first navigation. A preset asking for more is shortened
    on every call and every response says so. What a preset ships is what it
    gets, under the default ceiling."""
    # The bundled numbers are for the defaults, whatever this host's .env says.
    for name in ("page_task_timeout_s", "warmup_dwell_ms"):
        monkeypatch.setattr(settings, name, Settings.model_fields[name].default)
    with open(path, encoding="utf-8") as fh:
        defaults = json.load(fh)["request_defaults"]
    first_attempt_s = (
        settings.page_task_timeout_s
        - attempt_slack_s(settings.page_task_timeout_s)
        - BEFORE_FIRST_NAVIGATION_S
    )
    fits = affordable_attempt_timeout_ms(
        first_attempt_s, defaults["timeout_ms"],
        wait_for_selector=defaults.get("wait_for_selector"), warmup=defaults.get("warmup"),
    )
    assert fits == defaults["timeout_ms"], (
        f"{os.path.basename(path)} asks {defaults['timeout_ms']}ms per phase; "
        f"the first attempt can promise {fits}ms"
    )


# `links_stay_on_site` switches the self-referential-links guard off for a
# preset whose results ARE the site's own pages. The guard exists to catch a
# broken unwrap (amazon_search's `unwrap_param`, google_search's
# `resolve_redirects`), so a preset that unwraps or resolves anything has no
# business declaring it -- the flag pasted onto such a preset would silence the
# one regression the guard is for.
_UNWRAP_OPS = {"unwrap_param", "base64_decode"}


@pytest.mark.parametrize("path", BUILTIN_FILES, ids=lambda p: os.path.basename(p))
def test_links_stay_on_site_means_nothing_is_unwrapped(path):
    with open(path, encoding="utf-8") as fh:
        preset = json.load(fh)
    if not preset.get("links_stay_on_site"):
        pytest.skip("guard stays on")
    ops = {step["op"] for rule in preset["parsing_instructions"]["fields"].values() for step in rule.get("post_process") or []}
    assert not ops & _UNWRAP_OPS, f"{os.path.basename(path)} unwraps {sorted(ops & _UNWRAP_OPS)} yet declares links_stay_on_site"
    assert not preset["request_defaults"].get("resolve_redirects"), f"{os.path.basename(path)} resolves redirects yet declares links_stay_on_site"


def test_links_stay_on_site_invariant_is_not_vacuous():
    declaring = [os.path.basename(p) for p in BUILTIN_FILES if json.load(open(p, encoding="utf-8")).get("links_stay_on_site")]
    assert declaring == ["mobile_de_search_camoufox.json", "mobile_de_search_chromium.json"]
