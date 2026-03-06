from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
import os, uuid, httpx

app = FastAPI(title="KID AI Translator API")

# ---------- GUARD: jangan biarkan import Bot Framework mematikan app ----------
BOT_IMPORT_ERROR = None
BOTBUILDER_AVAILABLE = False
try:
    from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
    from botbuilder.schema import Activity
    BOTBUILDER_AVAILABLE = True
except Exception as e:
    BOT_IMPORT_ERROR = str(e)
    BOTBUILDER_AVAILABLE = False

# ABSOLUTE import lebih aman di App Service (pastikan file src/bot-api/bot.py ada)
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
    if not BOTBUILDER_AVAILABLE:
        return None
    app_id = os.getenv("MicrosoftAppId")
    app_pw = os.getenv("MicrosoftAppPassword")
    if not app_id or not app_pw:
        return None
    settings = BotFrameworkAdapterSettings(app_id=app_id, app_password=app_pw)
    return BotFrameworkAdapter(settings)

adapter = try_create_adapter()
# ------------------------------------------------------------------------------

# ====== Konfigurasi Translator (env) ======
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY = os.getenv("TRANSLATOR_KEY")

# ====== Model request ======
class TranslateRequest(BaseModel):
    text: str
    to: str = "en"
    from_lang: str | None = None

# ====== Health ======
@app.get("/healthz")
def health():
    return {
        "status": "ok",
        "translator_cfg": {
            "endpoint_set": bool(TRANSLATOR_ENDPOINT),
            "region_set": bool(TRANSLATOR_REGION),
            "key_set": bool(TRANSLATOR_KEY),
        },
        "bot_cfg": {
            "app_id_set": bool(MICROSOFT_APP_ID),
            "app_password_set": bool(MICROSOFT_APP_PASSWORD),
            "botbuilder_available": BOTBUILDER_AVAILABLE,
            "adapter_ready": adapter is not None,
            "bot_import_error": BOT_IMPORT_ERROR if not BOTBUILDER_AVAILABLE else None
        }
    }

# ====== Translate ======
@app.post("/translate")
async def translate(req: TranslateRequest):
    if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
        raise HTTPException(status_code=500, detail="translator_not_configured")

    # ✅ PATH BENAR untuk Translator v3 pada custom domain
    url = f"{TRANSLATOR_ENDPOINT}/translator/text/v3.0/translate?api-version=3.0&to={req.to}"
    if req.from_lang:
        url += f"&from={req.from_lang}"

    headers = {
        "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,
        "Content-type": "application/json",
        "X-ClientTraceId": str(uuid.uuid4())
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=[{"Text": req.text}], headers=headers)

            # ⬇️ Kunci: kirim status_code & body asli kalau gagal
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail={
                    "message": "translator_error",
                    "status_code": r.status_code,
                    "body": r.text
                })

            data = r.json()
            return {
                "detectedLanguage": data[0].get("detectedLanguage"),
                "translations": data[0].get("translations", [])
            }

    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"translator_unreachable: {e}")

# ====== Endpoint Bot Framework ======
@app.post("/api/messages")
async def messages(request: Request):
    if not adapter or not bot:
        # Jangan crash—balas 503 dengan alasan yang jelas
        raise HTTPException(status_code=503, detail={
            "error": "bot_unavailable",
            "botbuilder_available": BOTBUILDER_AVAILABLE,
            "bot_import_error": BOT_IMPORT_ERROR,
            "hint": "Cek App Settings MicrosoftAppId/MicrosoftAppPassword & pastikan paket botbuilder-core terpasang."
        })

    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    async def aux_turn(tc):
        await bot.on_turn(tc)

    await adapter.process_activity(activity, auth_header, aux_turn)
    return Response(status_code=201)
