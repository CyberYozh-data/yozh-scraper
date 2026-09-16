from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.presets import get_preset_store
from src.api.scrape_preset import router as scrape_preset_router
from src.presets.models import LocaleProfile, ParsingInstructions, Preset
from src.extract.models import FieldRule
from src.presets.store import BuiltInRegistry, FilePresetStore, PresetStore
from src.sessions.models import SessionRecord
from src.sessions.store import SessionExpired, SessionIncompatible, SessionNotFound


@pytest.fixture
def store(tmp_path: Path) -> PresetStore:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    preset = Preset(
        name="amazon_product",
        source="amazon",
        kind="builtin",
        url_template="https://www.amazon.{domain}/dp/{asin}",
        request_defaults={"device": "desktop", "proxy_type": "res_rotating"},
        locales={"us": LocaleProfile(domain="com", country="US")},
        default_locale="us",
        parsing_instructions=ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector="#productTitle", required=True)},
        ),
        updated_at=1_700_000_000.0,
    )
    (builtin / "amazon_product.json").write_text(
        json.dumps(preset.model_dump(mode="json"))
    )
    return PresetStore(
        builtin=BuiltInRegistry(base_path=builtin),
        user=FilePresetStore(base_path=user),
    )


@pytest.fixture
def client(store: PresetStore, mocker) -> TestClient:
    # Patch submit_job_internal so pages are captured without real Redis/taskiq.
    submit_mock = AsyncMock(return_value="job_xyz")
    mocker.patch("src.scrape_service.scrape_service.submit_job_internal", submit_mock)

    app = FastAPI()
    app.include_router(scrape_preset_router, prefix="/api/v1/scrape/preset")
    app.dependency_overrides[get_preset_store] = lambda: store
    test_client = TestClient(app)
    test_client._submit = submit_mock  # type: ignore[attr-defined]
    return test_client


class TestScrapePresetPage:
    def test_returns_job_id(self, client: TestClient):
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={
                "source": "amazon_product",
                "preset_params": {"asin": "B0CRTYZG5C"},
                "locale": "us",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"job_id": "job_xyz"}

    def test_materialized_request_submitted(self, client: TestClient):
        client.post(
            "/api/v1/scrape/preset/page",
            json={"source": "amazon_product", "preset_params": {"asin": "X"}},
        )
        submitted = client._submit.await_args.args[0]
        assert len(submitted) == 1
        page = submitted[0]
        assert str(page.url) == "https://www.amazon.com/dp/X"
        assert page.preset_meta.name == "amazon_product"
        assert page.extract is not None

    def test_unknown_preset_404(self, client: TestClient):
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={"source": "no_such_preset", "preset_params": {}},
        )
        assert resp.status_code == 404

    def test_missing_template_param_400(self, client: TestClient):
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={"source": "amazon_product", "preset_params": {}},
        )
        assert resp.status_code == 400
        assert "asin" in resp.json()["detail"]

    def test_unknown_locale_400(self, client: TestClient):
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={
                "source": "amazon_product",
                "preset_params": {"asin": "X"},
                "locale": "zz",
            },
        )
        assert resp.status_code == 400


class TestScrapePresetPages:
    def test_batch_submits_all(self, client: TestClient):
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "pages": [
                    {"source": "amazon_product", "preset_params": {"asin": "A"}},
                    {"source": "amazon_product", "preset_params": {"asin": "B"}},
                ]
            },
        )
        assert resp.status_code == 200
        submitted = client._submit.await_args.args[0]
        assert [str(p.url) for p in submitted] == [
            "https://www.amazon.com/dp/A",
            "https://www.amazon.com/dp/B",
        ]

    def test_batch_unknown_preset_404(self, client: TestClient):
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "pages": [
                    {"source": "amazon_product", "preset_params": {"asin": "A"}},
                    {"source": "ghost", "preset_params": {}},
                ]
            },
        )
        assert resp.status_code == 404

    def test_batch_bad_params_400(self, client: TestClient):
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={"pages": [{"source": "amazon_product", "preset_params": {}}]},
        )
        assert resp.status_code == 400

    def test_batch_error_names_failing_index(self, client: TestClient):
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "pages": [
                    {"source": "amazon_product", "preset_params": {"asin": "A"}},
                    {"source": "amazon_product", "preset_params": {}},
                ]
            },
        )
        assert resp.status_code == 400
        assert "1" in resp.json()["detail"]  # zero-based index of the bad page


