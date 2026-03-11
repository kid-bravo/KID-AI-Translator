from __future__ import annotations
import base64
import os, re, json, asyncio, logging, datetime, random, time, mimetypes, pickle, string
from typing import List, Optional, Tuple, Dict, Any

import aiohttp
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
# ------------ Bot Framework SDK ------------
from botbuilder.core import (
    BotFrameworkAdapter, BotFrameworkAdapterSettings,
    ConversationState, MemoryStorage, MessageFactory, TurnContext, CardFactory, UserState, Storage
)
from botbuilder.core.teams import TeamsActivityHandler
from botbuilder.dialogs import (
    DialogSet, DialogTurnStatus, WaterfallDialog, WaterfallStepContext,
    OAuthPrompt, OAuthPromptSettings, TextPrompt
)
from botbuilder.schema import (
    Activity, ActivityTypes, Attachment, HeroCard, CardAction, ActionTypes, ChannelAccount
)

# Azure Blob SDK (async) – NO BlobRequestConditions
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.blob import ContentSettings
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from app.services.blob import clear_prefix

# ============= ENV =============
from dotenv import load_dotenv
load_dotenv()

APP_ID              = os.environ.get("MicrosoftAppId", "").strip()
APP_PASSWORD        = os.environ.get("MicrosoftAppPassword", "").strip()
APP_TENANT_ID       = os.environ.get("MicrosoftAppTenantId", "").strip()
OAUTH_SCOPE         = os.environ.get("ToChannelFromBotOAuthScope", "https://api.botframework.com/.default").strip()
OAUTH_CONNECTION    = os.environ.get("OAUTH_CONNECTION_NAME", "msgraph").strip()

TRANSLATOR_API      = os.environ.get("TRANSLATOR_API", "http://localhost:8080").rstrip("/")
UPLOAD_CREATE_PATH  = os.environ.get("UPLOAD_CREATE_PATH", "/upload/create").strip()
JOB_DETAIL_PATH     = os.environ.get("JOB_DETAIL_PATH", "/jobs").strip()

BOT_MAX_POLL_SEC    = int(os.environ.get("BOT_MAX_POLL_SEC", "1800"))

PUBLIC_BASE_URL     = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TEAMS_JWT_SECRET    = os.environ.get("TEAMS_JWT_SECRET", "dev-secret")
TEAMS_JWT_EXP_MIN   = int(os.environ.get("TEAMS_JWT_EXP_MIN", "30"))

AZURE_OPENAI_ENDPOINT   = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY    = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_API_VERSION= os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")

CHAT_MAX_TURNS       = int(os.environ.get("CHAT_MAX_TURNS", "30"))
CHAT_SUMMARIZE_AFTER = int(os.environ.get("CHAT_SUMMARIZE_AFTER", "12"))

# STRICT mode
STRICT_TARGET_FROM_START = True
PENDING_TTL_SEC = int(os.environ.get("PENDING_TTL_SEC", "1800"))

# State storage (Azure Blob)
_AZ_CONN = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
_AZ_CONT = os.environ.get("STATE_BLOB_CONTAINER", "").strip()
_USE_BLOB = bool(_AZ_CONN and _AZ_CONT)
_BLOB_PREFIX = os.environ.get("STATE_BLOB_PREFIX", "botstate").strip().strip("/")  # folder dalam container

# -------------------- CHANGED: system prompt now enforces language & fixed address --------------------
# CHAT_SYSTEM_PROMPT = """
# You are SBCS helper. Be concise, friendly, and accurate.

# LANGUAGE POLICY:
# - Always reply in the same language as the user's latest message.
# - If the message is mixed, use the dominant language.
# - Do not switch languages unless explicitly requested.

# FIXED FACT — SBCS INDONESIA ADDRESS:
# - If the user asks for the address/location of SBCS/SBCSI Indonesia, reply with exactly:
#   Menara SMBC 38 Floor Jl. Dr. Ide Anak Agung Gde Agung Kav. 5.5-5.6, Jakarta, Daerah Khusus Ibukota Jakarta 12950
# - Do not add or change any characters around that address.
# """.strip()
# ------------------------------------------------------------------------------------------------------

# -------------------- ADDED: Deterministic address + intent regex --------------------
# ADDRESS_FIXED = "Menara SMBC 38 Floor Jl. Dr. Ide Anak Agung Gde Agung Kav. 5.5-5.6, Jakarta, Daerah Khusus Ibukota Jakarta 12950"

# ADDR_RE = re.compile(
#     r"(alamat|address|lokasi|where\s+is|where'?s)\b.*\b(sbcs|sbcsi)\b", re.I
# )
# ------------------------------------------------------------------------------------

# WHO_INTENT_RE = re.compile(
#     r"\b(who\s+are\s+you|who\s+is\s+s?bcs|what\s+is\s+s?bcs|about\s+s?bcs|tell\s+me\s+about\s+s?bcs)\b",
#     re.I,
# )

# -------------------- (keep original content; not sent to users anymore) --------------------
# SBCS_BIO_EN = ("""
# [ROLE]
# Anda adalah asisten riset & konsultasi untuk PT SBCS Indonesia (SBCSI).

# [COMPANY BLURB (EN)]
# PT SBCS Indonesia (SBCSI) was established in 2012 as a member of the Sumitomo Mitsui Banking Corporation (SMBC) Group with the primary purpose of supporting Japanese companies expanding their business into Indonesia. We currently offer various services—research, consulting, and business matching proposals—to companies aiming to expand in Indonesia and from Indonesia to other countries. As an affiliate research & consulting company of a global bank, we leverage professional knowledge and deep insights to contribute to the long-term development of clients and society by accelerating efforts to “Pursue Economic Value” and “Create Social Value.”
# Official site: https://sbcs.co.id/

# [LANGUAGE POLICY — MUST FOLLOW INPUT LANGUAGE]
# - Selalu balas menggunakan bahasa yang sama dengan pesan pengguna.
# - Jika pesan bercampur, utamakan bahasa yang paling dominan dalam pesan tersebut.
# - Jangan ganti bahasa di tengah percakapan kecuali pengguna meminta eksplisit.

# [FIXED FACT — SBCS INDONESIA ADDRESS]
# - Jika pengguna menanyakan alamat SBCS Indonesia (dengan frasa seperti: “alamat sbcs”, “alamat kantor sbcsi”, “lokasi sbcs indonesia”, dsb.), jawab **tepat**:
#   Menara SMBC 38 Floor Jl. Dr. Ide Anak Agung Gde Agung Kav. 5.5-5.6, Jakarta, Daerah Khusus Ibukota Jakarta 12950
# - Jangan ubah ejaan, urutan, atau menambahkan info lain kecuali diminta.

# [STYLE]
# - Jawab ringkas, jelas, dan to the point.
# - Gunakan bullet points untuk daftar.
# - Jika tidak yakin atau di luar cakupan, katakan tidak yakin dan minta klarifikasi singkat (tetap dalam bahasa pengguna).

# [GUARDRAILS]
# - Jangan berspekulasi. Gunakan informasi di prompt ini saat relevan.
# - Hormati kebijakan bahasa dan alamat di atas secara konsisten.
# """
# )

# ------------ optional AOAI ------------
# try:
#     from openai import AzureOpenAI  # type: ignore
# except Exception:
#     AzureOpenAI = None

# def _aoai() -> Optional["AzureOpenAI"]:
#     if not (AzureOpenAI and AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT):
#         return None
#     return AzureOpenAI(
#         api_key=AZURE_OPENAI_API_KEY,
#         api_version=AZURE_OPENAI_API_VERSION,
#         azure_endpoint=AZURE_OPENAI_ENDPOINT,
#     )

