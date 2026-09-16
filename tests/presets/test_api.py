from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.presets import get_preset_store, router as presets_router
from src.presets.models import LocaleProfile, Preset
from src.presets.store import BuiltInRegistry, FilePresetStore, PresetStore


@pytest.fixture
def store_dirs(tmp_path: Path) -> tuple[Path, Path]:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    # one shipped builtin so list/get coverage isn't empty
    preset = Preset(
        name="amazon_product",
        source="amazon",
        kind="builtin",
        request_defaults={"device": "desktop"},
        locales={"us": LocaleProfile(domain="com", country="US")},
        default_locale="us",
        updated_at=1_700_000_000.0,
    )
    (builtin / "amazon_product.json").write_text(
        json.dumps(preset.model_dump(mode="json"))
    )
    return builtin, user


@pytest.fixture
def client(store_dirs: tuple[Path, Path]) -> TestClient:
    builtin_dir, user_dir = store_dirs
    test_store = PresetStore(
        builtin=BuiltInRegistry(base_path=builtin_dir),
        user=FilePresetStore(base_path=user_dir),
    )
    app = FastAPI()
    app.include_router(presets_router, prefix="/api/v1/presets")
    app.dependency_overrides[get_preset_store] = lambda: test_store
    return TestClient(app)


def _payload(name: str = "user_one", **overrides) -> dict:
    base = dict(
        name=name,
        source="custom",
        request_defaults={"device": "desktop"},
        locales={"us": {"domain": "com", "country": "US"}},
        default_locale="us",
        updated_at=1_700_000_000.0,
    )
    base.update(overrides)
    return base


class TestListEndpoint:
    def test_list_returns_builtin_when_empty_user_store(self, client: TestClient):
        response = client.get("/api/v1/presets")
        assert response.status_code == 200
        data = response.json()
        assert {"items"} <= data.keys()
        names = [item["name"] for item in data["items"]]
        assert names == ["amazon_product"]

    def test_list_filter_by_kind(self, client: TestClient):
        client.post("/api/v1/presets", json=_payload())
        builtin_only = client.get("/api/v1/presets?kind=builtin").json()
        user_only = client.get("/api/v1/presets?kind=user").json()
        assert [i["name"] for i in builtin_only["items"]] == ["amazon_product"]
        assert [i["name"] for i in user_only["items"]] == ["user_one"]

    def test_list_filter_by_source(self, client: TestClient):
        client.post("/api/v1/presets", json=_payload(source="ebay"))
        response = client.get("/api/v1/presets?source=ebay")
        assert [i["name"] for i in response.json()["items"]] == ["user_one"]


class TestGetEndpoint:
    def test_get_builtin(self, client: TestClient):
        response = client.get("/api/v1/presets/amazon_product")
        assert response.status_code == 200
        assert response.json()["kind"] == "builtin"

    def test_get_missing(self, client: TestClient):
        response = client.get("/api/v1/presets/does_not_exist")
        assert response.status_code == 404


class TestCreateEndpoint:
    def test_create_succeeds(self, client: TestClient):
        response = client.post("/api/v1/presets", json=_payload())
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "user_one"
        assert body["kind"] == "user"

    def test_create_rejects_non_user_prefix(self, client: TestClient):
        response = client.post("/api/v1/presets", json=_payload(name="my_preset"))
        assert response.status_code == 400

    def test_create_rejects_builtin_name(self, client: TestClient):
        response = client.post("/api/v1/presets", json=_payload(name="amazon_product"))
        # builtin already shipped under that name → 409 conflict
        assert response.status_code == 409

    def test_create_rejects_duplicate_user(self, client: TestClient):
        client.post("/api/v1/presets", json=_payload())
        response = client.post("/api/v1/presets", json=_payload())
        assert response.status_code == 409

    def test_create_rejects_invalid_payload(self, client: TestClient):
        response = client.post(
            "/api/v1/presets",
            json={"name": "user_bad"},  # missing required fields
        )
        assert response.status_code == 422


class TestUpdateEndpoint:
    def test_update_user_preset(self, client: TestClient):
        client.post("/api/v1/presets", json=_payload())
        payload = _payload(description="updated")
        response = client.put("/api/v1/presets/user_one", json=payload)
        assert response.status_code == 200
        assert response.json()["description"] == "updated"

    def test_update_builtin_rejected(self, client: TestClient):
        response = client.put(
            "/api/v1/presets/amazon_product", json=_payload(name="amazon_product")
        )
        assert response.status_code == 403

    def test_a_preset_another_writer_holds_answers_503_not_500(
        self, client: TestClient, store_dirs: tuple[Path, Path]
    ):
        """A write refused because somebody else holds the lock is
        operational, and retrying is the right answer -- which a traceback
        does not say. Every other store refusal here has a status of its
        own."""
        import fcntl

        import src.presets.store as store_mod

        client.post("/api/v1/presets", json=_payload())
        _, user_dir = store_dirs
        lock = store_mod._lock_path(user_dir / "user_one.json")

        with open(lock, "w", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(store_mod, "PRESET_LOCK_TIMEOUT_S", 0.1)
                response = client.put(
                    "/api/v1/presets/user_one", json=_payload(description="mine")
                )

        assert response.status_code == 503
        assert response.json()["detail"] == "preset_locked"

    def test_update_missing(self, client: TestClient):
        response = client.put("/api/v1/presets/user_missing", json=_payload(name="user_missing"))
        assert response.status_code == 404


class TestDeleteEndpoint:
    def test_delete_user_preset(self, client: TestClient):
        client.post("/api/v1/presets", json=_payload())
        response = client.delete("/api/v1/presets/user_one")
        assert response.status_code == 204
        # gone now
        assert client.get("/api/v1/presets/user_one").status_code == 404

    def test_delete_builtin_rejected(self, client: TestClient):
        response = client.delete("/api/v1/presets/amazon_product")
        assert response.status_code == 403

    def test_delete_missing(self, client: TestClient):
        response = client.delete("/api/v1/presets/user_missing")
        assert response.status_code == 404


class TestTestEndpoint:
    def test_missing_sample_returns_422(self, client: TestClient):
        # Empty body — neither sample_url nor sample_html. Must be a clean
        # 422, not an unhandled PresetValidationError surfacing as 500.
        response = client.post("/api/v1/presets/amazon_product/test", json={})
        assert response.status_code == 422
        assert "sample" in response.json()["detail"]

    def test_unknown_preset_returns_404(self, client: TestClient):
        response = client.post(
            "/api/v1/presets/does_not_exist/test",
            json={"sample_html": "<h1>x</h1>"},
        )
        assert response.status_code == 404
