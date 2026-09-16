"""CRUD endpoints for scraping presets.

This router is mounted at `/api/v1/presets` and exposes:
  - GET    /              list (filter by `kind` and/or `source`)
  - GET    /{name}        full preset by name
  - POST   /              create user preset
  - PUT    /{name}        replace user preset
  - DELETE /{name}        delete user preset

Built-in presets are read-only. POST/PUT/DELETE against a built-in name return
403; attempting to create a user preset without the `user_` prefix returns 400.

The store is injected via FastAPI dependency so tests can substitute a
temp-directory backend without spinning the full app lifespan.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response

from src.presets.exceptions import PresetValidationError, SampleFetchError
from src.presets.llm.client import LLMError
from src.presets.models import Preset
from src.presets.requests import (
    PresetGenerateRequest,
    PresetPreviewRequest,
    PresetTestRequest,
)
from src.presets.service import preset_service
from src.presets.store import (
    PresetAlreadyExists,
    PresetNameInvalid,
    PresetNotFound,
    PresetLockUnavailable,
    PresetReadOnly,
    PresetStore,
)

log = logging.getLogger(__name__)

router = APIRouter()


@lru_cache(maxsize=1)
def get_preset_store() -> PresetStore:
    """FastAPI dependency. Returns a process-wide PresetStore instance backed
    by the default builtin / user directories. Tests must override via
    `app.dependency_overrides[get_preset_store]` to inject a temp store.
    """
    return PresetStore()


@router.get("", operation_id="list_presets")
def list_presets(
    kind: Literal["builtin", "user"] | None = None,
    source: str | None = None,
    store: PresetStore = Depends(get_preset_store),
) -> dict:
    items = preset_service.list_presets(kind, source, store)
    return {"items": [preset.model_dump(mode="json") for preset in items]}


@router.get("/llm-models", operation_id="list_llm_models")
def list_llm_models() -> dict:
    return preset_service.list_llm_models()


@router.post("/generate", status_code=201, operation_id="generate_preset")
async def generate_preset(
    req: PresetGenerateRequest,
    store: PresetStore = Depends(get_preset_store),
) -> Preset:
    try:
        return await preset_service.generate(req, store)
    except PresetValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except PresetNameInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PresetAlreadyExists as exc:
        raise HTTPException(status_code=409, detail="preset_already_exists") from exc
    except SampleFetchError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=502, detail="llm_error: upstream model call failed"
        ) from exc


@router.post("/{name}/test", operation_id="test_preset")
async def test_preset(
    name: str,
    req: PresetTestRequest,
    store: PresetStore = Depends(get_preset_store),
) -> dict:
    try:
        return await preset_service.test(name, req, store)
    except PresetValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except PresetNotFound as exc:
        raise HTTPException(status_code=404, detail="preset_not_found") from exc
    except SampleFetchError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=502, detail="llm_error: upstream model call failed"
        ) from exc


@router.post("/preview", operation_id="preview_preset")
async def preview_preset(req: PresetPreviewRequest) -> dict:
    try:
        return await preset_service.preview(req)
    except PresetValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except SampleFetchError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=502, detail="llm_error: upstream model call failed"
        ) from exc


@router.get("/{name}", operation_id="get_preset")
def get_preset(name: str, store: PresetStore = Depends(get_preset_store)) -> Preset:
    try:
        return preset_service.get(name, store)
    except PresetNotFound as exc:
        raise HTTPException(status_code=404, detail="preset_not_found") from exc


@router.post("", status_code=201, operation_id="create_preset")
def create_preset(
    preset: Preset, store: PresetStore = Depends(get_preset_store)
) -> Preset:
    try:
        return preset_service.create(preset, store)
    except PresetValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except PresetNameInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PresetAlreadyExists as exc:
        raise HTTPException(status_code=409, detail="preset_already_exists") from exc
    except PresetReadOnly as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.put("/{name}", operation_id="update_preset")
def update_preset(
    name: str,
    preset: Preset,
    store: PresetStore = Depends(get_preset_store),
) -> Preset:
    try:
        return preset_service.update(name, preset, store)
    except PresetValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except PresetNotFound as exc:
        raise HTTPException(status_code=404, detail="preset_not_found") from exc
    except PresetReadOnly as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PresetLockUnavailable as exc:
        # Operational, and retrying is the right response -- which a 500 does
        # not say. Something else is writing this preset, or a writer died
        # holding the lock.
        log.warning("preset write refused, lock held: %s", name)
        raise HTTPException(status_code=503, detail="preset_locked") from exc


@router.delete("/{name}", status_code=204, operation_id="delete_preset")
def delete_preset(
    name: str, store: PresetStore = Depends(get_preset_store)
) -> Response:
    try:
        preset_service.delete(name, store)
    except PresetNotFound as exc:
        raise HTTPException(status_code=404, detail="preset_not_found") from exc
    except PresetLockUnavailable as exc:
        log.warning("preset delete refused, lock held: %s", name)
        raise HTTPException(status_code=503, detail="preset_locked") from exc
    except PresetReadOnly as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return Response(status_code=204)