# ------------ Logger ------------
def setup_logging(service: str = "bot") -> logging.Logger:
    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            # Hindari overwrite field standar seperti 'created'
            payload = {
                "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "lvl": record.levelname,
                "name": record.name,
                "msg": record.getMessage(),
                "pid": os.getpid(),
                "service": getattr(record, "service", None),
            }
            # copy fields lain dari record.__dict__ (kecuali standar)
            for k, v in getattr(record, "__dict__", {}).items():
                if k in ("name", "msg", "args", "levelname", "levelno", "pathname",
                         "filename", "module", "exc_info", "exc_text", "stack_info",
                         "lineno", "funcName", "created", "msecs", "relativeCreated",
                         "thread", "threadName", "processName", "process", "message"):
                    continue
                if k not in payload and not k.startswith("_"):
                    try:
                        json.dumps(v)
                        payload[k] = v
                    except Exception:
                        payload[k] = str(v)
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return json.dumps(payload, ensure_ascii=False)

    logger = logging.getLogger(service)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)

    # Service start facts (sekali di-import)
    try:
        import shutil, platform, psutil  # type: ignore
    except Exception:
        psutil = None
    facts = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "hostname": platform.node(),
        "cpu_count": os.cpu_count(),
    }
    try:
        total = round((psutil.virtual_memory().total if psutil else 0) / (1024**3), 2)
        avail = round((psutil.virtual_memory().available if psutil else 0) / (1024**3), 2)
        facts["mem_total_gb"] = total
        facts["mem_avail_gb"] = avail
    except Exception:
        pass
    try:
        facts["home"] = {
            "total_gb": round(shutil.disk_usage(os.path.expanduser("~")).total / (1024**3), 2),
            "free_gb": round(shutil.disk_usage(os.path.expanduser("~")).free / (1024**3), 2),
        }
        facts["tmp"] = {
            "total_gb": round(shutil.disk_usage("/tmp").total / (1024**3), 2),
            "free_gb": round(shutil.disk_usage("/tmp").free / (1024**3), 2),
        }
    except Exception:
        pass
    for env_name in ("WEBSITE_SITE_NAME", "WEBSITE_SKU", "WEBSITE_INSTANCE_ID", "REGION_NAME"):
        if os.getenv(env_name):
            facts[env_name] = os.getenv(env_name)

    logger.info("SERVICE_START", extra={"service": service, "facts": facts, "engine_small": False})
    return logger

logger = setup_logging("bot")

# ============= Helpers =============
def _is_who_are_you_query(text: Optional[str]) -> bool:
    return bool(WHO_INTENT_RE.search((text or "").lower()))

def _aad_token_endpoint() -> str:
    tenant = (APP_TENANT_ID or "").strip()
    if not tenant:
        raise RuntimeError("Missing MicrosoftAppTenantId")
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

async def _get_bot_access_token_direct() -> str:
    scope = (OAUTH_SCOPE or "https://api.botframework.com/.default").strip()
    url = _aad_token_endpoint()
    data = {"client_id": APP_ID, "client_secret": APP_PASSWORD, "grant_type": "client_credentials", "scope": scope}
    t0 = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(url, data=data) as r:
            txt = await r.text()
            dur = round((time.perf_counter() - t0)*1000, 2)
            if r.status != 200:
                try: j = json.loads(txt)
                except: j = {"raw": txt[:300]}
                logger.error("auth.bot_token.failed", extra={"status": r.status, "duration_ms": dur, "error": j.get("error"), "desc": (j.get("error_description","") or "")[:300]})
                err = j.get("error"); desc = j.get("error_description") or j.get("raw", "")
                raise RuntimeError(f"Failed to get access token with error: {err}, error_description: {desc}")
            j = json.loads(txt)
            token = j.get("access_token")
            logger.info("auth.bot_token.ok", extra={"status": r.status, "duration_ms": dur})
            if not token:
                raise RuntimeError("No access_token in token response")
            return token

async def _get_fresh_user_token(turn_context: TurnContext, connection_name: str) -> Optional[str]:
    try:
        token_response = await turn_context.adapter.get_user_token(turn_context, connection_name)
        return token_response.token if token_response and token_response.token else None
    except Exception as e:
        logger.error("get_user_token_failed", extra={"err": str(e)})
        return None

def share_id_from_weburl(web_url: str) -> str:
    return "u!" + base64.urlsafe_b64encode(web_url.encode("utf-8")).decode("ascii").rstrip("=")


