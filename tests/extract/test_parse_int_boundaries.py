"""`parse_int` must not fabricate a number out of unrelated digits.

It strips every non-digit and concatenates what is left, so any text carrying a
second number — a rating beside a count, a date, a price — comes back as the
digits glued together. Unlike a null that gets noticed, this returns a
plausible-looking integer, and `selector_gen` teaches the LLM to emit `parse_int`
on whatever node it picked, so user presets reach it with arbitrary text.
"""
from __future__ import annotations

from src.extract.extractor import _parse_int, extract_fields
from src.extract.models import ExtractRule, FieldRule, PostProcess


def _n(html: str) -> int | None:
    rule = ExtractRule(
        type="css",
        fields={"n": FieldRule(selector="span", post_process=[PostProcess(op="parse_int")])},
    )
    data, _ = extract_fields(html, rule)
    return data["n"]


def test_a_second_number_in_the_text_is_not_glued_on():
    """The shape amazon_product.review_count is one markup change away from:
    `#acrCustomerReviewText` beside a rating gives "4.5 out of 5" -> 455."""
    assert _n("<span>4.5 out of 5</span>") == 4
    assert _n("<span>4.5 out of 5 stars, 1,234 ratings</span>") == 4


def test_thousands_separators_still_group():
    assert _n("<span>(1,234) reviews</span>") == 1234
    assert _n("<span>1.234 Bewertungen</span>") == 1234
    # `_text` collapses U+00A0 to an ASCII space before post_process runs, so
    # the ASCII spelling is the one that actually arrives from a localised page.
    assert _n("<span>1\u00a0234 отзыва</span>") == 1234
    assert _n("<span>1 234 отзыва</span>") == 1234
    assert _n("<span>12345 reviews</span>") == 12345


def test_thin_and_narrow_spaces_group_on_the_html_path():
    """On attr="html" nothing collapses the spaces before the regex runs: Ozon's
    "1 724 отзывов" (U+2009) read as 1 and a French "1 234" (U+202F) as 1
    until both joined `_GROUPING_SPACES`."""
    rule = ExtractRule(
        type="css",
        fields={"n": FieldRule(selector="span", attr="html", post_process=[
            PostProcess(op="regex", args=[r"(\d[\d\s]*)\s*отзыв"]), PostProcess(op="parse_int")])},
    )
    data, _ = extract_fields("<span>1\u2009724 отзывов</span>", rule)
    assert data["n"] == 1724
    assert _n("<span>1\u202f234 avis</span>") == 1234


def test_a_line_break_between_digit_runs_is_not_grouping():
    """Only the four grouping spaces group; a newline or a tab between two digit
    runs separates two numbers. Tested on the parser itself: on the attr="text"
    path `_text` has already collapsed the newline to an ASCII space, and
    "199 500" IS a grouped number there."""
    assert _parse_int("199\n500") == 199
    assert _parse_int("199\t500") == 199


def test_a_lone_number_beside_another_is_not_grouped_with_it():
    """A separator groups only when a COMPLETE three-digit run follows, so the
    "5 items 3 left" shape that the old code read as 53 stops at the 5."""
    assert _n("<span>5 items 3 left</span>") == 5
    assert _n("<span>3 of 4 in stock</span>") == 3


def test_a_group_that_is_not_exactly_three_digits_is_not_a_group():
    """The first version of this fix claimed "exactly three digits follow" and
    did not enforce it, so a longer run was partially consumed and glued —
    silently dropping a digit, which is worse than the bug it replaced:
    "+1 800 555 0199" came back as 1800555019.
    """
    assert _n("<span>1,2345</span>") == 1
    assert _n("<span>1.2026 something</span>") == 1
    assert _n("<span>1 2345</span>") == 1
    # A run of COMPLETE groups followed by another group-shaped run is not a
    # grouped number: a real one never repeats its separator after ending.
    assert _n("<span>+1 800 555 0199</span>") == 1


def test_a_grouped_number_uses_one_separator_throughout():
    """The separator a number groups with is the separator it keeps, so the tail
    after it identifies what the tail is: a decimal switches, another group does
    not. That is the locale question answered without knowing the locale."""
    assert _n("<span>12 345,67</span>") == 12345
    assert _n("<span>1.234.567,89</span>") == 1234567
    assert _n("<span>1,000,000 downloads</span>") == 1000000
    assert _n("<span>1 234 567 890</span>") == 1234567890


def test_a_non_string_value_is_accepted():
    """xpath number() yields a float; `str()` of it must not become "3.0" -> 30."""
    from src.extract.extractor import _parse_int

    assert _parse_int(3.0) == 3
    assert _parse_int(1234) == 1234


def test_an_absurdly_long_run_of_digits_is_none_not_a_crash():
    """CPython refuses int() past sys.int_max_str_digits."""
    from src.extract.extractor import _parse_int

    assert _parse_int("9" * 5000) is None


def test_a_minus_counts_only_when_it_is_attached_to_the_number():
    assert _n("<span>-5</span>") == -5
    assert _n("<span>Total: -5</span>") == -5
    # A hyphen inside a token is punctuation, not a sign.
    assert _n("<span>XL-5</span>") == 5
    # A dash used as a separator is not a sign either.
    assert _n("<span>Sale - 19</span>") == 19


def test_text_without_digits_is_still_none():
    assert _n("<span>no numbers here</span>") is None
    assert _n("<span>-</span>") is None
