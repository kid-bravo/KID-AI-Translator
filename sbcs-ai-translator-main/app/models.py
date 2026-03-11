# app/models.py
from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, BigInteger, TIMESTAMP
from app.db import Base  # <-- pakai Base dari app/db.py yang sudah kamu buat

class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str]             = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(256), default=None)

    filename: Mapped[str]       = mapped_column(String(512))
    status: Mapped[str]         = mapped_column(String(64), default="QUEUED")
    detail: Mapped[str]         = mapped_column(Text, default="")

    batch_id: Mapped[str]       = mapped_column(String(256), default="")

    result_blob:  Mapped[str]   = mapped_column(String(1024), default="")
    download_url: Mapped[str]   = mapped_column(String(2048), default="")

    onedrive_item_id: Mapped[str | None] = mapped_column(String(256), default=None)
    onedrive_url:     Mapped[str | None] = mapped_column(String(2048), default=None)

    source_lang: Mapped[str]    = mapped_column(String(16), default="auto")
    target_lang: Mapped[str]    = mapped_column(String(16), default="id")

    created_at: Mapped[int]     = mapped_column(BigInteger, default=0)
    updated_at: Mapped[int]     = mapped_column(BigInteger, default=0)


class User(Base):
    __tablename__ = "users"

    user_id:     Mapped[str]           = mapped_column(String(512), primary_key=True)
    name:        Mapped[str | None]    = mapped_column(String(256), default=None)
    email:       Mapped[str | None]    = mapped_column(String(256), default=None)
    flow:        Mapped[str | None]    = mapped_column(String(32),  default=None)
    account_json:Mapped[str | None]    = mapped_column(Text,        default=None)
    token_json:  Mapped[str | None]    = mapped_column(Text,        default=None)   # untuk Graph tokens
    expires_at:  Mapped[int | None]    = mapped_column(BigInteger,  default=None)   # epoch detik UTC
    updated_at:  Mapped[str | None]    = mapped_column(TIMESTAMP,   default=None)
