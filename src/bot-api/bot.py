from botbuilder.core import ActivityHandler, TurnContext
import os, httpx, uuid, logging, asyncio, datetime

# ====== Translator (GLOBAL endpoint) ======
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION   = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY      = os.getenv("TRANSLATOR_KEY")

# ====== Storage (Document Translation) ======
from azure.storage.blob import (
    BlobServiceClient, generate_blob_sas, BlobSasPermissions,
    generate_container_sas, ContainerSasPermissions
)
STORAGE_ACCOUNT_NAME      = os.getenv("STORAGE_ACCOUNT_NAME")
STORAGE_ACCOUNT_KEY       = os.getenv("STORAGE_ACCOUNT_KEY")
STORAGE_CONTAINER_SOURCE  = os.getenv("STORAGE_CONTAINER_SOURCE", "input")
STORAGE_CONTAINER_TARGET  = os.getenv("STORAGE_CONTAINER_TARGET", "output")

# ====== Kredensial Bot (untuk unduh lampiran protected) ======
from botframework.connector.auth import MicrosoftAppCredentials
MICROSOFT_APP_ID       = os.getenv("MicrosoftAppId")
MICROSOFT_APP_PASSWORD = os.getenv("MicrosoftAppPassword")

logging.basicConfig(level=logging.INFO)
MAX_TEXT_LEN = 5000


class TranslatorBot(ActivityHandler):
    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        welcome = (
            "Halo! 👋 Saya AI Translator.\n"
            "• Teks: `id->en Selamat pagi` atau ketik kalimat (default id->en)\n"
            "• Dokumen: unggah PDF/DOCX/PPTX/XLSX ke chat ini\n"
            "Ketik `ping` untuk uji sambungan."
        )
        await turn_context.send_activity(welcome)

    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()

        # Heartbeat — cek jalur pesan
        if text.lower() == "ping":
            await turn_context.send_activity("pong")
            return

        # ===== Attachment? → proses dokumen =====
        if turn_context.activity.attachments:
            try:
                await self._handle_attachments(turn_context)  # default target: en
            except Exception as e:
                logging.exception("handle_attachments failed")
                await turn_context.send_activity(f"⚠️ Gagal memproses lampiran: {e}")
            return

        # ===== Alur TEKS =====
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

    # ===== Parser arah 'xx->yy kalimat' =====
    def _parse_direction(self, text: str):
        """
        Return (from_lang, to_lang, content).
        Jika tidak ada 'xx->yy', from_lang=None (auto), to_lang='en'.
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

    # ===== Document Translation handler (dengan protected download fallback) =====
    async def _handle_attachments(self, turn_context: TurnContext, to_lang: str = "en"):
        att = turn_context.activity.attachments[0]
        name = att.name or f"file-{uuid.uuid4()}"
        content_url = getattr(att, "content_url", "") or ""
        att_type = getattr(att, "content_type", "")
        att_content = getattr(att, "content", None)

        logging.info(f"[att] name={name} type={att_type} url={content_url}")

        # 1) Tentukan URL unduh yang benar:
        #    - Jika ada content.downloadUrl (Teams File Download Info) → pakai itu
        #    - Jika tidak, pakai attachment.content_url
        download_url = None
        if isinstance(att_content, dict) and att_content.get("downloadUrl"):
            download_url = att_content.get("downloadUrl")
            logging.info(f"[att] gunakan content.downloadUrl: {download_url}")
        else:
            download_url = content_url
            logging.info(f"[att] gunakan attachment.content_url: {download_url}")

        if not download_url:
            await turn_context.send_activity("Lampiran tidak memiliki URL unduh yang valid.")
            return

        # 2) Download file → coba tanpa auth dulu, bila gagal, retry dengan Bearer token bot
        file_bytes = None
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(download_url)
                if r.status_code == 200 and r.content:
                    file_bytes = r.content
                else:
                    if not (MICROSOFT_APP_ID and MICROSOFT_APP_PASSWORD):
                        raise Exception(f"contentUrl protected ({r.status_code}) dan bot tidak punya kredensial.")
                    creds = MicrosoftAppCredentials(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
                    token = await creds.get_access_token()
                    r2 = await client.get(download_url, headers={"Authorization": f"Bearer {token}"})
                    r2.raise_for_status()
                    file_bytes = r2.content
        except Exception:
            logging.exception("download attachment failed")
            await turn_context.send_activity(
                "Gagal mengunduh file dari Teams. Coba drag-&-drop dari perangkat. "
                "Jika tetap gagal, kemungkinan file tersimpan di OneDrive/SharePoint (perlu izin Graph)."
            )
            return

        # 3) Upload ke Blob input/
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

        # 4) SAS read untuk input blob
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

        # 5) SAS write untuk output container (folder = job_id)
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

        # 6) Submit Document Translation (Batch API)
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

        # 7) Poll status hingga selesai (~90 detik)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for _ in range(30):
                    s = await client.get(status_url, headers=headers)
                    data = s.json()
                    if data.get("status") in ("Succeeded", "Failed", "Cancelled"):
                        break
                    await asyncio.sleep(3)

            if data.get("status") != "Succeeded":
                await turn_context.send_activity(f"Job gagal/berhenti. Status: **{data.get('status')}**")
                return

            # 8) Enumerasi hasil di output/<job_id>/
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
