import time, json
from typing import Tuple, Optional, Dict, Any
import msal
from sqlalchemy import select
from app.db import AsyncSessionLocal
from app.models import User
from app.config import settings
from datetime import datetime


AUTHORITY = f"https://login.microsoftonline.com/{settings.AZ_TENANT_ID}"
CLIENT_ID = settings.AZ_APP_CLIENT_ID
CLIENT_SECRET = settings.AZ_APP_CLIENT_SECRET
SCOPES = ["https://graph.microsoft.com/.default"]

def _app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET
    )

def _parse_token_json(s: Optional[str]) -> Dict[str, Any]:
    if not s: return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

async def _load_user(session: AsyncSessionLocal, user_id: str) -> Optional[User]:
    res = await session.execute(select(User).where(User.user_id == user_id))
    return res.scalar_one_or_none()

async def _save_tokens(session: AsyncSessionLocal, user: User, access_token: str, refresh_token: str, expires_at: int) -> None:
    tj = _parse_token_json(user.token_json)
    tj.update({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "provider": "microsoft-graph",
        "scope": tj.get("scope") or "graph",
    })
    user.token_json = json.dumps(tj, ensure_ascii=False)
    user.expires_at = expires_at  # CRITICAL FIX
    user.updated_at = datetime.utcnow()
    session.add(user)
    await session.commit()

def _still_valid(expires_at: Optional[int], *, skew_sec: int = 300) -> bool:
    return bool(expires_at) and (int(expires_at) - int(time.time())) > skew_sec

async def get_graph_token(user_id: str) -> Tuple[str, int]:
    async with AsyncSessionLocal() as session:
        user = await _load_user(session, user_id)
        if not user:
            raise RuntimeError("User tidak ditemukan")

        tj = _parse_token_json(user.token_json)
        access_token = tj.get("access_token")
        refresh_token = tj.get("refresh_token")
        expires_at = int(user.expires_at or 0)

        if access_token and _still_valid(expires_at, skew_sec=300):
            return access_token, expires_at

        if not refresh_token:
            raise RuntimeError("User belum connect OneDrive / refresh_token kosong")

        app = _app()
        result = app.acquire_token_by_refresh_token(refresh_token, scopes=SCOPES)
        if "access_token" not in result:
            err = result.get("error")
            desc = result.get("error_description")
            raise RuntimeError(f"Refresh token gagal ({err}): {desc}")

        new_access = result["access_token"]
        new_exp = int(time.time()) + int(result.get("expires_in", 3600))
        new_rt = result.get("refresh_token") or refresh_token

        await _save_tokens(session, user, new_access, new_rt, new_exp)
        return new_access, new_exp