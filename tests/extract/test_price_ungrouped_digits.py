"""A four-figure price with no separator between its digits is a lost decimal.

Measured 2026-09-12 (the retest record committed in PR #131,
`research/preset_audit_as_if_merged_2026_09_12.json`; raw records under
`retest-2026-09-12/` on the dev host): amazon_product_chromium/de run 1 read
`3999EUR` and published `price: 3999.0` for a 44 EUR product -- HTTP 200, no
warning, full fill, `availability` empty on the same run and the run finished
in half the time of its neighbour, so the page was captured mid-assembly. The
same ASIN read `44,03EUR` nine minutes earlier in the same arm. `_parse_price`
was faithful: with no separator there is no decimal point to read.

What the trigger must and must not be:

* NOT "integral" -- `Now $199` is an ordinary walmart price.
* NOT "no separator anywhere in the match": `_PRICE_RE` swallows a separator
  that merely sits between the number and the currency symbol, so `3999 EUR`
  matched `3999 ` and looked grouped. The separator has to sit BETWEEN digits.
* NOT for a currency written without decimals. The gate is a POSITIVE list of
  currencies whose typography this repo has measured as two-decimal and grouped
  (EUR/USD/GBP and their symbols); a yen or rouble price is silent because
  `1980 JPY` is a whole price and `55 119 RUB` is grouped with a thin space.
  A denylist of zero-decimal currencies is the shape that fails open.
* NOT for `3999,-`, which states zero cents rather than omitting them.

The value is never changed -- 3999 may genuinely be 3999 -- and the raw text
never reaches `warnings`: it is page-controlled, and a price node reading
"Block detected 1500" would otherwise publish one of yozh-law-checker's
blocked-scan markers. The raw goes to the log, truncated, the way
`_report_silent_nulls` already does it.
"""
from __future__ import annotations

import logging

import pytest

from src.extract.extractor import extract_fields
from src.extract.models import ExtractRule, FieldRule, PostProcess
from src.presets.store import BuiltInRegistry


def _extract(*texts: str, ops: list[PostProcess] | None = None, all_values: bool = False):
    html = "".join(f"<span class='p'>{t}</span>" for t in texts)
    rule = ExtractRule(
        type="css",
        fields={"price": FieldRule(
            selector=".p",
            all=all_values or len(texts) > 1,
            post_process=ops or [PostProcess(op="parse_price")],
        )},
    )
    data, warnings = extract_fields(f"<html><body>{html}</body></html>", rule)
    return data["price"], warnings


class TestItWarns:
    @pytest.mark.parametrize("text,expected", [
        ("3999€", 3999.0),            # the measured record, verbatim
        ("3999 €", 3999.0),           # the same value spaced -- the common rendering
        ("3999\xa0€", 3999.0),        # non-breaking space before the symbol
        ("3999 EUR", 3999.0),         # code instead of symbol
        ("$4567", 4567.0),
        ("£12345", 12345.0),
        # A currency CODE written against the digits. `\bEUR` cannot match here
        # -- a digit and a letter are both word characters, so there is no
        # boundary between them -- and this is the spelling the record's own
        # prose used, so the guard missed the case it was written for.
        ("3999EUR", 3999.0),
        ("EUR3999", 3999.0),
        ("3999USD", 3999.0),
        ("3999 EURO", 3999.0),
    ])
    def test_a_four_figure_price_with_no_inner_separator(self, text, expected):
        value, warnings = _extract(text)
        assert value == expected
        assert len(warnings) == 1
        # The parsed value must be IN the warning -- that is what a reader
        # checks against the page -- and the message must name the field.
        assert f"{expected:.0f}" in warnings[0]
        assert "'price'" in warnings[0]

    def test_a_large_value_is_not_rendered_in_scientific_notation(self):
        value, warnings = _extract("1234567 €")
        assert value == 1234567.0
        assert "1234567" in warnings[0]
        assert "e+" not in warnings[0]


