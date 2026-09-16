"""Guards for the google_search row-alignment fix.

extract_fields matches each field's selector against the whole document
independently and returns flat parallel arrays; consumers zip them by index.
Any field matching a different number of nodes per result misaligns the
response — no exception, just wrong data lined up under the wrong result.
The `row_alignment_mismatch` guard catches the subset where the field lengths
end up unequal; a value corrupted in place keeps the lengths equal and stays
invisible to it.

pydantic's default `extra="ignore"` means a typo'd JSON key vanishes
silently instead of failing validation, so these tests don't just check the
preset parses — they pin the exact selector/attr configuration on the loaded
`FieldRule` objects, and exercise `extract_fields` against a small hand-built
SERP fixture that reproduces the real edge cases the fix addresses (ads/PAA
blocks with a generic `data-hveid` marker, a non-h3 link inside a result, and
snippet markup with a nested `.VwiC3b` span that must not be double-counted).
"""
from __future__ import annotations

import json

from src.extract.extractor import extract_fields
from src.presets.models import Preset
from src.presets.store import DEFAULT_BUILTIN_DIR


def _load(name: str) -> Preset:
    path = DEFAULT_BUILTIN_DIR / f"{name}.json"
    return Preset(**json.loads(path.read_text(encoding="utf-8")))


_GOOGLE_SERP = """
<html><body>
<div id="rso">
  <div class="tF2Cxc">
    <div class="yuRUbf">
      <a href="https://a.example/1" jsname="UWckNb"><h3>Result One</h3><cite>https://a.example<span> › 1</span></cite></a>
      <a href="https://cache.example/1">Cached</a>
    </div>
    <div class="VwiC3b" data-sncf="1">Snippet one text<span class="VwiC3b"> nested duplicate</span></div>
  </div>
  <div class="tF2Cxc">
    <div class="yuRUbf"><a href="https://b.example/2" jsname="UWckNb"><h3>Result Two</h3><cite>https://b.example › 2</cite></a></div>
    <div class="VwiC3b" data-sncf="1">Snippet two text</div>
  </div>
</div>
<div data-hveid="ad1" class="ads-ad">
  <h3>Sponsored result, not organic</h3>
  <a href="https://ads.example/x">ad link</a>
</div>
<div data-hveid="paa1">
  <div>People also ask block<h3>PAA question</h3></div>
</div>
</body></html>
"""


class TestGoogleSearchSelectors:
    def setup_method(self):
        self.preset = _load("google_search_chromium")
        self.fields = self.preset.parsing_instructions.fields

    def test_titles_scoped_to_rso_result_container(self):
        assert self.fields["titles"].selector == "#rso div.tF2Cxc h3"
        assert self.fields["titles"].required is True

    def test_links_use_structural_has_h3_not_generic_http_href(self):
        rule = self.fields["links"]
        assert rule.selector == "#rso div.tF2Cxc a:has(h3)"
        assert rule.attr == "href"

    def test_snippets_anchored_to_always_present_result_container(self):
        """snippets must NOT select div[data-sncf='1'] directly.

        That node exists only when the result HAS a snippet, so a snippet-less
        result shrinks the array and shifts every later row up one. Anchor to
        div.tF2Cxc (present on every result) and dig the text out of its html.
        """
        rule = self.fields["snippets"]
        assert rule.selector == "#rso div.tF2Cxc"
        assert rule.attr == "html"
        # Union with .VwiC3b was the double-count bug — never bring it back.
        assert "VwiC3b" not in rule.selector

        ops = [step.op for step in rule.post_process]
        assert ops == ["regex", "strip_tags"]
        # The regex must stay anchored on the snippet node's own marker and
        # non-greedy: a greedy slice of the block's html swallows the title.
        pattern = rule.post_process[0].args[0]
        assert "data-sncf" in pattern
        assert "(.*?)" in pattern, "regex must be non-greedy/bounded"

    def test_result_blocks_scoped_not_generic_hveid(self):
        rule = self.fields["result_blocks"]
        assert rule.selector == "#rso div.tF2Cxc"
        assert rule.attr == "html"

    def test_self_heal_still_enabled(self):
        assert self.preset.self_heal is True

    def test_version_bumped(self):
        assert self.preset.version >= 2


