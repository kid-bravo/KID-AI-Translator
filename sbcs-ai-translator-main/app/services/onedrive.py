from __future__ import annotations
import os, json, time, asyncio
from typing import Optional, Tuple
import httpx
from sqlalchemy import select


GRAPH = "https://graph.microsoft.com/v1.0"

TENANT  = os.getenv("MicrosoftAppTenantId", "")
CLIENT  = os.getenv("MicrosoftAppId", "")
SECRET  = os.getenv("MicrosoftAppPassword", "")

# ====== helper: ambil token dari DB + refresh kalau expired ======
async def _read_user_row(session, user_id: str):
    from app.models import User
    res = await session.execute(select(User).where(User.user_id == user_id))
    return res.scalar_one_or_none()

async def get_valid_user_token(session, user_id: str) -> Optional[str]:
    """
    Ambil access_token user yang masih valid.
    Jika expired dan ada refresh_token -> refresh ke AAD, lalu simpan balik ke DB.
    """
    user = await _read_user_row(session, user_id)
    if not user: 
        return None

    try:
        tok = json.loads(user.token_json or "{}")
    except Exception:
        tok = {}

    access_token  = tok.get("access_token")
    refresh_token = tok.get("refresh_token")
    expires_at    = int(user.expires_at or 0)

    now = int(time.time())
    if access_token and now + 60 < expires_at:
        return access_token

    # butuh refresh
    if not (refresh_token and TENANT and CLIENT and SECRET):
        # tidak bisa refresh
        return access_token

    token_url = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT,
        "client_secret": SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        # scope minimal untuk OneDrive upload + offline
        "scope": "Files.ReadWrite files.readwrite.all offline_access",
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(token_url, data=data)
        if r.status_code != 200:
            return access_token
        j = r.json()
        access_token = j.get("access_token") or access_token
        new_refresh  = j.get("refresh_token") or refresh_token
        expires_in   = int(j.get("expires_in", 3600))
        new_exp      = now + expires_in - 30

        # simpan balik
        try:
            tok["access_token"]  = access_token
            tok["refresh_token"] = new_refresh
            user.token_json = json.dumps(tok)
            user.expires_at = new_exp
            await session.commit()
        except Exception:
            pass

    return access_token

async def _ensure_translated_folder(user_token: str, client: httpx.AsyncClient) -> Optional[str]:
    headers = {"Authorization": f"Bearer {user_token}"}
    r = await client.get(f"{GRAPH}/me/drive/root/children?$select=name,id,folder", headers=headers)
    if r.status_code == 401:
        return None
    r.raise_for_status()
    for it in r.json().get("value", []):
        if it.get("name") == "Translated" and "folder" in it:
            return it["id"]

    body = {"name":"Translated","folder":{},"@microsoft.graph.conflictBehavior":"rename"}
    r2 = await client.post(f"{GRAPH}/me/drive/root/children", headers=headers, json=body)
    r2.raise_for_status()
    return r2.json()["id"]

async def upload_bytes_to_user_onedrive(
    session, user_id: str, filename: str, data: bytes
) -> Tuple[Optional[str], Optional[str]]:
    """
    Upload ke OneDrive/Translated milik user_id.
    Return (item_id, web_url)
    """
    token = await get_valid_user_token(session, user_id)
    if not token:
        return (None, None)

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        folder_id = await _ensure_translated_folder(token, client)
        if not folder_id:
            return (None, None)

        # upload session agar aman untuk file besar
        r = await client.post(
            f"{GRAPH}/me/drive/items/{folder_id}:/{filename}:/createUploadSession",
            headers=headers, json={"@microsoft.graph.conflictBehavior":"replace"}
        )
        if r.status_code == 401:
            # sekali lagi coba refresh keras
            token = await get_valid_user_token(session, user_id)
            if not token:
                return (None, None)
            headers = {"Authorization": f"Bearer {token}"}
            r = await client.post(
                f"{GRAPH}/me/drive/items/{folder_id}:/{filename}:/createUploadSession",
                headers=headers, json={"@microsoft.graph.conflictBehavior":"replace"}
            )

        r.raise_for_status()
        upload_url = r.json()["uploadUrl"]

        # kirim chunk
        size=len(data); chunk=10*1024*1024; start=0
        last_resp = None
        while start < size:
            end=min(start+chunk,size)-1
            piece=data[start:end+1]
            last_resp = await client.put(
                upload_url,
                headers={"Content-Range": f"bytes {start}-{end}/{size}"},
                content=piece
            )
            if last_resp.status_code not in (200,201,202):
                last_resp.raise_for_status()
            start=end+1

        item = last_resp.json()
        item_id = item.get("id")
        web_url = item.get("webUrl")

        # sharing link (opsional)
        try:
            r2 = await client.post(
                f"{GRAPH}/me/drive/items/{item_id}/createLink",
                headers=headers, json={"type":"view","scope":"anonymous"}
            )
            if r2.status_code == 200:
                web_url = (r2.json().get("link") or {}).get("webUrl") or web_url
        except Exception:
            pass

        return (item_id, web_url)
