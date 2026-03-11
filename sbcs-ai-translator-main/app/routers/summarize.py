# app/routers/summarize.py
from __future__ import annotations

from typing import Optional, List
import json
import time
from uuid import uuid4

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, insert, update

from ..db import get_session
from ..models import Job, User
from ..services.blob import upload_bytes_with_prefix
from ..services.queue import enqueue_job
from ..config import settings

router = APIRouter(prefix="/summarize", tags=["summarize"])

async def _gen_unique_job_id(session: AsyncSession, max_tries: int = 5) -> str:
    for _ in range(max_tries):
        jid = str(uuid4())
        exists = await session.scalar(select(Job.id).where(Job.id == jid))
        if not exists:
            return jid
    return f"{int(time.time()*1000)}-{uuid4()}"

@router.post("/create")
async def create_jobs(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("auto"),           # pdf|pptx|docx|xlsx|auto
    prompt: Optional[str] = Form(None), # custom instruction (opsional)
    user_id: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    # (A) Persist user token bila ada → untuk OneDrive (opsional, sama seperti /upload/create)
    auth = request.headers.get("Authorization") or ""
    bearer = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else ""
    if user_id and bearer:
        token_obj = {"access_token": bearer}
        now = int(time.time())
        row = await session.execute(select(User).where(User.user_id == user_id))
        existing = row.scalar_one_or_none()
        if existing:
            await session.execute(
                update(User)
                .where(User.user_id == user_id)
                .values(token_json=json.dumps(token_obj), expires_at=now + 3600)
            )
        else:
            await session.execute(
                insert(User).values(
                    user_id=user_id, name=None, email=None,
                    token_json=json.dumps(token_obj), expires_at=now + 3600,
                )
            )

    data = await file.read()
    size_mb = len(data) / (1024 * 1024)
    if size_mb > settings.MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"File too large: {file.filename}")

    job_id = await _gen_unique_job_id(session)

    # simpan source ke blob input
    prefix = f"jobs/{job_id}/input"
    _url, blob_name = await upload_bytes_with_prefix(
        data=data,
        content_type=file.content_type or "application/octet-stream",
        filename=file.filename,
        prefix=prefix,
    )

    # detail job: simpan action & meta (mode/prompt) → fallback untuk worker
    detail = {
        "action": "summarize",
        "mode": (mode or "auto").lower(),
        "prompt": prompt or "",
    }

    job = Job(
        id=job_id,
        filename=file.filename,
        status="QUEUED",
        detail=json.dumps(detail, ensure_ascii=False),
        batch_id="",
        result_blob=blob_name,
        source_lang="auto",
        target_lang="",   # tidak dipakai di summarize
        user_id=user_id or None,
    )
    session.add(job)
    await session.commit()

    # ENQUEUE → sertakan action agar worker langsung tahu ini summarize
    await enqueue_job(
        {"job_id": job_id, "action": "summarize", "mode": detail["mode"], "prompt": detail["prompt"]},
        visibility_timeout=5
    )

    return {"job_ids": [job_id], "count": 1, "status": "QUEUED"}
