# app/routers/oauth.py
from __future__ import annotations
import json, time
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, insert
from datetime import datetime
from sqlalchemy import func
from ..db import get_session
from ..models import User
from ..services.msal_client import (
    build_auth_url, exchange_code_for_tokens, refresh_tokens,
    exchange_obo_from_teams_token, graph_me, access_token_of, expires_at_of,
)

router = APIRouter(prefix="/oauth", tags=["oauth"])

def _normalize_user_id(me: dict, fallback: Optional[str]) -> str:
    """
    Ambil UPN dulu, lalu objectId, terakhir fallback.
    Selalu lowercase untuk konsistensi.
    """
    uid = (me.get("userPrincipalName") or me.get("id") or (fallback or "unknown")).strip()
    return uid.lower()

def _safe_graph_me(at: Optional[str]) -> dict:
    if not at:
        return {}
    try:
        return graph_me(at) or {}
    except Exception:
        return {}

def _safe_expires(tok: dict) -> int:
    now = int(time.time())
    exp = expires_at_of(tok) if tok else None
    try:
        return int(exp) if exp else (now + 3500)
    except Exception:
        return now + 3500

@router.get("/login")
async def oauth_login(user_id: str):
    """Kickoff auth-code flow untuk user yang belum connect OneDrive."""
    url = build_auth_url(user_id)
    return RedirectResponse(url,status_code=302)

@router.get("/callback")
async def oauth_callback(code: str, state: Optional[str] = None, session: AsyncSession = Depends(get_session)):
    """
    Terima code → tukar token → simpan di tabel users (tanpa ubah skema).
    """
    tok = exchange_code_for_tokens(code)
    at = access_token_of(tok)
    if not at:
        raise HTTPException(400, "Token exchange failed (no access token)")

    me = _safe_graph_me(at)
    uid = _normalize_user_id(me, state)
    now = int(time.time())
    token_json = json.dumps(tok, ensure_ascii=False)
    expires_at = _safe_expires(tok)

    row = await session.execute(select(User).where(User.user_id == uid))
    existing = row.scalar_one_or_none()

    if existing:
        # JANGAN set updated_at=None; biarkan default DB/trigger atau set epoch
        values = {
            "name": me.get("displayName"),
            "email": me.get("userPrincipalName"),
            "token_json": token_json,
            "expires_at": expires_at,
        }
        # Jika kolom updated_at tidak ada default/trigger, gunakan epoch:
        values["updated_at"] = func.now()
        await session.execute(update(User).where(User.user_id == uid).values(**values))
    else:
        await session.execute(
            insert(User).values(
                user_id=uid,
                name=me.get("displayName"),
                email=me.get("userPrincipalName"),
                flow="auth_code",
                account_json=json.dumps({}, ensure_ascii=False),
                token_json=token_json,
                expires_at=expires_at,
                # tambahkan updated_at jika model tidak punya default:
                updated_at=func.now(),
            )
        )

    await session.commit()
    return JSONResponse({"ok": True, "mode": "auth_code", "user_id": uid, "displayName": me.get("displayName")})

@router.get("/check")
async def oauth_check(
    user_id: Optional[str] = None,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    session: AsyncSession = Depends(get_session),
):
    """
    Dua mode:
    - Ada Authorization: Bearer <TeamsSSO> → OBO → simpan.
    - Tidak ada: cek DB (auth code flow) apakah sudah connect.
    """
    now = int(time.time())

    # --------- MODE OBO (Teams SSO) ----------
    if authorization and authorization.lower().startswith("bearer "):
        teams = authorization.split(" ", 1)[1].strip()
        tok = exchange_obo_from_teams_token(teams)
        at = access_token_of(tok)
        if not at:
            raise HTTPException(401, "OBO exchange failed (no access token)")

        me = _safe_graph_me(at)
        uid = _normalize_user_id(me, user_id)
        token_json = json.dumps(tok, ensure_ascii=False)
        expires_at = _safe_expires(tok)

        row = await session.execute(select(User).where(User.user_id == uid))
        existing = row.scalar_one_or_none()
        values = {
            "name": me.get("displayName"),
            "email": me.get("userPrincipalName"),
            "flow": "obo",
            "token_json": token_json,
            "expires_at": expires_at,
            "updated_at": func.now(),
        }

        if existing:
            await session.execute(update(User).where(User.user_id == uid).values(**values))
        else:
            await session.execute(insert(User).values(user_id=uid, account_json=json.dumps({}, ensure_ascii=False), **values))
        await session.commit()

        return {
            "connected": True,
            "mode": "obo",
            "user_id": uid,
            "displayName": me.get("displayName"),
            "upn": me.get("userPrincipalName"),
        }

    # --------- MODE AUTH CODE (cek DB) ----------
    if not user_id:
        raise HTTPException(400, "user_id required when no Authorization header")

    row = await session.execute(select(User).where(User.user_id == user_id))
    u = row.scalar_one_or_none()
    if not u or not u.token_json:
        return {"connected": False, "mode": "auth_code", "user_id": user_id}

    try:
        tok = json.loads(u.token_json)
        at = access_token_of(tok)

        # kalau expired & ada refresh token → refresh
        if (not at) or int(u.expires_at or 0) <= now:
            if "refresh_token" in tok:
                tok = refresh_tokens(tok["refresh_token"])
                await session.execute(
                    update(User)
                    .where(User.user_id == user_id)
                    .values(
                        token_json=json.dumps(tok, ensure_ascii=False),
                        expires_at=_safe_expires(tok),
                        updated_at=func.now(),
                    )
                )
                await session.commit()
                at = access_token_of(tok)

        me = _safe_graph_me(at) if at else {}
        return {
            "connected": True if at else False,
            "mode": "auth_code",
            "user_id": user_id,
            "displayName": me.get("displayName"),
            "upn": me.get("userPrincipalName"),
        }
    except Exception as e:
        return {"connected": False, "mode": "auth_code", "user_id": user_id, "error": str(e)}