class TestRequestDefaultsBadType:
    def test_non_dict_headers_yields_400_not_500(
        self, store: PresetStore, mocker
    ):
        # ship a preset whose request_defaults.headers is the wrong type
        bad = Preset(
            name="user_bad_headers",
            source="custom",
            kind="user",
            url_template="https://example.com/{x}",
            request_defaults={"headers": "not-a-dict"},
            locales={"us": LocaleProfile(domain="com", country="US")},
            default_locale="us",
            updated_at=1_700_000_000.0,
        )
        store.user.create(bad)

        submit_mock = AsyncMock(return_value="j")
        mocker.patch("src.scrape_service.scrape_service.submit_job_internal", submit_mock)
        app = FastAPI()
        app.include_router(scrape_preset_router, prefix="/api/v1/scrape/preset")
        app.dependency_overrides[get_preset_store] = lambda: store
        c = TestClient(app)

        resp = c.post(
            "/api/v1/scrape/preset/page",
            json={"source": "user_bad_headers", "preset_params": {"x": "1"}},
        )
        assert resp.status_code == 400


@pytest.fixture
def session_store(mocker):
    store = MagicMock()
    store.get = AsyncMock()
    store.assert_compatible_with_request = MagicMock()
    mocker.patch(
        "src.api.session_guard.get_session_store", return_value=store
    )
    return store


def _ok_record() -> SessionRecord:
    return SessionRecord(
        session_id="sess_ok",
        status="ready",
        created_at=1.0,
        expires_at=10_000_000_000.0,
        last_used_at=1.0,
        device="desktop",
        proxy_type="res_rotating",
    )


class TestScrapePresetPageSession:
    def test_success_threads_session_id(self, client, session_store):
        session_store.get = AsyncMock(return_value=_ok_record())
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={
                "source": "amazon_product",
                "preset_params": {"asin": "X"},
                "session_id": "sess_ok",
            },
        )
        assert resp.status_code == 200
        page = client._submit.await_args.args[0][0]
        assert page.session_id == "sess_ok"

    def test_unknown_session_404(self, client, session_store):
        session_store.get = AsyncMock(side_effect=SessionNotFound("sess_x"))
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={
                "source": "amazon_product",
                "preset_params": {"asin": "X"},
                "session_id": "sess_x",
            },
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "session_not_found"

    def test_expired_session_410(self, client, session_store):
        session_store.get = AsyncMock(
            side_effect=SessionExpired("sess_x", expired_at=123.0)
        )
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={
                "source": "amazon_product",
                "preset_params": {"asin": "X"},
                "session_id": "sess_x",
            },
        )
        assert resp.status_code == 410

    def test_incompatible_session_422(self, client, session_store):
        session_store.get = AsyncMock(return_value=_ok_record())
        session_store.assert_compatible_with_request = MagicMock(
            side_effect=SessionIncompatible("device mismatch")
        )
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={
                "source": "amazon_product",
                "preset_params": {"asin": "X"},
                "session_id": "sess_ok",
            },
        )
        assert resp.status_code == 422

    def test_no_session_id_skips_validation(self, client, session_store):
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={"source": "amazon_product", "preset_params": {"asin": "X"}},
        )
        assert resp.status_code == 200
        session_store.get.assert_not_awaited()

    def test_empty_string_session_id_treated_as_real_404(self, client, session_store):
        # An empty string is a real (non-None) id, not "no session": it must
        # reach the store (404), not be silently coerced to no-session.
        session_store.get = AsyncMock(side_effect=SessionNotFound(""))
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={
                "source": "amazon_product",
                "preset_params": {"asin": "X"},
                "session_id": "",
            },
        )
        assert resp.status_code == 404
        session_store.get.assert_awaited_once_with("")

    def test_materialize_session_conflict_422_before_store(
        self, client, session_store
    ):
        # A field-vs-request_override session_id conflict is rejected during
        # materialization, before (shadowing) any session-store lookup.
        resp = client.post(
            "/api/v1/scrape/preset/page",
            json={
                "source": "amazon_product",
                "preset_params": {"asin": "X"},
                "session_id": "sess_field",
                "request_override": {"session_id": "sess_override"},
            },
        )
        assert resp.status_code == 422
        assert "session_id conflict" in str(resp.json()["detail"])
        session_store.get.assert_not_awaited()


