from __future__ import annotations

import os, io, sys, time, uuid, shutil, subprocess, tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from pypdf import PdfReader, PdfWriter, PdfMerger
from azure.storage.blob import (
    BlobServiceClient, ContentSettings,
    generate_container_sas, ContainerSasPermissions
)

from dotenv import load_env
load_env()

# ---------- Config ----------
MAX_DOC_MB = 39.5
MAX_BATCH_MB = 240.0
POLL_INTERVAL = 3.0

AZ_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
INPUT_CONTAINER  = os.getenv("AZURE_INPUT_CONTAINER", "input")
OUTPUT_CONTAINER = os.getenv("AZURE_OUTPUT_CONTAINER", "output")

AZ_TRN_ENDPOINT = (os.getenv("AZURE_TRANSLATOR_ENDPOINT", "") or "").rstrip("/")
AZ_TRN_REGION   = os.getenv("AZURE_TRANSLATOR_REGION", "")
AZ_TRN_KEY      = os.getenv("AZURE_TRANSLATOR_KEY", "")

if not all([AZ_CONN_STR, AZ_TRN_ENDPOINT, AZ_TRN_REGION, AZ_TRN_KEY]):
    print("[large_translation] Missing required ENV. Please set storage conn string + translator endpoint/region/key.", file=sys.stderr)

_blob = BlobServiceClient.from_connection_string(AZ_CONN_STR)
_src = _blob.get_container_client(INPUT_CONTAINER)
_tgt = _blob.get_container_client(OUTPUT_CONTAINER)

def _sizeof_mb_path(p: Path) -> float:
    return p.stat().st_size / (1024*1024)

def _has_soffice() -> bool:
    from shutil import which
    return which("soffice") is not None

def _to_pdf_if_needed(local_path: Path) -> Path:
    ext = local_path.suffix.lower()
    if ext == ".pdf":
        return local_path
    if ext in (".docx", ".doc", ".pptx", ".ppt"):
        if not _has_soffice():
            raise RuntimeError("LibreOffice (soffice) not found; required for DOCX/PPTX -> PDF.")
        out_dir = local_path.parent
        cmd = ["soffice","--headless","--norestore","--invisible","--convert-to","pdf","--outdir",str(out_dir),str(local_path)]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"LibreOffice convert failed:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}")
        pdf = out_dir / (local_path.stem + ".pdf")
        if not pdf.exists():
            # fallback: pick the newest pdf
            cands = list(out_dir.glob("*.pdf"))
            if not cands:
                raise RuntimeError("Conversion done but PDF not found.")
            pdf = max(cands, key=lambda p: p.stat().st_mtime)
        return pdf
    raise RuntimeError("Unsupported format. Use PDF/DOCX/PPTX.")

def _split_pdf_by_size(src_pdf: Path, max_mb: float=MAX_DOC_MB) -> List[Path]:
    reader = PdfReader(str(src_pdf))
    out_dir = src_pdf.parent / f"parts_{uuid.uuid4().hex[:6]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    parts: List[Path] = []
    n = len(reader.pages)
    start = 0
    while start < n:
        end = start
        last_ok: Optional[Path] = None
        while end < n:
            w = PdfWriter()
            for i in range(start, end+1):
                w.add_page(reader.pages[i])
            cand = out_dir / f"part_{len(parts)+1:03d}.pdf"
            with cand.open("wb") as f: w.write(f)
            if _sizeof_mb_path(cand) <= max_mb:
                last_ok = cand; end += 1
            else:
                cand.unlink(missing_ok=True); break
        if last_ok is None:
            w = PdfWriter(); w.add_page(reader.pages[start])
            cand = out_dir / f"part_{len(parts)+1:03d}.pdf"
            with cand.open("wb") as f: w.write(f)
            parts.append(cand); start += 1
        else:
            parts.append(last_ok); start = end
    return parts

def _merge_pdfs(pdf_paths: List[Path], out_path: Path) -> Path:
    merger = PdfMerger()
    for p in pdf_paths:
        merger.append(str(p))
    with out_path.open("wb") as f:
        merger.write(f)
    merger.close()
    return out_path

def _upload_files(container, paths: List[Path], prefix: str) -> List[str]:
    blob_names = []
    for p in paths:
        with p.open("rb") as f:
            name = f"{prefix}/{p.name}"
            container.upload_blob(name=name, data=f, overwrite=True, content_settings=ContentSettings(content_type="application/pdf"))
            blob_names.append(name)
    return blob_names