async def graph_item_min(user_token: str, web_url: str):
    """Return (downloadUrl, webUrl, size) dari webUrl OneDrive/SharePoint."""
    share_id = share_id_from_weburl(web_url)
    url = ("https://graph.microsoft.com/v1.0/shares/"
        f"{share_id}/driveItem?$select=webUrl,@microsoft.graph.downloadUrl,size")
    headers = {"Authorization": f"Bearer {user_token}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            j = await r.json()
            return j.get("@microsoft.graph.downloadUrl"), j.get("webUrl"), int(j.get("size", 0) or 0)
        
async def _graph_item_min(user_token: str, web_url: str):
    share_id = _share_id_from_weburl(web_url)
    url = f"https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem?$select=webUrl,@microsoft.graph.downloadUrl,size"
    headers = {"Authorization": f"Bearer {user_token}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            j = await r.json()
            return j.get("@microsoft.graph.downloadUrl"), j.get("webUrl"), int(j.get("size", 0) or 0)
        
async def _pptx_ready(url: str, expect_size: int|None) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.head(url, allow_redirects=True) as r:
                if r.status >= 400: return False
                ct = (r.headers.get("Content-Type") or "").lower()
                cl = int(r.headers.get("Content-Length") or 0)
                if ("presentationml" not in ct) and ("zip" not in ct) and ("octet-stream" not in ct):
                    return False
                if expect_size and cl and abs(cl - expect_size) > max(4096, expect_size // 20):
                    return False
            async with s.get(url, headers={"Range":"bytes=0-3"}) as r2:
                if r2.status not in (200,206): return False
                return (await r2.read()).startswith(b"PK")
    except Exception:
        return False
        
async def looks_like_pptx(url: str, expect_size: int|None = None) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.head(url, allow_redirects=True) as r:
                if r.status >= 400: return False
                ct = (r.headers.get("Content-Type") or "").lower()
                cl = int(r.headers.get("Content-Length") or 0)
            if not any(k in ct for k in ("presentationml", "zip", "octet-stream")):
                return False
            if expect_size and cl and abs(cl - expect_size) > max(2048, expect_size//20):
                return False
            async with aiohttp.ClientSession() as s2:
                async with s2.get(url, headers={"Range":"bytes=0-3"}) as r2:
                    if r2.status not in (200,206): return False
                    return (await r2.read()).startswith(b"PK")  # ZIP magic
    except Exception:
        return False
    
async def ensure_graph_download(user_token: str, dl: Optional[str], web: Optional[str], attempts: int = 4):
    """Pastikan kita pegang URL yang benar-benar mengunduh PPTX (bukan HTML)."""
    _, size = None, None
    if not dl and web:
        dl, web, size = await graph_item_min(user_token, web)

    delay = 0.8
    for _ in range(attempts):
        if dl and await looks_like_pptx(dl, size):
            return dl, web
        await asyncio.sleep(delay)
        delay = min(3.0, delay*1.7)
        if web:
            dl, web, size = await graph_item_min(user_token, web)
    return None, web

async def graph_download_url_from_weburl(user_token: str, web_url: str):
    sid = share_id_from_weburl(web_url)
    url = f"https://graph.microsoft.com/v1.0/shares/{sid}/driveItem?$select=@microsoft.graph.downloadUrl,webUrl"
    headers = {"Authorization": f"Bearer {user_token}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            j = await r.json()
            return j.get("@microsoft.graph.downloadUrl"), j.get("webUrl")

async def ensure_graph_download(user_token: str,
                                dl: Optional[str],
                                web: Optional[str],
                                attempts: int = 4):
    """
    Selalu prefer @microsoft.graph.downloadUrl jika webUrl tersedia.
    Fallback ke dl (SAS) hanya bila Graph gagal.
    """
    preferred, size = None, None

    # 1) Coba ambil direct download dari OneDrive/SharePoint
    if web:
        preferred, web, size = await graph_item_min(user_token, web)

    # 2) Kandidat awal = Graph kalau ada, kalau tidak pakai dl yang ada
    candidate = preferred or dl

    delay = 0.8
    for _ in range(attempts):
        if candidate and await looks_like_pptx(candidate, size):
            return candidate, web
        await asyncio.sleep(delay)
        delay = min(3.0, delay * 1.7)
        # refresh downloadUrl (link Graph itu short-lived)
        if web:
            preferred, web, size = await graph_item_min(user_token, web)
            candidate = preferred or candidate

    # kalau tetap gagal, terakhir balikin dl (SAS) apa adanya
    return dl, web


from msrest.exceptions import ClientRequestError
from requests.exceptions import ConnectionError as ReqConnectionError
from urllib3.exceptions import ProtocolError

async def _safe_send(context: TurnContext, activity, attempts: int = 4):
    delay = 0.6
    for i in range(attempts):
        try:
            return await context.send_activity(activity)
        except (ClientRequestError, ReqConnectionError, ProtocolError, ConnectionResetError):
            if i == attempts - 1:
                raise
            await asyncio.sleep(delay + random.random() * 0.4)
            delay = min(3.0, delay * 1.8)

# ============= Azure Blob Storage Lite (State) =============
_SAFE = set(string.ascii_letters + string.digits + "._-")

def _sanitize_key(s: str) -> str:
    s = s.replace(":", "_").replace("/", "_").replace("\\", "_")
    return "".join(ch if ch in _SAFE else "_" for ch in s)

class BlobStorageLite(Storage):
    """Minimal Storage untuk BotBuilder di atas Azure Blob (async).
       - Serialisasi pakai pickle (menghindari error JSON non-serializable).
       - Tidak pakai BlobRequestConditions (overwrite=True).
    """
    def __init__(self, conn_str: str, container_name: str, prefix: str = "botstate"):
        self._svc = BlobServiceClient.from_connection_string(conn_str)
        self._container = self._svc.get_container_client(container_name)
        self._prefix = prefix.strip("/")

    async def _ensure(self):
        try:
            await self._container.create_container()
            logger.info("storage.init", extra={"kind": "azure_blob", "container": self._container.container_name, "container_created": True})
        except Exception:
            logger.info("storage.init", extra={"kind": "azure_blob", "container": self._container.container_name, "container_created": False})

    def _blob_name(self, key: str) -> str:
        safe = _sanitize_key(key)
        return f"{self._prefix}/{safe}.bin"

    async def read(self, keys: List[str]) -> Dict[str, object]:
        if not keys:
            return {}
        await self._ensure()
        result: Dict[str, object] = {}
        for k in keys:
            name = self._blob_name(k)
            bc = self._container.get_blob_client(name)
            try:
                downloader = await bc.download_blob()
                data = await downloader.readall()
                try:
                    obj = pickle.loads(data)
                except Exception:
                    # fallback JSON -> pickle migration
                    obj = json.loads(data.decode("utf-8"))
                # tandai e_tag di dict (dipakai oleh SDK)
                if isinstance(obj, dict):
                    obj["e_tag"] = (downloader.properties.etag if downloader and downloader.properties else "*")
                result[k] = obj
                logger.info("storage.read.ok", extra={"key": k, "blob": name, "bytes": len(data)})
            except ResourceNotFoundError:
                continue
            except Exception as e:
                logger.error("storage.read.err", extra={"key": k, "blob": name, "err": str(e)})
        return result

    async def write(self, changes: Dict[str, object]):
        if not changes:
            return
        await self._ensure()
        for k, v in changes.items():
            name = self._blob_name(k)
            bc = self._container.get_blob_client(name)
            payload = v
            # Jika dict, buang 'e_tag'
            if isinstance(payload, dict):
                payload = dict(payload)
                payload.pop("e_tag", None)
            try:
                data = pickle.dumps(payload)
            except Exception:
                data = json.dumps(payload, default=lambda o: getattr(o, "__dict__", str(o)), ensure_ascii=False).encode("utf-8")
            try:
                await bc.upload_blob(
                    data,
                    overwrite=True,
                    content_settings=ContentSettings(content_type="application/octet-stream"),
                )
                logger.info("storage.write.ok", extra={"key": k, "blob": name, "bytes": len(data)})
            except Exception as e:
                logger.error("storage.write.err", extra={"key": k, "blob": name, "err": str(e)})
                raise

    async def delete(self, keys: List[str]):
        if not keys:
            return
        await self._ensure()
        for k in keys:
            name = self._blob_name(k)
            bc = self._container.get_blob_client(name)
            try:
                await bc.delete_blob()
                logger.info("storage.delete.ok", extra={"key": k, "blob": name})
            except ResourceNotFoundError:
                pass
            except Exception as e:
                logger.error("storage.delete.err", extra={"key": k, "blob": name, "err": str(e)})

# ============= Idempotency Gate (hindari double reply) =============
class ActivityGate:
    def __init__(self, conn_str: Optional[str], container: Optional[str], prefix: str = "events"):
        self._enabled = bool(conn_str and container)
        if not self._enabled:
            self._svc = None
            self._cont = None
            return
        self._svc = BlobServiceClient.from_connection_string(conn_str)  # type: ignore
        self._cont = self._svc.get_container_client(container)         # type: ignore
        self._prefix = prefix.strip("/")

    async def _ensure(self):
        if not self._enabled:
            return
        try:
            await self._cont.create_container()  # type: ignore
        except Exception:
            pass

    async def first_time(self, channel: str, conv_id: str, activity_id: str) -> bool:
        if not self._enabled:
            return True
        await self._ensure()
        name = f"{self._prefix}/{_sanitize_key(channel)}/{_sanitize_key(conv_id)}/{_sanitize_key(activity_id)}.lock"
        bc = self._cont.get_blob_client(name)  # type: ignore
        try:
            await bc.upload_blob(b"1", overwrite=False, content_settings=ContentSettings(content_type="text/plain"))
            return True
        except ResourceExistsError:
            logger.info("event.duplicate.skip", extra={"channel": channel, "conversation_id": conv_id, "activity_id": activity_id})
            return False
        except Exception:
            # Jangan blokir, biar bot tetap jalan
            return True

# ============= Teams file download + attachment check =============
async def _download_teams_file(turn_context: TurnContext, attachment: Attachment, user_token: str) -> bytes:
    ct = (attachment.content_type or "").lower()

    if ct == "application/vnd.microsoft.teams.file.download.info" and isinstance(attachment.content, dict):
        download_url = (attachment.content.get("downloadUrl") or attachment.content.get("downloadurl") or attachment.content.get("download_url"))
        if not download_url:
            raise RuntimeError("downloadUrl not found in Teams file.download.info")
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # coba tanpa auth
            try:
                async with session.get(download_url) as res:
                    if res.status == 200:
                        return await res.read()
                    await res.release()
            except Exception:
                pass
            # coba dengan user token (SharePoint/OneDrive)
            headers = {"Authorization": f"Bearer {user_token}", "Accept": "application/octet-stream"}
            async with session.get(download_url, headers=headers) as res2:
                if res2.status == 200:
                    return await res2.read()
                elif res2.status == 401:
                    raise Exception("User token unauthorized for SharePoint access")
                res2.raise_for_status()

    # Fallback via Bot token
    file_url = attachment.content_url
    if not file_url:
        raise RuntimeError("No contentUrl available")
    bot_token = await _get_bot_access_token_direct()
    headers = {"Authorization": f"Bearer {bot_token}", "Accept": "application/octet-stream"}
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(file_url, headers=headers) as res:
            res.raise_for_status()
            return await res.read()

def _is_valid_file_attachment(att) -> bool:
    try:
        if getattr(att, "content_url", None):
            return True
        ct = (att.content_type or "").lower()
        content = att.content if isinstance(att.content, dict) else {}

        if ct == "application/vnd.microsoft.teams.file.download.info":
            du = content.get("downloadUrl") or content.get("downloadurl") or content.get("download_url")
            return bool(du)

        if ct in (
            "application/vnd.microsoft.card.o365connector",
            "application/vnd.microsoft.teams.card.file.consent",
            "application/vnd.microsoft.card.file",
        ):
            du = (
                content.get("downloadUrl") or content.get("downloadurl") or
                (content.get("content", {}) if isinstance(content.get("content"), dict) else {}).get("downloadUrl")
            )
            return bool(du)
    except Exception:
        pass
    return False

# ============= Language helpers + UI Cards =============
LANG_MAP: Dict[str, str] = {
    "id": "Indonesian", "en": "English", "ja": "Japanese", "vi": "Vietnamese",
    "lo": "Lao", 
    "zh-hans": "Chinese (Simplified)", "zh-hant": "Chinese (Traditional)",
    "ko": "Korean", "fr": "French", "de": "German", "es": "Spanish",
    "th": "Thai", "ru": "Russian",
    "fil": "Filipino",
    "pt": "Portuguese",
}

def _is_valid_lang(code: str) -> bool:
    return (code or "").lower() in LANG_MAP or (code or "").lower() in ("zh-hans","zh-hant","auto")

def _normalize_tgt(t: str) -> str:
    t = (t or "").lower()
    return "zh-hans" if t == "zh" else t

def menu_card() -> Attachment:
    body = HeroCard(
        title="AI Docs Translator",
        subtitle="Translate documents",
        text="Attach a file to translate, or choose an option below.",
        buttons=[
            CardAction(type=ActionTypes.im_back, title="📄 Translate document", value="translate"),
            # CardAction(type=ActionTypes.im_back, title="💬 Chat with AI", value="chat"),
            CardAction(type=ActionTypes.im_back, title="📘 How to upload", value="howto"),
        ],
    )
    return CardFactory.hero_card(body)

HOWTO_TEXT = (
    "**How to translate a document**\n"
    "1) Click **Translate document**.\n"
    "2) Pick **target language** and press **Start**.\n"
    "3) Attach your PDF/DOCX/PPTX/XLSX (same chat).\n"
    "4) Sign in. Result saved to OneDrive › Translated."
)

def translate_form_card(default_tgt: str = "en") -> Attachment:
    choices = [{"title": f"{LANG_MAP[c]} ({c})", "value": c} for c in
               ["id","en","ja","vi","lo","zh-hans","zh-hant", "ko","fr","de","es","th","ru","fil","pt"]]
    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "weight": "Bolder", "size": "Medium", "text": "Translate document"},
            {"type": "TextBlock", "text": "Source language", "spacing": "Small"},
            {"type": "TextBlock", "text": "Auto-detect", "isSubtle": True},
            {"type": "TextBlock", "text": "Target language", "spacing": "Medium"},
            {"type": "Input.ChoiceSet", "id": "target", "value": default_tgt, "choices": choices},
            {"type": "TextBlock", "text": "Tip: attach your PDF/DOCX/PPTX/XLSX in the same message.", "isSubtle": True, "wrap": True}
        ],
        "actions": [{"type": "Action.Submit", "title": "Start", "data": {"type": "start-translate"}}]
    }
    return Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card)

def _friendly_error(detail: Any) -> str:
    msg = "Translation failed. Please try again."
    try:
        d = json.loads(detail) if isinstance(detail, str) else (detail or {})
    except Exception:
        d = {}
    # Special case
    raw = (detail or "")
    if isinstance(raw, str) and "already exists" in raw.lower():
        return "A file with the same name already exists. I will use a unique name and try again."
    err = (d.get("error") or {}) if isinstance(d, dict) else {}
    emsg = (err.get("message") or (err.get("innerError") or {}).get("message")) if isinstance(err, dict) else None
    if emsg:
        emsg = emsg.strip()
        return (emsg[:177] + "...") if len(emsg) > 180 else emsg
    return msg


async def ensure_valid_download_url(user_token: str, job_id: str, od_url: Optional[str] = None, max_retries: int = 3):
    """
    Ensure we get a valid direct download URL that actually points to a PPTX file.
    
    Args:
        user_token: Microsoft Graph API token
        job_id: Job ID to get links from backend
        od_url: Optional OneDrive URL if already known
        max_retries: Maximum number of retry attempts
    
    Returns:
        Tuple of (download_url, onedrive_url)
    """
    logger = logging.getLogger(__name__)
    
    for attempt in range(max_retries):
        try:
            # First try to get URLs from job
            dl, od = await _get_job_links(job_id)
            
            # Use provided od_url if we don't have one
            if not od and od_url:
                od = od_url
            
            # Validate if download URL is actually a PPTX
            if dl:
                logger.info(f"Checking download URL validity (attempt {attempt + 1})")
                if await looks_like_pptx(dl):
                    logger.info("Download URL validated as PPTX")
                    return dl, od
                else:
                    logger.warning("Download URL does not point to valid PPTX")
                    dl = None  # Reset invalid URL
            
            # If no valid download URL, try to get from Graph API using OneDrive URL
            if od and not dl:
                logger.info("Attempting to get download URL from Graph API")
                try:
                    fresh_dl, fresh_od, size = await graph_item_min(user_token, od)
                    if fresh_dl:
                        # Validate the fresh URL
                        if await looks_like_pptx(fresh_dl, size):
                            logger.info("Graph API download URL validated")
                            return fresh_dl, fresh_od or od
                        else:
                            logger.warning("Graph API URL does not point to valid PPTX")
                except Exception as e:
                    logger.error(f"Graph API call failed: {e}")
            
            # If still no valid URL and we have retries left, wait before retry
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                logger.info(f"Waiting {wait_time} seconds before retry")
                await asyncio.sleep(wait_time)
        
        except Exception as e:
            logger.error(f"Error in ensure_valid_download_url attempt {attempt + 1}: {e}")
            if attempt == max_retries - 1:
                break
    
    # Failed to get valid download URL after all retries
    logger.warning("Failed to obtain valid download URL after all retries")
    return None, od

# def _result_card(download_url: Optional[str], onedrive_url: Optional[str]) -> Attachment:
#     actions = []
#     if download_url: actions.append({"type":"Action.OpenUrl","title":"⬇️ Download","url": download_url})
#     if onedrive_url: actions.append({"type":"Action.OpenUrl","title":"☁️ Open in OneDrive","url": onedrive_url})
#     ac = {"type":"AdaptiveCard","version":"1.4",
#           "body":[{"type":"TextBlock","weight":"Bolder","size":"Medium","text":"Translation completed"}],
#           "actions": actions or [{"type":"Action.Submit","title":"Back to menu","data":{"type":"dismiss"}}]}
#     return Attachment(content_type="application/vnd.microsoft.card.adaptive", content=ac)

# def _retry_or_dismiss_card(src: str, tgt: str, filename: str) -> Attachment:
#     actions = [
#         {"type": "Action.Submit", "title": "🔁 Start again", "data": {"type": "start-translate", "target": tgt}},
#         {"type": "Action.Submit", "title": "🧹 Dismiss", "data": {"type": "dismiss"}}
#     ]
#     ac = {
#         "type": "AdaptiveCard", "version": "1.4",
#         "body": [
#             {"type":"TextBlock","weight":"Bolder","size":"Medium","text":"What would you like to do?"},
#             {"type":"TextBlock","isSubtle":True,"wrap":True,
#              "text": f"Last file: **{filename}**  \nSource: **{src}** → Target: **{tgt}**"}
#         ],
#         "actions": actions
#     }
#     return Attachment(content_type="application/vnd.microsoft.card.adaptive", content=ac)

def _result_card(download_url: Optional[str], onedrive_url: Optional[str], has_valid_download: bool = True) -> Attachment:
    """
    Create result card with download and OneDrive view buttons.
    
    Args:
        download_url: Direct download URL (should be @microsoft.graph.downloadUrl)
        onedrive_url: OneDrive web URL for viewing
        has_valid_download: Whether we have a valid download URL
    
    Returns:
        Adaptive card attachment
    """
    actions = []
    body_text = "Translation completed"
    
    # Only add download button if we have a valid download URL
    # if download_url and has_valid_download:
        # actions.append({
        #     "type": "Action.OpenUrl",
        #     "title": "⬇️ Download",
        #     "url": download_url
        # })
    if download_url and not has_valid_download:
        # We have a URL but it's not validated, add with warning
        body_text = "Translation completed ⚠️"
        # actions.append({
        #     "type": "Action.OpenUrl",
        #     "title": "⬇️ Try Download",
        #     "url": download_url
        # })
    
    # OneDrive URL is for viewing, not downloading
    if onedrive_url:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "☁️ View in OneDrive",
            "url": onedrive_url
        })
        
        # If no valid download URL, instruct user to download from OneDrive
        if not download_url or not has_valid_download:
            body_text = "Translation completed. Please download from OneDrive."
    
    # If no actions available, just show dismiss
    if not actions:
        body_text = "Translation completed but no download link available."
        actions = [{
            "type": "Action.Submit",
            "title": "Back to menu",
            "data": {"type": "dismiss"}
        }]
    
    ac = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "weight": "Bolder",
                "size": "Medium",
                "text": body_text
            }
        ],
        "actions": actions
    }
    
    return Attachment(content_type="application/vnd.microsoft.card.adaptive", content=ac)


