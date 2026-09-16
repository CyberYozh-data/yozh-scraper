"""A label before the number must not swallow the price.

`_parse_price` searched for `-?[\\d., ]+`, and that character class contains a
SPACE. On any string where a space precedes the number and is not itself
followed by a digit — "Now $199.00", "From $12.50", a leading non-breaking
space — the search matched the space alone, `replace(" ", "")` reduced it to
the empty string, and the `if not raw` guard returned None.

Found on walmart_product, whose live price node renders as "Now $199.00": the
selector always matched, the field was not `required`, and the preset returned
`price: null` on every run with no warning. The same shape reaches any preset
whose price node carries a label, so the fix belongs here rather than in a
per-preset regex.

The space stays in the class deliberately — it is a thousands separator in
several locales ("1 234,56"). Requiring the match to START with a digit is what
distinguishes a separator inside a number from a gap before one.
"""
from __future__ import annotations

import pytest

from src.extract.extractor import _parse_price, extract_fields
from src.extract.models import ExtractRule, FieldRule, PostProcess


class TestLabelledPrices:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Now $199.00", 199.0),
            ("From $12.50", 12.5),
            ("Price when purchased online $199.00", 199.0),
            ("\xa0Now $199.00", 199.0),
            ("Sale price $1,299.99", 1299.99),
            ("Now $199", 199.0),
        ],
    )
    def test_label_before_the_number_does_not_swallow_it(self, value, expected):
        assert _parse_price(value) == expected


class TestSpaceThousandsSeparatorStillWorks:
    """The reason the space is in the character class at all."""

    def test_space_grouped_thousands_us(self):
        assert _parse_price("1 234.56") == 1234.56

    def test_space_grouped_thousands_eu(self):
        assert _parse_price("1 234,56") == 1234.56

    def test_space_grouped_millions(self):
        assert _parse_price("12 345 678.90") == 12345678.90

    def test_label_and_space_grouping_together(self):
        assert _parse_price("Now 1 234,56 €") == 1234.56

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("55\u2009119\u2009₽", 55119.0),  # Ozon: U+2009 thin spaces, measured 2026-09-06
            ("1\u00a0234,56 €", 1234.56),  # U+00A0 as served, before `_text` ever collapses it
            ("1\u202f234,56 €", 1234.56),  # French narrow no-break space
            ("1\u2009234\u2009567 ₽", 1234567.0),
        ],
    )
    def test_every_grouping_space_spelling_groups(self, value, expected):
        """The html path hands the parser the page's own spaces; "55 119 ₽" with
        a thin space parsed as 55.0 -- a price 1000x low under a full fill-rate."""
        assert _parse_price(value) == expected

    def test_a_newline_between_digit_runs_is_not_grouping(self):
        assert _parse_price("199\n500") == 199.0


class TestBehaviourNotRegressed:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("$19.99", 19.99),
            # Corrected, not unchanged: this used to assert 89900.0. A lone
            # comma with a 2-digit tail is unambiguously a decimal point in
            # any locale -- no locale groups thousands two digits at a time
            # -- so the "us" hint no longer overrides what the text itself
            # says. See TestTextOverridesLocaleHint for the live failure
            # this shape is a mirror of.
            ("899,00 €", 899.0),
            ("1.234,56", 1234.56),
            # Newly correct, not unchanged: the sign used to ride inside the
            # match, so "-$5.00" matched the bare "-", reduced to "" and
            # returned None. It is now read from the text before the digits.
            ("-$5.00", -5.0),
            ("no digits here", None),
            ("", None),
            (None, None),
        ],
    )
    def test_shapes_that_must_keep_working(self, value, expected):
        assert _parse_price(value) == expected

    def test_eu_locale_comma_decimal(self):
        assert _parse_price("899,00 €") == 899.0

    def test_leading_minus_is_still_a_sign_not_a_separator(self):
        assert _parse_price("Now -$5.00") == -5.0


