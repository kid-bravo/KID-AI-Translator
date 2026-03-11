from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .db import engine
from .models import Base
from .services.http import http_client
from .routers import health, upload, jobs
from .routers import oauth
from app.routers.repair import router as repair_router
app = FastAPI(title=settings.APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.CORS_ORIGINS == "*" else [settings.CORS_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.on_event("shutdown")
async def on_shutdown():
    await http_client.close()


@app.get("/healthz")
async def healthz():
    return {"ok": True}

app.include_router(health.router)
app.include_router(upload.router)
app.include_router(jobs.router)
app.include_router(oauth.router)
app.include_router(repair_router)
