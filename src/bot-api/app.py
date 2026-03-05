
from fastapi import FastAPI
from pydantic import BaseModel
import os, uuid, httpx

app = FastAPI(title="KID AI Translator API")

TRANSLATOR_ENDPOINT = os.getenv("TRANSLATOR_ENDPOINT", "").rstrip("/")
TRANSLATOR_REGION = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY = os.getenv("TRANSLATOR_KEY")

class TranslateRequest(BaseModel):
    text: str
    to: str = "en"
    from_lang: str | None = None

@app.get("/healthz")
def health():
    return {"status": "ok"}

@app.post("/translate")
async def translate(req: TranslateRequest):
    if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
        return {"error": "translator_not_configured"}

    url = f"{TRANSLATOR_ENDPOINT}/translate?api-version=3.0&to={req.to}"
    if req.from_lang:
        url += f"&from={req.from_lang}"

    headers = {
        "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,
        "Content-type": "application/json",
        "X-ClientTraceId": str(uuid.uuid4())
    }

    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, json=[{"Text": req.text}], headers=headers)
        res.raise_for_status()
        data = res.json()

    return {
        "detectedLanguage": data[0].get("detectedLanguage"),
        "translations": data[0].get("translations", [])
    }
