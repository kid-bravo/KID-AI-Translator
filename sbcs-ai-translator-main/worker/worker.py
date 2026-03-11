from __future__ import annotations

import os, sys, io, json, zipfile, asyncio, datetime as dt, platform, socket, shutil
from pathlib import Path
from typing import Optional, Tuple, List, Dict
from urllib.parse import quote

import httpx
from azure.storage.queue import QueueClient
from azure.storage.blob import ContainerSasPermissions, BlobSasPermissions, generate_blob_sas
from sqlalchemy import select

# ---------- bootstrap PYTHONPATH ----------
HERE = Path(__file__).resolve()
CANDIDATES = [
    HERE.parents[4] if len(HERE.parents) >= 5 else None,
    Path("/home/site/wwwroot"),
]
for root in CANDIDATES:
    if not root:
        continue
    app_dir = root / "app"
    if app_dir.exists():
        site_pkgs = root / ".python_packages" / "lib" / "site-packages"
        if site_pkgs.exists():
            sys.path.insert(0, str(site_pkgs))
        sys.path.insert(0, str(root))
        os.environ.setdefault(
            "PYTHONPATH",
            (str(site_pkgs) + os.pathsep if site_pkgs.exists() else "") + str(root),
        )
        break

# ---------- project imports ----------
from app.config import settings
from app.db import AsyncSessionLocal
from app.models import Job
from app.services.blob import (
    _blob,
    put_bytes as blob_put_bytes,
    generate_container_sas_url,
    generate_blob_sas_url,
)
from app.services.office_fonts import enforce_fonts_by_lang
from app.services.onedrive import upload_bytes_to_user_onedrive
from app.services.blob import clear_prefix
# ---------- logging ----------
try:
    from app.logger_setup import setup_logging
    logger = setup_logging(service="worker")
except Exception:
    # Fallback kalau setup_logging gak ada
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("worker")

try:
    from app.services.glossary import compose_glossary_tsv
except Exception:
    async def compose_glossary_tsv(src, tgt, sample):  # fallback dummy
        return b""

TRANSLATOR_DOC_ENDPOINT = os.getenv("AZURE_TRANSLATOR_DOC_ENDPOINT", "").rstrip("/")
TRANSLATOR_KEY = os.getenv("AZURE_TRANSLATOR_KEY", "") or getattr(settings, "AZURE_TRANSLATOR_KEY", "")
TRANSLATOR_REGION = os.getenv("AZURE_TRANSLATOR_REGION", "southeastasia")

INPUT_CONTAINER = os.getenv("AZURE_INPUT_CONTAINER", "input")
OUTPUT_CONTAINER = os.getenv("AZURE_OUTPUT_CONTAINER", "output")

_ACCOUNT_NAME = _blob.account_name
_ACCOUNT_KEY  = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "") or getattr(settings, "AZURE_STORAGE_ACCOUNT_KEY", "")

# ---------- Azure Queue ----------
_qc = QueueClient.from_connection_string(
    os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
    os.getenv("AZURE_STORAGE_QUEUE_NAME", "translation-jobs"),
)

try:
    _qc.create_queue()
    props = _qc.get_queue_properties()
    from urllib.parse import urlparse
    acc = urlparse(_qc.url).netloc  # contoh: mystorage.queue.core.windows.net
    logger.info("queue.binding", extra={
        "account": acc,
        "queue": _qc.queue_name,
        "approx_count": getattr(props, "approximate_message_count", None)
    })
except Exception as e:
    logger.warning("queue.init_warn", extra={"error": str(e)})

# ==================== utils ====================
import unicodedata
import re
_WHITESPACE_RX = re.compile(r"\s+")
_MULTI_SLASH_RX = re.compile(r"/+")

def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")

def _collapse_ws(s: str) -> str:
    s = (s or "").replace("\u00A0", " ")
    return _WHITESPACE_RX.sub(" ", s)

def _strip_problematic_edges(s: str) -> str:
    s = (s or "").strip().strip("\u200b\u200c\u200d\u200e\u200f")
    while s.endswith((" ", ".")):
        s = s[:-1]
    return s

