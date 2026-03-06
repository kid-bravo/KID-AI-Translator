from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os, uuid, httpx

app = FastAPI(title="KID AI Translator API")

# ====== Konfigurasi Translator (ENV) ======
# Kamu memilih ENDPOINT GLOBAL: https://api.cognitive.microsofttranslator.com/
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION   = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY      = os.getenv("TRANSLATOR_KEY")

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
            "region_set":   bool(TRANSLATOR_REGION),
            "key_set":      bool(TRANSLATOR_KEY),
        }
    }

# ====== Translate (pakai ENDPOINT GLOBAL) ======
@app.post("/translate")
async def translate(req: TranslateRequest):
    if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
        raise HTTPException(status_code=500, detail="translator_not_configured")

    # ✅ Untuk endpoint global, path yang benar:
    #    /translate?api-version=3.0
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