# ============= AOAI helpers (optional) =============
def _ensure_memory(state: Optional[dict]) -> dict:
    if state is None: state = {}
    state.setdefault("summary",""); state.setdefault("messages",[])
    state.setdefault("pending_lang_by_user", {})
    return state

# # -------------------- ADDED: company knowledge for consistency --------------------
# SBCS_KNOWLEDGE = """
# PT SBCS Indonesia (SBCSI) is a member of the SMBC Group (est. 2012) providing research, consulting, and business matching services for companies expanding in or from Indonesia. Official site: https://sbcs.co.id/
# """.strip()
# # ----------------------------------------------------------------------------------

# async def _aoai_chat(client, messages, temperature=0.3):
#     resp = client.chat.completions.create(
#         model=AZURE_OPENAI_DEPLOYMENT,
#         messages=messages,
#         temperature=temperature,
#     )
#     return resp.choices[0].message.content

# async def _summarize_if_needed(client, mem: dict):
#     msgs = mem.get("messages", [])
#     if not client or len(msgs) < CHAT_SUMMARIZE_AFTER: return
#     convo = ""
#     for m in msgs:
#         tag = "User" if m.get("r")=="user" else "Assistant"
#         convo += f"{tag}: {m.get('c','')}\n"
#     system = "Summarize the following chat (<250 tokens)."
#     summ = await _aoai_chat(client, [{"role":"system","content":system},
#                                      {"role":"user","content":convo}], temperature=0.2)
#     mem["summary"] = (mem.get("summary","").strip() + ("\n" if mem.get("summary") else "") + (summ or "").strip()).strip()
#     mem["messages"] = msgs[-6:]

