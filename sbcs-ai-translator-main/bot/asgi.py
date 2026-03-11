# bot/asgi.py
from fastapi import FastAPI
from bot.main import router  # your existing router

app = FastAPI(title="SBCS Bot")
app.include_router(router)
