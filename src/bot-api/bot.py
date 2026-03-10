from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Attachment, Activity
import os, httpx, uuid, logging, asyncio, datetime, json
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

# ===================== Translator (Text - GLOBAL) =====================
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION   = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY      = os.getenv("TRANSLATOR_KEY")

# ===== Translator (Document Translation - RESOURCE endpoint) ==========
DOC_TRANSLATION_ENDPOINT = (os.getenv("DOC_TRANSLATION_ENDPOINT") or "").rstrip("/")
DOC_TRANSLATION_KEY      = os.getenv("DOC_TRANSLATION_KEY") or os.getenv("TRANSLATOR_KEY")

# ===================== Storage (Blob) =================================
from azure.storage.blob import (
    BlobServiceClient, generate_blob_sas, BlobSasPermissions,
    generate_container_sas, ContainerSasPermissions
)
STORAGE_ACCOUNT_NAME      = os.getenv("STORAGE_ACCOUNT_NAME")
STORAGE_ACCOUNT_KEY       = os.getenv("STORAGE_ACCOUNT_KEY")
STORAGE_CONTAINER_SOURCE  = os.getenv("STORAGE_CONTAINER_SOURCE", "input")
STORAGE_CONTAINER_TARGET  = os.getenv("STORAGE_CONTAINER_TARGET", "output")

# ===================== Bot Credentials (protected downloads) ==========
try:
    from botframework.connector.auth import MicrosoftAppCredentials
except Exception:
    MicrosoftAppCredentials = None
MICROSOFT_APP_ID       = os.getenv("MicrosoftAppId")
MICROSOFT_APP_PASSWORD = os.getenv("MicrosoftAppPassword")

logging.basicConfig(level=logging.INFO)
MAX_TEXT_LEN = 5000

# ===================== Preferensi bahasa (in-memory) ==================
SESSIONS = {}  # { user_id: { "from_lang": None|"id"|..., "to_lang": "en"|... } }

# ===================== Daftar bahasa di Card ==========================
LANG_CHOICES = [
    ("Indonesian (id)", "id"),
    ("English (en)",    "en"),
    ("Japanese (ja)",   "ja"),
    ("Vietnamese (vi)", "vi"),
    ("Lao (lo)",        "lo"),
    ("Chinese (Simplified) (zh-Hans)", "zh-Hans"),
    ("Chinese (Traditional) (zh-Hant)", "zh-Hant"),
    ("Korean (ko)",     "ko"),
    ("French (fr)",     "fr"),
    ("German (de)",     "de"),
    ("Spanish (es)",    "es"),
    ("Thai (th)",       "th"),
    ("Russian (ru)",    "ru"),
    ("Filipino (fil)",  "fil"),
    ("Portuguese (pt)", "pt"),
]

# ===================== Util: mask 'sig' SAS di log ====================
def _mask_sas(url: str) -> str:
    try:
        parts = urlsplit(url)
        qs = dict(parse_qsl(parts.query))
        if "sig" in qs:
            qs["sig"] = "***masked***"
        new_q = urlencode(qs, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_q, parts.fragment))
    except Exception:
        return url