# ============= Backend submit & polling =============
async def _post_upload_create(bearer: str, filename: str, content_type: str, data: bytes, src: str, tgt: str, user_id: str) -> str:
    url = f"{TRANSLATOR_API}{UPLOAD_CREATE_PATH}"
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type=content_type)
    form.add_field("source_lang", str(src or "auto"))
    form.add_field("target_lang", str(tgt or "en"))
    form.add_field("user_id", str(user_id or "unknown"))
    # hint optional – backend boleh abaikan
    form.add_field("dedupe", "true")
    form.add_field("overwrite", "false")

    headers: Dict[str, str] = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    timeout = aiohttp.ClientTimeout(total=600)
    t0 = time.perf_counter()
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(url, data=form, headers=headers) as r:
            txt = await r.text()
            dur = round((time.perf_counter()-t0)*1000,2)
            if r.status != 200:
                logger.error("backend.upload_create.failed", extra={"status": r.status, "duration_ms": dur, "body_head": txt[:300]})
                raise RuntimeError(f"upload/create failed: {r.status} {txt}")
            js = json.loads(txt)
            job_ids = js.get("job_ids") or []
            logger.info("backend.upload_create.ok", extra={
                "status": r.status, "duration_ms": dur, "job_ids_len": len(job_ids),
                "bytes": len(data), "file_name": filename, "src": src, "tgt": tgt
            })
            if not job_ids:
                raise RuntimeError(f"upload/create: no job_ids in response: {txt}")
            return job_ids[0]

async def wait_job_until_done(job_id: str, max_wait_sec: int) -> dict:
    deadline=None; hard_cap=7200; loop=asyncio.get_event_loop()
    if max_wait_sec>0: deadline = loop.time()+max_wait_sec
    started = loop.time(); delay=2.0
    async with aiohttp.ClientSession() as sess:
        while True:
            async with sess.get(f"{TRANSLATOR_API}{JOB_DETAIL_PATH}/{job_id}") as r:
                r.raise_for_status()
                js = await r.json()
            st = (js.get("status") or "").lower()
            if st in ("succeeded","failed"):
                return js
            now = loop.time()
            if (deadline and now>=deadline) or (now-started>=hard_cap):
                js["status"]="timeout"; return js
            jitter = 0.75 * (os.urandom(1)[0]/255.0)
            await asyncio.sleep(delay + jitter); delay = min(10.0, delay*1.6)

async def _get_job_links(job_id: str) -> Tuple[Optional[str], Optional[str]]:
    url = f"{TRANSLATOR_API}{JOB_DETAIL_PATH}/{job_id}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as r:
            if r.status != 200:
                return None, None
            js = await r.json()
    detail_raw = js.get("detail") or {}
    if isinstance(detail_raw, str):
        try: detail = json.loads(detail_raw)
        except: detail = {}
    else:
        detail = detail_raw
    download_url = js.get("download_url") or detail.get("download_url") or js.get("result_url") or detail.get("result_url") or ""
    onedrive_url = js.get("onedrive_url") or detail.get("onedrive_url") or ""
    return (download_url or None, onedrive_url or None)

# ============= Misc helpers =============
mimetypes.init()

def _preserve_filename(name: str) -> str:
    if not name: return "document"
    name = name.replace("/", "_").replace("\\", "_")
    return "".join(ch for ch in name if ord(ch) >= 32) or "document"

def _guess_content_type(filename: str, fallback: str = "application/octet-stream") -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or fallback

