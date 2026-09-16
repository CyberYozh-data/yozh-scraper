"""`request_defaults` must be a request the scraper would accept.

`POST /presets` wrote the dict straight to the store. The cross-field rules in
`ScrapeRequest` only run when `materializer.py` builds the request at scrape
time, so an impossible combination was accepted at save time and then failed
EVERY run off that preset -- `device='mobile'` with `browser_engine='firefox'`
(Playwright Firefox has no mobile emulation and refuses the context), a
camoufox-only knob on a chromium preset, a `spoof_os` conflict. The materializer
checked key NAMES against `ScrapeRequest.model_fields` and never the values.

The check is a dry `ScrapeRequest` construction with a placeholder url, not a
full `materialize()`: materialising needs a locale and the params a
`url_template` wants, so it would refuse legitimate presets. Measured over
every shipped builtin (see `test_every_builtin_preset_still_saves`) -- no false
refusal.

It raises `PresetValidationError`, which the API already maps to 422: the same
status the scrape endpoints give for the same pairing, rather than a new code
for the same mistake caught earlier.
"""
from __future__ import annotations

import json

from pathlib import Path

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.presets import get_preset_store, router as presets_router
from src.presets.models import LocaleProfile, Preset
from src.presets.store import BuiltInRegistry, FilePresetStore, PresetStore


from src.presets.store import DEFAULT_BUILTIN_DIR

BUILTIN_FILES = sorted(DEFAULT_BUILTIN_DIR.glob("*.json"))


def _preset(**overrides) -> dict:
    preset = Preset(
        name="user_probe_preset",
        source="probe",
        kind="user",
        request_defaults={"device": "desktop"},
        locales={"us": LocaleProfile(domain="com", country="US")},
        default_locale="us",
        updated_at=1_700_000_000.0,
    )
    body = preset.model_dump(mode="json")
    body.update(overrides)
    return body


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    store = PresetStore(
        builtin=BuiltInRegistry(base_path=builtin),
        user=FilePresetStore(base_path=user),
    )
    app = FastAPI()
    app.include_router(presets_router, prefix="/api/v1/presets")
    app.dependency_overrides[get_preset_store] = lambda: store
    client = TestClient(app)
    client.user_dir = user  # type: ignore[attr-defined]  # for the repair test
    return client


class TestRequestDefaultsAreValidated:
    def test_an_impossible_pairing_is_refused_at_save_time(self, client: TestClient):
        body = _preset(request_defaults={"device": "mobile", "browser_engine": "firefox"})
        response = client.post("/api/v1/presets", json=body)
        assert response.status_code == 422
        detail = json.dumps(response.json())
        assert "device" in detail and "firefox" in detail

    def test_the_update_path_is_guarded_too(self, client: TestClient):
        assert client.post("/api/v1/presets", json=_preset()).status_code == 201
        bad = _preset(request_defaults={"device": "mobile", "browser_engine": "camoufox"})
        response = client.put("/api/v1/presets/user_probe_preset", json=bad)
        assert response.status_code == 422

    def test_a_sound_preset_still_saves(self, client: TestClient):
        body = _preset(request_defaults={
            "device": "mobile", "browser_engine": "chromium", "stealth": True,
            "proxy_type": "prem_res_rotating", "timeout_ms": 50000,
        })
        assert client.post("/api/v1/presets", json=body).status_code == 201

    def test_an_empty_request_defaults_still_saves(self, client: TestClient):
        assert client.post("/api/v1/presets", json=_preset(request_defaults={})).status_code == 201

    def test_the_builtin_sweep_is_not_vacuous(self):
        # A moved directory would make the sweep below collect zero cases and
        # stay green: "28 of 28" silently becoming "0 of 0".
        assert len(BUILTIN_FILES) >= 20

    @pytest.mark.parametrize("path", BUILTIN_FILES, ids=lambda p: p.stem)
    def test_every_builtin_preset_still_saves(self, client: TestClient, path: Path):
        shipped = json.loads(path.read_text(encoding="utf-8"))
        body = _preset(
            request_defaults=shipped.get("request_defaults") or {},
            name=f"user_probe_{path.stem}",
        )
        response = client.post("/api/v1/presets", json=body)
        assert response.status_code == 201, response.text