def _split_dir_base(path: str) -> Tuple[str, str]:
    if not path:
        return "", ""
    path = path.replace("\\", "/")
    path = _MULTI_SLASH_RX.sub("/", path)
    if "/" not in path:
        return "", path
    a, b = path.rsplit("/", 1)
    return a, b

# def _safe_basename_for_blob(name: str) -> str:
#     name = _nfc(name)
#     name = _collapse_ws(name)
#     name = name.replace("\\", "/").split("/")[-1]
#     name = _strip_problematic_edges(name)
#     return name or "file"

def _safe_basename_for_blob(name: str) -> str:
    # normalisasi yang sudah ada
    name = _nfc(name)
    name = _collapse_ws(name)
    name = name.replace("\\", "/").split("/")[-1]
    name = _strip_problematic_edges(name)

    # tambahan: sterilkan karakter yang rawan di Blob / URL / Office
    bad = ' #%&+?;=@[]<>:"\\|{}^`'
    for ch in bad:
        name = name.replace(ch, "_")

    # konsisten pakai underscore untuk spasi
    name = name.replace(" ", "_")
    return name or "file"

def _generate_blob_sas_read(container: str, blob_name: str, minutes: int = 120) -> str:
    if not _ACCOUNT_KEY:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_KEY missing")
    expiry = dt.datetime.utcnow() + dt.timedelta(minutes=minutes)
    sas = generate_blob_sas(
        account_name=_ACCOUNT_NAME,
        container_name=container,
        blob_name=blob_name,
        account_key=_ACCOUNT_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return f"https://{_ACCOUNT_NAME}.blob.core.windows.net/{container}/{blob_name}?{sas}"

async def _fetch_blob_bytes(container: str, name: str) -> Tuple[Optional[bytes], Optional[str]]:
    loop = asyncio.get_event_loop()
    candidates = [name]
    base = os.path.basename(name)
    if base != name:
        candidates.append(base)

    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            bc = _blob.get_blob_client(container=container, blob=cand)
            dl = await loop.run_in_executor(None, bc.download_blob)
            data = await loop.run_in_executor(None, dl.readall)
            props = getattr(dl, "properties", None)
            ctype = getattr(getattr(props, "content_settings", None), "content_type", None)
            return data, ctype
        except Exception:
            continue
    return None, None

# ==================== Translator (Document Translation) ====================
BATCHES_URL = f"{TRANSLATOR_DOC_ENDPOINT}/translator/text/batch/v1.0/batches"

def _common_headers_json() -> Dict[str, str]:
    return {
        "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

async def _assert_head_ok(url: str, label: str) -> None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.head(url)
        r.raise_for_status()

async def _translator_start(
    client: httpx.AsyncClient,
    *,
    src_container_sas_url: str,
    src_prefix: str,
    dst_container_sas_url: str,
    source_lang: str,
    target_lang: str,
    glossary_url: Optional[str] = None,
) -> str:
    source: Dict[str, object] = {
        "sourceUrl": src_container_sas_url,
        "filter": {"prefix": src_prefix},
    }
    if (source_lang or "").lower() not in ("", "auto"):
        source["language"] = source_lang

    target: Dict[str, object] = {"targetUrl": dst_container_sas_url, "language": target_lang or "en"}
    if glossary_url:
        target["glossaries"] = [{"glossaryUrl": glossary_url, "format": "TSV"}]

    payload = {"inputs": [{"source": source, "targets": [target]}]}
    r = await client.post(BATCHES_URL, headers=_common_headers_json(), json=payload)
    r.raise_for_status()
    data = r.json() if r.headers.get("content-type", "").lower().startswith("application/json") else {}
    return data.get("id") or r.headers.get("operation-location", "")

async def _translator_poll(client: httpx.AsyncClient, batch_id_or_loc: str, *, timeout_s: int = 3600, interval_s: int = 3) -> dict:
    headers = _common_headers_json()
    status_url = batch_id_or_loc if "/batches/" in batch_id_or_loc else f"{BATCHES_URL}/{batch_id_or_loc}"
    deadline = dt.datetime.utcnow() + dt.timedelta(seconds=timeout_s)
    while dt.datetime.utcnow() < deadline:
        r = await client.get(status_url, headers=headers)
        r.raise_for_status()
        data = r.json()
        state = (data.get("status") or "").lower()
        if state in ("succeeded", "failed", "validationfailed", "cancelled"):
            return data
        await asyncio.sleep(interval_s)
    raise TimeoutError("Translator polling timeout")

# ==================== Core job ====================
async def _set_job_status(session, job: Job, status: str, detail: str = "", **extra):
    job.status = status
    job.detail = detail
    for k, v in extra.items():
        setattr(job, k, v)
    job.updated_at = int(dt.datetime.utcnow().timestamp())
    await session.commit()

async def process_job(job_id: str) -> bool:
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Job).where(Job.id == job_id))
        job: Optional[Job] = res.scalar_one_or_none()
        if not job:
            logger.warning("job_not_found", extra={"job_id": job_id})
            return True

        # 1) pakai path blob dari DB apa adanya
        src_blob_name = job.result_blob or job.filename
        if not src_blob_name or not src_blob_name.strip():
            await _set_job_status(session, job, "FAILED", "Source blob name not set")
            logger.error("job_fail_no_src_blob", extra={"job_id": job_id})
            return True

        # 2) cek eksistensi
        data, ctype = await _fetch_blob_bytes(INPUT_CONTAINER, src_blob_name)
        if not data:
            await _set_job_status(session, job, "FAILED", f"Input blob not found: {src_blob_name}")
            logger.error("job_fail_src_not_found", extra={"job_id": job_id, "blob_name": src_blob_name})
            return True

        try:
            sample_text = data[:32768].decode("utf-8", errors="ignore")
        except Exception:
            sample_text = ""

        # 3) glossary (optional)
        try:
            glossary_bytes = await compose_glossary_tsv(job.source_lang or "auto", job.target_lang or "en", sample_text)
            glossary_blob_name = f"jobs/{job_id}/glossary.tsv"
            blob_put_bytes(INPUT_CONTAINER, glossary_blob_name, glossary_bytes, content_type="text/tab-separated-values")
            glossary_sas = generate_blob_sas_url(INPUT_CONTAINER, glossary_blob_name, minutes=180)
            logger.info("glossary_ready", extra={"job_id": job_id, "blob_name": glossary_blob_name, "bytes": len(glossary_bytes)})
        except Exception as e:
            logger.warning("glossary_failed", extra={"job_id": job_id, "error": str(e)})
            glossary_sas = None

        # 4) SAS container + prefix (dari folder file sumber)
        try:
            src_container_sas = generate_container_sas_url(
                INPUT_CONTAINER,
                minutes=180,
                permission=ContainerSasPermissions(read=True, list=True),
            )
            dst_container_sas = generate_container_sas_url(
                OUTPUT_CONTAINER,
                minutes=180,
                permission=ContainerSasPermissions(write=True, create=True, add=True, list=True, read=True),
            )
        except Exception as e:
            await _set_job_status(session, job, "FAILED", f"Cannot create container SAS: {e}")
            logger.error("sas_container_fail", extra={"job_id": job_id, "error": str(e)})
            return True

        src_dir, _ = _split_dir_base(src_blob_name)
        src_prefix = f"{src_dir}/" if src_dir else ""

        # Preflight HEAD (optional)
        try:
            sas_src = generate_blob_sas_url(INPUT_CONTAINER, src_blob_name, minutes=30)
            await _assert_head_ok(sas_src, "Source blob SAS")
            logger.info("preflight_ok", extra={"job_id": job_id, "blob_name": src_blob_name})
        except Exception as e:
            await _set_job_status(session, job, "FAILED", f"Preflight source SAS failed: {e}")
            logger.error("preflight_fail", extra={"job_id": job_id, "blob_name": src_blob_name, "error": str(e)})
            return True

        # 5) submit → poll
        async with httpx.AsyncClient(timeout=120.0) as client:
            
            try:
                logger.info("translator_start", extra={
                    "job_id": job_id, "src_prefix": src_prefix, "src_lang": job.source_lang or "auto", "tgt_lang": job.target_lang or "en"
                })

                dst_prefix = src_prefix  # contoh: "jobs/<job_id>/input/"

                if os.getenv("WORKER_CLEAN_OUTPUT_BEFORE_SUBMIT", "1") == "1":
                    try:
                        deleted = clear_prefix(OUTPUT_CONTAINER, dst_prefix)
                        logger.info(
                            "clean_output_prefix",
                            extra={"job_id": job_id, "prefix": dst_prefix, "deleted": deleted}
                        )
                    except Exception as e:
                        logger.warning(
                            "clean_output_prefix_error",
                            extra={"job_id": job_id, "prefix": dst_prefix, "error": str(e)}
                        )

                batch_id = await _translator_start(
                    client,
                    src_container_sas_url=src_container_sas,
                    src_prefix=src_prefix,
                    dst_container_sas_url=dst_container_sas,
                    source_lang=(job.source_lang or "auto"),
                    target_lang=(job.target_lang or "en"),
                    glossary_url=glossary_sas,
                )
                logger.info("translator_batch", extra={"job_id": job_id, "batch_id": batch_id})
            except httpx.HTTPStatusError as e:
                err = e.response.text if e.response is not None else str(e)
                await _set_job_status(session, job, "FAILED", detail=f"Translator start error: {err}")
                logger.error("translator_start_error", extra={"job_id": job_id, "error": err})
                return True

            if not batch_id:
                await _set_job_status(session, job, "FAILED", detail="Failed to start document translation (no batch id)")
                logger.error("translator_no_batch_id", extra={"job_id": job_id})
                return True

            result = await _translator_poll(client, batch_id, timeout_s=3600, interval_s=3)
            logger.info("translator_result", extra={"job_id": job_id, "status": result.get("status")})

        if (result.get("status") or "").lower() != "succeeded":
            await _set_job_status(session, job, "FAILED", detail=json.dumps(result)[:4000])
            logger.error("translator_failed", extra={"job_id": job_id, "detail_snippet": json.dumps(result)[:500]})
            return True

        # 6) ambil hasil dari OUTPUT container (path sama)
        data_out, ctype_out = await _fetch_blob_bytes(OUTPUT_CONTAINER, src_blob_name)
        if not data_out:
            base = os.path.basename(src_blob_name)
            data_out, ctype_out = await _fetch_blob_bytes(OUTPUT_CONTAINER, base)
            if not data_out:
                await _set_job_status(
                    session, job, "FAILED",
                    detail=f"Translated file not found in output container (tried '{src_blob_name}' and '{base}')",
                )
                logger.error("output_not_found", extra={"job_id": job_id, "tried": [src_blob_name, base]})
                return True
            src_blob_name = base  # translator mungkin menaruh di root

        # 7) rename output → <original>_<tgt>.<ext>
        src_dir, src_base = _split_dir_base(src_blob_name)
        src_base_clean = _safe_basename_for_blob(src_base)
        name_noext, ext = os.path.splitext(src_base_clean)
        tgt = (job.target_lang or "en").lower()

        out_base = _safe_basename_for_blob(f"{name_noext}_{tgt}{ext or '.pdf'}")
        out_blob_name = f"{src_dir}/{out_base}" if src_dir else out_base

        # 8) fonts pass (optional)
        try:
            data_out = enforce_fonts_by_lang(job.filename or src_base_clean, data_out, tgt)
        except Exception as e:
            logger.warning("font_pass_error", extra={"job_id": job_id, "error": str(e)})

        # 9) simpan hasil
        blob_put_bytes(OUTPUT_CONTAINER, out_blob_name, data_out, content_type=ctype_out)

        # 10) SAS download
        sas_url = generate_blob_sas_url(OUTPUT_CONTAINER, out_blob_name, minutes=180)

        # 11) OneDrive (optional)
        onedrive_item_id, onedrive_url = (None, None)
        try:
            if job.user_id:
                safe_onedrive_name = out_base if "." in out_base else (out_base + (ext or ".pdf"))
                onedrive_item_id, onedrive_url = await upload_bytes_to_user_onedrive(
                    session, job.user_id, safe_onedrive_name, data_out
                )
                logger.info("onedrive_ok", extra={"job_id": job_id, "item_id": onedrive_item_id, "url": onedrive_url})
        except Exception as e:
            logger.warning("onedrive_fail", extra={"job_id": job_id, "error": str(e)})

        # 12) update DB
        await _set_job_status(
            session, job, "SUCCEEDED", detail="",
            result_blob=out_blob_name,
            download_url=sas_url,
            onedrive_item_id=onedrive_item_id or "",
            onedrive_url=onedrive_url or "",
        )
        logger.info("job_succeeded", extra={
            "job_id": job_id, "result_blob": out_blob_name, "download_url": sas_url, "tgt": tgt
        })
        return True

