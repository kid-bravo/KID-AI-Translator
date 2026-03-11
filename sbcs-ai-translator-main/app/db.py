"""
db.py - Module untuk proyek
"""
import ssl
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from .config import settings

# === Declarative base untuk semua model ===
Base = declarative_base()

# === Engine async dengan SSL (sesuai kode kamu) ===
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

engine = create_async_engine(
    settings.DATABASE_URL,
    connect_args={"ssl": ssl_ctx}
)

AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

# Dependency-style helper (FastAPI friendly)
async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session