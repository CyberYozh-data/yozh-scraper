from __future__ import annotations

from fastapi import APIRouter, Request as FastapiRequest

from ..settings import settings


router = APIRouter()


@router.get("", operation_id="health")
async def health(app_req: FastapiRequest) -> dict:
    scraper = app_req.app.state.scraper_client
    store = app_req.app.state.job_store
    scraper_ok = await scraper.health()
    active = sum(1 for jid in store.all_ids() if (rec := store.get(jid)) and rec.status == "running")
    return {
        "status": "ok",
        "workers": settings.workers,
        "scraper_url": settings.scraper_url,
        "scraper_reachable": scraper_ok,
        "jobs_active": active,
        "jobs_total": len(store.all_ids()),
    }
