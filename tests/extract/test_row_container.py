"""A rule scoped to its rows, so columns cannot drift apart.

The defect this closes is the one this repo keeps re-finding: a column whose
selector draws from a WIDER set than its siblings. `titles` picks every `h3`
in the document, `snippets` picks one per result block, and the moment a
result carries sitelinks the two columns have different lengths -- every row
after the offender is wrong, and `row_alignment_mismatch` can only report the
mismatch after the fact, never prevent it.

`container` sits on the RULE. Every field is selected relative to each row and
yields exactly one entry per row, so all columns are the same length by
construction. On the field it would not have closed the class: set it on
`titles`, forget it on `snippets`, and the drift is back with the one existing
detector structurally unable to fire, because that detector compares LENGTHS.

`tests/presets/test_google_search_row_alignment.py` pins the unfixed shape on
the shipped recipe; this module tests the engine that makes fixing it
possible. The recipes are deliberately NOT migrated in the same change.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.extract.extractor import extract_fields
from src.extract.models import ExtractRule

# The sitelinks SERP from the pinned preset test, reduced to what matters: the
# first result carries two extra <h3> inside it, the second carries none.
SERP = """
<html><body>
<h3>OUTSIDE ABOVE</h3>
<div id="rso">
  <div class="tF2Cxc">
    <div class="yuRUbf"><a href="https://main.example"><h3>Main Result</h3></a></div>
    <div class="VwiC3b">Main snippet</div>
    <div class="HiHjCd">
      <a href="https://main.example/about"><h3 class="zBAuLc">About</h3></a>
      <a href="https://main.example/careers"><h3 class="zBAuLc">Careers</h3></a>
    </div>
  </div>
  <div class="tF2Cxc">
    <div class="yuRUbf"><a href="https://b.example"><h3>Second Result</h3></a></div>
    <div class="VwiC3b">Second snippet</div>
  </div>