class TestGoogleSearchExtraction:
    def setup_method(self):
        self.preset = _load("google_search_chromium")

    def test_all_fields_aligned_and_ads_paa_excluded(self):
        data, warnings = extract_fields(_GOOGLE_SERP, self.preset.parsing_instructions)

        assert data["titles"] == ["Result One", "Result Two"]
        # exactly one link per organic result, not every href in the block
        assert data["links"] == ["https://a.example/1", "https://b.example/2"]
        assert len(data["snippets"]) == 2
        assert data["snippets"][0] == "Snippet one text nested duplicate"
        assert data["snippets"][1] == "Snippet two text"
        assert len(data["result_blocks"]) == 2

        for key in ("titles", "links", "snippets", "result_blocks"):
            assert len(data[key]) == len(data["titles"]), key

        assert not warnings

    def test_snippet_not_doubled_by_nested_vwic3b(self):
        data, _ = extract_fields(_GOOGLE_SERP, self.preset.parsing_instructions)
        # old union selector matched the parent AND the nested .VwiC3b span,
        # doubling every snippet that had inline-highlighted nested markup.
        assert len(data["snippets"]) == len(data["titles"])


class TestGoogleSearchSnippetlessResult:
    """The regression this preset's snippet anchoring exists to prevent.

    REAL, observed live on google.co.uk?q=github (2026-07-17): the FIRST
    organic result (the rich/featured GitHub result) carries no data-sncf
    node. While `snippets` selected `div[data-sncf='1']` directly — a node
    that exists only when the snippet does — that gave snippets=5 against
    titles=6 and shifted every snippet up one row. It went unnoticed because
    the `row_alignment_mismatch` guard did not exist yet; it would report the
    same shape today, though the anchoring below is what prevents it.
    """

    def setup_method(self):
        self.preset = _load("google_search_chromium")

    def test_result_without_snippet_yields_none_in_its_own_slot(self):
        serp = """
        <html><body><div id="rso">
          <div class="tF2Cxc">
            <div class="yuRUbf"><a href="https://a.example"><h3>Rich result, no snippet</h3><cite>https://a.example</cite></a></div>
          </div>
          <div class="tF2Cxc">
            <div class="yuRUbf"><a href="https://b.example"><h3>Second</h3><cite>https://b.example</cite></a></div>
            <div class="kb0PBd" data-sncf="1"><div class="VwiC3b"><span>Snippet that belongs to Second</span></div></div>
          </div>
        </div></body></html>
        """
        data, warnings = extract_fields(serp, self.preset.parsing_instructions)

        assert data["titles"] == ["Rich result, no snippet", "Second"]
        # The missing snippet lands as None in ITS OWN slot; it does not shrink
        # the array, so "Second" keeps its own snippet at its own index.
        assert data["snippets"] == [None, "Snippet that belongs to Second"]
        for key in ("titles", "links", "snippets", "result_blocks"):
            assert len(data[key]) == len(data["titles"]), key
        assert not warnings

    def test_real_google_snippet_markup_yields_text_not_markup(self):
        """Live markup: <em> highlights + a trailing "Read more" <a> whose
        href/ping carry &amp; entities. A bare regex capture would ship all of
        that as the snippet's "text"; strip_tags renders it down to the same
        string attr='text' produced before the anchoring change.
        """
        serp = """
        <html><body><div id="rso">
          <div class="tF2Cxc">
            <div class="yuRUbf"><a href="https://a.example"><h3>GitHub</h3></a></div>
            <div class="kb0PBd A9Y9g" data-snf="nke7rc" data-sncf="1"><div class="VwiC3b yXK7lf" style="-webkit-line-clamp:2"><span>GitHub is a <em>proprietary developer platform</em> that allows developers to create, store, manage, and share their code.</span><a class="vzmbzf" href="https://en.wikipedia.org/wiki/GitHub#:~:text=x&amp;text=y" ping="/url?sa=t&amp;source=web"><span>Read more</span></a></div></div>
          </div>
        </div></body></html>
        """
        data, warnings = extract_fields(serp, self.preset.parsing_instructions)

        assert data["snippets"] == [
            "GitHub is a proprietary developer platform that allows developers "
            "to create, store, manage, and share their code.Read more"
        ]
        snippet = data["snippets"][0]
        assert "<" not in snippet and "&amp;" not in snippet
        assert "wikipedia.org" not in snippet, "href must not leak into the text"
        assert not warnings