class TestSignMustBeAdjacent:
    """A dash that is not attached to the number is not a minus sign.

    The first attempt read an unbounded prefix, so "Sale - $19.99" parsed as
    -19.99 and "XL-$19.99" — which main got right — flipped to negative. A
    wrong sign is invisible downstream; the null it replaced was not.
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Sale - $19.99", 19.99),
            ("Now - $19.99", 19.99),
            ("Rollback - $12.50", 12.5),
            ("Deal of the Day - $24.99", 24.99),
            ("XL-$19.99", 19.99),
        ],
    )
    def test_separator_dash_is_not_a_minus(self, value, expected):
        assert _parse_price(value) == expected

    @pytest.mark.parametrize("value", ["-$5.00", "Now -$5.00", "-5.00", "-€5,00"])
    def test_adjacent_minus_is_still_a_minus(self, value):
        assert _parse_price(value) == -5.0


class TestLeadingSeparator:
    """"$.99" is ordinary sub-dollar retail typography, and a `regex` step can
    hand this function a leading-dot slice directly. Requiring a bare leading
    digit moved the match onto the fraction and multiplied the value by 100."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (".99", 0.99),
            ("$.99", 0.99),
            ("$.99 each", 0.99),
            (".5", 0.5),
            (",99", 0.99),
            ("-.5", -0.5),
        ],
    )
    def test_leading_separator_keeps_the_magnitude(self, value, expected):
        assert _parse_price(value) == expected


class TestTextOverridesLocaleHint:
    """The audit's highest-severity defect: `locale` comes from the preset's
    DECLARED country (materializer.py injects it from `LocaleProfile`, never
    from the page), while the actual separator convention comes from
    whatever page the browser was actually served. The two can disagree.

    Live failure: a preset declared for Germany carried `locale="eu"`, but
    the browser announced `en-DE` and Amazon served an English dot-decimal
    page, so `price_raw="€32.99"` parsed as 3299.0 -- numeric, positive, euro
    sign intact in `price_raw` -- passing every shipped check. The mirror,
    `_parse_price("32,99€")`, produces the same 3299.0 the other way.

    A lone separator followed by exactly two digits is unambiguous from the
    TEXT alone: no locale groups its thousands two digits at a time, so that
    shape can only be a decimal point, regardless of `locale`. A 3-or-more
    digit tail, and a repeated separator of one kind, are likewise
    hint-INVARIANT the other way -- always a thousands grouping. Measured,
    not assumed: `_parse_price("1.399") == _parse_price("1.399")
    == 1399.0`. The four "must keep working" cases below pin exactly that.

    Since 2026-09-05 a 1-digit tail is read from the text too (a thousands
    group is three digits in every locale), so no hint reaches the parser at
    all: the op accepts its locale arg and ignores it, pinned below.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            # The audit's live failure: Amazon served a dot-decimal page
            # because the browser announced en-DE, while the preset's hint
            # said eu.
            ("€32.99", 32.99),
            # The mirror, which the same branch of the code produces.
            ("32,99€", 32.99),
            # Hint-INVARIANT, not hint-decided: a 3-digit tail is always a
            # thousands grouping and both-separators-present is always
            # last-position-wins, under any locale. Kept as regression
            # coverage that the 2-digit fix does not creep into these
            # shapes -- see the class docstring for the measurement.
            ("1.399,00 €", 1399.00),
            ("1,399.00", 1399.00),
            ("€1.399", 1399.0),
            ("$1,399", 1399.0),
        ],
    )
    def test_parse_price_trusts_the_text(self, text, expected):
        assert _parse_price(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["1.3", "1,3", "4,2", "4.2"])
    def test_a_one_digit_tail_is_a_decimal(self, text):
        # A thousands group is three digits in every locale, so a lone separator
        # followed by ONE digit cannot be grouping any more than two can.
        assert _parse_price(text) == pytest.approx(float(text.replace(",", ".")))

    @pytest.mark.parametrize("args", [[], ["us"], ["eu"]])
    def test_the_ops_locale_arg_is_accepted_and_ignored(self, args):
        rule = ExtractRule(type="css", fields={
            "n": FieldRule(selector="i", post_process=[PostProcess(op="parse_price", args=args)]),
        })
        data, _ = extract_fields("<i>4,2</i>", rule)
        assert data["n"] == pytest.approx(4.2), args
