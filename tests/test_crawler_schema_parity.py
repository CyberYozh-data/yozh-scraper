"""`yozh-crawler` re-declares the scrape options it forwards. Keep them equal.

The crawler builds its own image (`COPY src /app/src` and nothing else) and its
gate installs only its own requirements, so one shared contract module is not
available -- the duplication is structural. What is not acceptable is silent
drift, and this seam has produced both kinds:

* a mirror that is NARROWER than the scraper refuses a value the scraper
  accepts, as a 422 about a field the crawler does not read (`legacy_wap`);
* a mirror that is MISSING a field drops it, because pydantic's default
  `extra="ignore"` throws away what it has not heard of and `fetcher.py`
  forwards what survives. That one is worse: `container` reached extraction as
  document-scoped columns, and a per-field `type` / `post_process` reached it
  as the rule's language and an unparsed string -- each with nothing in the
  response to say the recipe had been altered.

This file pins both directions. It lives on the SCRAPER side, like
`tests/security/test_policy_parity.py`, because only here can both sides be
loaded: `src.schemas` imports lxml and playwright-adjacent modules the
crawler's gate does not install, while the crawler's `schemas.py` needs only
pydantic -- so the dependency arrow that works is scraper imports crawler.
Consequence worth stating: a narrowing committed in the crawler turns THIS job
red, not the crawler's.

`model_fields` is read without `model_rebuild()`: the names are all this file
claims, and the crawler's models are deliberately not validated here.
"""
from __future__ import annotations

import importlib.util

from pathlib import Path
from typing import get_args

import pytest

import src.schemas as scraper_schemas

from src.extract import models as extract_models


_CRAWLER_SCHEMAS = Path(__file__).resolve().parents[1] / "yozh-crawler" / "src" / "schemas.py"

# Literals spelled the same on both sides. `ScopeMode` is the crawler's own and
# `CrawlJobStatus` is its name for `JobStatus`, so neither is a mirror this file
# can speak for.
MIRRORED_LITERALS = ("Device", "ScrapeProxyType", "WaitUntil", "ExtractType", "PostProcessOp")

# Models the crawler mirrors, as (crawler name, scraper model).
MIRRORED_MODELS = (
    ("FieldRule", extract_models.FieldRule),
    ("ExtractRule", extract_models.ExtractRule),
    ("PostProcess", extract_models.PostProcess),
    ("Cookie", scraper_schemas.Cookie),
    ("PremProxyOptions", scraper_schemas.PremProxyOptions),
    ("ProxyGeo", scraper_schemas.ProxyGeo),
)

# `ScrapeRequest` fields `ScrapeOptions` deliberately does not forward. Every
# name here is a decision, not an oversight -- `url` is injected per page by the
# frontier, and the rest are single-page concerns the crawler does not expose.
# A new scraper field fails this test until it is mirrored or listed, which is
# the whole point: the choice becomes explicit instead of silent.
NOT_FORWARDED = {
    "addons",
    "block_webgl",
    "browser_engine",
    "element_selector",
    "fingerprint_profile",
    "formats",
    "headless",
    "humanize",
    "markdown_options",
    "max_retries",
    "parser_plan",
    "preset_meta",
    "raw_html",
    "resolve_redirects",
    "spoof_os",
    "url",
    "viewport",
    "warmup",
}


def _crawler_schemas():
    """Import the crawler's schema module by path. It needs only pydantic, so
    this does not require the crawler's dependencies."""
    spec = importlib.util.spec_from_file_location("_crawler_schemas", _CRAWLER_SCHEMAS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def crawler():
    assert _CRAWLER_SCHEMAS.is_file(), f"crawler tree missing at {_CRAWLER_SCHEMAS}"
    return _crawler_schemas()


@pytest.mark.parametrize("name", MIRRORED_LITERALS)
def test_mirrored_literal_carries_every_value(crawler, name):
    ours = getattr(scraper_schemas, name, None) or getattr(extract_models, name)
    theirs = getattr(crawler, name, None)
    assert theirs is not None, f"{name} is no longer mirrored in the crawler"
    assert set(get_args(theirs)) == set(get_args(ours))


@pytest.mark.parametrize("name,ours", MIRRORED_MODELS, ids=[n for n, _ in MIRRORED_MODELS])
def test_mirrored_model_carries_every_field(crawler, name, ours):
    theirs = getattr(crawler, name, None)
    assert theirs is not None, f"{name} is no longer mirrored in the crawler"
    dropped = set(ours.model_fields) - set(theirs.model_fields)
    assert not dropped, f"crawler {name} silently drops {sorted(dropped)}"


def test_scrape_options_forwards_every_field_it_does_not_disclaim(crawler):
    ours = set(scraper_schemas.ScrapeRequest.model_fields)
    theirs = set(crawler.ScrapeOptions.model_fields)
    assert theirs <= ours, f"crawler sends fields the scraper has no name for: {sorted(theirs - ours)}"
    assert ours - theirs == NOT_FORWARDED, (
        "the crawler's forwarded set changed: "
        f"newly unmirrored {sorted((ours - theirs) - NOT_FORWARDED)}, "
        f"newly mirrored {sorted(NOT_FORWARDED - (ours - theirs))}"
    )
