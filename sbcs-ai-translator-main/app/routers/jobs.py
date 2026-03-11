# app/routers/jobs.py
from __future__ import annotations

import os, json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..db import get_session
from ..models import Job
from ..config import settings

# util to build SAS if DB doesn't have it yet
try:
    from app.services.blob import generate_blob_sas_url  # your existing helper
except Exception:
    generate_blob_sas_url = None  # fallback handled below

router = APIRouter(prefix="/jobs", tags=["jobs"])

JOBS_SAS_EXP_MINUTES = int(os.getenv("JOBS_SAS_EXP_MINUTES", "10080"))  # default 7 hari
OUTPUT_CONTAINER = os.getenv("AZURE_OUTPUT_CONTAINER", "output")

def _parse_detail(detail: str | dict | None) -> dict:
    if not detail:
        return {}
    if isinstance(detail, dict):
        return detail
    try:
        return json.loads(detail)
    except Exception:
        return {"message": detail}

@router.get("/{job_id}")
async def get_job(job_id: str, session: AsyncSession = Depends(get_session)):
    res = await session.execute(select(Job).where(Job.id == job_id))
    job = res.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Not found")

    # ambil kolom top-level
    download_url = getattr(job, "download_url", "") or ""
    onedrive_url = getattr(job, "onedrive_url", "") or ""
    result_blob  = getattr(job, "result_blob", "") or ""
    print()
    return {
        "job_id": job.id,
        "status": job.status,
        "filename": job.filename,
        "batch_id": job.batch_id or "",
        "source_lang": job.source_lang,
        "target_lang": job.target_lang,
        "download_url": job.download_url or "",
        "onedrive_url": job.onedrive_url or "",
        "result_url": job.download_url or "",   # kompat lama
        "result_blob": job.result_blob or "",
        "detail": job.detail or "{}",
    }