# ============= BOT IMPLEMENTATION =============
class SBCSBot(TeamsActivityHandler):
    def __init__(self, conversation_state: ConversationState, user_state: UserState, gate: ActivityGate):
        self.conversation_state = conversation_state
        self.user_state = user_state
        self.dialog_state = self.conversation_state.create_property("DialogState")
        self.dialogs = DialogSet(self.dialog_state)
        self.memory_state = self.conversation_state.create_property("ChatMemory")
        self.user_prefs = self.user_state.create_property("UserPrefs")
        self._gate = gate

        self.dialogs.add(OAuthPrompt("OAuthPrompt", OAuthPromptSettings(
            connection_name=OAUTH_CONNECTION, text="Sign in to Microsoft 365 to continue.",
            title="Sign In", timeout=300000)))
        self.dialogs.add(TextPrompt("TextPrompt"))
        # self.dialogs.add(WaterfallDialog("ChatDialog", [self._chat_prompt_step]))
        self.dialogs.add(WaterfallDialog("TranslateDialog", [
            self._translate_ask_lang_step, self._translate_login_step, self._translate_submit_and_wait_step
        ]))
        self.dialogs.add(WaterfallDialog("RootDialog", [self._route_step]))

    async def _load_mem(self, context) -> dict:
        return _ensure_memory(await self.memory_state.get(context, {}))
    async def _save_mem(self, context, mem: dict):
        await self.memory_state.set(context, mem)
        await self.conversation_state.save_changes(context)

    async def _get_user_prefs(self, context) -> dict:
        return await self.user_prefs.get(context, {"last_target":"en"})
    async def _set_user_prefs(self, context, prefs: dict):
        await self.user_prefs.set(context, prefs)
        await self.user_state.save_changes(context)

    async def on_turn(self, turn_context: TurnContext):
        # Idempotency gate (hindari duplicate reply)
        act = turn_context.activity
        ch  = (act.channel_id or "unknown")
        cid = getattr(act.conversation, "id", "") or "unknown"
        aid = act.id or f"noid-{int(time.time()*1000)}"
        if not await self._gate.first_time(ch, cid, aid):
            return

        # Handle action submit
        if act.type == ActivityTypes.message and isinstance(act.value, dict):
            val = act.value
            if (val.get("type") or "").lower() == "start-translate":
                tgt = (val.get("target") or "en").lower()
                if not _is_valid_lang(tgt): tgt = "en"
                tgt = _normalize_tgt(tgt)
                mem = await self._load_mem(turn_context)
                acct = getattr(act, "from_property", None)
                uid = (getattr(acct, "aad_object_id", None) or getattr(acct, "id", None) or getattr(acct, "name", None) or "unknown")
                mem.setdefault("pending_lang_by_user", {})[uid] = {"src":"auto","tgt":tgt,"ts":time.time()}
                await self._save_mem(turn_context, mem)
                prefs = await self._get_user_prefs(turn_context)
                prefs["last_target"] = tgt
                await self._set_user_prefs(turn_context, prefs)
                await _safe_send(turn_context, f"✅ Target language set to **{LANG_MAP.get(tgt, tgt)} ({tgt})**. Now attach your file here.")
                if any(_is_valid_file_attachment(a) for a in (act.attachments or [])):
                    dialog_ctx = await self.dialogs.create_context(turn_context)
                    await dialog_ctx.begin_dialog("TranslateDialog")
                return

            if (val.get("type") or "").lower() == "dismiss":
                await _safe_send(turn_context, MessageFactory.attachment(menu_card()))
                return

        dialog_ctx = await self.dialogs.create_context(turn_context)

        if act.type == ActivityTypes.message:
            atts = act.attachments or []
            if any(_is_valid_file_attachment(a) for a in atts):
                acct = getattr(act, "from_property", None)
                uid = (getattr(acct, "aad_object_id", None) or getattr(acct, "id", None) or getattr(acct, "name", None) or "unknown")
                mem = await self._load_mem(turn_context)
                pend = (mem.get("pending_lang_by_user") or {}).get(uid)

                if STRICT_TARGET_FROM_START:
                    if not (pend and (time.time() - float(pend.get("ts", 0)) < PENDING_TTL_SEC)):
                        await _safe_send(turn_context, "Please choose the target language first (click Start), then upload the file")
                        last = (await self._get_user_prefs(turn_context)).get("last_target","en")
                        await _safe_send(turn_context, MessageFactory.attachment(translate_form_card(last)))
                        return

                if dialog_ctx.active_dialog is not None:
                    await dialog_ctx.cancel_all_dialogs()
                await dialog_ctx.begin_dialog("TranslateDialog")
                await self.conversation_state.save_changes(turn_context)
                await self.user_state.save_changes(turn_context)
                return

            result = await dialog_ctx.continue_dialog()
            if result.status == DialogTurnStatus.Empty:
                await dialog_ctx.begin_dialog("RootDialog")
        else:
            await dialog_ctx.continue_dialog()

        await self.conversation_state.save_changes(turn_context)
        await self.user_state.save_changes(turn_context)

    async def on_members_added_activity(self, members_added: List[ChannelAccount], turn_context: TurnContext):
        for m in members_added:
            if m.id != turn_context.activity.recipient.id:
                await _safe_send(turn_context, MessageFactory.attachment(menu_card()))

    async def on_teams_signin_verify_state(self, turn_context: TurnContext):
        dc = await self.dialogs.create_context(turn_context); await dc.continue_dialog()
    async def on_teams_signin_token_exchange(self, turn_context: TurnContext):
        dc = await self.dialogs.create_context(turn_context); await dc.continue_dialog()

    async def _route_step(self, step: WaterfallStepContext):
        text = (step.context.activity.text or "").strip(); low = text.lower()

        # -------------------- ADDED: deterministic fixed-address reply --------------------
        # if ADDR_RE.search(low):
        #     await _safe_send(step.context, ADDRESS_FIXED)
        #     return await step.end_dialog()
        # ----------------------------------------------------------------------------------

        if low in ("translate","terjemah","terjemahkan","terjemahan"):
            prefs = await self._get_user_prefs(step.context)
            await _safe_send(step.context, MessageFactory.attachment(translate_form_card(prefs.get("last_target","en"))))
            return await step.end_dialog()

        if low in ("menu","help","hi","hello",""):
            await _safe_send(step.context, MessageFactory.attachment(menu_card()))
            return await step.end_dialog()
        if low.startswith("howto"):
            await _safe_send(step.context, HOWTO_TEXT); return await step.end_dialog()
            
        # if low.startswith("chat"):
        #     return await step.begin_dialog("ChatDialog")

        # # -------------------- CHANGED: don't send SBCS_BIO_EN raw to user --------------------
        # if _is_who_are_you_query(text):
        #     step.values["prefill"] = text
        #     return await step.begin_dialog("ChatDialog")
        # # -------------------------------------------------------------------------------------

        # step.values["prefill"] = step.context.activity.text
        # return await step.begin_dialog("ChatDialog")

    async def _chat_prompt_step(self, step: WaterfallStepContext):
        text = step.values.get("prefill") or (step.context.activity.text or "")
        return await step.next(text)

    # async def _chat_call_step(self, step: WaterfallStepContext):
    #     user_q = step.result; client = _aoai()
    #     mem = await self._load_mem(step.context); mem["messages"].append({"r":"user","c":user_q})
    #     if client:
    #         msgs=[{"role":"system","content":CHAT_SYSTEM_PROMPT}]
    #         if mem.get("summary"): msgs.append({"role":"system","content":f"Conversation summary:\n{mem['summary']}"} )
    #         # -------------------- ADDED: add company knowledge to system --------------------
    #         msgs.append({"role":"system","content":SBCS_KNOWLEDGE})
    #         # --------------------------------------------------------------------------------
    #         for m in mem["messages"][-CHAT_MAX_TURNS:]:
    #             role="user" if m.get("r")=="user" else "assistant"
    #             msgs.append({"role":role,"content":m.get("c","")})
    #         # try: ans = await _aoai_chat(client, msgs, temperature=0.3)
    #         # except Exception as e:
    #         #     logging.exception("aoai chat error"); ans = f"(temporary error) {e}"
    #     else:
    #         ans = "Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT & AZURE_OPENAI_API_KEY."
    #     mem["messages"].append({"r":"assistant","c":ans}); await _summarize_if_needed(client, mem); await self._save_mem(step.context, mem)
    #     await _safe_send(step.context, ans)
    #     await _safe_send(step.context, MessageFactory.attachment(menu_card()))
    #     return await step.end_dialog()

    async def _translate_ask_lang_step(self, step: WaterfallStepContext):
        mem = await self._load_mem(step.context)
        acct = getattr(step.context.activity, "from_property", None)
        uid = (getattr(acct, "aad_object_id", None) or getattr(acct, "id", None) or getattr(acct, "name", None) or "unknown")
        pend_map = mem.get("pending_lang_by_user") or {}
        pl = pend_map.pop(uid, None)

        if not (pl and (time.time() - float(pl.get("ts", 0)) < PENDING_TTL_SEC)):
            await self._save_mem(step.context, mem)
            await _safe_send(step.context, "No target language is set yet. Click **Start**, select the target language, then upload the file.")
            last = (await self._get_user_prefs(step.context)).get("last_target","en")
            await _safe_send(step.context, MessageFactory.attachment(translate_form_card(last)))
            return await step.end_dialog()

        src = (pl.get("src") or "auto").lower()
        tgt = (pl.get("tgt") or "").lower()

        if not _is_valid_lang(src): src = "auto"
        if not _is_valid_lang(tgt):
            await _safe_send(step.context, "Invalid target language.")
            last = (await self._get_user_prefs(step.context)).get("last_target","en")
            await _safe_send(step.context, MessageFactory.attachment(translate_form_card(last)))
            return await step.end_dialog()

        tgt = _normalize_tgt(tgt)
        await self._save_mem(step.context, mem)
        step.values["src"] = src
        step.values["tgt"] = tgt
        return await step.next(None)

    async def _translate_login_step(self, step: WaterfallStepContext):
        return await step.begin_dialog("OAuthPrompt")
    

    # async def _translate_submit_and_wait_step(self, step: WaterfallStepContext):
    #     token_response = step.result
    #     if not token_response or not token_response.token:
    #         await _safe_send(step.context, "Sign-in was cancelled. Click **Translate document** to try again.")
    #         return await step.end_dialog()

    #     raw_atts = step.context.activity.attachments or []
    #     valids = [a for a in raw_atts if _is_valid_file_attachment(a)]
    #     if not valids:
    #         await _safe_send(step.context, "I couldn't find a valid file attachment. Please attach a PDF/DOCX/PPTX/XLSX file and try again.")
    #         last = (await self._get_user_prefs(step.context)).get("last_target","en")
    #         await _safe_send(step.context, MessageFactory.attachment(translate_form_card(last)))
    #         return await step.end_dialog()

    #     src = step.values.get("src", "auto")
    #     tgt = step.values.get("tgt", None)
    #     if not _is_valid_lang(tgt or ""):
    #         await _safe_send(step.context, "Target language belum dipilih. Klik **Start** dulu.")
    #         last = (await self._get_user_prefs(step.context)).get("last_target","en")
    #         await _safe_send(step.context, MessageFactory.attachment(translate_form_card(last)))
    #         return await step.end_dialog()

    #     tgt = _normalize_tgt(tgt)

    #     att = valids[0]
    #     orig_filename = _preserve_filename(getattr(att, "name", None) or "document")
    #     base, ext = os.path.splitext(orig_filename)
    #     unique_suffix = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    #     filename = f"{base}__{unique_suffix}{ext}"
    #     raw_ct = (att.content_type or "").lower()
    #     content_type = _guess_content_type(filename, "application/octet-stream") if raw_ct in ("application/octet-stream", "", None) else raw_ct

    #     lang_name = LANG_MAP.get(tgt, tgt).upper()
    #     await _safe_send(step.context, f"📄 Translating **{filename}** to **{lang_name} ({tgt})**...")

    #     user_token = token_response.token

    #     data = None
    #     for attempt in range(2):
    #         try:
    #             if attempt > 0:
    #                 fresh = await _get_fresh_user_token(step.context, OAUTH_CONNECTION)
    #                 if fresh:
    #                     user_token = fresh
    #                     await _safe_send(step.context, "🔄 Retrying with a refreshed token...")
    #             data = await _download_teams_file(step.context, att, user_token)
    #             break
    #         except Exception as e:
    #             if attempt == 1:
    #                 await _safe_send(step.context, f"❌ Failed to download file: {e}")
    #                 return await step.end_dialog()
    #             await _safe_send(step.context, f"⚠️ Download failed ({e}). Retrying...")

    #     if not data:
    #         await _safe_send(step.context, "❌ Failed to download the file.")
    #         return await step.end_dialog()

    #     MAX_SIZE = 40 * 1024 * 1024
    #     if len(data) > MAX_SIZE:
    #         await _safe_send(step.context, f"❌ File is too large ({len(data)/1024/1024:.1f} MB). Maximum is 40 MB.")
    #         return await step.end_dialog()

    #     await _safe_send(step.context, f"✅ File downloaded ({len(data)/1024/1024:.1f} MB)")

    #     acct = getattr(step.context.activity, "from_property", None)
    #     user_id = (getattr(acct, "aad_object_id", None) or getattr(acct, "id", None) or getattr(acct, "name", None) or "unknown")

    #     try:
    #         job_id = await _post_upload_create(user_token, filename, content_type, data, src, tgt, str(user_id))
    #     except Exception as e:
    #         await _safe_send(step.context, f"❌ Failed to submit to translator: {e}")
    #         return await step.end_dialog()

    #     result = await wait_job_until_done(job_id, BOT_MAX_POLL_SEC)
    #     status = (result.get("status") or "").lower()

    #     if status == "succeeded":
    #         dl, od = await _get_job_links(job_id)
    #         dl, od = await ensure_graph_download(user_token, dl, od)
    #         if not dl and od:
    #             from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    #             p = urlparse(od); q = dict(parse_qsl(p.query)); q["download"] = "1"
    #             od = urlunparse(p._replace(query=urlencode(q)))
    #         await _safe_send(step.context, MessageFactory.attachment(_result_card(dl, od)))
    #         await _safe_send(step.context, f"🎉 Translation to **{lang_name}** succeeded! Your file is saved to your OneDrive (if configured).")
    #     elif status in ("timeout", "failed"):
    #         raw_detail = result.get("detail")
    #         friendly = "⏰ The request timed out. Please try again." if status == "timeout" else _friendly_error(raw_detail)
    #         await _safe_send(step.context, friendly)
    #         await _safe_send(step.context, MessageFactory.attachment(_retry_or_dismiss_card(src, tgt, filename)))
    #     else:
    #         await _safe_send(step.context, f"❓ Unknown status: {status}")

    #     await _safe_send(step.context, MessageFactory.attachment(menu_card()))
    #     return await step.end_dialog()

    async def _translate_submit_and_wait_step(self, step: WaterfallStepContext):
        """
        Submit translation job and wait for completion, then provide download links.
        """
        token_response = step.result
        if not token_response or not token_response.token:
            await _safe_send(step.context, "Sign-in was cancelled. Click **Translate document** to try again.")
            return await step.end_dialog()

        raw_atts = step.context.activity.attachments or []
        valids = [a for a in raw_atts if _is_valid_file_attachment(a)]
        if not valids:
            await _safe_send(step.context, "I couldn't find a valid file attachment. Please attach a PDF/DOCX/PPTX/XLSX file and try again.")
            last = (await self._get_user_prefs(step.context)).get("last_target", "en")
            await _safe_send(step.context, MessageFactory.attachment(translate_form_card(last)))
            return await step.end_dialog()

        src = step.values.get("src", "auto")
        tgt = step.values.get("tgt", None)
        if not _is_valid_lang(tgt or ""):
            await _safe_send(step.context, "No target language selected. Please click **Start** to begin.")
            last = (await self._get_user_prefs(step.context)).get("last_target", "en")
            await _safe_send(step.context, MessageFactory.attachment(translate_form_card(last)))
            return await step.end_dialog()

        tgt = _normalize_tgt(tgt)

        att = valids[0]
        orig_filename = _preserve_filename(getattr(att, "name", None) or "document")
        base, ext = os.path.splitext(orig_filename)
        unique_suffix = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        filename = f"{base}__{unique_suffix}{ext}"
        raw_ct = (att.content_type or "").lower()
        content_type = _guess_content_type(filename, "application/octet-stream") if raw_ct in ("application/octet-stream", "", None) else raw_ct

        lang_name = LANG_MAP.get(tgt, tgt).upper()
        await _safe_send(step.context, f"📄 Translating **{filename}** to **{lang_name} ({tgt})**...")

        user_token = token_response.token

        # Download file from Teams with retry
        data = None
        for attempt in range(2):
            try:
                if attempt > 0:
                    fresh = await _get_fresh_user_token(step.context, OAUTH_CONNECTION)
                    if fresh:
                        user_token = fresh
                        await _safe_send(step.context, "🔄 Retrying with a refreshed token...")
                data = await _download_teams_file(step.context, att, user_token)
                break
            except Exception as e:
                if attempt == 1:
                    await _safe_send(step.context, f"❌ Failed to download file: {e}")
                    return await step.end_dialog()
                await _safe_send(step.context, f"⚠️ Download failed ({e}). Retrying...")

        if not data:
            await _safe_send(step.context, "❌ Failed to download the file.")
            return await step.end_dialog()

        MAX_SIZE = 40 * 1024 * 1024
        if len(data) > MAX_SIZE:
            await _safe_send(step.context, f"❌ File is too large ({len(data)/1024/1024:.1f} MB). Maximum is 40 MB.")
            return await step.end_dialog()

        await _safe_send(step.context, f"✅ File downloaded ({len(data)/1024/1024:.1f} MB)")

        acct = getattr(step.context.activity, "from_property", None)
        user_id = (getattr(acct, "aad_object_id", None) or getattr(acct, "id", None) or getattr(acct, "name", None) or "unknown")

        # Submit to translator
        try:
            job_id = await _post_upload_create(user_token, filename, content_type, data, src, tgt, str(user_id))
        except Exception as e:
            await _safe_send(step.context, f"❌ Failed to submit to translator: {e}")
            return await step.end_dialog()

        # Wait for job completion
        result = await wait_job_until_done(job_id, BOT_MAX_POLL_SEC)
        status = (result.get("status") or "").lower()

        if status == "succeeded":
            # Get valid download URLs with retry mechanism
            dl, od = await ensure_valid_download_url(user_token, job_id, None, max_retries=3)
            
            # Check if we have a validated download URL
            has_valid_download = dl is not None
            
            # Send result card with appropriate buttons
            await _safe_send(step.context, MessageFactory.attachment(_result_card(dl, od, has_valid_download)))
            
            # Send appropriate success message
            if has_valid_download:
                await _safe_send(step.context, 
                    f"🎉 Translation to **{lang_name}** succeeded! Click **View In OneDrive** to view your file.")
            elif od:
                await _safe_send(step.context, 
                    f"✅ Translation to **{lang_name}** succeeded! Please download your file from OneDrive.")
            else:
                await _safe_send(step.context, 
                    f"⚠️ Translation completed but download link is temporarily unavailable. Please check your OneDrive folder.")
                
        elif status in ("timeout", "failed"):
            raw_detail = result.get("detail")
            friendly = "⏰ The request timed out. Please try again." if status == "timeout" else _friendly_error(raw_detail)
            await _safe_send(step.context, friendly)
            # await _safe_send(step.context, MessageFactory.attachment(_retry_or_dismiss_card(src, tgt, filename)))
        else:
            await _safe_send(step.context, f"❓ Unknown status: {status}")

        await _safe_send(step.context, MessageFactory.attachment(menu_card()))
        return await step.end_dialog()