</div>
<h3>OUTSIDE BELOW</h3>
</body></html>
"""

ROWS_CSS = "#rso div.tF2Cxc"
ROWS_XPATH = "//div[@class='tF2Cxc']"


def _css(**fields):
    return ExtractRule(type="css", container=ROWS_CSS, fields=fields)


class TestRowScoping:
    def test_without_a_container_the_columns_still_drift(self):
        """The control: without this feature the engine cannot express
        one-value-per-row, and the sitelinks card grows two columns."""
        data, _ = extract_fields(SERP, ExtractRule(type="css", fields={
            "titles": {"selector": "#rso h3", "all": True},
            "snippets": {"selector": "#rso div.VwiC3b", "all": True},
        }))
        assert len(data["titles"]) == 4
        assert len(data["snippets"]) == 2

    def test_a_container_gives_one_value_per_row(self):
        data, warnings = extract_fields(SERP, _css(
            titles={"selector": "h3", "all": True},
            links={"selector": "a:has(h3)", "attr": "href", "all": True},
            snippets={"selector": "div.VwiC3b", "all": True},
        ))
        assert data["titles"] == ["Main Result", "Second Result"]
        assert data["links"] == ["https://main.example", "https://b.example"]
        assert data["snippets"] == ["Main snippet", "Second snippet"]
        assert not warnings

    def test_the_columns_are_paired_not_merely_equal_in_length(self):
        """Equal lengths are not the claim -- `titles[i]` must belong to the
        same result as `snippets[i]`. Judged by pairing, because the row COUNT
        is supposed to fall here and a count check reads that as a regression.
        """
        data, _ = extract_fields(SERP, _css(
            titles={"selector": "h3", "all": True},
            snippets={"selector": "div.VwiC3b", "all": True},
        ))
        assert list(zip(data["titles"], data["snippets"])) == [
            ("Main Result", "Main snippet"),
            ("Second Result", "Second snippet"),
        ]

    def test_a_row_missing_the_field_holds_its_place_as_none(self):
        """The alignment claim only holds if a gap is a null IN PLACE rather
        than a shorter column -- a shortened column is the original defect."""
        html = """
        <div id="rso">
          <div class="tF2Cxc"><h3>One</h3><div class="VwiC3b">first</div></div>
          <div class="tF2Cxc"><h3>Two</h3></div>
          <div class="tF2Cxc"><h3>Three</h3><div class="VwiC3b">third</div></div>
        </div>
        """
        data, _ = extract_fields(html, _css(
            titles={"selector": "h3", "all": True},
            snippets={"selector": "div.VwiC3b", "all": True},
        ))
        assert data["titles"] == ["One", "Two", "Three"]
        assert data["snippets"] == ["first", None, "third"]

    def test_no_rows_at_all_is_an_empty_column_not_an_error(self):
        data, warnings = extract_fields("<div id='rso'></div>", _css(
            titles={"selector": "h3", "all": True},
        ))
        assert data["titles"] == []
        assert not warnings

    def test_post_process_still_runs_per_row(self):
        html = """
        <div id="rso">
          <div class="tF2Cxc"><span class="p">1 299,00 &#8364;</span></div>
          <div class="tF2Cxc"><span class="p">899,50 &#8364;</span></div>
        </div>
        """
        data, _ = extract_fields(html, _css(
            prices={"selector": "span.p", "all": True,
                    "post_process": [{"op": "parse_price"}]},
        ))
        assert data["prices"] == [1299.0, 899.5]


class TestRequiredStillMeansSomething:
    """`required` is the one signal that survives a container.

    `row_alignment_mismatch` compares lengths and every column here is the
    same length by construction, so it can never fire. If `required` also went
    quiet, a recipe whose field selector died while its row selector kept
    working would return a full-height column of nulls and say nothing.
    """

    def test_no_rows_names_the_container(self):
        _, warnings = extract_fields("<div id='rso'></div>", _css(
            titles={"selector": "h3", "all": True, "required": True},
        ))
        assert warnings == ["field 'titles': container matched no rows"]

    def test_rows_present_but_no_row_matched_still_warns(self):
        """The drift mode a container creates: the row selector is the coarse,
        stable one, so it keeps working after the inner selector dies."""
        data, warnings = extract_fields(SERP, _css(
            titles={"selector": "span.gone", "all": True, "required": True},
        ))
        assert data["titles"] == [None, None]
        assert warnings == [
            "field 'titles': required selector matched no row (0 of 2)"
        ]

    def test_one_row_matching_is_enough(self):
        html = """
        <div id="rso">
          <div class="tF2Cxc"><h3>One</h3></div>
          <div class="tF2Cxc"></div>
        </div>
        """
        _, warnings = extract_fields(html, _css(
            titles={"selector": "h3", "all": True, "required": True},
        ))
        assert not warnings


class TestAMalformedSelectorCostsTheFieldNotTheJob:
    """`extract` is caller-supplied. An exception here reaches the worker's
    outer handler, which discards the whole ScrapeResponse -- the page
    rendered fine and the caller loses the HTML, screenshot and meta over one
    string. Every shape reports as a field warning instead.
    """

    @pytest.mark.parametrize("container,kind,expected", [
        ("#rso div.tF2Cxc[", "css", "container selector is invalid"),
        ("count(//div)", "xpath", "container selector did not match elements"),
        ("boolean(//h3)", "xpath", "container selector did not match elements"),
        ("//div[@class='tF2Cxc']/@class", "xpath",
         "container selector did not match elements"),
    ])
    def test_a_container_that_is_not_rows_is_reported(self, container, kind, expected):
        """The language is stated, not guessed from the first character, and
        the exact message is asserted -- an earlier version passed even with
        half the guard deleted, because it only checked that SOMETHING warned.
        """
        data, warnings = extract_fields(SERP, ExtractRule(
            type=kind, container=container,
            fields={"titles": {"selector": ".//h3" if kind == "xpath" else "h3",
                               "all": True}},
        ))
        assert data["titles"] == []
        assert warnings == [expected]

    def test_a_broken_container_is_said_once_not_once_per_field(self):
        data, warnings = extract_fields(SERP, ExtractRule(
            type="css", container="#rso div[",
            fields={n: {"selector": "h3", "all": True} for n in "abcde"},
        ))
        assert all(data[n] == [] for n in "abcde")
        assert warnings == ["container selector is invalid"]

    def test_an_exotic_node_costs_the_field_not_the_response(self):
        """`.//comment()` reaches `_pick` as an HtmlComment and raises; an
        exception here reaches the worker's outer handler, which throws away
        the whole ScrapeResponse."""
        data, warnings = extract_fields(
            '<div id="rso"><div class="tF2Cxc"><!-- c --><h3>One</h3></div></div>',
            ExtractRule(type="xpath", container=ROWS_XPATH,
                        fields={"t": {"selector": ".//comment()", "all": True}}),
        )
        assert data["t"] == []
        assert warnings == ["field 't': invalid selector"]

    def test_a_bad_field_selector_is_reported(self):
        data, warnings = extract_fields(SERP, _css(
            titles={"selector": "h3[", "all": True},
        ))
        assert data["titles"] == []
        assert warnings == ["field 'titles': invalid selector"]


class TestNonRelativeXpathIsRefused:
    """Written as an allowlist, because the denylist leaked.

    The first draft rejected `selector.startswith('/')`. Ten forms walked past
    it — measured, each returning values from OUTSIDE the row with no warning
    at all. That is worse than the defect: a container makes every column the
    same length, so the length-based detector is structurally unable to fire,
    and the result is confidently mispaired rows.
    """

    ESCAPES = [
        "//h3", "/html/body//h3", "(//h3)[1]", ".//h3 | //h3",
        "ancestor::div//h3", "parent::*//h3", "..//h3", "id('rso')//h3",
        "ancestor-or-self::body//h3", "preceding::h3", "following::h3",
        "following-sibling::div//h3", " //h3", ".//div/../h3", ".//h3/ancestor::div",
        "", "   ",
    ]

    @pytest.mark.parametrize("selector", ESCAPES)
    def test_every_escaping_form_is_rejected(self, selector):
        with pytest.raises(ValidationError):
            ExtractRule(type="xpath", container=ROWS_XPATH,
                        fields={"titles": {"selector": selector, "all": True}})

    @pytest.mark.parametrize("selector", [
        ".//h3", "descendant::h3", "descendant-or-self::h3", "self::*",
        "child::div", "./div", "attribute::class", ".",
    ])
    def test_the_row_safe_axes_are_accepted(self, selector):
        rule = ExtractRule(type="xpath", container=ROWS_XPATH,
                           fields={"titles": {"selector": selector, "all": True}})
        assert rule.fields["titles"].selector == selector

    def test_the_field_type_override_is_what_decides(self):
        with pytest.raises(ValidationError):
            ExtractRule(type="css", container=ROWS_CSS, fields={
                "titles": {"selector": "//h3", "type": "xpath", "all": True},
            })

    def test_absolute_xpath_without_a_container_is_still_fine(self):
        """Every shipped recipe writes `//...` at document scope; the rule
        applies only where a container changes what the leading slash means."""
        rule = ExtractRule(type="xpath",
                           fields={"titles": {"selector": "//h3", "all": True}})
        assert rule.fields["titles"].selector == "//h3"

    def test_a_css_selector_needs_no_such_rule(self):
        """cssselect compiles to `descendant-or-self::`, so a CSS selector
        under a container can only under-match, never escape upward --
        measured across `a > h3`, `a:has(h3)`, `div.wrap h3`, `#rso h3`,
        `body h3` and `:root h3`."""
        rule = _css(titles={"selector": "body h3", "all": True})
        assert rule.fields["titles"].selector == "body h3"

    def test_a_container_makes_every_field_a_column(self):
        """`all=False` under a container would mean "the first ROW's value",
        which no recipe wants and which silently flips meaning for anyone
        adding a container to an existing field. Refused instead."""
        with pytest.raises(ValidationError, match="all"):
            _css(titles={"selector": "h3"})


class TestTheAllowlistIsAboutBehaviourNotSpelling:
    """The validator is a claim about lxml; these are the measurements."""

    def test_a_form_smuggled_past_the_validator_is_still_caught_at_runtime(self):
        """Defence in depth, because the validator is syntax and syntax has
        been wrong twice already.

        Mutated past the validator on purpose. The matches that came from
        another row are discarded rather than returned, and the field is
        named -- silence here would be the original defect wearing a badge
        that says it is fixed, since the column is still full height and
        `row_alignment_mismatch` compares lengths.
        """
        rule = ExtractRule(type="xpath", container=ROWS_XPATH,
                           fields={"titles": {"selector": ".//h3", "all": True}})
        rule.fields["titles"].selector = "following::h3"  # bypass, on purpose
        data, warnings = extract_fields(SERP, rule)
        assert data["titles"] == [None, None], (
            "a value from another row must not be returned at all"
        )
        assert warnings == [
            "field 'titles': selector matched outside its row; "
            "those matches were discarded"
        ]

    def test_relative_xpath_scopes_correctly(self):
        data, _ = extract_fields(SERP, ExtractRule(type="xpath", container=ROWS_XPATH,
                                                   fields={"titles": {"selector": ".//h3", "all": True}}))
        assert data["titles"] == ["Main Result", "Second Result"]

    def test_an_xpath_container_can_carry_css_fields(self):
        data, _ = extract_fields(SERP, ExtractRule(
            type="xpath", container=ROWS_XPATH,
            fields={"titles": {"selector": "h3", "type": "css", "all": True}},
        ))
        assert data["titles"] == ["Main Result", "Second Result"]

    def test_attribute_and_text_selectors_work_per_row(self):
        """The shape a real xpath recipe uses -- `bing_search` would."""
        data, _ = extract_fields(SERP, ExtractRule(type="xpath", container=ROWS_XPATH, fields={
            "links": {"selector": ".//a/@href", "all": True},
            "titles": {"selector": ".//h3/text()", "all": True},
        }))
        assert data["links"] == ["https://main.example", "https://b.example"]
        assert data["titles"] == ["Main Result", "Second Result"]


class TestTheRowItselfIsAddressable:
    """Three of the five google fields ARE the row (`attr: html` on the card).

    A container that could only reach DESCENDANTS would leave those three on
    the old shape, splitting one recipe's idea of a row in two -- which is the
    defect. `self::*` is on the allowlist for exactly this.
    """

    def test_a_field_can_select_the_row_node_itself(self):
        data, _ = extract_fields(SERP, _css(
            titles={"selector": "h3", "all": True},
            blocks={"selector": "self::*", "type": "xpath", "attr": "html", "all": True},
        ))
        assert data["titles"] == ["Main Result", "Second Result"]
        assert len(data["blocks"]) == 2
        assert "Main snippet" in data["blocks"][0]
        assert "Second snippet" in data["blocks"][1]


class TestAcceptanceAgainstTheShippedRecipe:
    """The pinned defect, on the recipe that actually ships."""

    SHIPPED = {
        "titles": {"selector": "#rso div.tF2Cxc h3", "all": True},
        "links": {"selector": "#rso div.tF2Cxc a:has(h3)", "attr": "href", "all": True},
    }
    SCOPED = {
        "titles": {"selector": "h3", "all": True},
        "links": {"selector": "a:has(h3)", "attr": "href", "all": True},
    }

    def test_the_shipped_selectors_still_grow_on_sitelinks(self):
        data, _ = extract_fields(SERP, ExtractRule(type="css", fields=self.SHIPPED))
        assert data["titles"] == ["Main Result", "About", "Careers", "Second Result"]
        assert len(data["links"]) == 4

    def test_the_scoped_selectors_do_not(self):
        data, warnings = extract_fields(SERP, _css(**self.SCOPED))
        assert data["titles"] == ["Main Result", "Second Result"]
        assert data["links"] == ["https://main.example", "https://b.example"]
        assert not warnings

    def test_an_ordinary_serp_extracts_identically_either_way(self):
        """The migration must be a no-op where nothing was broken -- otherwise
        the fix buys alignment by changing every healthy page too."""
        plain = """
        <div id="rso">
          <div class="tF2Cxc"><a href="https://a.example"><h3>A</h3></a></div>
          <div class="tF2Cxc"><a href="https://b.example"><h3>B</h3></a></div>
          <div class="tF2Cxc"><a href="https://c.example"><h3>C</h3></a></div>
        </div>
        """
        shipped, _ = extract_fields(plain, ExtractRule(type="css", fields=self.SHIPPED))
        scoped, _ = extract_fields(plain, _css(**self.SCOPED))
        assert shipped == scoped
        assert scoped["titles"] == ["A", "B", "C"]


class TestCssCanEscapeToo:
    """The exemption CSS used to enjoy was measured on the wrong sample.

    `cssselect` compiles descendant and child combinators to
    `descendant-or-self::`, which is where the claim came from. It compiles
    the SIBLING combinators to `following-sibling::`: measured on two adjacent
    rows, `.row ~ .row h3` evaluated inside the first row returns the SECOND
    row's heading. Same length, wrong pairing, and before this the guard did
    not look at CSS at all.
    """

    ADJACENT = """
    <div id="rso">
      <div class="row"><h3>One</h3></div>
      <div class="row"><h3>Two</h3></div>
    </div>
    """

    @pytest.mark.parametrize("selector", [".row ~ .row h3", ".row + .row h3"])
    def test_a_sibling_combinator_is_refused(self, selector):
        with pytest.raises(ValidationError, match="following-sibling"):
            ExtractRule(type="css", container="#rso div.row",
                        fields={"titles": {"selector": selector, "all": True}})

    def test_it_really_would_have_reached_the_next_row(self):
        rule = ExtractRule(type="css", container="#rso div.row",
                           fields={"titles": {"selector": "h3", "all": True}})
        rule.fields["titles"].selector = ".row ~ .row h3"  # bypass, on purpose
        data, warnings = extract_fields(self.ADJACENT, rule)
        assert data["titles"] == [None, None]
        assert warnings and "outside its row" in warnings[0]

    @pytest.mark.parametrize("selector", ["h3", "div.row h3", "a:has(h3)", "body h3"])
    def test_ordinary_css_is_untouched(self, selector):
        rule = ExtractRule(type="css", container="#rso div.row",
                           fields={"titles": {"selector": selector, "all": True}})
        assert rule.fields["titles"].selector == selector


class TestThePredicateIsNotAStep:
    """A row-safe selector must not be refused for the shape of its filter.

    Splitting on `/` and `::` naively read `.//a[descendant::h3]` as a step
    named `a[descendant` and the `|` inside `.//a[@title="a|b"]` as a union.
    A false rejection costs an author a working recipe just as surely as a
    missed escape costs a caller a wrong one.
    """

    @pytest.mark.parametrize("selector", [
        ".//a[descendant::h3]", ".//a[child::h3]", ".//a[@title='a|b']",
        ".//a[contains(@href,'..')]", './/a[@data-x="self::x"]',
    ])
    def test_a_safe_selector_with_an_awkward_predicate_is_accepted(self, selector):
        rule = ExtractRule(type="xpath", container=ROWS_XPATH,
                           fields={"t": {"selector": selector, "all": True}})
        assert rule.fields["t"].selector == selector

    @pytest.mark.parametrize("selector", [
        ".//h3 | .//h2", ".//h3|//h3", ".//h3[1]|//h3", ".//a[@href]|//a",
        ".//h3[1] | //h3", "descendant::h3[1]|/html//h3",
    ])
    def test_a_real_union_is_still_refused(self, selector):
        """Including one hidden behind a predicate: reading only up to the
        first `[` let `.//h3[1]|//h3` through."""
        with pytest.raises(ValidationError, match="union"):
            ExtractRule(type="xpath", container=ROWS_XPATH,
                        fields={"t": {"selector": selector, "all": True}})


class TestAnEmptyContainerIsAMistake:
    """`container: ""` used to pass validation as "set" -- so every field was
    forced to `all: true` -- and then fail the truthiness test in the
    extractor, silently reverting to document-wide extraction with none of the
    promised warnings."""

    @pytest.mark.parametrize("container", ["", "   "])
    def test_it_is_refused(self, container):
        with pytest.raises(ValidationError, match="container"):
            ExtractRule(type="css", container=container,
                        fields={"t": {"selector": "h3", "all": True}})

    def test_omitting_it_is_still_how_you_say_no_rows(self):
        rule = ExtractRule(type="css", fields={"t": {"selector": "h3"}})
        assert rule.container is None


class TestTheStepScannerItself:
    """Pinned directly, because reverting it to `text.split("/")` left the
    whole suite green -- the case that distinguishes them is a slash inside a
    string literal, which no other test happens to write."""

    def test_a_slash_inside_a_string_literal_is_not_a_step(self):
        from src.extract.models import _top_level_steps

        assert _top_level_steps(".//a[contains(@href,'/..')]") == [
            ".", "", "a[contains(@href,'/..')]"
        ]

    def test_such_a_selector_is_accepted(self):
        rule = ExtractRule(type="xpath", container=ROWS_XPATH, fields={
            "t": {"selector": ".//a[contains(@href,'/..')]", "all": True},
        })
        assert rule.fields["t"].selector == ".//a[contains(@href,'/..')]"

    def test_a_slash_outside_one_still_splits(self):
        from src.extract.models import _top_level_steps

        assert _top_level_steps(".//div/h3") == [".", "", "div", "h3"]
