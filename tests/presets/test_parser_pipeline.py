from __future__ import annotations

import pytest

from src.extract.models import FieldRule
from src.presets.models import ParsingInstructions
from src.presets import parser_pipeline as pp


HTML_OK = "<html><body><h1 id='t'>Widget</h1><span class='p'>$9.99</span></body></html>"
HTML_CHANGED = "<html><body><h2 class='new-title'>Widget</h2></body></html>"


def _instr(selector: str = "#t") -> ParsingInstructions:
    return ParsingInstructions(
        type="css",
        fields={"title": FieldRule(selector=selector, required=True)},
    )


class TestDeterministic:
    @pytest.mark.asyncio
    async def test_happy_path_no_llm(self, mocker):
        spy = mocker.patch.object(pp, "generate_selectors")
        result = await pp.run(
            HTML_OK,
            instructions=_instr("#t"),
            self_heal=True,
            llm_model="openai/gpt-5.4-mini",
            output_schema=None,
            llm_extract_prompt=None,
        )
        assert result.mode == "deterministic"
        assert result.data["title"] == "Widget"
        assert result.healed_instructions is None
        spy.assert_not_called()  # LLM never touched when selectors work


class TestSelfHeal:
    @pytest.mark.asyncio
    async def test_regenerates_when_required_missing(self, mocker):
        healed = _instr(".new-title")
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )
        result = await pp.run(
            HTML_CHANGED,
            instructions=_instr("#t"),  # stale selector, matches nothing
            self_heal=True,
            llm_model="openai/gpt-5.4-mini",
            output_schema=None,
            llm_extract_prompt=None,
        )
        assert result.mode == "self_healed"
        assert result.data["title"] == "Widget"
        # Not `is healed`: what goes to the preset carries the ORIGINAL contract
        # with the healed selectors, so identity with the model's object is the
        # thing that must not hold.
        assert result.healed_instructions.fields["title"].selector == ".new-title"
        assert result.healed_instructions.fields["title"].required is True
        assert any("self_healed" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_no_self_heal_when_disabled(self, mocker):
        spy = mocker.patch.object(pp, "generate_selectors")
        result = await pp.run(
            HTML_CHANGED,
            instructions=_instr("#t"),
            self_heal=False,
            llm_model="openai/gpt-5.4-mini",
            output_schema=None,
            llm_extract_prompt=None,
        )
        assert result.mode == "deterministic"
        # required field missing -> warning, but no LLM
        assert result.data["title"] is None
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_self_heal_without_model(self, mocker):
        spy = mocker.patch.object(pp, "generate_selectors")
        result = await pp.run(
            HTML_CHANGED,
            instructions=_instr("#t"),
            self_heal=True,
            llm_model=None,  # LLM disabled
            output_schema=None,
            llm_extract_prompt=None,
        )
        assert result.mode == "deterministic"
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_heal_still_failing_returns_partial_deterministic(
        self, mocker
    ):
        # healed selectors also miss and there's no AI-only fallback: the
        # caller still gets the (incomplete) deterministic data + warnings,
        # not a null result.
        mocker.patch.object(
            pp,
            "generate_selectors",
            new=mocker.AsyncMock(return_value=_instr("#still-wrong")),
        )
        result = await pp.run(
            HTML_CHANGED,
            instructions=_instr("#t"),
            self_heal=True,
            llm_model="m",
            output_schema=None,
            llm_extract_prompt=None,
        )
        assert result.mode == "deterministic"
        assert result.healed_instructions is None
        assert any("self_heal_did_not_recover" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_llm_error_during_heal_is_swallowed(self, mocker):
        from src.presets.llm.client import LLMError

        mocker.patch.object(
            pp,
            "generate_selectors",
            new=mocker.AsyncMock(side_effect=LLMError("boom")),
        )
        result = await pp.run(
            HTML_CHANGED,
            instructions=_instr("#t"),
            self_heal=True,
            llm_model="m",
            output_schema=None,
            llm_extract_prompt=None,
        )
        assert result.mode == "deterministic"
        assert any("self_heal_failed" in w for w in result.warnings)


class TestAiOnly:
    @pytest.mark.asyncio
    async def test_ai_only_when_no_instructions(self, mocker):
        mocker.patch.object(
            pp,
            "llm_extract",
            new=mocker.AsyncMock(return_value={"title": "Widget"}),
        )
        result = await pp.run(
            HTML_OK,
            instructions=None,
            self_heal=False,
            llm_model="m",
            output_schema={"title": "string"},
            llm_extract_prompt=None,
        )
        assert result.mode == "llm_extracted"
        assert result.data == {"title": "Widget"}

    @pytest.mark.asyncio
    async def test_ai_only_prompt_mode(self, mocker):
        mocker.patch.object(
            pp,
            "llm_extract",
            new=mocker.AsyncMock(return_value={"summary": "x"}),
        )
        result = await pp.run(
            HTML_OK,
            instructions=None,
            self_heal=False,
            llm_model="m",
            output_schema=None,
            llm_extract_prompt="summarize",
        )
        assert result.mode == "llm_extracted"

    @pytest.mark.asyncio
    async def test_no_parser_at_all(self):
        result = await pp.run(
            HTML_OK,
            instructions=None,
            self_heal=False,
            llm_model=None,
            output_schema=None,
            llm_extract_prompt=None,
        )
        assert result.mode == "none"
        assert result.data is None

    @pytest.mark.asyncio
    async def test_ai_only_llm_error_returns_none_mode(self, mocker):
        from src.presets.llm.client import LLMError

        mocker.patch.object(
            pp, "llm_extract", new=mocker.AsyncMock(side_effect=LLMError("x"))
        )
        result = await pp.run(
            HTML_OK,
            instructions=None,
            self_heal=False,
            llm_model="m",
            output_schema={"title": "string"},
            llm_extract_prompt=None,
        )
        assert result.mode == "none"
        assert any("llm_extract_failed" in w for w in result.warnings)


class TestHealSuccessIsJudgedByThePresetNotByTheLLM:
    """Whose `required` decides that a heal worked.

    The healed instructions come back from the model, `required` flags and all,
    and the pipeline used them to grade its own homework: a plan that marks
    nothing required trivially "recovers", the result is labelled self_healed,
    and for a user preset those selectors are then written over the working
    ones. The preset's own contract is the only one the caller ever agreed to.
    """

    @pytest.mark.asyncio
    async def test_a_heal_that_drops_required_is_not_a_recovery(self, mocker):
        # The model returns selectors that extract nothing from this page, and
        # quietly marks the field optional — the shape that used to "succeed".
        healed = ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector=".does-not-exist", required=False)},
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )

        result = await pp.run(
            HTML_CHANGED,
            instructions=_instr("#t"),
            self_heal=True,
            llm_model="openai/gpt-5.4-mini",
            output_schema=None,
            llm_extract_prompt=None,
        )

        assert result.mode != "self_healed", "an empty title is not a recovered title"
        assert result.healed_instructions is None, "nothing to persist over a good preset"
        assert "self_heal_did_not_recover" in result.warnings

    @pytest.mark.asyncio
    async def test_a_heal_that_fills_the_required_field_still_counts(self, mocker):
        """The fix must not break the case self-heal exists for."""
        healed = ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector=".new-title", required=False)},
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )

        result = await pp.run(
            HTML_CHANGED,
            instructions=_instr("#t"),
            self_heal=True,
            llm_model="openai/gpt-5.4-mini",
            output_schema=None,
            llm_extract_prompt=None,
        )

        assert result.mode == "self_healed"
        assert result.data["title"] == "Widget"
        # NOT `is healed`: what gets persisted must carry the PRESET's contract.
        # Pinning the model's object is what let `required: false` reach the
        # preset and silence the next drift.
        assert result.healed_instructions is not None
        assert result.healed_instructions.fields["title"].selector == ".new-title"
        assert result.healed_instructions.fields["title"].required is True