# ============= FastAPI router & State storage =============
prod_settings = BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD)
if APP_TENANT_ID:
    try: setattr(prod_settings, "channel_auth_tenant", APP_TENANT_ID)
    except Exception: pass
adapter_prod = BotFrameworkAdapter(prod_settings)
adapter_dev  = BotFrameworkAdapter(BotFrameworkAdapterSettings(None, None))

# Inisiasi storage
if _USE_BLOB:
    try:
        storage: Storage = BlobStorageLite(_AZ_CONN, _AZ_CONT, prefix=_BLOB_PREFIX)
        # Kick container ensure log
        asyncio.get_event_loop().run_until_complete(storage.read([]))  # no-op ensure
        logger.info("state.storage", extra={"type": "azure_blob", "container": _AZ_CONT})
    except Exception as e:
        logger.error("state.storage.init_failed", extra={"type": "azure_blob", "err": str(e)})
        storage = MemoryStorage()
        logger.warning("state.storage.fallback", extra={"type": "memory"})
else:
    storage = MemoryStorage()
    logger.warning("state.storage.fallback", extra={"type": "memory"})

# Activity gate (boleh tetap aktif saat _USE_BLOB True)
activity_gate = ActivityGate(_AZ_CONN if _USE_BLOB else None, _AZ_CONT if _USE_BLOB else None)

