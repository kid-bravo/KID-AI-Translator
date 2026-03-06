from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
import os, uuid, httpx

app = FastAPI(title="KID AI Translator API")

# ===========================================================
#              GUARD untuk Bot Framework (AMAN)
# -> Jika paket/credential belum siap, app tetap hidup.
# ===========================================================
BOT_IMPORT_ERROR = None
BOTBUILDER_AVAILABLE = False
try:
    from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
    from botbuilder.schema import Activity
    BOTBUILDER_AVAILABLE = True
except Exception as e:
    BOT_IMPORT_ERROR = str(e)
    BOTBUILDER_AVAILABLE = False

# ABSOLUTE import untuk logic bot (src/bot-api/bot.py)
try:
    from bot import TranslatorBot
except Exception as e:
    TranslatorBot = None
    if BOT_IMPORT_ERROR is None:
        BOT_IMPORT_ERROR = f"import bot module failed: {e}"

MICROSOFT_APP_ID = os.getenv("MicrosoftAppId")
MICROSOFT_APP_PASSWORD = os.getenv("MicrosoftAppPassword")

adapter = None
bot = TranslatorBot() if TranslatorBot else None

def try_create_adapter():
    """Buat adapter hanya jika paket botbuilder ada dan kredensial tersedia."""
    if not BOTBUILDER_AVAILABLE:
        return None
    app_id = os.getenv("MicrosoftAppId")
    app_pw = os.getenv("MicrosoftAppPassword")
    if not app_id or not app_pw:
        return None
    settings = BotFrameworkAdapterSettings(app_id=app_id, app_password=app_pw)
    return BotFrameworkAdapter(settings)

adapter = try_create_adapter()

# ===========================================================
#                 KONFIGURASI TRANSLATOR (ENV)
# ===========================================================
# ENDPOINT GLOBAL: https://api.cognitive.microsofttranslator.com/
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION   = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY      = os.getenv("TRANSLATOR_KEY")

# ===========================================================
#                      MODEL REQUEST
# ===========================================================
class TranslateRequest(BaseModel):
    text: str
    to: str = "en"
    from_lang: str | None = None

# ===========================================================
#                         HEALTH
# ===========================================================
@app.get("/healthz")
def health():
    return {
        "status": "ok",
        "translator_cfg": {
            "endpoint_set": bool(TRANSLATOR_ENDPOINT),
            "region_set":   bool(TRANSLATOR_REGION),
            "key_set":      bool(TRANSLATOR_KEY),
        },
        "bot_cfg": {
            "app_id_set":           bool(MICROSOFT_APP_ID),
            "app_password_set":     bool(MICROSOFT_APP_PASSWORD),
            "botbuilder_available": BOTBUILDER_AVAILABLE,
            "adapter_ready":        adapter is not None,
            "bot_import_error":     BOT_IMPORT_ERROR if not BOTBUILDER_AVAILABLE else None
        }
    }

# ===========================================================
#                        TRANSLATE
# ===========================================================
@app.post("/translate")
async def translate(req: TranslateRequest):
    if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
        raise HTTPException(status_code=500, detail="translator_not_configured")

    # ✅ Karena pakai endpoint global → path /translate?api-version=3.0
    url = f"{TRANSLATOR_ENDPOINT}/translate?api-version=3.0&to={req.to}"
    if req.from_lang:
        url += f"&from={req.from_lang}"

    headers = {
        "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,  # wajib untuk endpoint global
        "Content-type": "application/json",
        "X-ClientTraceId": str(uuid.uuid4())
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=[{"Text": req.text}], headers=headers)

            # Tampilkan detail error asli jika Translator balas 4xx/5xx
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail={
                    "message": "translator_error",
                    "status_code": r.status_code,
                    "body": r.text
                })

            data = r.json()

    except httpx.RequestError as e:
        # Jaringan / timeout menuju Translator
        raise HTTPException(status_code=502, detail=f"translator_unreachable: {e}")

    return {
        "detectedLanguage": data[0].get("detectedLanguage"),
        "translations":     data[0].get("translations", [])
    }

# ===========================================================
#                    BOT FRAMEWORK ENDPOINT
#  Aman: untuk tes manual tanpa token → balas 401 (bukan 500)
# ===========================================================
@app.post("/api/messages")
async def messages(request: Request):
    # 1) Pastikan adapter & bot siap
    if not adapter or not bot:
        raise HTTPException(status_code=503, detail={
            "error": "bot_unavailable",
            "botbuilder_available": BOTBUILDER_AVAILABLE,
            "bot_import_error": BOT_IMPORT_ERROR,
            "hint": "Pastikan botbuilder-core terpasang dan App Settings MicrosoftAppId/MicrosoftAppPassword terisi, lalu restart."
        })

    # 2) Jika tidak ada token Bearer dari Azure Bot Service → 401 (tes manual)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return Response(status_code=401)

    # 3) Validasi body minimal supaya tidak 500 saat payload tidak lengkap
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    try:
        activity = Activity().deserialize(body)
    except Exception:
        return Response(status_code=400)

    # 4) Proses turn bot (dengan token Bearer dari Bot Service)
    try:
        async def aux_turn(tc):
            await bot.on_turn(tc)

        await adapter.process_activity(activity, auth_header, aux_turn)
        return Response(status_code=201)
    except Exception as e:
        print(f"/api/messages error: {e}")  # terlihat di Log stream
        return Response(status_code=500)
