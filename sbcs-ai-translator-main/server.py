# server.py
from fastapi import FastAPI
from app.main import app as api_app       # FastAPI utama (healthz, upload, jobs, dll)
from bot.asgi import app as bot_app       # Aplikasi Bot (ASGI)

app = FastAPI(title="api-bot-composed")

# Penting: mount yang lebih spesifik dulu
app.mount("/bot", bot_app)   # semua endpoint bot di bawah /bot (termasuk /bot/api/messages, /bot/diag)
app.mount("/", api_app)      # sisanya ke API utama
