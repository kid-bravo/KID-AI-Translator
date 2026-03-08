from botbuilder.core import ActivityHandler, TurnContext
import os, httpx, uuid, logging, asyncio

# Endpoint GLOBAL (api.cognitive.microsofttranslator.com)
TRANSLATOR_ENDPOINT = (os.getenv("TRANSLATOR_ENDPOINT") or "").rstrip("/")
TRANSLATOR_REGION   = os.getenv("TRANSLATOR_REGION", "southeastasia")
TRANSLATOR_KEY      = os.getenv("TRANSLATOR_KEY")

logging.basicConfig(level=logging.INFO)

MAX_LEN = 5000

class TranslatorBot(ActivityHandler):
    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        welcome = (
            "Halo! 👋 Saya AI Translator.\n"
            "Contoh: `id->en Selamat pagi` atau `ja->id おはようございます`\n"
            "Kalau tidak menulis arah, default `id->en`.\n"
            "Ketik `help` untuk petunjuk."
        )
        await turn_context.send_activity(welcome)

    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()
        logging.info(f"[bot] pesan: {text!r}")

        # Help
        if text.lower() in ("help", "/help", "?"):
            await turn_context.send_activity(
                "Format:\n• `xx->yy kalimat` (contoh: `id->en Selamat pagi`)\n"
                "• Jika tanpa arah, diasumsikan `id->en`.\n"
                "Batas: 5000 karakter per pesan."
            )
            return

        # Parsing arah
        from_lang, to_lang, content = self._parse_direction(text)

        if not content:
            await turn_context.send_activity(
                "Format: `id->en Selamat pagi` atau ketik kalimat langsung (default id->en)."
            )
            return

        if len(content) > MAX_LEN:
            await turn_context.send_activity(
                f"Teks terlalu panjang ({len(content)}). Batas {MAX_LEN} karakter."
            )
            return

        if not TRANSLATOR_ENDPOINT or not TRANSLATOR_KEY:
            await turn_context.send_activity("Translator belum dikonfigurasi di server.")
            return

        # Translator (endpoint global → /translate?api-version=3.0)
        url = f"{TRANSLATOR_ENDPOINT}/translate?api-version=3.0&to={to_lang}"
        if from_lang:
            url += f"&from={from_lang}"

        headers = {
            "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
            "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,  # wajib untuk global
            "Content-type": "application/json",
            "X-ClientTraceId": str(uuid.uuid4())
        }

        payload = [{"Text": content}]
        try:
            data = await self._post_with_retry(url, headers, payload)
            translated = data[0]["translations"][0]["text"]
            await turn_context.send_activity(translated)
        except httpx.HTTPStatusError as he:
            msg = f"Translator error {he.response.status_code}: {he.response.text[:300]}"
            logging.warning(f"[bot] {msg}")
            await turn_context.send_activity(msg)
        except Exception as e:
            logging.exception(f"[bot] gagal memanggil Translator: {e}")
            await turn_context.send_activity(f"Gagal menerjemahkan: {e}")

    def _parse_direction(self, text: str):
        """
        Pola: 'xx->yy kalimat'. Jika tidak ada, auto-detect source (None), target=en.
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

    async def _post_with_retry(self, url, headers, payload, attempts: int = 3):
        backoff = 0.7
        async with httpx.AsyncClient(timeout=15) as client:
            for i in range(1, attempts + 1):
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504) and i < attempts:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