class TestKeysTheMaterializerSuppliesItself:
    """`materialize()` passes url/extract/preset_meta/parser_plan as explicit
    kwargs, so the same key in `request_defaults` is a `TypeError` there -- not
    a `MaterializeError`, so not a 400 either: a 500 on EVERY scrape off that
    preset. And `url` breaks the guard's own dry construction the same way,
    which turned a save that used to return 201 into a 500."""

    # The VALUE is valid for each field -- None is accepted by the three
    # optional ones and the url is a real url -- so what the test pins is that
    # the KEY is refused, not that the value failed validation.
    @pytest.mark.parametrize("key,value", [
        ("url", "https://x.example/"),
        ("extract", None),
        ("preset_meta", None),
        ("parser_plan", None),
    ])
    def test_a_reserved_key_is_refused_at_save_time(self, client: TestClient, key, value):
        body = _preset(request_defaults={key: value})
        response = client.post("/api/v1/presets", json=body)
        assert response.status_code == 422
        assert key in json.dumps(response.json())

    def test_proxy_geo_is_not_reserved(self, client: TestClient):
        # It reaches ScrapeRequest through the merged dict, not as a kwarg, and
        # a preset may legitimately pin it.
        body = _preset(request_defaults={"proxy_geo": {"country_code": "DE"}})
        assert client.post("/api/v1/presets", json=body).status_code == 201


class TestTheGenerateRouteIsGuardedToo:
    def test_manual_mode_cannot_store_an_impossible_profile(self, client: TestClient):
        preset = _preset(name="user_via_generate",
                         request_defaults={"device": "mobile", "browser_engine": "firefox"})
        response = client.post("/api/v1/presets/generate",
                               json={"mode": "manual", "name": "user_via_generate",
                                     "preset": preset})
        assert response.status_code == 422
        assert client.get("/api/v1/presets/user_via_generate").status_code == 404

    def test_manual_mode_still_stores_a_sound_preset(self, client: TestClient):
        preset = _preset(name="user_via_generate_ok")
        response = client.post("/api/v1/presets/generate",
                               json={"mode": "manual", "name": "user_via_generate_ok",
                                     "preset": preset})
        assert response.status_code == 201


class TestTheLocalesAreValidatedWithTheProfile:
    def test_a_three_letter_country_is_refused(self, client: TestClient):
        # `LocaleProfile.country` is a free string and the materializer derives
        # `proxy_geo.country_code` from it, where ProxyGeo demands two letters:
        # saved cleanly, then every scrape died with a MaterializeError.
        body = _preset()
        body["locales"] = {"us": {"domain": "com", "country": "USA"}}
        response = client.post("/api/v1/presets", json=body)
        assert response.status_code == 422
        assert "country" in json.dumps(response.json())


class TestTheRefusalDoesNotEchoTheInput:
    def test_no_value_from_request_defaults_reaches_the_response(self, client: TestClient):
        secret = "hunter2-SECRET-pool"
        body = _preset(request_defaults={
            "device": "mobile", "browser_engine": "firefox",
            "proxy_pool_id": secret,
            "headers": {"Authorization": "Bearer SECRET-TOKEN"},
            "cookies": [{"name": "sid", "value": "SECRET-COOKIE", "domain": "x.example"}],
        })
        response = client.post("/api/v1/presets", json=body)
        assert response.status_code == 422
        detail = json.dumps(response.json())
        for leaked in (secret, "SECRET-TOKEN", "SECRET-COOKIE"):
            assert leaked not in detail
        # Pin the MECHANISM, not those three strings: pydantic truncates its
        # `input_value=` repr, so a leak can hide behind the ellipsis depending
        # on where the secret sits in the dict. The detail must carry no echoed
        # input at all, and no link to pydantic's error docs either.
        assert "input_value" not in detail
        assert "errors.pydantic.dev" not in detail
        # the refusal still has to say WHAT is wrong
        assert "device" in detail or "browser_engine" in detail


class TestIdentityErrorsKeepTheirStatus:
    def test_updating_a_preset_that_does_not_exist_is_still_404(self, client: TestClient):
        bad = _preset(request_defaults={"device": "mobile", "browser_engine": "firefox"})
        response = client.put("/api/v1/presets/user_not_here", json=bad)
        assert response.status_code == 404

    def test_a_corrupted_preset_is_still_repairable_by_put(self, client: TestClient):
        # The existence check must not deserialise: `_read_preset_file` returns
        # None for broken JSON, so reading the old contents would answer 404 for
        # a preset that is sitting right there -- and PUT is how it gets fixed.
        (client.user_dir / "user_broken.json").write_text("{not json at all")
        response = client.put("/api/v1/presets/user_broken", json=_preset(name="user_broken"))
        assert response.status_code == 200, response.text
        assert client.get("/api/v1/presets/user_broken").status_code == 200
