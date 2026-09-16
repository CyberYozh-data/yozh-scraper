"""Preset-driven scrape endpoints.

`POST /api/v1/scrape/preset/page` and `/pages` accept a PresetScrapeRequest,
expand it against the named preset (URL template + locale + request defaults +
parsing recipe) into a regular ScrapeRequest, then hand it to the same job
queue the raw /scrape endpoints use. The worker is preset-agnostic; it just
sees a ScrapeRequest whose `extract` carries the preset's parsing recipe.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.presets import get_preset_store
from src.api.session_guard import validate_session_id
from src.presets.materializer import (
    MaterializeError,
    SessionConflictError,
    PresetScrapeRequest,
    materialize,
)
from src.presets.store import PresetNotFound, PresetStore
from src.schemas import JobCreateResponse, ScrapeRequest
from src.scrape_service import QueueFull, scrape_service

log = logging.getLogger(__name__)
router = APIRouter()


class BatchPresetScrapeRequest(BaseModel):
    pages: list[PresetScrapeRequest] = Field(
        ..., description="Preset scrape requests; each resolved independently."
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Apply this session_id to every page in `pages`. Rejected (422) "
            "if any page already has a different session_id."
        ),
    )


def _resolve(
    store: PresetStore, req: PresetScrapeRequest, *, index: int | None = None
) -> ScrapeRequest:
    where = "" if index is None else f"page[{index}]: "
    try:
        # Stamped HERE, where the job reads the preset -- not at persist time
        # minutes later. The window a self-heal can overwrite is this read to
        # that write; a stamp taken just before the write covers none of it.
        preset, stamp = store.get_stamped(req.source)
    except PresetNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"{where}preset_not_found"
        ) from exc
    try:
        return materialize(preset, req, preset_stamp=stamp)
    except SessionConflictError as exc:
        raise HTTPException(status_code=422, detail=f"{where}{exc}") from exc
    except MaterializeError as exc:
        raise HTTPException(status_code=400, detail=f"{where}{exc}") from exc


@router.post("/page", response_model=JobCreateResponse, operation_id="run_scrape_preset_page")
async def scrape_preset_page(
    request: PresetScrapeRequest,
    store: PresetStore = Depends(get_preset_store),
) -> JobCreateResponse:
    scrape_req = _resolve(store, request)
    await validate_session_id(scrape_req)
    try:
        job_id = await scrape_service.submit_job_internal([scrape_req])
    except QueueFull as exc:
        raise HTTPException(status_code=503, detail="queue_full") from exc
    return JobCreateResponse(job_id=job_id)


@router.post("/pages", response_model=JobCreateResponse, operation_id="run_scrape_preset_pages")
async def scrape_preset_pages(
    request: BatchPresetScrapeRequest,
    store: PresetStore = Depends(get_preset_store),
) -> JobCreateResponse:
    pages = list(request.pages)
    if request.session_id is not None:
        for idx, page_req in enumerate(pages):
            if page_req.session_id is not None and page_req.session_id != request.session_id:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"page[{idx}]: per-page session_id={page_req.session_id} "
                        f"conflicts with batch-level session_id="
                        f"{request.session_id}"
                    ),
                )
        pages = [
            page_req.model_copy(update={"session_id": request.session_id})
            for page_req in pages
        ]

    scrape_reqs = []
    for idx, page in enumerate(pages):
        scrape_req = _resolve(store, page, index=idx)
        await validate_session_id(scrape_req)
        scrape_reqs.append(scrape_req)

    try:
        job_id = await scrape_service.submit_job_internal(scrape_reqs)
    except QueueFull as exc:
        raise HTTPException(status_code=503, detail="queue_full") from exc
    return JobCreateResponse(job_id=job_id)
