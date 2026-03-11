from __future__ import annotations

from typing import List, Optional
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

from ..services.path_sanitize import sanitize_blob_path
try:
    from ..services.resize import guess_mime
except Exception:
    def guess_mime(name: str) -> str:
        return "application/octet-stream"

router = APIRouter(prefix="/upload", tags=["upload"])


async def _gen_unique_job_id(session: AsyncSession, max_tries: int = 5) -> str:
    for _ in range(max_tries):
        jid = str(uuid4())
        exists = await session.scalar(select(Job.id).where(Job.id == jid))
        if not exists:
            return jid
    return f"{int(time.time() * 1000)}-{uuid4()}"


@router.post("/create")
async def create_jobs(
    request: Request,
    files: Optional[List[UploadFile]] = File(default=None),
    file: Optional[UploadFile] = File(default=None),
    target_lang: str = Form(settings.DEFAULT_TARGET_LANG),
    source_lang: str = Form("auto"),
    user_id: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    # (A) simpan token user (kalau dikirim via Authorization)
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
                    user_id=user_id,
                    name=None,
                    email=None,
                    token_json=json.dumps(token_obj),
                    expires_at=now + 3600,
                )
            )

    # (B) kumpulkan files
    all_files: List[UploadFile] = []
    if files:
        all_files.extend(files)
    if file:
        all_files.append(file)
    if not all_files:
        raise HTTPException(status_code=422, detail="Field required: files (or file)")

    job_ids: List[str] = []

    # 1) Upload & buat row job
    for f in all_files:
        data = await f.read()
        size_mb = len(data) / (1024 * 1024)
        if size_mb > settings.MAX_UPLOAD_MB:
            raise HTTPException(status_code=413, detail=f"File too large: {f.filename}")

        job_id = await _gen_unique_job_id(session)
        job_ids.append(job_id)

        prefix = f"jobs/{job_id}/input"
        desired_path = f"{prefix}/{f.filename or 'file'}"
        sanitized_path = sanitize_blob_path(desired_path)
        if not sanitized_path or not sanitized_path.startswith(prefix):
            sanitized_path = sanitize_blob_path(f"{prefix}/file")
        filename_sanitized = sanitized_path.split("/", 3)[-1] if "/" in sanitized_path else sanitized_path

        _url, _blobname = await upload_bytes_with_prefix(
            data=data,
            content_type=(f.content_type or guess_mime(f.filename) or "application/octet-stream"),
            filename=filename_sanitized,
            prefix=prefix,
        )

        job = Job(
            id=job_id,
            filename=f.filename,
            status="QUEUED",
            detail="",
            batch_id="",
            result_blob=_blobname,   # <- path sumber yang akan dipakai worker/translator
            source_lang=source_lang or "auto",
            target_lang=target_lang or settings.DEFAULT_TARGET_LANG,
            user_id=user_id or None,
        )
        session.add(job)

    # 2) commit supaya worker bisa baca
    await session.commit()

    # 3) enqueue
    for jid in job_ids:
        await enqueue_job({"job_id": jid}, visibility_timeout=5)

    return {"job_ids": job_ids, "count": len(job_ids), "status": "QUEUED"}