class TestWhatGetsWrittenToThePreset:
    """A heal replaces `parsing_instructions` wholesale, so the object returned
    for persistence is the preset's next CONTRACT, not just its next selectors.
    """

    @pytest.mark.asyncio
    async def test_the_original_required_flags_survive_a_heal(self, mocker):
        """Otherwise the second drift is silent.

        Measured before this: scrape 1 persisted `required: false`; scrape 2
        found `_missing_required` empty, never called the LLM, and returned
        {"title": None} labelled deterministic with an EMPTY warnings list.
        """
        healed = ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector=".new-title", required=False)},
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )

        result = await pp.run(
            HTML_CHANGED, instructions=_instr("#t"), self_heal=True,
            llm_model="openai/gpt-5.4-mini", output_schema=None,
            llm_extract_prompt=None,
        )

        persisted = result.healed_instructions
        assert persisted.fields["title"].required is True
        # And the consequence that matters: on the NEXT drift, the persisted
        # plan still reports the field as missing, so healing runs again.
        data, _ = pp.extract_fields("<html><body>nothing here</body></html>", persisted)
        assert pp._missing_required(persisted, data) == ["title"]

    @pytest.mark.asyncio
    async def test_a_heal_that_drops_fields_is_used_but_never_persisted(self, mocker):
        """Persisting it would truncate the preset for every later scrape."""
        original = ParsingInstructions(
            type="css",
            fields={
                "title": FieldRule(selector="#t", required=True),
                "price": FieldRule(selector=".p", required=False),
            },
        )
        healed = ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector=".new-title", required=True)},
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )

        result = await pp.run(
            HTML_CHANGED, instructions=original, self_heal=True,
            llm_model="openai/gpt-5.4-mini", output_schema=None,
            llm_extract_prompt=None,
        )

        assert result.data["title"] == "Widget", "the caller still gets the repair"
        assert result.healed_instructions is None, "but the preset keeps its fields"
        assert any("self_heal_not_persisted" in w for w in result.warnings)
        assert any("price" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_invented_fields_do_not_join_the_preset(self, mocker):
        healed = ParsingInstructions(
            type="css",
            fields={
                "title": FieldRule(selector=".new-title", required=True),
                "surprise": FieldRule(selector="h2", required=True),
            },
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )

        result = await pp.run(
            HTML_CHANGED, instructions=_instr("#t"), self_heal=True,
            llm_model="openai/gpt-5.4-mini", output_schema=None,
            llm_extract_prompt=None,
        )

        assert set(result.healed_instructions.fields) == {"title"}

    @pytest.mark.asyncio
    async def test_a_renamed_field_is_not_a_recovery(self, mocker):
        """Contentious on purpose: data WAS extracted, under another name.

        The preset's consumers read `title`; a plan that fills `product_title`
        leaves them with nothing, and persisting it would rename the field for
        every later scrape. So it fails the grade like any other empty required
        field.
        """
        healed = ParsingInstructions(
            type="css",
            fields={"product_title": FieldRule(selector=".new-title", required=True)},
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )

        result = await pp.run(
            HTML_CHANGED, instructions=_instr("#t"), self_heal=True,
            llm_model="openai/gpt-5.4-mini", output_schema=None,
            llm_extract_prompt=None,
        )

        assert result.mode != "self_healed"
        assert result.healed_instructions is None


class TestTheHealOnlyContributesSelectors:
    """Raimonte's review: restoring `required` alone is not the contract.

    A field rule is more than a selector — `all`, `attr` and `post_process` are
    what turn matched nodes into the shape a consumer reads. Taking those from
    the model meant `amazon_search.urls` (attr="href", all=True) could come back
    as one text string, and `prices` (post_process=[parse_price]) as an
    uncoerced label — silently, through the mechanism built to prevent exactly
    that. So the heal contributes the selector and nothing else.
    """

    @staticmethod
    def _rich_original() -> ParsingInstructions:
        return ParsingInstructions(
            type="css",
            fields={
                "prices": FieldRule(
                    selector=".old-price", all=True, required=True,
                    post_process=[{"op": "parse_price"}],
                ),
                "urls": FieldRule(selector=".old-link", attr="href", all=True),
            },
        )

    @pytest.mark.asyncio
    async def test_all_attr_and_post_process_come_from_the_preset(self, mocker):
        # The model answers with a bare scalar text rule for every field — the
        # shape an LLM asked only for selectors tends to produce.
        healed = ParsingInstructions(
            type="css",
            fields={
                "prices": FieldRule(selector=".new-price"),
                "urls": FieldRule(selector=".new-link"),
            },
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )
        html = (
            "<html><body>"
            "<span class='new-price'>$9.99</span><span class='new-price'>$12.50</span>"
            "<a class='new-link' href='/a'>A</a><a class='new-link' href='/b'>B</a>"
            "</body></html>"
        )

        result = await pp.run(
            html, instructions=self._rich_original(), self_heal=True,
            llm_model="openai/gpt-5.4-mini", output_schema=None,
            llm_extract_prompt=None,
        )

        assert result.mode == "self_healed"
        persisted = result.healed_instructions
        assert persisted.fields["prices"].selector == ".new-price", "the heal's contribution"
        assert persisted.fields["prices"].all is True, "list-ness is the preset's, not the model's"
        assert [p.op for p in persisted.fields["prices"].post_process] == ["parse_price"]
        assert persisted.fields["urls"].attr == "href", "an href field must not become text"
        assert persisted.fields["urls"].all is True

        # And the DATA the caller gets must come from that restored plan, not
        # from the model's: coerced numbers and lists, not labels and scalars.
        assert result.data["prices"] == [9.99, 12.5]
        assert result.data["urls"] == ["/a", "/b"]

    @pytest.mark.asyncio
    async def test_a_heal_whose_post_process_cannot_cope_is_not_a_recovery(self, mocker):
        """The case the review names: parse_price failing on the new markup.

        Graded on the model's plan the field looks filled, because without the
        coercion the raw label is a non-empty string. Graded on the RESTORED
        plan — the one that actually ships — the value is None and the heal has
        not recovered anything.
        """
        healed = ParsingInstructions(
            type="css",
            fields={
                "prices": FieldRule(selector=".not-a-price"),
                "urls": FieldRule(selector=".new-link"),
            },
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )
        html = (
            "<html><body><span class='not-a-price'>call for a quote</span>"
            "<a class='new-link' href='/a'>A</a></body></html>"
        )

        result = await pp.run(
            html, instructions=self._rich_original(), self_heal=True,
            llm_model="openai/gpt-5.4-mini", output_schema=None,
            llm_extract_prompt=None,
        )

        assert result.mode != "self_healed"
        assert result.healed_instructions is None


class TestSelfHealCannotBreakRowScoping:
    """`model_copy` does not re-run validators, and this is the one place a
    rule is assembled programmatically.

    `llm/selector_gen.py` never tells the model about `container`, so a heal
    always comes back document-scoped; the merge then re-attaches the
    original's container. Measured before the fix: `//h3` under a container
    returned the first row's value repeated once per row, with no warning, and
    for a user preset that was PERSISTED. A heal that cannot stay inside the
    row is refused, which keeps the working recipe.
    """

    ORIGINAL = ParsingInstructions(
        type="xpath", container="//div[@class='row']",
        fields={"t": {"selector": ".//h3", "all": True}},
    )

    def test_a_document_scoped_heal_is_refused(self):
        healed = ParsingInstructions(
            type="xpath", fields={"t": {"selector": "//h3", "all": True}},
        )
        assert pp._under_original_contract(self.ORIGINAL, healed) == (None, "escapes_container")

    def test_a_row_safe_heal_is_accepted_and_keeps_the_container(self):
        healed = ParsingInstructions(
            type="xpath", fields={"t": {"selector": ".//h2", "all": True}},
        )
        merged, refusal = pp._under_original_contract(self.ORIGINAL, healed)
        assert refusal is None and merged is not None
        assert merged.container == "//div[@class='row']"
        assert merged.fields["t"].selector == ".//h2"

    def test_an_unscoped_recipe_is_unaffected(self):
        original = ParsingInstructions(
            type="xpath", fields={"t": {"selector": "//h3", "all": True}},
        )
        healed = ParsingInstructions(
            type="xpath", fields={"t": {"selector": "//h2", "all": True}},
        )
        merged, refusal = pp._under_original_contract(original, healed)
        assert refusal is None
        assert merged is not None and merged.fields["t"].selector == "//h2"

    @pytest.mark.asyncio
    async def test_a_heal_that_escapes_the_row_is_not_run_for_this_request_either(
        self, mocker
    ):
        """Refusing to PERSIST it was not enough.

        `run()` falls back to executing the bare heal when the merge returns no
        plan, which drops the container and answers with unscoped columns --
        labelled `self_healed`, i.e. the misalignment the container exists to
        prevent, wearing a badge that says it is fixed.
        """
        rows = (
            '<div class="row"><h3>One</h3></div>'
            '<div class="row"><h3>Two</h3></div>'
        )
        original = ParsingInstructions(
            type="xpath", container="//div[@class='row']",
            fields={"t": {"selector": ".//span", "all": True, "required": True}},
        )
        healed = ParsingInstructions(
            type="xpath", fields={"t": {"selector": "//h3", "all": True}},
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )
        result = await pp.run(
            f"<html><body>{rows}</body></html>",
            instructions=original,
            self_heal=True,
            llm_model="openai/gpt-5.4-mini",
            output_schema=None,
            llm_extract_prompt=None,
        )
        assert result.mode == "deterministic", result.mode
        assert result.healed_instructions is None
        assert any("self_heal_refused" in w for w in result.warnings), result.warnings

    @pytest.mark.asyncio
    async def test_a_refused_heal_still_reaches_the_ai_only_tier(self, mocker):
        """The AI-only tier uses no selectors, so it cannot escape a container.

        An early return skipped it, which meant a recipe adopting `container`
        lost BOTH repair mechanisms at once -- and self-heal is refused every
        time for such a recipe unless the generator is told about the row.
        """
        original = ParsingInstructions(
            type="xpath", container="//div[@class='row']",
            fields={"t": {"selector": ".//span", "all": True, "required": True}},
        )
        healed = ParsingInstructions(
            type="xpath", fields={"t": {"selector": "//h3", "all": True}},
        )
        mocker.patch.object(
            pp, "generate_selectors", new=mocker.AsyncMock(return_value=healed)
        )
        extracted = mocker.patch.object(
            pp, "llm_extract", new=mocker.AsyncMock(return_value={"t": ["One"]})
        )
        result = await pp.run(
            '<html><body><div class="row"><h3>One</h3></div></body></html>',
            instructions=original,
            self_heal=True,
            llm_model="openai/gpt-5.4-mini",
            output_schema={"t": "list"},
            llm_extract_prompt=None,
        )
        extracted.assert_awaited_once()
        assert result.mode == "llm_extracted", result.mode
        # PRE-EXISTING, verified identical on origin/main: the AI-only tier
        # returns `warnings=["llm_extracted"]`, a hard-coded list that discards
        # everything accumulated before it -- `self_heal_did_not_recover` is
        # lost on this path today for the same reason. Pinned here so the next
        # reader does not assume the refusal note survives; fixing it changes
        # an existing path's warnings and belongs in its own change.
        assert result.warnings == ["llm_extracted"]

    @pytest.mark.asyncio
    async def test_the_generator_is_told_about_the_row(self, mocker):
        """Without this every heal of a row-scoped recipe comes back
        document-scoped and is refused -- self-heal traded away silently by
        the act of adopting `container`."""
        original = ParsingInstructions(
            type="xpath", container="//div[@class='row']",
            fields={"t": {"selector": ".//span", "all": True, "required": True}},
        )
        gen = mocker.patch.object(
            pp, "generate_selectors",
            new=mocker.AsyncMock(return_value=ParsingInstructions(
                type="xpath", container="//div[@class='row']",
                fields={"t": {"selector": ".//h3", "all": True}},
            )),
        )
        await pp.run(
            '<html><body><div class="row"><h3>One</h3></div></body></html>',
            instructions=original, self_heal=True,
            llm_model="openai/gpt-5.4-mini", output_schema=None,
            llm_extract_prompt=None,
        )
        assert gen.await_args.kwargs["container"] == "//div[@class='row']"

    def test_an_incomplete_heal_cannot_run_unscoped_either(self):
        """The same bypass through the other door.

        `incomplete` returned before the container was ever considered, and
        `run()` executes an incomplete plan bare for the current request --
        which for a scoped recipe means dropping the container and answering
        with unscoped columns labelled `self_healed`. Re-scoping it is not an
        option: the fields are columns of one row set, so a heal covering only
        some of them says nothing about keeping the rest aligned.
        """
        original = ParsingInstructions(
            type="xpath", container="//div[@class='row']",
            fields={
                "titles": {"selector": ".//h3", "all": True, "required": True},
                "snippets": {"selector": ".//p", "all": True},
            },
        )
        healed = ParsingInstructions(  # covers only the required field
            type="xpath", fields={"titles": {"selector": "//h3", "all": True}},
        )
        assert pp._under_original_contract(original, healed) == (
            None, "escapes_container"
        )

    def test_an_incomplete_heal_is_still_usable_when_there_is_no_container(self):
        """Unscoped recipes keep the existing behaviour: usable for this
        request, never persisted."""
        original = ParsingInstructions(
            type="xpath",
            fields={
                "titles": {"selector": "//h3", "all": True, "required": True},
                "snippets": {"selector": "//p", "all": True},
            },
        )
        healed = ParsingInstructions(
            type="xpath", fields={"titles": {"selector": "//h2", "all": True}},
        )
        assert pp._under_original_contract(original, healed) == (None, "incomplete")

    def test_the_container_keeps_its_own_language_when_the_heal_changes_type(self):
        """The container comes from the original, so it speaks the original's
        dialect. Taking the rule `type` from the heal handed an XPath
        container to cssselect and returned empty columns from a heal that
        otherwise worked."""
        original = ParsingInstructions(
            type="xpath", container="//div[@class='row']",
            fields={"t": {"selector": ".//h3", "all": True}},
        )
        healed = ParsingInstructions(
            type="css", fields={"t": {"selector": "h2", "all": True}},
        )
        merged, refusal = pp._under_original_contract(original, healed)
        assert refusal is None and merged is not None
        assert merged.type == "xpath", "the container's language must survive"
        assert merged.fields["t"].type == "css", "the field's dialect travels with it"
