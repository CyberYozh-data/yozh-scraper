from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.extract.models import FieldRule, ExtractRule, PostProcess


class TestFieldRule:
    def test_field_rule_defaults(self):
        """Defaults for FieldRule"""
        rule = FieldRule(selector="h1")

        assert rule.selector == "h1"
        assert rule.attr == "text"
        assert rule.all is False
        assert rule.required is False

    def test_field_rule_custom_attr(self):
        """FieldRule with custom attribute"""
        rule = FieldRule(selector="a", attr="href")

        assert rule.selector == "a"
        assert rule.attr == "href"

    def test_field_rule_all_flag(self):
        """FieldRule with set all"""
        rule = FieldRule(selector=".item", all=True)

        assert rule.all is True

    def test_field_rule_required_flag(self):
        """FieldRule with set required"""
        rule = FieldRule(selector=".required", required=True)

        assert rule.required is True

    def test_field_rule_html_attr(self):
        """FieldRule with attr html"""
        rule = FieldRule(selector="div.content", attr="html")

        assert rule.attr == "html"

    def test_field_rule_full(self):
        """Full FieldRule"""
        rule = FieldRule(
            selector=".items > .item",
            attr="data-id",
            all=True,
            required=True,
        )

        assert rule.selector == ".items > .item"
        assert rule.attr == "data-id"
        assert rule.all is True
        assert rule.required is True


class TestExtractRule:
    def test_extract_rule_css(self):
        """ExtractRule with CSS"""
        rule = ExtractRule(
            type="css",
            fields={
                "title": FieldRule(selector="h1"),
            },
        )

        assert rule.type == "css"
        assert "title" in rule.fields

    def test_extract_rule_xpath(self):
        """ExtractRule with XPath"""
        rule = ExtractRule(
            type="xpath",
            fields={
                "title": FieldRule(selector="//h1/text()"),
            },
        )

        assert rule.type == "xpath"

    def test_extract_rule_multiple_fields(self):
        """ExtractRule with a few fields"""
        rule = ExtractRule(
            type="css",
            fields={
                "title": FieldRule(selector="h1"),
                "description": FieldRule(selector="p.desc"),
                "links": FieldRule(selector="a", attr="href", all=True),
            },
        )

        assert len(rule.fields) == 3
        assert "title" in rule.fields
        assert "description" in rule.fields
        assert "links" in rule.fields

    def test_extract_rule_validation_css(self):
        """Validation CSS type"""
        rule = ExtractRule(type="css", fields={})
        assert rule.type == "css"

    def test_extract_rule_validation_xpath(self):
        """Validation XPath type"""
        rule = ExtractRule(type="xpath", fields={})
        assert rule.type == "xpath"


class TestPostProcessDocumentsTheUnwrapParamSafetyProperty:
    """`PostProcess.op`'s Field description IS the OpenAPI contract for this
    op -- it is what a preset author (and any client generating against
    /openapi.json) reads. The refusal `unwrap_param` performs was added as a
    documented property, not an implementation detail, so deleting the sentence
    that states it must fail. Nothing else in the suite reads this text.
    """

    @staticmethod
    def _description() -> str:
        return PostProcess.model_fields["op"].description or ""

    def test_the_refusal_is_stated(self):
        text = self._description()
        assert "absolute http(s) URL with a host" in text
        assert "plain relative path" in text
        for scheme in ("'javascript:'", "'data:'", "'file:'", "'ftp:'"):
            assert scheme in text, f"{scheme} not named as refused"
        assert "//host/path" in text, "the protocol-relative case is not named"
        assert "TWO OR MORE slashes or backslashes" in text, (
            "the slash-run rule -- not just the two-slash spelling -- must be "
            "stated, since that is what fix round 2 widened"
        )
        assert "naming no path segment of its own" in text

    def test_the_failure_DIRECTION_is_stated(self):
        """The property is not merely "bad values are rejected" -- it is that
        rejection is fail-SAFE (passthrough, never null). A caller relying on
        row alignment needs that half."""
        text = self._description()
        assert "UNCHANGED (never null)" in text
        assert "passthrough" in text

    def test_the_same_host_NON_guarantee_is_stated(self):
        """Fix-round-3 finding 1. This paragraph used to end "this op cannot
        introduce ... via a following 'urljoin' -- one on a host the page's own
        URL did not supply", which is FALSE: an absolute http(s) URL naming any
        host is accepted by design, and `urljoin` leaves an absolute reference
        alone. Measured through the shipped amazon_search_chromium pipeline
        with the materializer's base injected, `url=https%3A%2F%2Fevil.example
        .com%2Fx` yields `https://evil.example.com/x`.

        The op adds no capability a plain `href="https://evil.example.com"`
        did not already have, so this is a documentation defect rather than an
        exploit -- but it is the published /openapi.json text, a reader could
        take it as a same-host guarantee and skip their own check, and the
        sibling tests in this class exist to FREEZE this paragraph. A frozen
        false claim is worse than an unfrozen one."""
        text = self._description()
        assert "NOT a same-host guarantee" in text
        assert "must check the host itself" in text
        # The old wording must not come back.
        assert "did not supply" not in text


class TestUnwrapParamEncodingValidator:
    """The encoding argument is validated at preset-creation time, not per row.

    Added after review found the validator had NO test: deleting it outright
    left the whole suite green. Its own comment says an unknown encoding "would
    otherwise silently fall back to percent-decoding and ship a base64 blob as
    if it were a destination" -- which is exactly the shape of failure this
    repo keeps meeting, a guard that is present and proves nothing.
    """

    def test_a_known_encoding_is_accepted(self):
        for encoding in ("percent", "base64url"):
            assert PostProcess(op="unwrap_param", args=["u", encoding]).args[1] == encoding

    def test_the_default_needs_no_encoding_argument(self):
        assert PostProcess(op="unwrap_param", args=["url"]).args == ["url"]

    def test_an_unknown_encoding_is_refused(self):
        with pytest.raises(ValidationError, match="must be 'percent' or 'base64url'"):
            PostProcess(op="unwrap_param", args=["u", "rot13"])

    def test_the_param_name_is_still_required(self):
        with pytest.raises(ValidationError, match="requires 1 arg"):
            PostProcess(op="unwrap_param", args=[])