# ==================== Runner loop ====================
async def main():
    max_messages = int(os.getenv("WORKER_MAX_MESSAGES", "8"))
    visibility   = int(os.getenv("WORKER_VISIBILITY_TIMEOUT", "300"))
    poll_wait    = int(os.getenv("WORKER_POLL_WAIT", "60"))
    concurrency  = int(os.getenv("WORKER_CONCURRENCY", "5"))

    # SERVICE_START facts
    try:
        home_usage = shutil.disk_usage(str(Path.home()))
        tmp_usage  = shutil.disk_usage("/tmp") if os.path.isdir("/tmp") else None
        facts = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "cpu_count": os.cpu_count(),
            "mem_total_gb": round((os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES'))/ (1024**3), 2) if hasattr(os, "sysconf") else None,
            "home": {"total_gb": round(home_usage.total/(1024**3),2), "free_gb": round(home_usage.free/(1024**3),2)} if home_usage else None,
            "tmp": {"total_gb": round(tmp_usage.total/(1024**3),2), "free_gb": round(tmp_usage.free/(1024**3),2)} if tmp_usage else None,
            "WEBSITE_SITE_NAME": os.getenv("WEBSITE_SITE_NAME"),
            "WEBSITE_SKU": os.getenv("WEBSITE_SKU"),
            "WEBSITE_INSTANCE_ID": os.getenv("WEBSITE_INSTANCE_ID"),
            "REGION_NAME": os.getenv("REGION_NAME"),
        }
    except Exception:
        facts = {}

    logger.info("SERVICE_START", extra={"facts": facts})

    sem = asyncio.Semaphore(concurrency)
    logger.info("queue_listener_start", extra={
        "concurrency": concurrency, "max_messages": max_messages, "visibility": visibility, "poll_wait": poll_wait
    })

    async def _handle(msg):
        async with sem:
            try:
                body = json.loads(msg.content)
                job_id = body.get("job_id")
                if not job_id:
                    logger.warning("msg_missing_job_id")
                    _qc.delete_message(msg.id, msg.pop_receipt)

                    return
                logger.info("msg_submit", extra={"job_id": job_id})
                await process_job(job_id)
                try:
                    _qc.delete_message(msg.id, msg.pop_receipt)
                    logger.info("msg_done", extra={"job_id": job_id})
                except Exception as e:
                    logger.warning("msg_delete_warn", extra={"job_id": job_id, "error": str(e)})
            except Exception as e:
                logger.exception("msg_process_error", extra={"error": str(e)})

    loop_count = 0
    while True:
        try:
            loop_count += 1
            if loop_count % 10 == 0:
                logger.info("heartbeat", extra={"polls": loop_count})

            paged = _qc.receive_messages(
                messages_per_page=max_messages,
                visibility_timeout=visibility,
                timeout=poll_wait,
            )

            got = False
            tasks = []
            for page in paged.by_page():
                msgs = list(page)
                if msgs:
                    got = True
                    logger.info("messages_received", extra={"count": len(msgs)})
                for m in msgs:
                    tasks.append(asyncio.create_task(_handle(m)))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            if not got:
                await asyncio.sleep(1.0)

        except Exception as e:
            logger.error("queue_receive_error", extra={"error": str(e)})
            await asyncio.sleep(2.0)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass