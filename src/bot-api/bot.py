from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import ChannelAccount
import os, httpx, uuid

# Menggunakan endpoint global (api.cognitive.microsofttranslator.com)
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY = os.getenv("TRANSLATOR_KEY")


class TranslatorBot(ActivityHandler):
    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        welcome = (
            "Halo! 👋 Saya AI Translator.\n"
            "Contoh: `id->en Selamat pagi` atau `ja->id おはようございます`\n"
            "Jika tidak menulis arah, default `id->en`."
        )
        await turn_context.send_activity(welcome)

    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()
        from_lang, to_lang, content = self._parse_direction(text)

        if not content:
            await turn_context.send_activity(
                "Format: `id->en Selamat pagi` atau ketik kalimat (default id->en)."
            )
            return

        if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
            await turn_context.send_activity("Translator belum dikonfigurasi.")
            return

        # Endpoint GLOBAL → path harus /translate?api-version=3.0
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
                res = await client.post(url, json=[{"Text": content}], headers=headers)
                if res.status_code >= 400:
                    await turn_context.send_activity(
                        f"Error Translator {res.status_code}: {res.text}"
                    )
                    return
                data = res.json()

            translated = data[0]["translations"][0]["text"]
            await turn_context.send_activity(translated)

        except Exception as e:
            await turn_context.send_activity(f"Gagal menerjemahkan: {e}")

    def _parse_direction(self, text: str):
        # Pola: "xx->yy kalimat"
        default_to = "en"
        if not text:
            return None, default_to, ""

        first = text.split(" ")[0]
        if "->" in first and len(text.split(" ")) > 1:
            parts = first.split("->")
            if len(parts) == 2:
                return parts[0].lower(), parts[1].lower(), " ".join(text.split(" ")[1:])

        # Tidak ada arah → auto detect + default target English
        return None, default_to, text
