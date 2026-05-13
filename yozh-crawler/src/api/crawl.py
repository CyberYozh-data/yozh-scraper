from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request as FastapiRequest
from fastapi.responses import StreamingResponse

from ..schemas import (
    CancelResponse,
    CrawlJobRecord,
    CrawlRequest,
    JobCreateResponse,
)


log = logging.getLogger(__name__)
router = APIRouter()


@router.post("", response_model=JobCreateResponse, operation_id="create_crawl")
async def create_crawl(req: CrawlRequest, app_req: FastapiRequest) -> JobCreateResponse:
    store = app_req.app.state.job_store
    runner = app_req.app.state.job_runner
    rec = await store.create(req)
    try:
        await runner.submit(rec.job_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return JobCreateResponse(job_id=rec.job_id)


@router.get("/{job_id}", response_model=CrawlJobRecord, operation_id="get_crawl")
async def get_crawl(job_id: str, app_req: FastapiRequest) -> CrawlJobRecord:
    store = app_req.app.state.job_store
    rec = store.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return rec


@router.get("/{job_id}/results", response_model=CrawlJobRecord, operation_id="get_crawl_results")
async def get_crawl_results(job_id: str, app_req: FastapiRequest) -> CrawlJobRecord:
    store = app_req.app.state.job_store
    rec = store.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return rec


@router.get("/{job_id}/events", operation_id="stream_crawl_events")
async def stream_events(job_id: str, app_req: FastapiRequest) -> StreamingResponse:
    store = app_req.app.state.job_store
    rec = store.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job_not_found")

    async def gen():
        try:
            async for event in store.subscribe(job_id):
                yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "cancelled"):
                    return
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/{job_id}", response_model=CancelResponse, operation_id="cancel_crawl")
async def cancel_crawl(
    job_id: str,
    app_req: FastapiRequest,
    hard: bool = Query(default=False),
) -> CancelResponse:
    runner = app_req.app.state.job_runner
    store = app_req.app.state.job_store
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    cancelled = await runner.request_cancel(job_id, hard=hard)
    return CancelResponse(job_id=job_id, cancelled=cancelled, hard=hard)
