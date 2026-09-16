from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import time
from pathlib import Path

import pytest

from src.presets.models import LocaleProfile, Preset
import src.presets.store as store_mod
from src.presets.store import (
    BuiltInRegistry,
    FilePresetStore,
    PresetAlreadyExists,
    PresetChangedSinceRead,
    PresetLockUnavailable,
    PresetNameInvalid,
    PresetNotFound,
    PresetReadOnly,
    PresetStore,
)


def _make_preset(
    name: str = "user_test",
    *,
    kind: str = "user",
    source: str = "custom",
    locales: dict | None = None,
    default_locale: str = "us",
    **overrides,
) -> Preset:
    if locales is None:
        locales = {"us": LocaleProfile(domain="com", country="US")}
    return Preset(
        name=name,
        source=source,
        kind=kind,
        request_defaults={"device": "desktop"},
        locales=locales,
        default_locale=default_locale,
        updated_at=1_700_000_000.0,
        **overrides,
    )


# ---------------------------------------------------------------- FilePresetStore


class TestFilePresetStore:
    def test_create_and_read_roundtrip(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        original = _make_preset(name="user_amazon_extra")

        store.create(original)
        loaded = store.get("user_amazon_extra")
        assert loaded == original

        # file actually exists on disk
        assert (tmp_path / "user_amazon_extra.json").exists()

    def test_create_rejects_duplicate(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one"))
        with pytest.raises(PresetAlreadyExists):
            store.create(_make_preset(name="user_one"))

    def test_update_overwrites(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one"))

        updated = _make_preset(name="user_one", description="new")
        store.update("user_one", updated)
        loaded = store.get("user_one")
        assert loaded.description == "new"

    def test_update_missing_raises(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        with pytest.raises(PresetNotFound):
            store.update("user_one", _make_preset(name="user_one"))

    def test_update_survives_a_neighbour_writer_with_the_same_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A second update of the same preset lands between the first one's
        temp write and its rename -- same pid by construction, which is what
        two worker containers look like to a pid-derived temp name.
        """
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        real_replace = os.replace
        neighbour_landed = False

        def replace_after_the_neighbour(src, dst):
            nonlocal neighbour_landed
            if not neighbour_landed:
                neighbour_landed = True
                # Written straight to disk rather than through update(): the
                # neighbour stands for a writer in ANOTHER container, and
                # calling update() here would instead be this process waiting
                # on the lock it is already holding.
                neighbour = _make_preset(name="user_one", source="amazon")
                (tmp_path / "user_one.json").write_text(
                    json.dumps(neighbour.model_dump(mode="json")), encoding="utf-8"
                )
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", replace_after_the_neighbour)
        store.update("user_one", _make_preset(name="user_one", source="ebay"))

        assert neighbour_landed
        assert store.get("user_one").source == "ebay"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_update_failure_leaves_the_file_intact_and_no_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))

        def refuse(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", refuse)
        with pytest.raises(OSError):
            store.update("user_one", _make_preset(name="user_one", source="ebay"))

        assert store.get("user_one").source == "custom"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_update_works_for_the_longest_name_create_accepts(self, tmp_path: Path):
        """NAME_MAX is 255: `<name>.json` fits at 250 characters, and the temp
        name must fit wherever the preset file does."""
        name = "user_" + "x" * 245
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name=name))
        store.update(name, _make_preset(name=name, source="ebay"))
        assert store.get(name).source == "ebay"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_an_orphaned_temp_is_invisible_to_readers(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one"))
        (tmp_path / "user_one.k3v9x2qa.tmp").write_text("{ torn")
        assert [p.name for p in store.list()] == ["user_one"]
        assert store.get("user_one").name == "user_one"

    def test_update_keeps_the_file_mode(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one"))
        target = tmp_path / "user_one.json"
        target.chmod(0o640)
        store.update("user_one", _make_preset(name="user_one", source="ebay"))
        assert target.stat().st_mode & 0o777 == 0o640

    def test_warns_when_the_store_is_not_writable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setattr(os, "access", lambda _path, _mode: False)
        with caplog.at_level("WARNING", logger="src.presets.store"):
            FilePresetStore(base_path=tmp_path).warn_if_not_writable()
        assert any(
            "not readable, writable" in r.getMessage() and str(tmp_path) in r.getMessage()
            for r in caplog.records
        )

    def test_probe_requires_read_write_and_search_access(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """`chmod o+w` on a 0700 directory gives 0702: writable by the bit, unusable
        without search permission. The probe must ask for all three."""
        modes: list[int] = []
        monkeypatch.setattr(os, "access", lambda _path, mode: modes.append(mode) or True)
        FilePresetStore(base_path=tmp_path).warn_if_not_writable()
        assert modes == [os.R_OK | os.W_OK | os.X_OK]

    def test_probe_survives_an_unsearchable_ancestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """`Path.exists()` raises PermissionError when a parent lacks the search
        bit; the probe must warn, not take the service down."""

        def refuse(_self: Path) -> bool:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "exists", refuse)
        with caplog.at_level("WARNING", logger="src.presets.store"):
            FilePresetStore(base_path=tmp_path).warn_if_not_writable()
        [record] = caplog.records
        assert str(tmp_path) in record.getMessage() and "Permission denied" in record.getMessage()

    def test_writable_store_stays_silent(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        with caplog.at_level("WARNING", logger="src.presets.store"):
            FilePresetStore(base_path=tmp_path).warn_if_not_writable()
        assert caplog.records == []

    def test_probe_walks_up_to_the_nearest_existing_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        probed: list[Path] = []
        monkeypatch.setattr(os, "access", lambda path, _mode: probed.append(Path(path)) or True)
        FilePresetStore(base_path=tmp_path / "missing" / "presets").warn_if_not_writable()
        assert probed == [tmp_path]

    def test_delete(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one"))
        store.delete("user_one")
        with pytest.raises(PresetNotFound):
            store.get("user_one")
        assert not (tmp_path / "user_one.json").exists()

    def test_delete_missing_raises(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        with pytest.raises(PresetNotFound):
            store.delete("user_one")

    def test_list_returns_all(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_a", source="amazon"))
        store.create(_make_preset(name="user_b", source="ebay"))
        names = sorted(p.name for p in store.list())
        assert names == ["user_a", "user_b"]

    def test_list_filter_by_source(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_a", source="amazon"))
        store.create(_make_preset(name="user_b", source="ebay"))
        names = [p.name for p in store.list(source="amazon")]
        assert names == ["user_a"]

    def test_ignores_non_json_files(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        (tmp_path / "README.md").write_text("not a preset")
        assert store.list() == []

    def test_skips_corrupt_files(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        (tmp_path / "broken.json").write_text("{ not valid json")
        # corrupt files must not crash list(); they are just skipped
        assert store.list() == []


# ---------------------------------------------------------------- BuiltInRegistry


class TestBuiltInRegistry:
    def test_loads_bundled_jsons(self, tmp_path: Path):
        # write a fake built-in JSON
        preset_dict = _make_preset(
            name="fake_builtin", kind="builtin"
        ).model_dump(mode="json")
        (tmp_path / "fake_builtin.json").write_text(json.dumps(preset_dict))

        registry = BuiltInRegistry(base_path=tmp_path)
        items = registry.list()
        assert len(items) == 1
        assert items[0].name == "fake_builtin"
        assert items[0].kind == "builtin"

    def test_get_missing_raises(self, tmp_path: Path):
        registry = BuiltInRegistry(base_path=tmp_path)
        with pytest.raises(PresetNotFound):
            registry.get("unknown")

    def test_real_builtin_dir_has_at_least_one_preset(self):
        """Sanity check: shipped built-ins parse correctly."""
        registry = BuiltInRegistry()  # default path = src/presets/builtin/
        items = registry.list()
        names = {p.name for p in items}
        expected = {
            "amazon_product_chromium",
            "amazon_product_camoufox",
            "google_search_chromium",
            "google_search_camoufox",
            "amazon_search_chromium",
            "amazon_search_camoufox",
            "google_shopping_chromium",
            "google_shopping_camoufox",
            "ebay_search_chromium",
            "ebay_search_camoufox",
            "walmart_product_chromium",
            "walmart_product_camoufox",
            "youtube_video_chromium",
            "youtube_video_camoufox",
            "linkedin_profile_chromium",
            "linkedin_profile_camoufox",
        }
        assert expected.issubset(names)
        # every shipped built-in is kind="builtin" and parses cleanly
        for p in items:
            assert p.kind == "builtin"
            assert p.url_template, f"{p.name} missing url_template"
            assert p.locales, f"{p.name} missing locales"

        # Self-heal only fires when a *required* field comes back empty
        # (parser_pipeline._missing_required). A built-in that advertises
        # self_heal but marks nothing required can never heal — it would
        # silently return nulls forever. Pin: every self_heal built-in with
        # deterministic instructions must mark >=1 field required.
        by_name = {p.name: p for p in items}
        for name, p in by_name.items():
            if p.self_heal and p.parsing_instructions is not None:
                req_fields = [
                    f for f in p.parsing_instructions.fields.values()
                    if f.required
                ]
                assert req_fields, (
                    f"{name}: self_heal=true but no required field — "
                    f"self-heal can never trigger"
                )


# ---------------------------------------------------------------- PresetStore facade


class TestPresetStoreFacade:
    def test_resolves_builtin_first(self, tmp_path: Path):
        builtin_dir = tmp_path / "builtin"
        user_dir = tmp_path / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()

        builtin = _make_preset(name="amazon_product", kind="builtin")
        (builtin_dir / "amazon_product.json").write_text(
            json.dumps(builtin.model_dump(mode="json"))
        )

        store = PresetStore(
            builtin=BuiltInRegistry(base_path=builtin_dir),
            user=FilePresetStore(base_path=user_dir),
        )
        found = store.get("amazon_product")
        assert found.kind == "builtin"

    def test_falls_back_to_user(self, tmp_path: Path):
        builtin_dir = tmp_path / "builtin"
        user_dir = tmp_path / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()

        store = PresetStore(
            builtin=BuiltInRegistry(base_path=builtin_dir),
            user=FilePresetStore(base_path=user_dir),
        )
        store.create(_make_preset(name="user_custom"))
        found = store.get("user_custom")
        assert found.kind == "user"

    def test_create_rejects_builtin_name(self, tmp_path: Path):
        """User cannot shadow a built-in by reusing its name."""
        builtin_dir = tmp_path / "builtin"
        user_dir = tmp_path / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()
        builtin = _make_preset(name="amazon_product", kind="builtin")
        (builtin_dir / "amazon_product.json").write_text(
            json.dumps(builtin.model_dump(mode="json"))
        )

        store = PresetStore(
            builtin=BuiltInRegistry(base_path=builtin_dir),
            user=FilePresetStore(base_path=user_dir),
        )
        with pytest.raises(PresetAlreadyExists):
            store.create(_make_preset(name="amazon_product"))

    def test_create_enforces_user_prefix(self, tmp_path: Path):
        """User-defined preset names must start with 'user_' to avoid
        accidental collisions with future built-ins."""
        builtin_dir = tmp_path / "builtin"
        user_dir = tmp_path / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()

        store = PresetStore(
            builtin=BuiltInRegistry(base_path=builtin_dir),
            user=FilePresetStore(base_path=user_dir),
        )
        with pytest.raises(PresetNameInvalid):
            store.create(_make_preset(name="my_preset"))

    def test_update_and_delete_refuse_builtin(self, tmp_path: Path):
        builtin_dir = tmp_path / "builtin"
        user_dir = tmp_path / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()
        builtin = _make_preset(name="amazon_product", kind="builtin")
        (builtin_dir / "amazon_product.json").write_text(
            json.dumps(builtin.model_dump(mode="json"))
        )

        store = PresetStore(
            builtin=BuiltInRegistry(base_path=builtin_dir),
            user=FilePresetStore(base_path=user_dir),
        )
        with pytest.raises(PresetReadOnly):
            store.update("amazon_product", builtin)
        with pytest.raises(PresetReadOnly):
            store.delete("amazon_product")

    def test_list_merges_builtin_and_user(self, tmp_path: Path):
        builtin_dir = tmp_path / "builtin"
        user_dir = tmp_path / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()

        builtin = _make_preset(name="amazon_product", kind="builtin")
        (builtin_dir / "amazon_product.json").write_text(
            json.dumps(builtin.model_dump(mode="json"))
        )

        store = PresetStore(
            builtin=BuiltInRegistry(base_path=builtin_dir),
            user=FilePresetStore(base_path=user_dir),
        )
        store.create(_make_preset(name="user_one"))
        names = sorted(p.name for p in store.list())
        assert names == ["amazon_product", "user_one"]

    def test_list_filter_by_kind(self, tmp_path: Path):
        builtin_dir = tmp_path / "builtin"
        user_dir = tmp_path / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()
        builtin = _make_preset(name="amazon_product", kind="builtin")
        (builtin_dir / "amazon_product.json").write_text(
            json.dumps(builtin.model_dump(mode="json"))
        )

        store = PresetStore(
            builtin=BuiltInRegistry(base_path=builtin_dir),
            user=FilePresetStore(base_path=user_dir),
        )
        store.create(_make_preset(name="user_one"))
        assert [p.name for p in store.list(kind="builtin")] == ["amazon_product"]
        assert [p.name for p in store.list(kind="user")] == ["user_one"]


class TestAConditionalUpdateKeepsTheOtherWritersWork:
    """`if_stamp` exists for one situation: a self-heal job read the preset
    minutes ago, the person who owns it has saved their own selectors since,
    and the job is about to write a language model's guess over that."""

    def test_the_write_is_refused_and_the_stored_preset_is_untouched(
        self, tmp_path: Path
    ):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        _, stamp = store.get_stamped("user_one")

        store.update("user_one", _make_preset(name="user_one", source="ebay"))

        with pytest.raises(PresetChangedSinceRead):
            store.update(
                "user_one",
                _make_preset(name="user_one", source="amazon"),
                if_stamp=stamp,
            )
        assert store.get("user_one").source == "ebay"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_an_unchanged_preset_still_takes_the_write(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        _, stamp = store.get_stamped("user_one")

        store.update(
            "user_one",
            _make_preset(name="user_one", source="amazon"),
            if_stamp=stamp,
        )

        assert store.get("user_one").source == "amazon"

    def test_a_rewrite_with_identical_bytes_is_not_a_change(self, tmp_path: Path):
        """The stamp is over content, so re-saving the same preset -- a PUT
        that changed nothing -- must not cost the self-heal its write."""
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        _, stamp = store.get_stamped("user_one")

        store.update("user_one", _make_preset(name="user_one", source="custom"))

        store.update(
            "user_one",
            _make_preset(name="user_one", source="amazon"),
            if_stamp=stamp,
        )
        assert store.get("user_one").source == "amazon"

    def test_a_human_put_is_still_unconditional(self, tmp_path: Path):
        """No `if_stamp`, no refusal: a person replacing their own preset
        overwrites whatever a worker healed in the meantime, on purpose."""
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        store.update("user_one", _make_preset(name="user_one", source="ebay"))

        store.update("user_one", _make_preset(name="user_one", source="amazon"))

        assert store.get("user_one").source == "amazon"

    def test_the_stamp_describes_the_bytes_it_was_read_with(self, tmp_path: Path):
        """Two calls (`get` then a separate hash) could straddle a write and
        return a stamp for a revision the caller never saw -- in the direction
        that loses the newer file. One read cannot."""
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        first, stamp_one = store.get_stamped("user_one")

        store.update("user_one", _make_preset(name="user_one", source="ebay"))
        second, stamp_two = store.get_stamped("user_one")

        assert first.source == "custom" and second.source == "ebay"
        assert stamp_one != stamp_two

    def test_a_missing_preset_raises_not_found(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        with pytest.raises(PresetNotFound):
            store.get_stamped("user_nope")

    def test_a_builtin_reads_back_with_no_stamp(self, tmp_path: Path):
        """The API resolves either kind through one call, so this must not
        raise: a built-in is never written, so there is nothing to lose under
        a writer and None is the honest answer."""
        builtin_dir = tmp_path / "builtin"
        builtin_dir.mkdir()
        (builtin_dir / "b_one.json").write_text(
            json.dumps(_make_preset(name="b_one", kind="builtin").model_dump(mode="json")),
            encoding="utf-8",
        )
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        store = PresetStore(
            builtin=BuiltInRegistry(base_path=builtin_dir),
            user=FilePresetStore(base_path=user_dir),
        )
        preset, stamp = store.get_stamped("b_one")

        assert preset.name == "b_one"
        assert stamp is None, "a built-in cannot be written, so nothing can be lost"

    def test_a_writer_inside_the_critical_section_excludes_the_next_one(
        self, tmp_path: Path
    ):
        """The lock's whole job. Without it the compare and the replace stop
        being one step: a second writer reads the same stamp, passes it, and
        the two writes race -- which is the loss the compare was added to
        prevent."""
        import fcntl

        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        seen_from_the_other_side: list[int | None] = []
        real_replace = os.replace

        def look_from_outside_while_we_hold_it(src, dst):
            lock = store_mod._lock_path(tmp_path / "user_one.json")
            with open(lock, "w", encoding="utf-8") as other:
                try:
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen_from_the_other_side.append(None)  # got in: not locked
                except OSError as exc:
                    seen_from_the_other_side.append(exc.errno)
            return real_replace(src, dst)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "replace", look_from_outside_while_we_hold_it)
            store.update("user_one", _make_preset(name="user_one", source="ebay"))

        assert seen_from_the_other_side == [errno.EWOULDBLOCK], (
            "another writer could take the lock mid-write"
        )

    def test_a_lock_nobody_releases_refuses_the_write_instead_of_taking_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Writing anyway is not the safe fallback it looks like: unlocked,
        the compare and the replace are two steps, so the edit this write was
        checked against can land in between and be overwritten with no
        conflict raised. Refusing loses a machine's guess; proceeding loses a
        person's work."""
        import fcntl

        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        _, stamp = store.get_stamped("user_one")
        monkeypatch.setattr(store_mod, "PRESET_LOCK_TIMEOUT_S", 0.1)

        lock = store_mod._lock_path(tmp_path / "user_one.json")
        with open(lock, "w", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with pytest.raises(PresetLockUnavailable):
                store.update(
                    "user_one",
                    _make_preset(name="user_one", source="amazon"),
                    if_stamp=stamp,
                )

        assert store.get("user_one").source == "custom"

    def test_a_deleted_preset_is_not_resurrected_by_a_write_behind_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The delete lands while the write is on its way in -- after it
        decided the preset exists, before it holds the lock. With that check
        outside the lock the replace put the file back: the owner got their
        204 and the preset returned. Serializing the two is not enough; the
        write has to re-decide inside the lock.
        """
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        real_lock = store_mod._write_lock
        deleted = False

        @contextlib.contextmanager
        def delete_it_while_this_writer_is_on_its_way_in(path, timeout_s=None):
            nonlocal deleted
            if not deleted:
                deleted = True  # set first: delete() takes this lock too
                store.delete("user_one")  # the owner's DELETE, fully completed
            with real_lock(path, timeout_s):
                yield

        monkeypatch.setattr(
            store_mod, "_write_lock", delete_it_while_this_writer_is_on_its_way_in
        )

        with pytest.raises(PresetNotFound):
            store.update("user_one", _make_preset(name="user_one", source="ebay"))

        assert list(tmp_path.glob("*.json")) == []
        assert list(tmp_path.glob("*.tmp")) == []

    def test_a_writer_that_will_not_wait_says_so_at_once(
        self, tmp_path: Path
    ):
        """The self-heal writer passes 0: it runs inside a scrape's budget,
        and every second waiting is a second the page task's ceiling can
        cancel the parse in -- losing data already extracted to save a write
        that is best-effort."""
        import fcntl

        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        lock = store_mod._lock_path(tmp_path / "user_one.json")

        with open(lock, "w", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            with pytest.raises(PresetLockUnavailable):
                store.update(
                    "user_one",
                    _make_preset(name="user_one", source="amazon"),
                    lock_timeout_s=0.0,
                )
            waited = time.monotonic() - started

        assert waited < 0.5, f"waited {waited:.2f}s for a lock it should not wait for"
        assert store.get("user_one").source == "custom"

    def test_a_filesystem_without_locks_says_what_is_wrong(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """NFS without lockd answers instantly and forever. Spinning out the
        deadline and then blaming "another writer" sends the next person
        hunting a writer that does not exist."""
        import fcntl

        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))

        def no_locks_here(_fd, _op):
            raise OSError(errno.ENOLCK, "no locks available")

        monkeypatch.setattr(fcntl, "flock", no_locks_here)

        with pytest.raises(OSError) as caught:
            store.update("user_one", _make_preset(name="user_one", source="ebay"))

        assert not isinstance(caught.value, PresetLockUnavailable)
        assert caught.value.errno == errno.ENOLCK

    def test_the_lock_is_group_writable_whatever_the_umask(self, tmp_path: Path):
        """The lock inode is never replaced, so whoever creates it decides who
        can write the preset from then on. Under the usual umask 022 the
        creation mode alone lands 0640 -- and audit H-06 step 3 moves these
        containers off a shared uid."""
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        store.update("user_one", _make_preset(name="user_one", source="ebay"))

        lock = store_mod._lock_path(tmp_path / "user_one.json")
        assert stat.S_IMODE(lock.stat().st_mode) & 0o060 == 0o060

    def test_the_lock_file_is_not_mistaken_for_a_preset(self, tmp_path: Path):
        """Readers glob `*.json`; the sidecar must not turn into a preset
        that fails to parse on every list()."""
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_one", source="custom"))
        store.update("user_one", _make_preset(name="user_one", source="ebay"))

        assert store_mod._lock_path(tmp_path / "user_one.json").exists()
        assert [preset.name for preset in store.list()] == ["user_one"]

    def test_the_longest_legal_preset_can_still_be_locked(self, tmp_path: Path):
        """`<name>.json.lock` does not fit where `<name>.json` barely does,
        so the longest name `create` accepts would have been the one name
        that could never be written."""
        name = "user_" + "x" * 245
        lock = store_mod._lock_path(tmp_path / f"{name}.json")

        assert len(lock.name) <= 255
        assert lock.name.endswith(".lock")

    def test_two_presets_do_not_share_a_lock(self, tmp_path: Path):
        """The stem is capped at 32 characters, so names that agree on their
        first 32 would collide if the digest were not over the whole name --
        two unrelated presets serializing against each other."""
        prefix = "user_" + "y" * 40
        first = store_mod._lock_path(tmp_path / f"{prefix}_a.json")
        second = store_mod._lock_path(tmp_path / f"{prefix}_b.json")

        assert first != second


class TestAWriteForAPresetThatIsNotThere:
    """The sidecar lock must not be created for a preset that does not exist.

    `_write_lock` leaves the sidecar behind ON PURPOSE -- unlinking it
    reintroduces the inode race it exists to close -- so taking it before
    knowing the preset is there writes a file that nothing is allowed to clean
    up, under a name the caller chose. The preset endpoints carry no token, so
    that is a volume filled one request at a time. The same order made a fresh
    deploy answer 500: with `data/presets` not yet created, the lock's own
    `os.open` raised FileNotFoundError before anything could call it a 404.

    The authoritative check stays INSIDE the lock, where a DELETE that lands
    mid-write cannot slip past it; this one is only an early exit.
    """

    def test_deleting_a_missing_preset_leaves_nothing_behind(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        for name in ("user_nope_a", "user_nope_b", "user_nope_c"):
            with pytest.raises(PresetNotFound):
                store.delete(name)
        assert sorted(p.name for p in tmp_path.iterdir()) == []

    def test_updating_a_missing_preset_leaves_nothing_behind(self, tmp_path: Path):
        store = FilePresetStore(base_path=tmp_path)
        with pytest.raises(PresetNotFound):
            store.update("user_nope", _make_preset(name="user_nope"))
        assert sorted(p.name for p in tmp_path.iterdir()) == []

    def test_a_store_directory_that_does_not_exist_is_not_found_not_a_crash(
        self, tmp_path: Path
    ):
        store = FilePresetStore(base_path=tmp_path / "never_created")
        with pytest.raises(PresetNotFound):
            store.delete("user_nope")
        with pytest.raises(PresetNotFound):
            store.update("user_nope", _make_preset(name="user_nope"))

    def test_a_preset_that_exists_still_keeps_its_sidecar(self, tmp_path: Path):
        # The other half of the contract: for a real preset the lock is left
        # behind deliberately, and a future tidy-up must not remove it.
        store = FilePresetStore(base_path=tmp_path)
        store.create(_make_preset(name="user_here"))
        store.update("user_here", _make_preset(name="user_here", source="changed"))
        assert any(p.suffix == ".lock" for p in tmp_path.iterdir())
