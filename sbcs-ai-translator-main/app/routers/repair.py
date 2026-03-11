from __future__ import annotations

import os
import json
from typing import Optional

import aiohttp
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_400_BAD_REQUEST, HTTP_413_REQUEST_ENTITY_TOO_LARGE
import jwt
from jwt import InvalidTokenError
from dotenv import load_dotenv
load_dotenv()

APP_PORT = os.getenv("APP_PORT", "8080")
TRANSLATOR_API = os.getenv("TRANSLATOR_API", f"http://127.0.0.1:{APP_PORT}").rstrip("/")
TRANSLATE_PATH = os.getenv("TRANSLATE_PATH", "translate").strip("/")
TEAMS_JWT_SECRET = os.getenv("TEAMS_JWT_SECRET", "dev-secret")
MAX_REPAIR_UPLOAD_MB = int(os.getenv("MAX_REPAIR_UPLOAD_MB", "200"))  # batas ukuran file (MB)


try:
    from app.services.msgraph_auth import acquire_token_silent
except Exception:
    acquire_token_silent = None

router = APIRouter()


def verify_repair_token(token: str) -> dict:
    if not token:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Missing token")
    try:
        return jwt.decode(token, TEAMS_JWT_SECRET, algorithms=["HS256"])
    except InvalidTokenError as e:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {e}")