class TestGoogleSearchKnownAlignmentLimitations:
    """Pins a way the `#rso div.tF2Cxc` scoping still misaligns.

    NOT fixed — documented so the next maintainer sees the real boundary of
    the fix instead of trusting the scoping blindly. `snippets` is anchored to
    the always-present result container, but titles/links still rest on a
    1-h3-per-result assumption, so this remains the array-grow class: wrong
    for every row after the first offender. The `row_alignment_mismatch` guard
    reports it (the grown fields end up longer), but does not prevent it.
    """

    def setup_method(self):
        self.preset = _load("google_search_chromium")

    def test_sitelinks_inside_result_grow_titles_and_links(self):
        """Latent: an extra <h3> inside a tF2Cxc breaks the 1-h3-per-result
        assumption titles/links rest on.

        Not observed on the live google.co.uk captures for ?q=github or
        ?q=python+tutorial (every organic block had exactly 1 h3; no
        HiHjCd/zBAuLc sitelink markup inside #rso div.tF2Cxc), so the scoping
        holds for Google's current layout — but the selectors do not
        structurally prevent it.

        This fixture also pins mode (a) of the snippet regex's boundary — a
        following div sibling appends its text to the snippet. That is one of
        three modes; see TestGoogleSearchSnippetRegexBoundary for the other two
        and for the live measurements.
        """
        serp = """
        <html><body><div id="rso">
          <div class="tF2Cxc">
            <div class="yuRUbf"><a href="https://main.example"><h3>Main Result</h3><cite>https://main.example</cite></a></div>
            <div class="VwiC3b" data-sncf="1">Main snippet</div>
            <div class="HiHjCd"><table><tr>
              <td><a href="https://main.example/about"><h3 class="zBAuLc">About</h3></a></td>
              <td><a href="https://main.example/careers"><h3 class="zBAuLc">Careers</h3></a></td>
            </tr></table></div>
          </div>
          <div class="tF2Cxc">
            <div class="yuRUbf"><a href="https://b.example"><h3>Second Result</h3><cite>https://b.example</cite></a></div>
            <div class="VwiC3b" data-sncf="1">Second snippet</div>
          </div>
        </div></body></html>
        """
        data, warnings = extract_fields(serp, self.preset.parsing_instructions)

        # titles/links GREW to include sitelinks; snippets/result_blocks did not.
        assert data["titles"] == ["Main Result", "About", "Careers", "Second Result"]
        assert len(data["links"]) == 4
        assert len(data["snippets"]) == 2
        assert len(data["result_blocks"]) == 2
        # Misaligned and reported: titles/links (4) outrun the anchored
        # fields (2), which is exactly the shape the length guard detects.
        assert len(warnings) == 1
        assert warnings[0].startswith("row_alignment_mismatch:")
        assert "'titles'=4" in warnings[0]
        assert "'snippets'=2" in warnings[0]

        # Snippet stays one-per-result (alignment holds), but the sitelink text
        # rendered after it bleeds into the FIRST result's own snippet. The
        # second result — with nothing after its snippet — is unaffected.
        assert data["snippets"][0] == "Main snippet About Careers"
        assert data["snippets"][1] == "Second snippet"