def _get_container_sas_url(container_name: str, minutes: int=120) -> str:
    # obtain account info
    acct = _blob.account_name
    # try to get key from conn string
    key = None
    try:
        for kv in os.getenv("AZURE_STORAGE_CONNECTION_STRING","").split(";"):
            if kv.strip().startswith("AccountKey="):
                key = kv.split("=",1)[1].strip()
                break
    except Exception:
        pass
    if not key:
        # try internal credential
        cred = getattr(_blob, "credential", None)
        key = getattr(cred, "account_key", None)

    if not key:
        raise RuntimeError("Cannot get account key for SAS generation. Provide connection string with AccountKey.")

    sas = generate_container_sas(
        account_name=acct,
        container_name=container_name,
        account_key=key,
        permission=ContainerSasPermissions(read=True, write=True, list=True, create=True, add=True),
        expiry=int(time.time()) + minutes*60,
    )
    return f"https://{acct}.blob.core.windows.net/{container_name}?{sas}"

def _create_batch(source_container_sas: str, target_container_sas: str, prefix: str, target_lang: str, source_lang: str="") -> str:
    url = f"{AZ_TRN_ENDPOINT}/translator/document/batches?api-version=2024-05-01"
    headers = {
        "Ocp-Apim-Subscription-Key": AZ_TRN_KEY,
        "Ocp-Apim-Subscription-Region": AZ_TRN_REGION,
        "Content-Type": "application/json"
    }
    body = {
        "inputs": [{
            "source": {
                "sourceUrl": source_container_sas,
                **({"language": source_lang} if source_lang else {}),
                "filter": {"prefix": prefix}
            },
            "targets": [{
                "targetUrl": target_container_sas,
                "language": target_lang
            }]
        }]
    }
    r = requests.post(url, json=body, headers=headers, timeout=60)
    r.raise_for_status()
    op_loc = r.headers.get("Operation-Location") or r.headers.get("operation-location")
    if not op_loc:
        raise RuntimeError("Missing Operation-Location header from create batch.")
    # Extract batch id from body too (optional)
    return op_loc

def _poll_until_done(op_location: str, timeout_s: int=3600) -> dict:
    headers = {
        "Ocp-Apim-Subscription-Key": AZ_TRN_KEY,
        "Ocp-Apim-Subscription-Region": AZ_TRN_REGION
    }
    t0 = time.time()
    # op_location can be URL to the batch; normalize to status URL if needed
    url = op_location if "translator/document/batches/" in op_location else op_location
    while True:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"Polling failed: {r.status_code} {r.text}")
        data = r.json()
        st = (data.get("status") or "").lower()
        if st in ("succeeded","failed","cancelled"):
            return data
        if time.time() - t0 > timeout_s:
            raise TimeoutError("Polling timeout.")
        time.sleep(POLL_INTERVAL)

def _group_batches(paths, max_batch_mb=MAX_BATCH_MB):
    batches = []
    cur, cur_mb = [], 0.0
    for p in sorted(paths, key=lambda x: x.name):
        sz = _sizeof_mb_path(p)
        if cur and cur_mb + sz > max_batch_mb:
            batches.append(cur)
            cur, cur_mb = [], 0.0
        cur.append(p); cur_mb += sz
    if cur:
        batches.append(cur)
    return batches