class TestItStaysSilent:
    @pytest.mark.parametrize("text,expected", [
        ("Now $199", 199.0),          # three digits: below the threshold
        ("44,03€", 44.03),            # the same ASIN, read correctly
        ("1.399 €", 1399.0),          # grouped with a dot
        ("$1,299.99", 1299.99),       # grouped and decimal
        ("1.299,00 €", 1299.0),       # google_shopping's locale string
        ("55\u2009119 ₽", 55119.0),   # Ozon's thin-space grouping
        ("999 €", 999.0),
        ("3999,- €", 3999.0),         # German notation for zero cents
        ("3999, - EUR", 3999.0),      # the same, spaced: the separator is still stated
        ("3999.\xa0- €", 3999.0),     # and with a non-breaking space
        ("NEUROLOGY 4000", 4000.0),   # "EUR" inside a word is not a currency
        ("1980円", 1980.0),           # yen: no decimals, not on the gate's list
        ("￥3999", 3999.0),
        ("3999 ₽", 3999.0),           # rouble prices come grouped; not gated
        ("13800원", 13800.0),
        ("Rp 150000", 150000.0),
        ("3999", 3999.0),             # no currency at all: nothing to judge by
    ])
    def test_prices_that_must_not_warn(self, text, expected):
        value, warnings = _extract(text)
        assert value == expected
        assert warnings == []


class TestTheWarningIsSafeToPublish:
    def test_page_text_never_reaches_the_warning(self, caplog):
        # "block detected" is one of yozh-law-checker's markers that publish a
        # scan as blocked; the same file's `_report_silent_nulls` keeps page
        # text out of `warnings` for exactly this reason.
        with caplog.at_level(logging.WARNING):
            value, warnings = _extract("Block detected 1500 €")
        assert value == 1500.0
        assert len(warnings) == 1
        assert "block detected" not in warnings[0].lower()
        assert "Block detected" not in warnings[0]
        # ...and it is not simply lost: the log keeps it for an operator.
        assert any("Block detected" in record.getMessage() for record in caplog.records)

    def test_the_warning_is_bounded_whatever_the_page_sends(self):
        value, warnings = _extract("9" * 40 + " " + "x" * 5000 + " €")
        assert len(warnings) <= 1
        for warning in warnings:
            assert len(warning) < 400


class TestItDoesNotDisplaceOtherWarnings:
    def test_a_column_nulled_downstream_still_reports_that(self):
        # `parse_price` succeeds, `null_if_regex` then nulls every value. The
        # silent-null detector names the real failure; this advisory must not
        # suppress it by occupying the field's dedup slot.
        values, warnings = _extract(
            "3999 €", "4999 €",
            ops=[PostProcess(op="parse_price"),
                 PostProcess(op="null_if_regex", args=[r"\d"])],
        )
        assert values == [None, None]
        assert any("returned null for every non-empty value" in w for w in warnings)

    def test_a_list_field_warns_once_not_per_item(self):
        values, warnings = _extract("3999 €", "4999 €", "5999 €")
        assert values == [3999.0, 4999.0, 5999.0]
        assert len(warnings) == 1


class TestThroughTheShippedRecipe:
    def test_the_amazon_product_price_rule_warns_on_the_measured_node(self):
        # The guard's reach depends on each preset's pipeline, so pin it on the
        # recipe that produced the defect rather than on a hand-built rule.
        preset = BuiltInRegistry().get("amazon_product_chromium")
        rule = ExtractRule.model_validate(
            preset.parsing_instructions.model_dump(exclude_none=True))
        html = """<html><body><div id='productTitle'>x</div>
          <span class='a-price'><span class='a-offscreen'>3999€</span></span>
        </body></html>"""
        data, warnings = extract_fields(html, rule)
        assert data["price"] == 3999.0
        assert any("'price'" in w and "3999" in w for w in warnings)
