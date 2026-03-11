# app/services/translator.py
from __future__ import annotations

import asyncio
import time
from typing import Optional, Tuple, Dict, Any, List

from ..config import settings
from .http import http_client

# Prioritaskan endpoint Document Translator
_ENDPOINT = (
    (settings.AZURE_TRANSLATOR_ENDPOINT or "").strip().rstrip("/")
    or (settings.AZURE_TRANSLATOR_ENDPOINT or "").strip().rstrip("/")
)
if not _ENDPOINT:
    raise RuntimeError(
        "Set AZURE_TRANSLATOR_DOC_ENDPOINT atau AZURE_TRANSLATOR_ENDPOINT (tanpa trailing slash)."
    )

API_CANDIDATES: List[str] = [
    f"{_ENDPOINT}/translator/document/batches?api-version=2024-11-15",
    f"{_ENDPOINT}/translator/document/batches?api-version=2024-05-01",
    f"{_ENDPOINT}/translator/document/batches?api-version=2023-10-01-preview",
    f"{_ENDPOINT}/translator/text/batch/v1.0/batches",
]

_HEADERS = {
    "Ocp-Apim-Subscription-Key": settings.AZURE_TRANSLATOR_KEY,
    "Ocp-Apim-Subscription-Region": settings.AZURE_TRANSLATOR_REGION,
    "Content-Type": "application/json",
}

_printed_once = False
_last_working_url: Optional[str] = None


async def _create_batch_internal(sess, json_body: dict) -> Tuple[str, str]:
    global _last_working_url, _printed_once
    candidates = [
        f"{_ENDPOINT}/translator/document/batches?api-version=2024-05-01",
        f"{_ENDPOINT}/translator/document/batches?api-version=2023-04-01",
    ]
    tried = []
    for url in candidates:
        if not _printed_once:
            print("[translator] endpoint:", _ENDPOINT)
            print("[translator] region  :", settings.AZURE_TRANSLATOR_REGION)
            print("[translator] try     :", url)
            _printed_once = True
        else:
            print("[translator] try     :", url)

        async with sess.post(url, json=json_body, headers=_HEADERS) as resp:
            if resp.status == 404:
                tried.append(f"404:{url}")
                continue
            if resp.status >= 300:
                txt = await resp.text()
                raise RuntimeError(f"Create batch failed: {resp.status} {txt} | url={url}")

            op_loc = resp.headers.get("Operation-Location") or resp.headers.get("operation-location")
            if not op_loc:
                txt = await resp.text()
                raise RuntimeError(f"Missing Operation-Location | url={url} | body={txt}")

            _last_working_url = url
            return op_loc, url

    raise RuntimeError(f"All candidate URLs gave 404. Tried: {', '.join(tried)}")

async def _post_with_fallback(json_body: dict) -> tuple[str, str]:
    global _printed_once, _last_working_url
    sess = await http_client.get_session()

    tried: List[str] = []
    candidates = [_last_working_url] + [u for u in API_CANDIDATES if u != _last_working_url] if _last_working_url else API_CANDIDATES

    for url in candidates:
        if not _printed_once:
            print("[translator] endpoint:", _ENDPOINT)
            print("[translator] region  :", settings.AZURE_TRANSLATOR_REGION)
            print("[translator] try     :", url)
            _printed_once = True
        else:
            print("[translator] try     :", url)

        async with sess.post(url, json=json_body, headers=_HEADERS) as resp:
            if resp.status == 404:
                tried.append(f"404:{url}")
                continue
            if resp.status >= 300:
                txt = await resp.text()
                raise RuntimeError(f"Create batch failed: {resp.status} {txt} | url={url}")

            op_loc = resp.headers.get("Operation-Location") or resp.headers.get("operation-location")
            if not op_loc:
                txt = await resp.text()
                raise RuntimeError(f"Missing Operation-Location | url={url} | body={txt}")

            _last_working_url = url
            return op_loc, url

    raise RuntimeError(f"Semua kandidat URL 404. Dicoba: {', '.join(tried)}")


async def create_batch(
    source_container_sas: str,
    target_container_sas: str,
    *,
    target_lang: str,
    source_lang: Optional[str] = None,
    prefix: Optional[str] = None,
    glossary_url: Optional[str] = None,
) -> Tuple[str, str]:
    """Create a container-to-container batch translation job.
    Returns (operation_location, used_url)
    """
    source = {"sourceUrl": source_container_sas, "storageSource": "AzureBlob"}
    if source_lang and source_lang.lower() != "auto":
        source["language"] = source_lang
    if prefix:
        source["filter"] = {"prefix": prefix}

    target = {
        "targetUrl": target_container_sas,
        "storageSource": "AzureBlob",
        "language": target_lang,
        "category": "general",
    }
    if glossary_url:
        target["glossaries"] = [{"glossaryUrl": glossary_url, "format": "TSV"}]

    body = {"inputs": [{"source": source, "targets": [target], "storageType": "File"}]}

    async with http_client() as sess:
        op_loc, used = await _create_batch_internal(sess, body)
        return op_loc, used


async def poll_batch(operation_location: str, *, timeout_s: int = 3600, interval_s: float = 3.0) -> Dict[str, Any]:
    """Polls a batch job until it completes, returns the job JSON."""
    async with http_client() as sess:
        start = time.time()
        delay = interval_s
        while True:
            async with sess.get(operation_location, headers=_HEADERS) as resp:
                data = await resp.json()
            status = (data.get("status") or data.get("Status") or "").capitalize()
            if status in {"Succeeded", "Failed", "Cancelled"}:
                return data
            await asyncio.sleep(delay)
            delay = min(delay * 1.75, 20.0)
            if time.time() - start > timeout_s:
                raise TimeoutError("Batch polling timed out")


async def translate_texts(
    texts: List[str],
    *,
    to: str,
    source: Optional[str] = None,
    category: str = "general",
) -> List[str]:
    """Translate up to 100 texts in one request via Text Translator API."""
    if not texts:
        return []
    url = f"{_ENDPOINT}/translate?api-version=3.0&to={to}"
    if source and source.lower() != "auto":
        url += f"&from={source}"
    if category and category != "general":
        url += f"&category={category}"

    body = [{"Text": t if t is not None else ""} for t in texts]

    async with http_client() as sess:
        async with sess.post(url, headers={**_HEADERS, "Content-Type": "application/json"}, json=body) as resp:
            if resp.status >= 300:
                txt = await resp.text()
                raise RuntimeError(f"Text translate failed: {resp.status} {txt}")
            data = await resp.json()

    out: List[str] = []
    for item in data:
        tr = (item.get("translations") or [])
        out.append((tr[0].get("text") if tr else ""))
    return out
