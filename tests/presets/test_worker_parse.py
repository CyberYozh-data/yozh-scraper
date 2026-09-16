from __future__ import annotations

import time

import pytest

from src.extract.models import FieldRule, PostProcess
from src.presets import worker_parse as wp
from src.presets.models import LocaleProfile, ParsingInstructions, Preset
from src.presets.parser_pipeline import ParserResult
from src.presets.store import FilePresetStore, PresetStore


HTML = "<html><body><h1 id='t'>Widget</h1></body></html>"


def _instr(sel="#t"):
    return ParsingInstructions(
        type="css", fields={"title": FieldRule(selector=sel, required=True)}
    ).model_dump(mode="json")


def _preset(*, selector: str = "#old", version: int = 1) -> Preset:
    return Preset(
        name="user_p",
        source="custom",
        kind="user",
        url_template="https://e.com/{x}",
        request_defaults={},
        locales={"us": LocaleProfile(domain="com", country="US")},
        default_locale="us",
        parsing_instructions=ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector=selector, required=True)},
        ),
        version=version,
        updated_at=1.0,
    )


def _plan(**over):
    base = dict(
        self_heal=False,
        llm_model=None,
        output_schema=None,
        llm_extract_prompt=None,
        preset_name=None,
        preset_kind=None,
        materializer_injected={},
    )
    base.update(over)
    return base