class TranslatorBot(ActivityHandler):

    # ---------------------- Greetings ----------------------
    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        await self._send_menu_card(turn_context)

    # ---------------------- Message Entry ----------------------
    async def on_message_activity(self, turn_context: TurnContext):
        user_id = (turn_context.activity.from_property and turn_context.activity.from_property.id) or "unknown"
        text   = (turn_context.activity.text or "").strip()
        value  = turn_context.activity.value or {}

        # A) Submit dari Menu/Language Card
        if isinstance(value, dict):
            vtype  = value.get("type")
            action = value.get("action")
            if vtype == "menu" and action == "translate_document":
                await self._send_language_card(turn_context, user_id)
                return
            if vtype == "menu" and action == "how_to_upload":
                await self._send_howto(turn_context)
                return
            if vtype == "set_lang":
                src = value.get("src_lang")  # "auto" / code
                dst = value.get("dst_lang") or "en"
                SESSIONS[user_id] = {"from_lang": (None if src in (None, "", "auto") else src), "to_lang": dst}
                await turn_context.send_activity(
                    f"Bahasa diset. Sumber: `{src or 'auto'}`, Tujuan: `{dst}`.\n"
                    f"• Kirim teks untuk diterjemahkan **atau**\n"
                    f"• Unggah dokumen (PDF/DOCX/PPTX/XLSX) lalu tekan **Send**."
                )
                return

        # B) Heartbeat & entry menu
        if text.lower() in ("ping",):
            await turn_context.send_activity("pong")
            return
        if text.lower() in ("hi", "halo", "translate", "start", "menu", "help", "/help"):
            await self._send_menu_card(turn_context)
            return

        # C) Dokumen
        if turn_context.activity.attachments:
            pref = SESSIONS.get(user_id, {"to_lang": "en", "from_lang": None})
            try:
                await self._handle_attachments(turn_context, to_lang=(pref.get("to_lang") or "en"))
            except Exception as e:
                logging.exception("handle_attachments failed")
                await turn_context.send_activity(f"⚠️ Gagal memproses lampiran: {e}")
            return

        # D) Teks
        from_lang, to_lang, content = self._parse_direction(text)
        # Jika user tidak tulis arah, gunakan preferensi
        pref = SESSIONS.get(user_id, {"to_lang": "en", "from_lang": None})
        if to_lang is None:
            to_lang = pref.get("to_lang") or "en"
        if from_lang is None:
            from_lang = pref.get("from_lang")  # None → auto-detect

        if not content:
            await self._send_menu_card(turn_context)
            return

        if len(content) > MAX_TEXT_LEN:
            await turn_context.send_activity(f"Teks terlalu panjang ({len(content)}). Batas {MAX_TEXT_LEN} karakter.")
            return

        if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
            await turn_context.send_activity("Translator (text) belum dikonfigurasi di server.")
            return

        url = f"{TRANSLATOR_ENDPOINT}/translate?api-version=3.0&to={to_lang}"
        if from_lang:
            url += f"&from={from_lang}"

        headers = {
            "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
            "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,
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

    # ---------- MENU CARD ----------
    async def _send_menu_card(self, turn_context: TurnContext):
        card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "body": [
                {"type": "TextBlock", "text": "KID AI Translator", "weight": "Bolder", "size": "Large"},
                {"type": "TextBlock", "text": "Translate documents + Chat with AI", "spacing": "Small"},
                {"type": "TextBlock", "text": "Attach a file to translate, or choose an option below.", "isSubtle": True, "spacing": "Small"}
            ],
            "actions": [
                {"type": "Action.Submit", "title": "📄 Translate document", "data": {"type": "menu", "action": "translate_document"}},
                {"type": "Action.Submit", "title": "ℹ️ How to upload", "data": {"type": "menu", "action": "how_to_upload"}}
            ]
        }
        attachment = Attachment(
            content_type="application/vnd.microsoft.card.adaptive",
            content=card
        )
        activity = Activity(type="message", attachments=[attachment])
        await turn_context.send_activity(activity)

    # ---------- HOW TO UPLOAD ----------
    async def _send_howto(self, turn_context: TurnContext):
        steps = (
            "Cara upload dokumen:\n"
            "1) Klik ikon **Attach (+)** → **Upload from this device** (jangan pilih _Attach cloud files_).\n"
            "2) Pilih file **PDF/DOCX/PPTX/XLSX**, lalu tekan **Send**.\n"
            "3) Tunggu 10–60 detik, hasil akan muncul sebagai **file** di chat.\n"
            "Tip: ketik `translate` untuk memilih bahasa tujuan."
        )
        await turn_context.send_activity(steps)

    # ---------- Adaptive Card: pilih bahasa ----------
    async def _send_language_card(self, turn_context: TurnContext, user_id: str):
        pref = SESSIONS.get(user_id, {"to_lang": "en", "from_lang": None})
        dst_default = pref.get("to_lang") or "en"
        src_default = pref.get("from_lang") or "auto"

        choices_json = [{"title": label, "value": code} for (label, code) in LANG_CHOICES]
        src_choices   = [{"title": "Auto detect", "value": "auto"}] + choices_json

        card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "body": [
                {"type": "TextBlock", "text": "Pilih bahasa", "weight": "Bolder", "size": "Medium"},
                {"type": "TextBlock", "text": "Sumber", "spacing": "Small"},
                {"type": "Input.ChoiceSet", "id": "src_lang", "style": "compact", "value": src_default, "choices": src_choices},
                {"type": "TextBlock", "text": "Tujuan", "spacing": "Small"},
                {"type": "Input.ChoiceSet", "id": "dst_lang", "style": "compact", "value": dst_default, "choices": choices_json},
                {"type": "TextBlock", "text": "Setelah Start, kirim teks atau unggah file (PDF/DOCX/PPTX/XLSX).", "isSubtle": True, "spacing": "Small"}
            ],
            "actions": [
                {"type": "Action.Submit", "title": "Start", "data": {"type": "set_lang"}}
            ]
        }
        attachment = Attachment(
            content_type="application/vnd.microsoft.card.adaptive",
            content=card
        )
        activity = Activity(type="message", attachments=[attachment])
        await turn_context.send_activity(activity)

    # ---------- Parser arah 'xx->yy kalimat' ----------
    def _parse_direction(self, text: str):
        default_to = "en"
        if not text:
            return None, default_to, ""
        parts = text.split(" ")
        first = parts[0]
        if "->" in first and len(parts) > 1:
            a, b = first.split("->", 1)
            return a.lower(), b.lower(), " ".join(parts[1:])
        return None, default_to, text

    # ---------- Util: Teams File Download Card (MIME DIBETULKAN) ----------
    def _teams_file_download_card(self, file_name: str, download_url: str, unique_id: str) -> Attachment:
        ext = file_name.split(".")[-1].lower() if "." in file_name else "bin"
        # MIME harus EXACT seperti di bawah:
        return Attachment(
            content_type="application/vnd.microsoft.teams.card.file.download.info",
            content={
                "downloadUrl": download_url,
                "uniqueId": unique_id,
                "fileType": ext
            },
            name=file_name
        )

    # ---------- Dokumen ----------
    async def _handle_attachments(self, turn_context: TurnContext, to_lang: str = "en"):
        # Validasi endpoint/keys
        if (not DOC_TRANSLATION_ENDPOINT) or ("cognitive.microsofttranslator.com" in DOC_TRANSLATION_ENDPOINT):
            await turn_context.send_activity(
                "Konfigurasi belum lengkap: `DOC_TRANSLATION_ENDPOINT` harus endpoint **resource** Translator, contoh: "
                "`https://<nama-resource>.cognitiveservices.azure.com`."
            )
            return
        if not DOC_TRANSLATION_KEY:
            await turn_context.send_activity("`DOC_TRANSLATION_KEY` belum diisi.")
            return

        att = turn_context.activity.attachments[0]
        name = att.name or f"file-{uuid.uuid4()}"
        content_url = getattr(att, "content_url", "") or ""
        att_type = getattr(att, "content_type", "")
        att_content = getattr(att, "content", None)
        logging.info(f"[att] name={name} type={att_type} url={content_url}")

        # 1) Pilih URL unduh
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

        # 2) Unduh bytes (tanpa auth → fallback Bearer token bot)
        try:
            file_bytes = None
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(download_url)
                if r.status_code == 200 and r.content:
                    file_bytes = r.content
                else:
                    if not (MicrosoftAppCredentials and MICROSOFT_APP_ID and MICROSOFT_APP_PASSWORD):
                        raise Exception(f"contentUrl protected ({r.status_code}) dan kredensial bot tidak tersedia.")
                    creds = MicrosoftAppCredentials(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
                    token = await creds.get_access_token()
                    r2 = await client.get(download_url, headers={"Authorization": f"Bearer {token}"})
                    r2.raise_for_status()
                    file_bytes = r2.content
        except Exception:
            logging.exception("download attachment failed")
            await turn_context.send_activity(
                "Gagal mengunduh file dari Teams. Coba **drag-&-drop dari perangkat**. "
                "Jika tetap gagal, kemungkinan file tersimpan di OneDrive/SharePoint (butuh izin Graph)."
            )
            return

        # 3) Upload ke Blob input/<jobId>/<file>
        if not (STORAGE_ACCOUNT_NAME and STORAGE_ACCOUNT_KEY):
            await turn_context.send_activity("Storage belum dikonfigurasi di server.")
            return
        bs = BlobServiceClient(
            account_url=f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
            credential=STORAGE_ACCOUNT_KEY
        )

        job_id = str(uuid.uuid4())
        src_blob_name = f"{job_id}/{name}"
        bs.get_blob_client(container=STORAGE_CONTAINER_SOURCE, blob=src_blob_name).upload_blob(file_bytes, overwrite=True)

        # 4) SAS: Source=CONTAINER + filter.prefix, Target=FOLDER path (before ?)
        expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=4)

        # Source CONTAINER SAS: read + list
        sas_src_container = generate_container_sas(
            account_name=STORAGE_ACCOUNT_NAME,
            container_name=STORAGE_CONTAINER_SOURCE,
            account_key=STORAGE_ACCOUNT_KEY,
            permission=ContainerSasPermissions(read=True, list=True),
            expiry=expiry
        )
        source_container_url = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/{STORAGE_CONTAINER_SOURCE}?{sas_src_container}"

        # Target folder SAS (path sebelum ?), izin w+a+c+l(+r)
        sas_tgt_container = generate_container_sas(
            account_name=STORAGE_ACCOUNT_NAME,
            container_name=STORAGE_CONTAINER_TARGET,
            account_key=STORAGE_ACCOUNT_KEY,
            permission=ContainerSasPermissions(write=True, add=True, create=True, list=True, read=True),
            expiry=expiry
        )
        target_url = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/{STORAGE_CONTAINER_TARGET}/{job_id}?{sas_tgt_container}"

        # Debug log (masked) — hapus setelah stabil
        logging.warning(f"[DEBUG-SAS] SOURCE_CONTAINER_URL = {_mask_sas(source_container_url)}")
        logging.warning(f"[DEBUG-SAS] TARGET_URL          = {_mask_sas(target_url)}")
        logging.warning(f"[DEBUG-SAS] FILTER_PREFIX       = {job_id}/")

        # 5) Submit Document Translation
        batch_url = f"{DOC_TRANSLATION_ENDPOINT}/translator/text/batch/v1.0/batches"
        headers = {
            "Ocp-Apim-Subscription-Key": DOC_TRANSLATION_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "inputs": [{
                "source": { "sourceUrl": source_container_url, "filter": { "prefix": f"{job_id}/" } },
                "targets": [{ "targetUrl": target_url, "language": to_lang }]
            }]
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(batch_url, headers=headers, json=payload)
            if r.status_code not in (201, 202):
                await turn_context.send_activity(f"Submit job gagal {r.status_code}: {r.text[:400]}")
                return
            status_url = r.headers.get("Operation-Location") or r.headers.get("Location")

        await turn_context.send_activity(f"Job diterima untuk **{name}**. Menunggu hasil…")

        # 6) Poll status + tampilkan DETAIL bila gagal
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for _ in range(30):
                    s = await client.get(status_url, headers=headers)
                    data = s.json()
                    if data.get("status") in ("Succeeded", "Failed", "Cancelled", "ValidationFailed"):
                        break
                    await asyncio.sleep(3)

            if data.get("status") != "Succeeded":
                # Ambil detail error (job + per dokumen)
                err_msg = None
                try:
                    errs = data.get("errors") or []
                    if errs:
                        parts = [f"{e.get('code')}: {e.get('message')}" for e in errs[:3]]
                        err_msg = " | ".join(parts)

                    docs_url = (status_url.rstrip("/")) + "/documents?skip=0&top=20"
                    async with httpx.AsyncClient(timeout=15) as c2:
                        d = await c2.get(docs_url, headers=headers)
                    if d.status_code == 200:
                        dj = d.json()
                        failed = [
                            f"{(it.get('error') or {}).get('code')}: {(it.get('error') or {}).get('message')} (path={it.get('path')})"
                            for it in (dj.get('value') or []) if it.get('status') not in ('Succeeded', 'Running')
                        ]
                        if failed:
                            err_msg = (err_msg + " || " if err_msg else "") + " ; ".join(failed[:2])
                except Exception as ex:
                    logging.exception(f"pull-detail-failed: {ex}")

                raw_snippet = json.dumps(data)[:1200]
                msg = f"Job gagal/berhenti. Status: **{data.get('status')}**"
                if err_msg:
                    msg += f" — Detail: {err_msg}"
                msg += f"\n\nRAW job (snippet): ```{raw_snippet}```"
                await turn_context.send_activity(msg)
                return

            # 7) Kirim hasil: coba File Download Card → fallback ke link kalau ditolak
            cc = bs.get_container_client(STORAGE_CONTAINER_TARGET)
            blobs = list(cc.list_blobs(name_starts_with=f"{job_id}/"))
            if not blobs:
                await turn_context.send_activity("Job selesai tapi file hasil tidak ditemukan.")
                return

            await turn_context.send_activity("Hasil terjemahan:")
            for b in blobs:
                # SAS read untuk blob hasil
                sas_read_out = generate_blob_sas(
                    account_name=STORAGE_ACCOUNT_NAME,
                    container_name=STORAGE_CONTAINER_TARGET,
                    blob_name=b.name,
                    account_key=STORAGE_ACCOUNT_KEY,
                    permission=BlobSasPermissions(read=True),
                    expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=4)
                )
                download_url = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/{STORAGE_CONTAINER_TARGET}/{b.name}?{sas_read_out}"
                file_name = b.name.split("/", 1)[-1]  # buang prefix job_id/

                # --- kirim sebagai FILE BUBBLE ---
                try:
                    file_card = self._teams_file_download_card(file_name, download_url, unique_id=b.name)
                    await turn_context.send_activity(Activity(type="message", attachments=[file_card]))
                except Exception as send_err:
                    logging.exception(f"send-file-card failed: {send_err}")
                    # --- fallback: kirim link biasa agar user tetap dapat hasil ---
                    await turn_context.send_activity(download_url)

        except Exception as e:
            logging.exception("document-translation polling failed")
            await turn_context.send_activity(f"Gagal memproses dokumen: {e}")
