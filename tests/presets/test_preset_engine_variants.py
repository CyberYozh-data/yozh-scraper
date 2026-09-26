"""Every built-in ships as an engine-suffixed pair whose twins differ only in
how they drive the browser.

The design doc chose twenty hand-maintained files over ten matrixed ones with
the duplication cost stated and accepted. This test is how that cost is
contained: the predictable failure of the layout is a selector fixed in one twin
and forgotten in the other, and the 2026-08-27 dual-engine audit left two such
fixes pending. One reproduced on BOTH twins: amazon_search returned urls 22/22
relative with 6 sponsored /sspa/click rows mixed into the organic list. The
other reproduced on ONE: bing_search_chromium under-collected on de (2 rows and
5 rows) where bing_search_camoufox returned 10 and 10 from the same SERP -- so
that fix lands on one twin and the guard would not notice its absence from the
other. A third, google_search's links coming back as unusable
/goto?url= stubs, was recorded by an earlier audit and could not be judged on
2026-08-27 -- google_search returned zero rows on both twins that day, so there
were no links to inspect. It REPRODUCED on 2026-08-28, once the wait-strategy
fix recovered real SERPs: all 23 links across the three google_search_chromium
runs that returned rows are relative /goto?url= stubs, and
tests/presets/test_google_search_interstitial.py::TestRealSerpMarkupStillMatches
::test_links_are_the_real_goto_redirect_stubs pins that shape against a live
capture. It is open, unfixed on this branch, and IDENTICAL on both twins -- so
it is a preset defect, not the drift this test exists to catch. Without
this test that drift is silent for as long as nobody re-runs the audit.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

import pytest

from src.presets.materializer import (
    MaterializeError,
    PresetScrapeRequest,
    materialize,
)
from src.presets.models import Preset
from src.presets.store import DEFAULT_BUILTIN_DIR
from src.schemas import ScrapeRequest

ENGINE_SUFFIXES = ("_chromium", "_camoufox")

# request_defaults keys a twin may differ on. An allow-list, not a diff budget:
# `proxy_type` differing between twins fails even though it is "only one field",
# because which proxy to exit through is not an engine concern.
ENGINE_KEYS = frozenset(
    {
        "browser_engine",
        "fingerprint_profile",
        "stealth",
        "wait_until",
        "wait_for_selector",
        "timeout_ms",
        "humanize",
        "spoof_os",
    }
)

# Bases whose twins may exit differently, each with its reason. Which proxy to
# exit through is not an engine concern in general -- hence not in ENGINE_KEYS
# -- but for Google the engine decides whether an egress is worth spending:
# camoufox passes only from this host's own address, and the chromium twin,
# measured refused there, must spend a pool exit rather than burn that address
# (review on #136). Named, not blanket, so the exemption cannot spread; the test
# below also pins WHICH way round it goes.
EGRESS_MAY_DIFFER = {
    "google_search": "camoufox passes only direct; the walled chromium twin stays on the pool",
    "google_shopping": "camoufox passes only direct; the walled chromium twin stays on the pool",
}

# Everything else must match byte-for-byte. Expressed as what may differ rather
# than as a list of what must match, so a field added to `Preset` later is
# covered by this test on the day it is added instead of being silently exempt.
MAY_DIFFER_TOP_LEVEL = frozenset({"name", "description", "request_defaults"})


def _builtins() -> dict[str, dict]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(DEFAULT_BUILTIN_DIR.glob("*.json"))
    }


def _pairs() -> dict[str, dict[str, dict]]:
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for name, preset in _builtins().items():
        for suffix in ENGINE_SUFFIXES:
            if name.endswith(suffix):
                grouped[name[: -len(suffix)]][suffix.lstrip("_")] = preset
                break
    return grouped


BASES = sorted(_pairs())


def test_builtins_exist():
    assert _builtins(), "no builtin presets found"


def test_every_builtin_names_its_engine():
    """No bare names. A caller must never be unsure which engine answered."""
    unsuffixed = [
        name for name in _builtins() if not name.endswith(ENGINE_SUFFIXES)
    ]
    assert unsuffixed == [], f"builtins without an engine suffix: {unsuffixed}"


def test_fourteen_bases():
    """Ten shipped with the dual-engine split; ozon_search, ozon_product,
    mobile_de_search and mobile_de_ad joined on 2026-09-06."""
    assert len(BASES) == 14, f"expected 14 base presets, found {len(BASES)}: {BASES}"


@pytest.mark.parametrize("base", BASES)
def test_base_has_both_variants(base):
    assert set(_pairs()[base]) == {"chromium", "camoufox"}


@pytest.mark.parametrize("name,preset", sorted(_builtins().items()))
def test_name_field_matches_filename(name, preset):
    assert preset["name"] == name


@pytest.mark.parametrize("base", BASES)
def test_twins_share_everything_but_name_description_and_request_defaults(base):
    twins = _pairs()[base]
    stripped = {
        engine: {k: v for k, v in preset.items() if k not in MAY_DIFFER_TOP_LEVEL}
        for engine, preset in twins.items()
    }
    assert stripped["chromium"] == stripped["camoufox"], (
        f"{base}: the twins' parsing recipe or routing has drifted apart"
    )


@pytest.mark.parametrize("base", BASES)
def test_twins_differ_only_on_engine_keys(base):
    twins = _pairs()[base]
    chromium = twins["chromium"].get("request_defaults", {})
    camoufox = twins["camoufox"].get("request_defaults", {})
    differing = {
        key
        for key in set(chromium) | set(camoufox)
        if chromium.get(key) != camoufox.get(key)
    }
    allowed = ENGINE_KEYS | ({"proxy_type"} if base in EGRESS_MAY_DIFFER else set())
    assert differing <= allowed, (
        f"{base}: request_defaults differ on non-engine keys "
        f"{sorted(differing - allowed)}"
    )


@pytest.mark.parametrize("base", sorted(EGRESS_MAY_DIFFER))
def test_an_egress_exemption_goes_the_way_its_reason_says(base):
    """The exemption exists for one shape only: the camoufox twin direct, the
    chromium twin on a real proxy. The reverse -- the walled engine spending
    the host address -- is exactly what it was written to prevent."""
    twins = _pairs()[base]
    assert twins["camoufox"]["request_defaults"].get("proxy_type") == "none"
    assert twins["chromium"]["request_defaults"].get("proxy_type") not in (None, "none")


@pytest.mark.parametrize("base", BASES)
def test_chromium_variant_declares_chromium(base):
    defaults = _pairs()[base]["chromium"].get("request_defaults", {})
    assert defaults.get("browser_engine") == "chromium"


@pytest.mark.parametrize("base", BASES)
def test_chromium_variant_carries_no_fingerprint_profile(base):
    """The Chromium runner accepts the field and ignores it (runner.py:995).
    A file stating an intent the runtime never honours is worse than silence."""
    defaults = _pairs()[base]["chromium"].get("request_defaults", {})
    assert "fingerprint_profile" not in defaults


@pytest.mark.parametrize("base", BASES)
def test_camoufox_variant_is_host_aligned(base):
    defaults = _pairs()[base]["camoufox"].get("request_defaults", {})
    assert defaults.get("browser_engine") == "camoufox"
    assert defaults.get("fingerprint_profile") == "windows_on_host"


@pytest.mark.parametrize("base", BASES)
def test_camoufox_variant_drops_stealth(base):
    """Camoufox manages its own fingerprint; playwright-stealth does not run."""
    defaults = _pairs()[base]["camoufox"].get("request_defaults", {})
    assert "stealth" not in defaults


# playwright-stealth, combined with the always-on
# --disable-blink-features=AutomationControlled flag, is what Google detects
# and captchas. Two commits turned it off, on purpose, for four presets:
# 524e6ff disabled it on bing_search, google_search AND yandex_search in one
# hunk each ("disable stealth on Google/Bing/Yandex SERP presets"); a separate
# commit (077e2c7) did the same for google_shopping once it shipped its own
# captcha reports. yandex_search later moved to Camoufox, which drops the key
# entirely (accepted-and-ignored there) rather than restating it — that is
# housekeeping on the way to a different engine, not a reversal of the
# decision, so its Chromium twin still inherits the same `false`.
#
# `stealth` sits in ENGINE_KEYS above because an engine is allowed to need a
# different stealth posture than its twin — but "allowed to differ" is not
# "allowed to silently become True". Nothing before this test pins the actual
# value, so blanket-enabling stealth across every Chromium variant — exactly
# what would re-open the captcha these commits closed — passes every other
# test in this file unnoticed.
DELIBERATE_STEALTH_OFF_BASES = frozenset(
    {"bing_search", "google_search", "google_shopping", "yandex_search"}
)


@pytest.mark.parametrize("base", sorted(DELIBERATE_STEALTH_OFF_BASES))
def test_chromium_variant_keeps_deliberate_stealth_off(base):
    defaults = _pairs()[base]["chromium"].get("request_defaults", {})
    assert defaults.get("stealth") is False, (
        f"{base}_chromium: stealth must stay off — it was disabled on purpose "
        "to dodge SERP bot detection, not left over from a rename"
    )


# A twin measured as walled on its engine ships anyway (test_base_has_both_variants
# forces the pair) with the verdict in its description -- the google_shopping_camoufox
# precedent, followed by the ozon_* chromium twins on 2026-09-06. The one thing that
# description must not lose is where the data comes from: the sibling's name.
@pytest.mark.parametrize("name,preset", sorted(_builtins().items()))
def test_a_walled_twin_names_the_twin_that_carries_the_data(name, preset):
    if "WALLED" not in preset["description"]:
        pytest.skip("not a walled twin")
    suffix = next(s for s in ENGINE_SUFFIXES if name.endswith(s))
    base, engine = name[: -len(suffix)], suffix.lstrip("_")
    sibling = f"{base}_{'camoufox' if engine == 'chromium' else 'chromium'}"
    assert sibling in preset["description"], (
        f"{name} is walled on its engine; its description must send callers to {sibling}"
    )


# `description` sits in MAY_DIFFER_TOP_LEVEL, so every other test in this file
# is blind to what a description actually claims. That matters for one claim in
# particular: chromium_webgl_identity() runs only inside stealth_config(), which
# src/browser/runner.py:194 reaches only when stealth is on. A `stealth: false`
# Chromium variant therefore ships the browser's raw headless WebGL identity,
# and all four of them say so today. Nothing stopped a future edit from
# deleting that disclaimer, or from pasting the stealth-on twins' positive
# "WebGL identity follows this host" sentence onto a preset that cannot get it —
# a description promising a fingerprint the runtime never applies.
_WEBGL_DISCLAIMER = "does NOT apply here"
_WEBGL_RAW_IDENTITY = "raw headless WebGL identity"
_WEBGL_HOST_CLAIM = "WebGL identity follows this host"

# Derived from the files, not hard-coded, so a preset that turns stealth off
# later is covered on the day it does rather than on the day someone remembers.
STEALTH_OFF_CHROMIUM = sorted(
    base
    for base in BASES
    if _pairs()[base]["chromium"].get("request_defaults", {}).get("stealth")
    is False
)


def test_stealth_off_chromium_set_is_not_empty():
    """A parametrize over an empty list passes vacuously and guards nothing."""
    assert set(STEALTH_OFF_CHROMIUM) == DELIBERATE_STEALTH_OFF_BASES


@pytest.mark.parametrize("base", STEALTH_OFF_CHROMIUM)
def test_stealth_off_chromium_description_disclaims_host_webgl(base):
    description = _pairs()[base]["chromium"]["description"]
    assert _WEBGL_DISCLAIMER in description and _WEBGL_RAW_IDENTITY in description, (
        f"{base}_chromium runs with stealth off, so chromium_webgl_identity() "
        "never executes and the host-aligned WebGL claim does not apply to it. "
        "Its description must keep saying so — GET /api/v1/presets returns this "
        "text verbatim, and a caller who reads it picks a fingerprint that "
        "never ships."
    )
    assert _WEBGL_HOST_CLAIM not in description, (
        f"{base}_chromium runs with stealth off and cannot get the host-aligned "
        "WebGL identity, so its description must not claim it does"
    )


# Everything above reads the JSON as data. Nothing above ever built a
# ScrapeRequest out of it, and the whole point of a preset is that it becomes
# one: a file can satisfy every structural rule in this module and still be
# unusable, because `request_defaults` has to survive ScrapeRequest's
# cross-field validators as well as its field types. That gap shipped a real
# defect — see the session tests at the bottom of this file.

# Caller-supplied url_template placeholders. `domain`, `country`, `lang` and
# `lr` are DERIVED from the locale by the materializer and are deliberately
# absent: supplying them here would paper over a locale that fails to produce
# one, since preset_params wins over the locale-derived value.
TEMPLATE_PARAMS = {
    "asin": "B0CRTYZG5C",
    "query": "laptop",
    "username": "williamhgates",
    "product_id": "5689919121",
    "video_id": "dQw4w9WgXcQ",
    # mobile.de's own numeric make/model ids (3500/20 = BMW 525) and ad id
    "make_id": "3500",
    "model_id": "20",
    "ad_id": "391420794",
}
LOCALE_DERIVED_PLACEHOLDERS = frozenset({"domain", "country", "lang", "lr"})

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# (preset name, locale) for every locale every builtin offers — 104 cases
# today. Per-locale rather than per-preset because the locale feeds the URL and
# the proxy exit, so a single default-locale smoke test would cover 20 of them
# and leave the other 84 unexercised.
LOCALE_CASES = [
    (name, locale)
    for name, preset in sorted(_builtins().items())
    for locale in sorted(preset.get("locales", {}))
]


def test_every_builtin_offers_at_least_one_locale():
    """A parametrize over an empty list passes vacuously and guards nothing."""
    covered = {name for name, _ in LOCALE_CASES}
    assert covered == set(_builtins()), (
        f"builtins with no locales: {sorted(set(_builtins()) - covered)}"
    )


@pytest.mark.parametrize("name,preset", sorted(_builtins().items()))
def test_every_template_placeholder_is_known(name, preset):
    """A new placeholder must be added to TEMPLATE_PARAMS, not skipped.

    Without this, a builtin introducing e.g. `{store_id}` would make every
    materialize test below raise `missing template params` and the failure
    would read as a materializer bug rather than as a fixture gap.
    """
    placeholders = set(_PLACEHOLDER_RE.findall(preset.get("url_template") or ""))
    unknown = placeholders - LOCALE_DERIVED_PLACEHOLDERS - set(TEMPLATE_PARAMS)
    assert unknown == set(), (
        f"{name}: url_template needs params this test cannot supply: "
        f"{sorted(unknown)} — add them to TEMPLATE_PARAMS"
    )


@pytest.mark.parametrize(
    "name,locale", LOCALE_CASES, ids=[f"{n}-{loc}" for n, loc in LOCALE_CASES]
)
def test_every_builtin_locale_materializes(name, locale):
    """Every shipped (preset, locale) pair must expand into a valid request.

    `materialize` is where a preset meets ScrapeRequest's validators, and it is
    the only place a `request_defaults` combination the schema refuses can be
    caught before a caller gets an HTTP 400 from a preset we shipped.
    """
    request = materialize(
        Preset(**_builtins()[name]),
        PresetScrapeRequest(
            source=name, locale=locale, preset_params=TEMPLATE_PARAMS
        ),
    )
    assert isinstance(request, ScrapeRequest)
    # str(): ScrapeRequest.url is a pydantic URL object, not a str.
    url = str(request.url)
    assert not _PLACEHOLDER_RE.search(url), (
        f"{name}/{locale}: url still carries a placeholder: {url}"
    )
    expected_engine = "camoufox" if name.endswith("_camoufox") else "chromium"
    assert request.browser_engine == expected_engine, (
        f"{name}/{locale}: materialized as browser_engine="
        f"{request.browser_engine!r}, which contradicts its own name"
    )
    assert request.preset_meta is not None
    assert (request.preset_meta.name, request.preset_meta.locale) == (name, locale)


# ScrapeRequest._validate_camoufox_unsupported refuses `session_id`, `cookies`
# and `render=false` on browser_engine='camoufox' (the Camoufox runner accepts
# and silently ignores all three), and the materializer turns that into a
# MaterializeError, which src/api/scrape_preset.py:59-60 turns into HTTP 400.
#
# This is a CAPABILITY GAP between twins, not a detail: it means one half of
# every pair can never run authenticated. It shipped mis-documented —
# linkedin_profile_camoufox's own description told callers to pass session_id,
# on the one preset whose stated purpose requires one — because no test ever
# put a session on a builtin. These do.
SESSION_ID = "sess_engine_variant_guard"

CAMOUFOX_REJECTED_OVERRIDES = (
    ("session_id", {"session_id": SESSION_ID}),
    ("cookies", {"cookies": [{"name": "a", "value": "b", "domain": ".example.com", "path": "/"}]}),
    ("render=false", {"render": False}),
)


@pytest.mark.parametrize("base", BASES)
def test_chromium_variant_accepts_a_session_id(base):
    name = f"{base}_chromium"
    request = materialize(
        Preset(**_builtins()[name]),
        PresetScrapeRequest(
            source=name,
            preset_params=TEMPLATE_PARAMS,
            request_override={"session_id": SESSION_ID},
        ),
    )
    assert request.session_id == SESSION_ID, (
        f"{name}: a session_id passed through request_override must reach the "
        "materialized request — this variant is the authenticated half of the "
        "pair and nothing else can be"
    )


@pytest.mark.parametrize("base", BASES)
@pytest.mark.parametrize(
    "option,override",
    CAMOUFOX_REJECTED_OVERRIDES,
    ids=[option for option, _ in CAMOUFOX_REJECTED_OVERRIDES],
)
def test_camoufox_variant_rejects_what_its_runner_would_ignore(base, option, override):
    name = f"{base}_camoufox"
    with pytest.raises(MaterializeError) as excinfo:
        materialize(
            Preset(**_builtins()[name]),
            PresetScrapeRequest(
                source=name, preset_params=TEMPLATE_PARAMS, request_override=override
            ),
        )
    message = str(excinfo.value)
    assert option in message and "camoufox" in message, (
        f"{name}: rejecting {option} is the documented behaviour, but the error "
        f"must name both the option and the engine so a caller can act on it; "
        f"got: {message}"
    )


# A description that instructs an operation its own engine refuses is worse
# than one that says nothing: GET /api/v1/presets returns this text verbatim,
# so a caller follows it straight into an HTTP 400. The pairs that need the
# guard are derived from the CHROMIUM twin — if that half documents a session,
# the pair is session-relevant and the Camoufox half must state that it cannot
# have one and name the twin that can.
SESSION_RELEVANT_BASES = sorted(
    base for base in BASES if "session_id" in _pairs()[base]["chromium"]["description"]
)


def test_session_relevant_bases_is_not_empty():
    """Derived, so it must be checked: an empty list guards nothing."""
    assert SESSION_RELEVANT_BASES, (
        "no Chromium builtin mentions session_id any more — either the "
        "documentation regressed or this guard needs re-deriving"
    )


@pytest.mark.parametrize("base", SESSION_RELEVANT_BASES)
def test_camoufox_twin_of_a_session_preset_disclaims_sessions(base):
    description = _pairs()[base]["camoufox"]["description"]
    assert "session_id" in description, (
        f"{base}_camoufox: its twin documents a session and this variant cannot "
        "take one; silence leaves a caller to discover that as an HTTP 400"
    )
    assert f"{base}_chromium" in description, (
        f"{base}_camoufox: must name {base}_chromium as the variant to use when "
        "a session is needed"
    )
    assert "rejected" in description.lower(), (
        f"{base}_camoufox: must state that session_id is REJECTED here, not "
        "merely describe sessions — a caller who reads 'pass session_id' on "
        "this variant gets an HTTP 400"
    )