class TestApply:
    @pytest.mark.asyncio
    async def test_no_plan_no_extract_returns_none(self):
        data, warnings = await wp.apply(HTML, None, None)
        assert data is None
        assert warnings == []

    @pytest.mark.asyncio
    async def test_extract_only_path_without_plan(self):
        # raw /scrape (no preset): behaves like the old direct extract
        data, warnings = await wp.apply(HTML, _instr("#t"), None)
        assert data == {"title": "Widget"}

    @pytest.mark.asyncio
    async def test_plan_runs_pipeline(self, mocker):
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["deterministic"],
                    mode="deterministic",
                )
            ),
        )
        data, warnings = await wp.apply(HTML, _instr("#t"), _plan())
        assert data == {"title": "Widget"}
        assert "deterministic" in warnings

    @pytest.mark.asyncio
    async def test_self_healed_user_preset_is_persisted(self, tmp_path, mocker):
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        store = PresetStore(user=FilePresetStore(base_path=user_dir))
        store.create(
            Preset(
                name="user_p",
                source="custom",
                kind="user",
                url_template="https://e.com/{x}",
                request_defaults={},
                locales={"us": LocaleProfile(domain="com", country="US")},
                default_locale="us",
                parsing_instructions=ParsingInstructions(
                    type="css",
                    fields={"title": FieldRule(selector="#old", required=True)},
                ),
                version=1,
                updated_at=1.0,
            )
        )
        healed = ParsingInstructions(
            type="css", fields={"title": FieldRule(selector="#t", required=True)}
        )
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["self_healed"],
                    mode="self_healed",
                    healed_instructions=healed,
                )
            ),
        )
        mocker.patch.object(wp, "_get_store", return_value=store)

        data, warnings = await wp.apply(
            HTML,
            _instr("#old"),
            _plan(self_heal=True, llm_model="m", preset_name="user_p",
                  preset_kind="user"),
        )
        assert data == {"title": "Widget"}
        saved = store.get("user_p")
        assert saved.parsing_instructions.fields["title"].selector == "#t"
        assert saved.version == 2
        assert saved.updated_at > 1.0

    @pytest.mark.asyncio
    async def test_a_preset_saved_while_the_job_ran_is_not_overwritten(
        self, tmp_path, mocker
    ):
        """The ordering that actually happens: the job read the preset when it
        was enqueued, the owner saved their own selectors while it rendered,
        and only then does the worker try to persist a language model's guess
        over them. Theirs is the edit that must survive -- if the page is
        still drifted the next scrape heals again from what they wrote.

        The save lands BEFORE `apply()` is called, because that is where the
        minutes are. An earlier version of this test injected it between the
        persist path's own read and its write, which is a window of
        microseconds that production does not have: it passed against a fix
        that left the real one wide open.
        """
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        store = PresetStore(user=FilePresetStore(base_path=user_dir))
        store.create(_preset(selector="#old"))
        # What the job carries: the preset as it stood when it was enqueued.
        _, stamp_at_enqueue = store.get_stamped("user_p")

        # ... and now the owner saves their own fix, while the page renders.
        store.update("user_p", _preset(selector="#mine-i-fixed-it", version=4))

        healed = ParsingInstructions(
            type="css", fields={"title": FieldRule(selector="#llm-guess", required=True)}
        )
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["self_healed"],
                    mode="self_healed",
                    healed_instructions=healed,
                )
            ),
        )
        mocker.patch.object(wp, "_get_store", return_value=store)

        data, warnings = await wp.apply(
            HTML,
            _instr("#old"),
            _plan(self_heal=True, llm_model="m", preset_name="user_p",
                  preset_kind="user", preset_stamp=stamp_at_enqueue),
        )

        # The request itself still gets the healed data -- only the write is
        # dropped.
        assert data == {"title": "Widget"}
        assert any("self_heal_persist_skipped" in w for w in warnings)
        saved = store.get("user_p")
        assert saved.parsing_instructions.fields["title"].selector == "#mine-i-fixed-it"
        assert saved.version == 4

    @pytest.mark.asyncio
    async def test_a_preset_somebody_else_is_writing_costs_only_a_warning(
        self, tmp_path, mocker
    ):
        """This runs inside the page task the scrape deadline is enforced on.
        Waiting out another writer's lock spends the page's budget, and the
        ceiling cancels the parse whole -- so a best-effort write would take
        an extracted page and two LLM calls down with it. It must not wait,
        and the caller must still get the data.
        """
        import fcntl
        import time as _time

        import src.presets.store as store_mod

        user_dir = tmp_path / "user"
        user_dir.mkdir()
        store = PresetStore(user=FilePresetStore(base_path=user_dir))
        store.create(_preset(selector="#old"))
        _, stamp = store.get_stamped("user_p")
        healed = ParsingInstructions(
            type="css", fields={"title": FieldRule(selector="#llm-guess", required=True)}
        )
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["self_healed"],
                    mode="self_healed",
                    healed_instructions=healed,
                )
            ),
        )
        mocker.patch.object(wp, "_get_store", return_value=store)

        lock = store_mod._lock_path(user_dir / "user_p.json")
        with open(lock, "w", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            started = _time.monotonic()
            data, warnings = await wp.apply(
                HTML,
                _instr("#old"),
                _plan(self_heal=True, llm_model="m", preset_name="user_p",
                      preset_kind="user", preset_stamp=stamp),
            )
            waited = _time.monotonic() - started

        assert data == {"title": "Widget"}, "the page was lost to a best-effort write"
        assert waited < 0.5, f"waited {waited:.2f}s on somebody else's lock"
        assert any("self_heal_persist_skipped" in w for w in warnings)
        assert store.get("user_p").parsing_instructions.fields["title"].selector == "#old"

    @pytest.mark.asyncio
    async def test_an_untouched_preset_still_takes_the_healed_selectors(
        self, tmp_path, mocker
    ):
        """The other half: nothing changed under the job, so the heal lands.
        Without this, refusing every write would pass the test above."""
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        store = PresetStore(user=FilePresetStore(base_path=user_dir))
        store.create(_preset(selector="#old"))
        _, stamp_at_enqueue = store.get_stamped("user_p")

        healed = ParsingInstructions(
            type="css", fields={"title": FieldRule(selector="#llm-guess", required=True)}
        )
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["self_healed"],
                    mode="self_healed",
                    healed_instructions=healed,
                )
            ),
        )
        mocker.patch.object(wp, "_get_store", return_value=store)

        _, warnings = await wp.apply(
            HTML,
            _instr("#old"),
            _plan(self_heal=True, llm_model="m", preset_name="user_p",
                  preset_kind="user", preset_stamp=stamp_at_enqueue),
        )

        assert not any("self_heal_persist_skipped" in w for w in warnings)
        saved = store.get("user_p")
        assert saved.parsing_instructions.fields["title"].selector == "#llm-guess"
        assert saved.version == 2

    @pytest.mark.asyncio
    async def test_self_heal_does_not_persist_a_materializer_injected_base(
        self, tmp_path, mocker
    ):
        """Fix-round-1 finding 3: a self-heal must not freeze the URL/locale
        THIS request's materialize() call injected into the stored preset
        forever -- the next request (a different locale, a different domain)
        would then silently resolve relative urls against a stale, wrong
        base. `result.healed_instructions` copies post_process verbatim from
        the already-materialized instructions (see
        parser_pipeline._under_original_contract), so it carries whatever was
        injected; `plan.materializer_injected` is what tells `apply()` which
        (op, field) pairs to blank back to empty before writing to the store.
        """
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        store = PresetStore(user=FilePresetStore(base_path=user_dir))
        injected_urljoin = PostProcess(
            op="urljoin", args=["https://e.com/x?injected_by=this_one_request"]
        )
        store.create(
            Preset(
                name="user_p",
                source="custom",
                kind="user",
                url_template="https://e.com/{x}",
                request_defaults={},
                locales={"us": LocaleProfile(domain="com", country="US")},
                default_locale="us",
                parsing_instructions=ParsingInstructions(
                    type="css",
                    fields={
                        "title": FieldRule(selector="#old", required=True),
                        "urls": FieldRule(
                            selector="a", attr="href", all=True,
                            post_process=[injected_urljoin],
                        ),
                    },
                ),
                version=1,
                updated_at=1.0,
            )
        )
        healed = ParsingInstructions(
            type="css",
            fields={
                "title": FieldRule(selector="#t", required=True),
                # The heal carries the SAME injected base forward, exactly as
                # `_under_original_contract` does in the real pipeline.
                "urls": FieldRule(
                    selector="a", attr="href", all=True,
                    post_process=[injected_urljoin],
                ),
            },
        )
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["self_healed"],
                    mode="self_healed",
                    healed_instructions=healed,
                )
            ),
        )
        mocker.patch.object(wp, "_get_store", return_value=store)

        await wp.apply(
            HTML,
            _instr("#old"),
            _plan(
                self_heal=True, llm_model="m", preset_name="user_p",
                preset_kind="user",
                materializer_injected={"urljoin": ["urls"]},
            ),
        )

        saved = store.get("user_p")
        # The selector heal still landed ...
        assert saved.parsing_instructions.fields["title"].selector == "#t"
        # ... but the injected base did NOT freeze in: it's back to empty, so
        # the very next materialize() call (whatever locale/domain THAT
        # request uses) re-injects a fresh, correct value.
        assert saved.parsing_instructions.fields["urls"].post_process[0].args == []

    @pytest.mark.asyncio
    async def test_self_heal_leaves_an_authors_explicit_base_untouched(
        self, tmp_path, mocker
    ):
        """The strip must be scoped to exactly what materialize() injected
        THIS request (`plan.materializer_injected`) -- an author's own
        explicit `urljoin` base on a field materialize() never touched must
        survive a self-heal unchanged."""
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        store = PresetStore(user=FilePresetStore(base_path=user_dir))
        explicit = PostProcess(op="urljoin", args=["https://pinned.example"])
        store.create(
            Preset(
                name="user_p",
                source="custom",
                kind="user",
                url_template="https://e.com/{x}",
                request_defaults={},
                locales={"us": LocaleProfile(domain="com", country="US")},
                default_locale="us",
                parsing_instructions=ParsingInstructions(
                    type="css",
                    fields={
                        "title": FieldRule(selector="#old", required=True),
                        "pinned_url": FieldRule(
                            selector="a", attr="href", all=True,
                            post_process=[explicit],
                        ),
                    },
                ),
                version=1,
                updated_at=1.0,
            )
        )
        healed = ParsingInstructions(
            type="css",
            fields={
                "title": FieldRule(selector="#t", required=True),
                "pinned_url": FieldRule(
                    selector="a", attr="href", all=True,
                    post_process=[explicit],
                ),
            },
        )
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["self_healed"],
                    mode="self_healed",
                    healed_instructions=healed,
                )
            ),
        )
        mocker.patch.object(wp, "_get_store", return_value=store)

        # materializer_injected names a DIFFERENT field ("urls", which
        # doesn't even exist here) -- "pinned_url" was never injected.
        await wp.apply(
            HTML,
            _instr("#old"),
            _plan(
                self_heal=True, llm_model="m", preset_name="user_p",
                preset_kind="user",
                materializer_injected={"urljoin": ["urls"]},
            ),
        )

        saved = store.get("user_p")
        assert saved.parsing_instructions.fields["pinned_url"].post_process[0].args == [
            "https://pinned.example"
        ]

    @pytest.mark.asyncio
    async def test_self_healed_builtin_not_persisted_only_logged(
        self, tmp_path, mocker, caplog
    ):
        import logging

        store = PresetStore(user=FilePresetStore(base_path=tmp_path / "u"))
        mocker.patch.object(wp, "_get_store", return_value=store)
        healed = ParsingInstructions(
            type="css", fields={"title": FieldRule(selector="#t")}
        )
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["self_healed"],
                    mode="self_healed",
                    healed_instructions=healed,
                )
            ),
        )
        with caplog.at_level(logging.INFO):
            data, _ = await wp.apply(
                HTML,
                _instr("#old"),
                _plan(self_heal=True, llm_model="m",
                      preset_name="amazon_product", preset_kind="builtin"),
            )
        assert data == {"title": "Widget"}
        assert any(
            "amazon_product" in r.message and "built-in" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_persist_failure_does_not_break_scrape(self, mocker):
        healed = ParsingInstructions(
            type="css", fields={"title": FieldRule(selector="#t")}
        )
        mocker.patch.object(
            wp,
            "run_pipeline",
            new=mocker.AsyncMock(
                return_value=ParserResult(
                    data={"title": "Widget"},
                    warnings=["self_healed"],
                    mode="self_healed",
                    healed_instructions=healed,
                )
            ),
        )
        broken = mocker.Mock()
        broken.get.side_effect = RuntimeError("disk gone")
        mocker.patch.object(wp, "_get_store", return_value=broken)

        data, warnings = await wp.apply(
            HTML,
            _instr("#old"),
            _plan(self_heal=True, llm_model="m", preset_name="user_p",
                  preset_kind="user"),
        )
        # scrape still returns data; persistence failure is a warning only
        assert data == {"title": "Widget"}
        assert any("self_heal_persist_failed" in w for w in warnings)


def _user_store(tmp_path, fields: dict[str, FieldRule]) -> PresetStore:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    store = PresetStore(user=FilePresetStore(base_path=user_dir))
    store.create(
        Preset(
            name="user_p",
            source="custom",
            kind="user",
            url_template="https://e.com/{x}",
            request_defaults={},
            locales={"us": LocaleProfile(domain="com", country="US")},
            default_locale="us",
            parsing_instructions=ParsingInstructions(type="css", fields=fields),
            version=1,
            updated_at=1.0,
        )
    )
    return store


@pytest.mark.asyncio
async def test_a_heal_on_a_per_request_override_is_never_persisted(tmp_path, mocker):
    """Audit H-14: `parsing_override` is a public request field; a heal of ITS
    contract replaced the shared preset's fields for every other caller."""
    store = _user_store(tmp_path, {
        "title": FieldRule(selector="#t", required=True),
        "price": FieldRule(selector="#p"),
        "sku": FieldRule(selector="#s"),
    })
    override = ParsingInstructions(
        type="css", fields={"anything": FieldRule(selector="h1.zzz", required=True)}
    )
    healed = ParsingInstructions(
        type="css", fields={"anything": FieldRule(selector="body", required=True)}
    )
    mocker.patch.object(
        wp,
        "run_pipeline",
        new=mocker.AsyncMock(
            return_value=ParserResult(
                data={"anything": "Widget"},
                warnings=["self_healed"],
                mode="self_healed",
                healed_instructions=healed,
            )
        ),
    )
    mocker.patch.object(wp, "_get_store", return_value=store)

    data, warnings = await wp.apply(
        HTML,
        override.model_dump(mode="json"),
        _plan(self_heal=True, llm_model="m", preset_name="user_p", preset_kind="user",
              instructions_from_override=True),
    )
    assert data == {"anything": "Widget"}, "the heal still serves this request"
    saved = store.get("user_p")
    assert list(saved.parsing_instructions.fields) == ["title", "price", "sku"]
    assert saved.version == 1
    assert any(w.startswith("self_heal_not_persisted") for w in warnings), warnings