class TestScrapePresetPagesSession:
    def test_batch_level_applied_to_all(self, client, session_store):
        session_store.get = AsyncMock(return_value=_ok_record())
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "session_id": "sess_ok",
                "pages": [
                    {"source": "amazon_product", "preset_params": {"asin": "A"}},
                    {"source": "amazon_product", "preset_params": {"asin": "B"}},
                ],
            },
        )
        assert resp.status_code == 200
        submitted = client._submit.await_args.args[0]
        assert [p.session_id for p in submitted] == ["sess_ok", "sess_ok"]

    def test_batch_vs_per_page_conflict_422(self, client, session_store):
        session_store.get = AsyncMock(return_value=_ok_record())
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "session_id": "sess_batch",
                "pages": [
                    {
                        "source": "amazon_product",
                        "preset_params": {"asin": "A"},
                        "session_id": "sess_other",
                    },
                ],
            },
        )
        assert resp.status_code == 422
        assert "conflicts" in str(resp.json()["detail"])

    def test_batch_per_page_session_validated(self, client, session_store):
        session_store.get = AsyncMock(side_effect=SessionNotFound("nope"))
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "pages": [
                    {
                        "source": "amazon_product",
                        "preset_params": {"asin": "A"},
                        "session_id": "nope",
                    },
                ],
            },
        )
        assert resp.status_code == 404

    def test_batch_no_session_unaffected(self, client, session_store):
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "pages": [
                    {"source": "amazon_product", "preset_params": {"asin": "A"}},
                ]
            },
        )
        assert resp.status_code == 200
        session_store.get.assert_not_awaited()

    def test_batch_equals_per_page_no_conflict(self, client, session_store):
        # Guards the `!=` conflict branch: a per-page session_id EQUAL to the
        # batch one must NOT 422 — it falls through to propagation + submit.
        session_store.get = AsyncMock(return_value=_ok_record())
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "session_id": "sess_same",
                "pages": [
                    {
                        "source": "amazon_product",
                        "preset_params": {"asin": "A"},
                        "session_id": "sess_same",
                    },
                ],
            },
        )
        assert resp.status_code == 200
        submitted = client._submit.await_args.args[0]
        assert [p.session_id for p in submitted] == ["sess_same"]

    def test_batch_mixed_pages_all_get_batch_session(self, client, session_store):
        # Guards that model_copy propagation applies to EVERY page, including
        # one with no per-page session_id, when a batch-level value is set.
        session_store.get = AsyncMock(return_value=_ok_record())
        resp = client.post(
            "/api/v1/scrape/preset/pages",
            json={
                "session_id": "sess_b",
                "pages": [
                    {"source": "amazon_product", "preset_params": {"asin": "A"}},
                    {
                        "source": "amazon_product",
                        "preset_params": {"asin": "B"},
                        "session_id": "sess_b",
                    },
                ],
            },
        )
        assert resp.status_code == 200
        submitted = client._submit.await_args.args[0]
        assert [p.session_id for p in submitted] == ["sess_b", "sess_b"]


class TestTheJobCarriesThePresetItRead:
    """The window a self-heal can overwrite opens HERE, when the job reads the
    preset, and closes minutes later when the worker persists. A fingerprint
    taken at persist time covers microseconds of it; one taken here covers all
    of it, which is why it has to travel with the job."""

    def test_a_user_preset_ships_the_fingerprint_it_was_read_with(
        self, store: PresetStore
    ):
        from src.api.scrape_preset import _resolve
        from src.presets.materializer import PresetScrapeRequest

        user = _user_preset()
        store.create(user)
        _, stamp = store.get_stamped(user.name)

        scrape_req = _resolve(
            store,
            PresetScrapeRequest(source=user.name, preset_params={"asin": "B0"}),
        )

        assert scrape_req.parser_plan is not None
        assert scrape_req.parser_plan.preset_stamp == stamp

    def test_the_fingerprint_tracks_the_file_not_the_request(
        self, store: PresetStore
    ):
        """Two jobs enqueued around an edit must not carry the same stamp --
        that is the whole signal the worker compares against."""
        from src.api.scrape_preset import _resolve
        from src.presets.materializer import PresetScrapeRequest

        user = _user_preset()
        store.create(user)
        req = PresetScrapeRequest(source=user.name, preset_params={"asin": "B0"})
        before = _resolve(store, req).parser_plan.preset_stamp

        store.update(user.name, _user_preset(selector="#edited-by-a-person"))
        after = _resolve(store, req).parser_plan.preset_stamp

        assert before != after

    def test_a_builtin_ships_no_fingerprint(self, store: PresetStore):
        """Built-ins are never written back, so there is nothing to lose and
        nothing to compare -- and resolving one must not raise."""
        from src.api.scrape_preset import _resolve
        from src.presets.materializer import PresetScrapeRequest

        scrape_req = _resolve(
            store,
            PresetScrapeRequest(source="amazon_product", preset_params={"asin": "B0"}),
        )

        assert scrape_req.parser_plan.preset_stamp is None


def _user_preset(*, selector: str = "#productTitle") -> Preset:
    return Preset(
        name="user_amazon",
        source="amazon",
        kind="user",
        url_template="https://www.amazon.{domain}/dp/{asin}",
        request_defaults={"device": "desktop", "proxy_type": "res_rotating"},
        locales={"us": LocaleProfile(domain="com", country="US")},
        default_locale="us",
        parsing_instructions=ParsingInstructions(
            type="css",
            fields={"title": FieldRule(selector=selector, required=True)},
        ),
        updated_at=1_700_000_000.0,
    )
