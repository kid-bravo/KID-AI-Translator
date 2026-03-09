from botbuilder.core import ActivityHandler, TurnContext
import os, httpx, uuid, logging, asyncio, datetime

# ====== Translator (GLOBAL endpoint) ======
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION   = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY      = os.getenv("TRANSLATOR_KEY")

# ====== Storage (untuk Document Translation MVP) ======
from azure.storage.blob import (
    BlobServiceClient, generate_blob_sas, BlobSasPermissions,
    generate_container_sas, ContainerSasPermissions
)
STORAGE_ACCOUNT_NAME      = os.getenv("STORAGE_ACCOUNT_NAME")
STORAGE_ACCOUNT_KEY       = os.getenv("STORAGE_ACCOUNT_KEY")
STORAGE_CONTAINER_SOURCE  = os.getenv("STORAGE_CONTAINER_SOURCE", "input")
STORAGE_CONTAINER_TARGET  = os.getenv("STORAGE_CONTAINER_TARGET", "output")

logging.basicConfig(level=logging.INFO)

MAX_TEXT_LEN = 5000


class TranslatorBot(ActivityHandler):
    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        welcome = (
            "Halo! 👋 Saya AI Translator.\n"
            "Contoh: `id->en Selamat pagi` atau `ja->id おはようございます`.\n"
            "Jika tidak menulis arah, default `id->en`.\n"
            "Kirim *file* (PDF/DOCX/PPTX/XLSX) untuk terjemah dokumen."
        )
        await turn_context.send_activity(welcome)

    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()

        # Heartbeat — untuk cek cepat jalur pesan
        if text.lower() == "ping":
            await turn_context.send_activity("pong")
            return

        # ====== Jika ada lampiran: jalankan alur Document Translation ======
        if turn_context.activity.attachments:
            try:
                await self._handle_attachments(turn_context)
            except Exception as e:
                logging.exception("handle_attachments failed")
                await turn_context.send_activity(f"⚠️ Gagal memproses lampiran: {e}")
            return

        # ====== Alur TEKS ======
        from_lang, to_lang, content = self._parse_direction(text)

        if not content:
            await turn_context.send_activity(
                "Format: `id->en Selamat pagi` atau ketik kalimat (default id->en)."
            )
            return

        if len(content) > MAX_TEXT_LEN:
            await turn_context.send_activity(
                f"Teks terlalu panjang ({len(content)}). Batas {MAX_TEXT_LEN} karakter."
            )
            return

        if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
            await turn_context.send_activity("Translator belum dikonfigurasi di server.")
            return

        # GLOBAL endpoint → /translate?api-version=3.0
        url = f"{TRANSLATOR_ENDPOINT}/translate?api-version=3.0&to={to_lang}"
        if from_lang:
            url += f"&from={from_lang}"

        headers = {
            "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
            "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,  # wajib untuk global
            "Content-type": "application/json",
            "X-ClientTraceId": str(uuid.uuid4())
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=[{"Text": content}], headers=headers)
                if r.status_code >= 400:
                    await turn_context.send_activity(f"Translator error {r.status_code}: {r.text[:300]}")
                    return
                data = r.json()
            translated = data[0]["translations"][0]["text"]
            await turn_context.send_activity(translated)
        except Exception as e:
            logging.exception("translate-text failed")
            await turn_context.send_activity(f"Gagal menerjemahkan: {e}")

    # ====== Parser arah 'xx->yy kalimat' ======
    def _parse_direction(self, text: str):
        """
        Mengembalikan (from_lang, to_lang, content).
        Jika tidak ada 'xx->yy', from_lang=None (auto detect), to_lang='en'.
        """
        default_to = "en"
        if not text:
            return None, default_to, ""
        parts = text.split(" ")
        first = parts[0]
        if "->" in first and len(parts) > 1:
            a, b = first.split("->", 1)
            return a.lower(), b.lower(), " ".join(parts[1:])
        return None, default_to, text

    # ====== Document Translation MVP (pakai SAS link hasil) ======
    async def _handle_attachments(self, turn_context: TurnContext, to_lang: str = "en"):
        att = turn_context.activity.attachments[0]
        name = att.name or f"file-{uuid.uuid4()}"
        content_url = att.content_url

        # 1) Download file dari Teams
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.get(content_url)
                res.raise_for_status()
                file_bytes = res.content
        except Exception:
            await turn_context.send_activity("Gagal mengunduh file dari Teams. Coba drag-&-drop file langsung (bukan link).")
            return

        # 2) Upload ke Blob input/
        if not (STORAGE_ACCOUNT_NAME and STORAGE_ACCOUNT_KEY):
            await turn_context.send_activity("Storage belum dikonfigurasi di server.")
            return

        bs = BlobServiceClient(
            account_url=f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
            credential=STORAGE_ACCOUNT_KEY
        )

        blob_name = f"{uuid.uuid4()}-{name}"
        bs.get_blob_client(container=STORAGE_CONTAINER_SOURCE, blob=blob_name).upload_blob(
            file_bytes, overwrite=True
        )

        # 3) SAS read untuk input blob
        sas_read = generate_blob_sas(
            account_name=STORAGE_ACCOUNT_NAME,
            container_name=STORAGE_CONTAINER_SOURCE,
            blob_name=blob_name,
            account_key=STORAGE_ACCOUNT_KEY,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=2)
        )
        source_sas_url = (
            f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/"
            f"{STORAGE_CONTAINER_SOURCE}/{blob_name}?{sas_read}"
        )

        # 4) SAS write untuk output container + folder jobId
        job_id = str(uuid.uuid4())
        sas_write = generate_container_sas(
            account_name=STORAGE_ACCOUNT_NAME,
            container_name=STORAGE_CONTAINER_TARGET,
            account_key=STORAGE_ACCOUNT_KEY,
            permission=ContainerSasPermissions(write=True, add=True, create=True, list=True),
            expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=2)
        )
        target_sas_url = (
            f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/"
            f"{STORAGE_CONTAINER_TARGET}?{sas_write}"
        )

        # 5) Submit Document Translation Batch API
        if not (TRANSLATOR_ENDPOINT and TRANSLATOR_KEY):
            await turn_context.send_activity("Translator belum dikonfigurasi di server.")
            return

        batch_url = f"{TRANSLATOR_ENDPOINT}/translator/text/batch/v1.1/batches"
        headers = {
            "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
            "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,
            "Content-Type": "application/json"
        }
        payload = {
            "inputs": [{
                "source": {"sourceUrl": source_sas_url},
                "targets": [{
                    "targetUrl": f"{target_sas_url}/{job_id}",
                    "language": to_lang
                }]
            }]
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(batch_url, headers=headers, json=payload)
            if r.status_code not in (201, 202):
                await turn_context.send_activity(f"Submit job gagal: {r.status_code} {r.text[:300]}")
                return
            status_url = r.headers.get("Operation-Location") or r.headers.get("Location")

        await turn_context.send_activity(f"Job diterima untuk **{name}**. Menunggu hasil…")

        # 6) Poll status hingga selesai
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for _ in range(30):  # ~90 detik
                    s = await client.get(status_url, headers=headers)
                    data = s.json()
                    if data.get("status") in ("Succeeded", "Failed", "Cancelled"):
                        break
                    await asyncio.sleep(3)

            if data.get("status") != "Succeeded":
                await turn_context.send_activity(f"Job gagal/berhenti. Status: **{data.get('status')}**")
                return

            # 7) Ambil daftar hasil di output/<job_id>/
            cc = bs.get_container_client(STORAGE_CONTAINER_TARGET)
            blobs = list(cc.list_blobs(name_starts_with=f"{job_id}/"))

            if not blobs:
                await turn_context.send_activity("Job selesai tapi file hasil tidak ditemukan.")
                return

            await turn_context.send_activity("Hasil terjemahan (link unduh berlaku 2 jam):")
            for b in blobs:
                sas_read_out = generate_blob_sas(
                    account_name=STORAGE_ACCOUNT_NAME,
                    container_name=STORAGE_CONTAINER_TARGET,
                    blob_name=b.name,
                    account_key=STORAGE_ACCOUNT_KEY,
                    permission=BlobSasPermissions(read=True),
                    expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                )
                url = (
                    f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/"
                    f"{STORAGE_CONTAINER_TARGET}/{b.name}?{sas_read_out}"
                )
                await turn_context.send_activity(url)

        except Exception as e:
            logging.exception("document-translation polling failed")
            await turn_context.send_activity(f"Gagal memproses dokumen: {e}")