def run_large_translation(src_blob_name: str, target_lang: str, source_lang: str="") -> Tuple[str, str]:
    """
    src_blob_name: blob path in INPUT_CONTAINER (e.g., 'uploads/bigfile.pdf')
    Returns: (output_blob_name, sas_url_to_download)
    """
    # 1) Download to temp
    tmpdir = Path(tempfile.mkdtemp(prefix="lgtrn-"))
    try:
        dl = _src.download_blob(src_blob_name)
        raw = dl.readall()
    except Exception as e:
        raise RuntimeError(f"Cannot download input blob '{src_blob_name}': {e}")

    ext = Path(src_blob_name).suffix.lower()
    local_src = tmpdir / f"src{ext or '.bin'}"
    local_src.write_bytes(raw)

    # 2) Normalize to PDF
    pdf = _to_pdf_if_needed(local_src)

    # 3) Split to < 40MB parts
    parts = _split_pdf_by_size(pdf, MAX_DOC_MB)

    # 4) Group parts into batches under job prefix
    job_id = uuid.uuid4().hex[:12]
    batches = _group_batches(parts, MAX_BATCH_MB)

    # 5) Build SAS (source/target)
    src_sas = _get_container_sas_url(INPUT_CONTAINER, minutes=180)
    tgt_sas = _get_container_sas_url(OUTPUT_CONTAINER, minutes=180)

    out_dir = tmpdir / "out"; out_dir.mkdir(parents=True, exist_ok=True)
    translated_paths: List[Path] = []

    for bi, batch_files in enumerate(batches, start=1):
        prefix = f"jobs/{job_id}/parts/b{bi:02d}"
        _upload_files(_src, batch_files, prefix)

        # 6) Create batch with prefix & poll
        op_loc = _create_batch(src_sas, tgt_sas, prefix, target_lang=target_lang, source_lang=source_lang)
        result = _poll_until_done(op_loc, timeout_s=3600)
        if (result.get("status") or "").lower() != "succeeded":
            raise RuntimeError(f"Batch {bi} failed: {result}")

        # 7) Download translated parts for this batch
        for p in sorted(batch_files, key=lambda x: x.name):
            out_blob = f"{prefix}/{p.name}"
            try:
                stream = _tgt.download_blob(out_blob)
                data = stream.readall()
            except Exception:
                # fallback basename
                try:
                    stream = _tgt.download_blob(p.name)
                    data = stream.readall()
                except Exception:
                    raise RuntimeError(f"Translated chunk not found in output container: {out_blob}")
            dst = out_dir / p.name
            dst.write_bytes(data)
            translated_paths.append(dst)

    merged = tmpdir / f"{Path(src_blob_name).stem}_TRANSLATED_{target_lang}.pdf"
    _merge_pdfs(translated_paths, merged)

    # 8) Upload final merged PDF to OUTPUT (next to input path)
    parent = os.path.dirname(src_blob_name)
    out_blob_name = f"{parent}/{merged.name}" if parent else merged.name
    with merged.open("rb") as f:
        _tgt.upload_blob(name=out_blob_name, data=f, overwrite=True, content_settings=ContentSettings(content_type="application/pdf"))

    # 9) Make SAS for final file
    final_sas = _get_container_sas_url(OUTPUT_CONTAINER, minutes=180)
    # Convert container SAS to file SAS by appending &rest? No, easiest: rely on container SAS + path
    sas_url = f"{final_sas}&restype=container&comp=list"  # informational; usually you will share direct blob SAS in your worker

    # Better: generate a blob SAS
    # For simplicity here we just return a blob URL without SAS; integrate your own SAS helper in worker.
    direct_url = f"https://{_blob.account_name}.blob.core.windows.net/{OUTPUT_CONTAINER}/{out_blob_name}"
    return out_blob_name, direct_url

# ------------- CLI -------------
def _upload_local_file_to_input(local: Path, blob_name: str):
    with local.open("rb") as f:
        _src.upload_blob(name=blob_name, data=f, overwrite=True, content_settings=ContentSettings(content_type="application/pdf"))

def _sas_blob_download_url(container: str, blob_name: str, minutes: int=60) -> str:
    # generate blob-level SAS for direct download
    acct = _blob.account_name
    key = None
    for kv in os.getenv("AZURE_STORAGE_CONNECTION_STRING","").split(";"):
        if kv.strip().startswith("AccountKey="):
            key = kv.split("=",1)[1].strip()
            break
    from azure.storage.blob import generate_blob_sas, BlobSasPermissions
    sas = generate_blob_sas(
        account_name=acct,
        container_name=container,
        blob_name=blob_name,
        account_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=int(time.time()) + minutes*60,
    )
    return f"https://{acct}.blob.core.windows.net/{container}/{blob_name}?{sas}"

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Translate large document via split-batch-merge")
    ap.add_argument("input", help="Local file path (PDF/DOCX/PPTX). If provided, we'll upload then translate.")
    ap.add_argument("--to", required=True, help="Target language, e.g., id, en, ja")
    ap.add_argument("--from", dest="source_lang", default="", help="Source language code or empty for auto")
    ap.add_argument("--blob", default="", help="Optional blob path to use under INPUT container")
    args = ap.parse_args()

    local = Path(args.input).expanduser().resolve()
    if not local.exists():
        print(f"File not found: {local}", file=sys.stderr)
        sys.exit(2)

    blob_name = args.blob or f"uploads/{local.name}"
    # upload to input then run pipeline
    _upload_local_file_to_input(local, blob_name)
    out_blob, url = run_large_translation(blob_name, target_lang=args.to, source_lang=args.source_lang)
    print("OK")
    print("Output blob:", out_blob)
    print("Direct URL  :", url)