# ===== HTML + JS =====
_REPAIR_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Repair Upload — SBCS Translator</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root{
      --bg:#0b1020; --card:#12172b; --edge:#1e2440; --ink:#e6e9f0; --muted:#9aa3b2;
      --box:#0e1426; --accent:#2563eb; --accent-2:#60a5fa; --barbg:#0e1426; --barfill:#3b82f6;
    }
    html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif}
    .wrap{max-width:980px;margin:40px auto;padding:24px}
    .card{background:var(--card);border:1px solid var(--edge);border-radius:16px;padding:24px;box-shadow:0 8px 30px rgba(0,0,0,.35)}
    h1{margin:0 0 6px;font-size:24px}
    .muted{color:var(--muted)}
    .row{display:flex;gap:16px;flex-wrap:wrap;margin-top:16px}
    .box{flex:1 1 320px;background:var(--box);border:1px dashed #2a3358;border-radius:12px;padding:16px;min-height:160px;display:flex;align-items:center;justify-content:center;text-align:center}
    .left{align-items:center}
    .right{align-items:flex-start}
    input[type="file"]{display:none}
    .kbd{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;background:#0b1020;border:1px solid #2a3358;border-radius:6px;padding:3px 6px;color:#cbd5e1}
    button{appearance:none;background:var(--accent);border:none;color:#fff;padding:10px 14px;border-radius:10px;font-weight:600;cursor:pointer}
    button:disabled{opacity:.5;cursor:not-allowed}
    a{color:#93c5fd;text-decoration:none}
    .pill{display:inline-block;padding:4px 8px;border:1px solid #2a3358;border-radius:999px;background:#0b1020;margin-right:8px}

    /* progress */
    .progress{margin-top:14px}
    .bar{height:12px;background:var(--barbg);border-radius:999px;overflow:hidden;border:1px solid #1f2a44}
    .fill{height:100%;width:0;background:linear-gradient(45deg, var(--barfill), #1d4ed8 40%, var(--barfill));background-size:200% 100%;
      animation:move 2s linear infinite}
    @keyframes move{from{background-position:0 0} to{background-position:200% 0}}
    .label{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:6px}
    .log{white-space:pre-wrap;background:#0e1426;border:1px solid #2a3358;border-radius:12px;padding:12px;margin-top:16px;min-height:115px}
    .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .mt16{margin-top:16px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Repair Upload (Direct → Translator)</h1>
      <div class="muted">Use this page if the bot replied: <em>"File is not a zip file"</em> for PPTX with embedded contents.</div>

      <div class="row">
        <div class="box left" id="drop">
          <div>
            <div style="font-weight:700;margin-bottom:6px">Drag & drop file here</div>
            <div class="muted">or</div>
            <div style="margin-top:8px">
              <label for="file"><span class="kbd">Choose file</span></label>
              <input id="file" type="file" />
            </div>
            <div id="filename" class="muted" style="margin-top:8px"></div>
          </div>
        </div>
        <div class="box right">
          <div>
            <div style="font-weight:700;margin-bottom:8px">Parameters</div>
            <div class="muted">These come from the secure token (can be changed):</div>
            <div style="margin-top:10px">
              <span class="pill">source=<span id="src-pill">en</span></span>
              <span class="pill">target=<span id="tgt-pill">id</span></span>
            </div>
            <div style="margin-top:10px">
              <label>Source: <input id="src" class="kbd" value="en" size="4" /></label>
              &nbsp;&nbsp;
              <label>Target: <input id="tgt" class="kbd" value="id" size="4" /></label>
            </div>
            <div class="muted" style="margin-top:8px">We will upload to the server and let the backend handle OneDrive/Blob.</div>
          </div>
        </div>
      </div>

      <div class="grid2 mt16">
        <div>
          <div style="font-weight:700">Upload</div>
          <div class="progress">
            <div class="bar"><div id="upFill" class="fill" style="width:0%"></div></div>
            <div class="label"><span id="upText">Waiting…</span><span id="upPct">0%</span></div>
          </div>
        </div>
        <div>
          <div style="font-weight:700">Processing</div>
          <div class="progress">
            <div class="bar"><div id="prFill" class="fill" style="width:0%"></div></div>
            <div class="label"><span id="prText">Idle</span><span id="prPct">0%</span></div>
          </div>
        </div>
      </div>

      <div style="margin-top:16px;display:flex;gap:12px">
        <button id="btn" disabled>Upload & Translate</button>
        <div id="hint" class="muted"></div>
      </div>

      <div id="log" class="log"></div>
    </div>
  </div>

<script>
(function(){
  const qs = new URLSearchParams(location.search);
  let token = qs.get("token") || (window.__REPAIR_TOKEN || "");

  if(!token){
    try { token = localStorage.getItem("repair_token") || ""; } catch(e){}
  }
  if(!token){
    try {
      const m = document.cookie.match(/(?:^|;\\s*)repair_token=([^;]+)/);
      if(m) token = decodeURIComponent(m[1]);
    } catch(e){}
  }

  const el = (id)=>document.getElementById(id);
  const log = (m)=>{ el("log").textContent += (m + "\\n"); el("log").scrollTop = el("log").scrollHeight; };
  const setUpload = (pct, text)=>{ pct = Math.max(0, Math.min(100, Math.round(pct))); el("upFill").style.width = pct + "%"; el("upPct").textContent = pct + "%"; el("upText").textContent = text || ((pct>=100)?"Done":"Uploading…"); };
  const setProc = (pct, text)=>{ pct = Math.max(0, Math.min(100, Math.round(pct))); el("prFill").style.width = pct + "%"; el("prPct").textContent = pct + "%"; el("prText").textContent = text || "Processing…"; };

  if(token){
    el("hint").textContent = "Token detected. Ready.";
    try { localStorage.setItem("repair_token", token); } catch(e){}
    document.cookie = "repair_token=" + encodeURIComponent(token) + "; path=/; max-age=1800; SameSite=Lax";
  } else {
    el("hint").textContent = "Missing token — ask the bot again.";
  }

  // Prefill src/tgt from token
  try{
    const payload = JSON.parse(atob((token||'').split('.')[1] || ''));
    if(payload && payload.src){ el("src").value = payload.src; el("src-pill").textContent = payload.src; }
    if(payload && payload.tgt){ el("tgt").value = payload.tgt; el("tgt-pill").textContent = payload.tgt; }
  }catch(e){}

  const fileInput = el("file");
  const drop = el("drop");
  const btn = el("btn");

  const enableBtn = ()=> { btn.disabled = !(fileInput.files.length && token); };
  fileInput.addEventListener("change", ()=>{ enableBtn(); el("filename").textContent = fileInput.files.length? fileInput.files[0].name : ""; });

  drop.addEventListener("dragover",(e)=>{e.preventDefault(); drop.style.borderColor="#60a5fa";});
  drop.addEventListener("dragleave",()=>{drop.style.borderColor="#2a3358";});
  drop.addEventListener("drop",(e)=>{
    e.preventDefault(); drop.style.borderColor="#2a3358";
    if(e.dataTransfer.files.length){ fileInput.files = e.dataTransfer.files; enableBtn(); el("filename").textContent = fileInput.files[0].name; log("File selected: " + fileInput.files[0].name); }
  });

  const bytes = (n)=> {
    if(!isFinite(n)) return "";
    const u=["B","KB","MB","GB"]; let i=0; while(n>=1024 && i<u.length-1){n/=1024;i++} return n.toFixed(1)+" "+u[i];
  };

  async function pollJob(url){
    const absolute = url.startsWith("http") ? url : (location.origin + url);
    let lastStatus = "";
    while(true){
      await new Promise(r=>setTimeout(r, 2000));
      let js;
      try{
        const r = await fetch(absolute, {headers: {"Accept":"application/json","Cache-Control":"no-store"}});
        js = await r.json();
      }catch(e){
        log("Polling error: " + e);
        continue;
      }
      const s = (js.status||"").toLowerCase();
      if(s && s!==lastStatus){
        lastStatus = s;
        log("Job status: " + s);
      }
      if(s==="submitted" || s==="queued"){ setProc(25, "Queued…"); }
      else if(s==="running"){ setProc(70, "Translating…"); }
      else if(s==="succeeded"){
        setProc(100, "Done");
        if(js.download_url){ log("✅ Download: " + js.download_url); }
        else if(js.onedrive_url){ log("✅ Saved to OneDrive: " + js.onedrive_url); }
        break;
      } else if(s==="failed"){
        setProc(100, "Failed");
        log("❌ Failed: " + (js.detail || js.status));
        break;
      }
    }
  }

  btn.addEventListener("click", ()=>{
    if(!fileInput.files.length || !token) return;

    const src = el("src").value || "en";
    const tgt = el("tgt").value || "id";
    const f = fileInput.files[0];

    setUpload(0,"Starting…");
    setProc(0,"Waiting for upload…");
    btn.disabled = true;

    const form = new FormData();
    form.append("file", f, f.name);
    form.append("source_lang", src);
    form.append("target_lang", tgt);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/repair/upload");
    xhr.responseType = "json";
    xhr.setRequestHeader("X-Repair-Token", token);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Cache-Control", "no-store");

    xhr.upload.onprogress = (e)=>{
      if(e.lengthComputable){
        const pct = Math.round((e.loaded / e.total) * 100);
        setUpload(pct, `${bytes(e.loaded)} / ${bytes(e.total)}`);
      } else {
        setUpload(50, "Uploading…");
      }
    };
    xhr.onloadstart = ()=> setUpload(1, "Starting…");
    xhr.onerror = ()=> { log("Upload error"); btn.disabled=false; };
    xhr.onload = ()=>{
      try {
        const ctype = xhr.getResponseHeader("content-type") || "";
        if(!ctype.includes("application/json")){
          const txt = xhr.responseText || "(no text)";
          log("Server returned non-JSON ("+xhr.status+"):\n" + txt);
          btn.disabled = false;
          return;
        }
      } catch(e){}

      const res = xhr.response;
      log("Response: " + JSON.stringify(res, null, 2));
      setUpload(100, "Uploaded");

      if(res && res.job_id){
        setProc(15, "Submitted…");
        const check = res.check_status || res.check || ("/jobs/" + res.job_id);
        pollJob(check);
      } else {
        btn.disabled = false;
      }
    };
    xhr.send(form);
  });

  // initial enable state
  enableBtn();
})();
</script>
</body>
</html>
"""

def _inject_token_into_html(html: str, token: Optional[str]) -> str:
    """Sisipkan token ke window.__REPAIR_TOKEN agar JS bisa pakai kalau query kosong."""
    if not token:
        return html
    token_js = json.dumps(token)
    snippet = f"<script>window.__REPAIR_TOKEN={token_js};</script></head>"
    return html.replace("</head>", snippet)


@router.get("/repair")
async def repair_page(request: Request) -> HTMLResponse:
    """
    Bisa ambil token dari:
    - ?token=...
    - Header: X-Repair-Token atau Authorization: Bearer <token>
    - Cookie: repair_token
    """
    qp_token = request.query_params.get("token")
    auth = request.headers.get("Authorization", "")
    hdr_token = request.headers.get("X-Repair-Token") or (auth[7:] if auth.lower().startswith("bearer ") else None)
    cookie_token = request.cookies.get("repair_token")

    token = qp_token or hdr_token or cookie_token
    html = _inject_token_into_html(_REPAIR_HTML, token)

    resp = HTMLResponse(html, headers={"Cache-Control": "no-store"})
    if token:
        # Tidak HttpOnly agar JS bisa baca; masa hidup 30 menit (selaras JWT exp default)
        resp.set_cookie("repair_token", token, max_age=1800, path="/", samesite="lax")
    return resp


@router.post("/repair/upload")
async def repair_upload(
    request: Request,
    file: UploadFile = File(...),
    source_lang: str = Form(...),
    target_lang: str = Form(...),
):
    # Ambil token dari header / cookie / query (fleksibel)
    auth = request.headers.get("Authorization", "")
    token = (
        request.headers.get("X-Repair-Token")
        or (auth[7:] if auth.lower().startswith("bearer ") else None)
        or request.cookies.get("repair_token")
        or request.query_params.get("token")
        or ""
    )

    claims = verify_repair_token(token)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Token has no 'sub' (user_id).")

    src = (source_lang or claims.get("src") or "").strip() or "en"
    tgt = (target_lang or claims.get("tgt") or "").strip() or "id"

    # Info koneksi OneDrive (INFORMATIF saja; tidak memblokir proses)
    onedrive_connected = True
    if acquire_token_silent:
        try:
            tok = acquire_token_silent(user_id)
            if not tok:
                onedrive_connected = False
        except Exception:
            onedrive_connected = False

    # Baca file & validasi ukuran
    data = await file.read()
    if data is None or len(data) == 0:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Empty file")
    limit_bytes = MAX_REPAIR_UPLOAD_MB * 1024 * 1024
    if len(data) > limit_bytes:
        return JSONResponse(
            status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"ok": False, "error": "too_large", "detail": f"File {len(data)/1024/1024:.1f} MB > limit {MAX_REPAIR_UPLOAD_MB} MB"},
        )

    # Kirim langsung ke endpoint translator (form-data)
    form = aiohttp.FormData()
    form.add_field("file", data, filename=file.filename, content_type=file.content_type or "application/octet-stream")
    form.add_field("source_lang", src)
    form.add_field("target_lang", tgt)

    # Tentukan base URL backend
    host = (request.headers.get("host") or "").lower()
    if "ngrok" in host:
        base = f"http://127.0.0.1:{APP_PORT}"
    else:
        base = TRANSLATOR_API

    # Forward ke backend
    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.post(
                f"{base}/{TRANSLATE_PATH}",
                data=form,
                headers={
                    "X-User-Id": user_id,             # supaya job tercatat ke user ini
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                },
                timeout=aiohttp.ClientTimeout(total=600),
            ) as r:
                ctype = (r.headers.get("content-type") or "").lower()
                if "application/json" in ctype:
                    js = await r.json()
                else:
                    txt = await r.text()
                    return PlainTextResponse(txt, status_code=r.status)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": f"upstream_failed: {e}"})

    # Response akhir untuk UI
    out = {"ok": True, "onedrive_connected": onedrive_connected, **js}
    if "job_id" in js and "check_status" not in js:
        out["check_status"] = f"/jobs/{js['job_id']}"
    return JSONResponse(out, headers={"Cache-Control": "no-store"})