conv_state = ConversationState(storage)
user_state = UserState(storage)
bot = SBCSBot(conv_state, user_state, activity_gate)

router = APIRouter()

@router.post("/api/messages")
async def api_messages(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be application/json")

    if not isinstance(body, dict) or "type" not in body:
        raise HTTPException(status_code=400, detail="Invalid Bot Framework Activity: missing 'type'")

    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization","") or ""
    channel = (activity.channel_id or "").lower()
    has_auth = auth_header.lower().startswith("bearer ")
    use_dev = (channel=="emulator") and (not has_auth)
    adapter = adapter_dev if use_dev else adapter_prod

    corr = {
        "type": activity.type,
        "channel": channel,
        "conversation_id": getattr(activity.conversation, "id", None),
        "activity_id": getattr(activity, "id", None),
        "service_url": getattr(activity, "service_url", None),
        "from_id": getattr(getattr(activity, "from_property", None), "id", None),
        "recipient_id": getattr(getattr(activity, "recipient", None), "id", None),
        "use_dev": use_dev,
        "has_auth": has_auth,
        "path": "/api/messages",
    }
    logger.info("bot.incoming", extra=corr)

    if adapter is adapter_prod and not has_auth:
        logger.warning("bot.missing_auth_header", extra=corr)
        return JSONResponse(status_code=401, content={
            "error":"missing_auth_header",
            "detail":"Azure Bot Service must send Authorization: Bearer <JWT> to /api/messages.",
            "has_app_id": bool(APP_ID), "has_app_password": bool(APP_PASSWORD), "channel_id": activity.channel_id
        })

    try:
        invoke_response = await adapter.process_activity(activity, auth_header, bot.on_turn)
    except Exception as e:
        logger.exception("bot.adapter_error")
        msg = str(e)
        if any(t in msg.lower() for t in ["unauthorized","appid","app id","app password","jwt","token"]):
            return JSONResponse(status_code=401, content={"error":"auth_failed","detail":msg})
        raise HTTPException(status_code=500, detail=f"Bot error: {msg}")

    status = invoke_response.status if invoke_response else 201
    logger.info("bot.outgoing", extra={**corr, "status": status})
    return Response(status_code=status)

@router.get("/diag")
async def bot_diag():
    def tail(x: Optional[str]): return x[-6:] if x else None
    return {
        "has_app_id": bool(APP_ID),
        "has_app_password": bool(APP_PASSWORD),
        "app_id_tail": tail(APP_ID),
        "connection_name": OAUTH_CONNECTION,
        "tenant_tail": tail(APP_TENANT_ID),
        "scope": OAUTH_SCOPE,
        # "aoai": bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY),
        "backend_upload": f"{TRANSLATOR_API}{UPLOAD_CREATE_PATH}",
        "backend_job": f"{TRANSLATOR_API}{JOB_DETAIL_PATH}/{{id}}",
        "state_storage": "BlobStorageLite" if _USE_BLOB else "MemoryStorage",
        "strict": STRICT_TARGET_FROM_START,
        "pending_ttl_sec": PENDING_TTL_SEC
    }
