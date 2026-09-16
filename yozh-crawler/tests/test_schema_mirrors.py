"""What reaches this mirror must reach the scrape body unchanged.

`ScrapeOptions` and the models under it re-declare fields the crawler only
forwards -- it reads almost none of them. pydantic's default `extra="ignore"`
means a field this module has not heard of is dropped in silence, and
`fetcher.py` forwards what survives, so the loss shows up as altered
extraction with nothing in the response to explain it: `container` arrived as
document-scoped columns, a per-field `type` as the rule's own language, and
`post_process` as the raw string.

This file is the behavioural half -- a value set on the model is still there in
`model_dump()`, which is what `fetcher.fetch` splats into the scrape body. The
structural half, that the mirror carries every field and literal value the
scraper has, is pinned on the scraper side in `tests/test_crawler_schema_parity.py`:
only there can both trees be loaded (the crawler's gate does not install the
scraper's dependencies, and `src` here is the crawler's own package).
"""
from __future__ import annotations

import pytest

from pydantic import ValidationError

from src.schemas import ExtractRule, ScrapeOptions


def test_container_survives_the_mirror():
    rule = ExtractRule.model_validate({
        "type": "css",
        "container": "#rso div.tF2Cxc",
        "fields": {"titles": {"selector": "h3", "all": True}},
    })
    assert rule.container == "#rso div.tF2Cxc"
    assert rule.model_dump()["container"] == "#rso div.tF2Cxc"


def test_a_rule_without_it_still_round_trips():
    rule = ExtractRule.model_validate({
        "type": "css", "fields": {"titles": {"selector": "h3", "all": True}},
    })
    assert rule.container is None


def test_a_per_field_type_survives_the_mirror():
    rule = ExtractRule.model_validate({
        "type": "css",
        "fields": {"price": {"selector": "//span[@class='a-price']", "type": "xpath"}},
    })
    assert rule.fields["price"].type == "xpath"
    assert rule.model_dump()["fields"]["price"]["type"] == "xpath"


def test_post_process_survives_the_mirror():
    rule = ExtractRule.model_validate({
        "type": "css",
        "fields": {"price": {"selector": ".price", "post_process": [
            {"op": "regex", "args": [r"([\d.,]+)"]}, {"op": "parse_price"},
        ]}},
    })
    steps = rule.model_dump()["fields"]["price"]["post_process"]
    assert [step["op"] for step in steps] == ["regex", "parse_price"]
    assert steps[0]["args"] == [r"([\d.,]+)"]


def test_an_unknown_post_process_op_is_refused_here():
    # The op vocabulary is mirrored, so a typo is a 422 on POST /crawl rather
    # than a failure on every page of a 500-page crawl.
    with pytest.raises(ValidationError):
        ExtractRule.model_validate({
            "type": "css",
            "fields": {"price": {"selector": ".price",
                                 "post_process": [{"op": "parse_money"}]}},
        })


def test_legacy_wap_survives_the_mirror():
    # `render: false` because the mirror now carries the scraper's pairing rule
    # as well as the value (see the pairing test below).
    options = ScrapeOptions.model_validate({"device": "legacy_wap", "render": False})
    assert options.device == "legacy_wap"
    # The dump is what `fetcher.fetch` splats into the scrape body, so this is
    # the assertion that the value actually reaches the scraper.
    assert options.model_dump()["device"] == "legacy_wap"


def test_an_unknown_device_is_still_refused():
    with pytest.raises(ValidationError):
        ScrapeOptions.model_validate({"device": "smartwatch"})


def test_the_legacy_wap_pairing_is_refused_here_not_per_page():
    # The crawler forwards `device` and `render` and reads neither, so without
    # this rule the scraper refuses every page of the crawl instead of the
    # crawl being refused once.
    with pytest.raises(ValidationError):
        ScrapeOptions.model_validate({"device": "legacy_wap"})
    with pytest.raises(ValidationError):
        ScrapeOptions.model_validate({"device": "legacy_wap", "render": True})


def test_legacy_wap_with_rendering_off_is_accepted():
    options = ScrapeOptions.model_validate({"device": "legacy_wap", "render": False})
    assert options.model_dump()["device"] == "legacy_wap"
