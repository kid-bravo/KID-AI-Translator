# app/services/blob.py
from __future__ import annotations

import os
from datetime import datetime, timedelta
import datetime as dt
from typing import Optional
from urllib.parse import quote

from azure.storage.blob import (
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
    generate_container_sas,
    BlobSasPermissions,
    ContainerSasPermissions,
)

# Prefer instance `settings`, fallback ke ENV
try:
    from app.config import settings  # instance
except Exception:
    settings = None  # type: ignore


# ----------------------------- ENV helpers -----------------------------
def _get_env(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if not val and settings is not None and hasattr(settings, name):
        val = getattr(settings, name)  # type: ignore
    return (val or default).strip()

def _parse_account_key_from_conn_str(conn_str: str) -> str:
    for part in conn_str.split(";"):
        if part.strip().upper().startswith("ACCOUNTKEY="):
            return part.split("=", 1)[1]
    return ""

_BLOB_CS = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
# ------------------ BlobServiceClient (robust init) --------------------
_AZ_CONN_STR = _get_env("AZURE_STORAGE_CONNECTION_STRING")
_AZ_ACCOUNT  = _get_env("AZURE_STORAGE_ACCOUNT_NAME")
_AZ_KEY      = _get_env("AZURE_STORAGE_ACCOUNT_KEY")

if _AZ_CONN_STR:
    _blob = BlobServiceClient.from_connection_string(_AZ_CONN_STR)
    _ACCOUNT_NAME = _blob.account_name
    if not _AZ_KEY:
        _AZ_KEY = _parse_account_key_from_conn_str(_AZ_CONN_STR)
else:
    if not (_AZ_ACCOUNT and _AZ_KEY):
        raise RuntimeError(
            "Blob config kosong: set AZURE_STORAGE_CONNECTION_STRING "
            "ATAU (AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY)."
        )
    _blob = BlobServiceClient(
        account_url=f"https://{_AZ_ACCOUNT}.blob.core.windows.net",
        credential=_AZ_KEY,
    )
    _ACCOUNT_NAME = _AZ_ACCOUNT

if not _ACCOUNT_NAME:
    raise RuntimeError("Gagal menentukan Storage account name.")


# ----------------------------- Containers ------------------------------
def _ensure_container(name: str) -> None:
    try:
        _blob.create_container(name)
    except Exception:
        pass

_INPUT_CONTAINER  = _get_env("AZURE_INPUT_CONTAINER", "input") or "input"
_OUTPUT_CONTAINER = _get_env("AZURE_OUTPUT_CONTAINER", "output") or "output"
_ensure_container(_INPUT_CONTAINER)
_ensure_container(_OUTPUT_CONTAINER)


# ----------------------------- Utilities -------------------------------
def put_bytes(
    container: str,
    name: str,
    data: bytes,
    *,
    content_type: Optional[str] = None,
) -> str:
    _ensure_container(container)
    bc = _blob.get_blob_client(container=container, blob=name)
    bc.upload_blob(
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type) if content_type else None,
        max_concurrency=4,
    )
    return name


# ----------------------------- SAS makers ------------------------------
def _expiry(minutes: Optional[int]) -> dt.datetime:
    if minutes is None:
        minutes = int(os.getenv("BLOB_SAS_EXP_MIN", "1440"))  # default 24 jam
    return dt.datetime.utcnow() + dt.timedelta(minutes=minutes)

def generate_blob_sas_url(
    container: str,
    name: str,
    *,
    minutes: Optional[int] = None,
) -> str:
    if not _AZ_KEY:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_KEY tidak tersedia untuk generate SAS.")
    sas = generate_blob_sas(
        account_name=_ACCOUNT_NAME,
        container_name=container,
        blob_name=name,
        account_key=_AZ_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=_expiry(minutes),
    )
    encoded_path = quote(name, safe="/-_.()")
    return f"https://{_ACCOUNT_NAME}.blob.core.windows.net/{container}/{encoded_path}?{sas}"

def generate_container_sas_url(
    container: str,
    *,
    minutes: Optional[int] = None,
    permission: Optional[ContainerSasPermissions] = None,
) -> str:
    if not _AZ_KEY:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_KEY tidak tersedia untuk generate SAS.")
    perm = permission or ContainerSasPermissions(read=True, list=True)
    sas = generate_container_sas(
        account_name=_ACCOUNT_NAME,
        container_name=container,
        account_key=_AZ_KEY,
        permission=perm,
        expiry=_expiry(minutes),
    )
    return f"https://{_ACCOUNT_NAME}.blob.core.windows.net/{container}?{sas}"

def clear_prefix(container: str, prefix: str) -> int:
    """Hapus semua blob di container yang diawali prefix. Return jumlah yang dihapus."""
    bsc = BlobServiceClient.from_connection_string(_AZ_CONN_STR)
    cont = bsc.get_container_client(container)
    n = 0
    for blob in cont.list_blobs(name_starts_with=prefix):
        # kalau ada snapshot, aman-kan:
        cont.delete_blob(blob.name, delete_snapshots="include")
        n += 1
    return n

# ---------------- upload wrapper (kompat lama) -------------------------
async def upload_bytes_with_prefix(*args, **kwargs):
    """
    Mendukung:
      - Positional:
          (prefix, filename, data)
          (container, prefix, filename, data)
      - Keyword:
          prefix=..., filename=..., data=..., [container=..., content_type=...]
    Return: (sas_url, blob_name)
    """
    content_type = kwargs.pop("content_type", None)
    container = kwargs.pop("container", None)
    prefix = kwargs.pop("prefix", None)
    filename = kwargs.pop("filename", None)
    data = kwargs.pop("data", None)

    if filename is None or data is None:
        if len(args) == 3:
            prefix, filename, data = args
        elif len(args) == 4:
            container, prefix, filename, data = args
        else:
            raise TypeError(
                "upload_bytes_with_prefix expected (prefix, filename, data) "
                "atau (container, prefix, filename, data) atau keyword yang setara"
            )

    if not container:
        container = _INPUT_CONTAINER

    prefix = (prefix or "").strip().strip("/")
    blob_name = f"{prefix}/{filename}" if prefix else filename

    put_bytes(container, blob_name, data, content_type=content_type)
    sas_url = generate_blob_sas_url(container, blob_name)
    return sas_url, blob_name
