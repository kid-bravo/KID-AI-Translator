from botbuilder.core import ActivityHandler, TurnContext
import os, httpx, uuid, logging

# Endpoint GLOBAL (api.cognitive.microsofttranslator.com)
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION   = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY      = os.getenv("TRANSLATOR_KEY")

# Logging dasar (tersalur ke Log Stream & App Insights traces)
logging.basicConfig(level=logging.INFO)


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
        logging.info(f"[bot] pesan diterima: {text!r}")

        # 1) Kirim ECHO dulu sebagai diagnostik
        try:
            await turn_context.send_activity(f"Echo: {text}")
        except Exception as e:
            logging.exception(f"[bot] gagal kirim echo: {e}")

        # 2) Parsing pola arah bahasa
        from_lang, to_lang, content = self._parse_direction(text)

        if not content:
            await turn_context.send_activity(
                "Format: `id->en Selamat pagi` atau ketik kalimat langsung (default id->en)."
            )
            return

        if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
            await turn_context.send_activity("Translator belum dikonfigurasi di server.")
            return

        # 3) Panggil Translator (endpoint global → /translate?api-version=3.0)
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
                res = await client.post(url, json=[{"Text": content}], headers=headers)

            if res.status_code >= 400:
                # Balas pesan error dari Translator agar terlihat di Web Chat
                msg = f"Translator error {res.status_code}: {res.text}"
                logging.warning(f"[bot] {msg}")
                await turn_context.send_activity(msg)
                return

            data = res.json()
            translated = data[0]["translations"][0]["text"]
            await turn_context.send_activity(translated)

        except Exception as e:
            logging.exception(f"[bot] gagal memanggil Translator: {e}")
            await turn_context.send_activity(f"Gagal menerjemahkan: {e}")

    def _parse_direction(self, text: str):
        """
        Pola: "xx->yy kalimat". Jika tidak ada, auto-detect source (None), target=en.
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
