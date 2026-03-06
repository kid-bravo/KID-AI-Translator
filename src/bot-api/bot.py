# src/bot-api/bot.py
from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import ChannelAccount
import os, httpx, uuid

# Pakai ENV yang sama dengan API /translate (endpoint global)
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION   = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY      = os.getenv("TRANSLATOR_KEY")

class TranslatorBot(ActivityHandler):
    async def on_members_added_activity(self, members_added: list[ChannelAccount], turn_context: TurnContext):
        welcome = (
            "Halo! 👋 Saya AI Translator.\n"
            "Contoh: `id->en Selamat pagi` atau `ja->id おはようございます`.\n"
            "Kalau tidak tulis arah, default `id->en`."
        )
        await turn_context.send_activity(welcome)

    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()
        from_lang, to_lang, content = self._parse_direction(text)

        if not content:
            await turn_context.send_activity(
                "Format: `id->en Selamat pagi` atau ketik kalimat langsung (default id->en)."
            )
            return

        if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
            await turn_context.send_activity("Konfigurasi Translator belum lengkap di server.")
            return

        # Karena kita pakai ENDPOINT GLOBAL → path /translate?api-version=3.0
        url = f"{TRANSLATOR_ENDPOINT}/translate?api-version=3.0&to={to_lang}"
        if from_lang:
            url += f"&from={from_lang}"

        headers = {
            "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
            "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,  # wajib untuk global endpoint
            "Content-type": "application/json",
            "X-ClientTraceId": str(uuid.uuid4())
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=[{"Text": content}], headers=headers)
                if r.status_code >= 400:
                    await turn_context.send_activity(f"Translator error {r.status_code}: {r.text}")
                    return
                data = r.json()
            translated = data[0]["translations"][0]["text"]
            await turn_context.send_activity(translated)
        except Exception as e:
            await turn_context.send_activity(f"Gagal menerjemahkan: {e}")

    def _parse_direction(self, text: str):
        # Pola: "xx->yy kalimat". Jika tidak ada, biarkan auto-detect source (None), target=en.
        default_to = "en"
        if not text:
            return None, default_to, ""
        first = text.split(" ")[0]
        if "->" in first and len(text.split(" ")) > 1:
            a, b = first.split("->", 1)
            return a.lower(), b.lower(), " ".join(text.split(" ")[1:])
        return None, default_to, text
``
