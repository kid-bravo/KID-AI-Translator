# app/services/msal_client.py
from __future__ import annotations
import os, time, json, base64, hashlib
from typing import Optional, Dict, Any
import httpx

TENANT = os.environ.get("MSAL_TENANT_ID", "")
CLIENT_ID = os.environ.get("MSAL_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MSAL_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")  # e.g. https://<app>.azurewebsites.net/oauth/callback

# scopes minimum buat OneDrive upload + basic profile
SCOPES = [
    "offline_access",
    "openid",
    "profile",
    "Files.ReadWrite",
    "User.Read",
    "Files.ReadWrite.AppFolder"
]

AUTH_BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
TOKEN_URL = f"{AUTH_BASE}/token"
AUTH_URL = f"{AUTH_BASE}/authorize"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"

def build_auth_url(state: str) -> str:
    from urllib.parse import urlencode
    q = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
        # optional PKCE bisa dipasang kalau mau
    }
    return f"{AUTH_URL}?{urlencode(q)}"

def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
    }
    with httpx.Client(timeout=30.0) as c:
        r = c.post(TOKEN_URL, data=data)
        r.raise_for_status()
        return r.json()

def refresh_tokens(refresh_token: str) -> Dict[str, Any]:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(SCOPES),
    }
    with httpx.Client(timeout=30.0) as c:
        r = c.post(TOKEN_URL, data=data)
        r.raise_for_status()
        return r.json()

def exchange_obo_from_teams_token(teams_access_token: str) -> Dict[str, Any]:
    """OBO: tukar Teams SSO bearer → Graph token dengan scope di atas."""
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "requested_token_use": "on_behalf_of",
        "scope": " ".join(SCOPES),
        "assertion": teams_access_token,
    }
    with httpx.Client(timeout=30.0) as c:
        r = c.post(TOKEN_URL, data=data)
        r.raise_for_status()
        return r.json()

def graph_me(access_token: str) -> Dict[str, Any]:
    with httpx.Client(timeout=20.0) as c:
        r = c.get(GRAPH_ME, headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()

def access_token_of(tok: Dict[str, Any]) -> Optional[str]:
    return tok.get("access_token")

def expires_at_of(tok: Dict[str, Any]) -> int:
    # prefer absolute, else now + expires_in
    if "expires_on" in tok:
        try:
            return int(tok["expires_on"])
        except Exception:
            pass
    return int(time.time()) + int(tok.get("expires_in", 3600))