class TestGoogleSearchSnippetRegexBoundary:
    r"""The three ways `(?s)data-sncf=["']1["'][^>]*>(.*?)</div>\s*</div>` bends.

    The regex slices the snippet out of the result container's html by running
    from the snippet node's marker to the FIRST `</div></div>` pair it can
    reach. What that pair turns out to be depends on markup the pattern does
    not control, giving three distinct modes:

      (a) a following DIV sibling — the pair lands at the end of *that* div, so
          its text is APPENDED to the snippet. Pinned in
          TestGoogleSearchKnownAlignmentLimitations
          .test_sitelinks_inside_result_grow_titles_and_links
          ('Main snippet About Careers').
      (b) a following NON-div sibling (<span>, <h3>, <a>) — no `</div></div>`
          pair is ever reached, the match fails, and the snippet is DELETED
          outright (None). Pinned below.
      (c) an adjacent `</div></div>` INSIDE the snippet — the capture stops
          there and everything after it is DROPPED. Pinned below.

    (b) and (c) LOSE text where (a) adds it, which makes them the more
    dangerous pair: a None or half-length snippet reads downstream as "this
    result has no snippet" rather than as visible corruption. (b) is also the
    likeliest real drift — Google need only render a single inline node after
    the snippet to blank the whole column.

    None of the three breaks alignment: the wrong, short or None value always
    lands in its OWN row's slot, never a neighbour's, because the field stays
    anchored to div.tF2Cxc. They are therefore invisible to the
    `row_alignment_mismatch` length guard too — it compares field lengths, and
    a corrupted-but-present value keeps the lengths equal. The lone exception
    below is a trailing <h3>, which trips the guard by growing `titles`; that
    is the array-grow class riding along, not the snippet regex being caught.

    Measured 2026-07-17 on the live captures (google.co.uk ?q=github,
    ?q=python+tutorial, plus the preset corpus — 17 snippet-bearing blocks):
    every data-sncf node is the LAST element in its block (0 following
    siblings) and none contains an adjacent `</div></div>`. All three modes are
    therefore synthetic today — documented so the next maintainer knows the
    pattern's real edges instead of trusting it blindly.
    """

    def setup_method(self):
        self.preset = _load("google_search_chromium")

    def _extract(self, block_body: str):
        serp = f'<html><body><div id="rso"><div class="tF2Cxc">{block_body}</div></div></body></html>'
        return extract_fields(serp, self.preset.parsing_instructions)

    def _snippets(self, block_body: str):
        """Snippets for a one-result SERP, asserting the mode was alignment
        neutral: the corrupted value landed in its own row's slot, leaving
        every parallel array the same length and the guard quiet."""
        data, warnings = self._extract(block_body)
        assert not warnings, warnings
        return data["snippets"]

    @staticmethod
    def _block_with_trailing(tag: str) -> str:
        return (
            '<div class="yuRUbf"><a href="https://a.example"><h3>T</h3></a></div>'
            '<div class="VwiC3b" data-sncf="1">Snippet text</div>'
            f"<{tag}>trailing inline node</{tag}>"
        )

    def test_mode_b_non_div_sibling_after_snippet_deletes_it(self):
        # <span>/<a> after the snippet: the regex needs a `</div></div>`
        # pair and never finds one, so the snippet vanishes entirely — the
        # None still occupies its own slot, so no array changes length.
        for tag in ("span", "a"):
            assert self._snippets(self._block_with_trailing(tag)) == [None], tag

    def test_mode_b_trailing_h3_also_grows_titles(self):
        # <h3> is the one trailing node that is not alignment neutral: it
        # deletes the snippet like any non-div sibling AND matches the titles
        # selector, so titles outrun the anchored fields. That length gap —
        # not the snippet deletion — is what the guard can see.
        data, warnings = self._extract(self._block_with_trailing("h3"))

        assert data["snippets"] == [None]
        assert data["titles"] == ["T", "trailing inline node"]
        assert len(warnings) == 1
        assert warnings[0].startswith("row_alignment_mismatch:")

    def test_mode_b_keeps_the_none_in_its_own_slot(self):
        # The deletion is bounded to the offending row: the next result keeps
        # its own snippet at its own index rather than sliding up into slot 0.
        serp = """
        <html><body><div id="rso">
          <div class="tF2Cxc">
            <div class="yuRUbf"><a href="https://a.example"><h3>First</h3></a></div>
            <div class="VwiC3b" data-sncf="1">Snippet of First</div>
            <span>trailing inline node</span>
          </div>
          <div class="tF2Cxc">
            <div class="yuRUbf"><a href="https://b.example"><h3>Second</h3><cite>https://b.example</cite></a></div>
            <div class="VwiC3b" data-sncf="1">Snippet of Second</div>
          </div>
        </div></body></html>
        """
        data, warnings = extract_fields(serp, self.preset.parsing_instructions)
        assert data["titles"] == ["First", "Second"]
        assert data["snippets"] == [None, "Snippet of Second"]
        for key in ("titles", "links", "snippets", "result_blocks"):
            assert len(data[key]) == len(data["titles"]), key
        assert not warnings

    def test_mode_c_nested_divs_truncate_the_snippet(self):
        # A nested block inside the snippet closes two divs back-to-back; the
        # non-greedy capture stops at that pair and the tail is dropped.
        assert self._snippets(
            '<div class="yuRUbf"><a href="https://a.example"><h3>T</h3></a></div>'
            '<div class="kb0PBd" data-sncf="1">'
            '<div class="VwiC3b"><div class="nested">Inner</div></div>'
            "TAIL THAT IS SILENTLY DROPPED</div>"
        ) == ["Inner"]
