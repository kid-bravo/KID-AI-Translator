from __future__ import annotations

import json
from azure.storage.queue import QueueClient
from ..config import settings

_qc: QueueClient

if settings.AZURE_STORAGE_CONNECTION_STRING:
    _qc = QueueClient.from_connection_string(
        settings.AZURE_STORAGE_CONNECTION_STRING, settings.AZURE_STORAGE_QUEUE_NAME
    )
else:
    _qc = QueueClient(
        queue_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.queue.core.windows.net/"
                  f"{settings.AZURE_STORAGE_QUEUE_NAME}",
        credential=settings.AZURE_STORAGE_ACCOUNT_KEY,
    )

try:
    _qc.create_queue()
except Exception:
    pass


async def enqueue_job(payload: dict, *, visibility_timeout: int | None = None):
    """
    Kirim pesan ke queue.
    visibility_timeout (detik) memberi jeda sebelum pesan bisa diproses worker
    agar DB commit pasti terlihat. Default None -> gunakan default service.
    """
    import json
    vt = visibility_timeout if visibility_timeout and visibility_timeout > 0 else None
    _ = _qc.send_message(json.dumps(payload), visibility_timeout=vt)
