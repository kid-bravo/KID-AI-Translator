#!/usr/bin/env bash
set -Eeuo pipefail

# ==== Path dasar ====
JOBDIR="/home/site/wwwroot/App_Data/jobs/continuous/translator-worker"
SITEPKG="/home/site/wwwroot/.python_packages/lib/site-packages"
STAMP="/home/site/wwwroot/App_Data/.reqhash-translator-worker"
REQ="$JOBDIR/requirements.txt"

# ==== Env dasar ====
export TZ=UTC
export PYTHONDONTWRITEBYTECODE=1
export PIP_ROOT_USER_ACTION=ignore
export PYTHONPATH="${SITEPKG}:/home/site/wwwroot:/home/site/wwwroot/app:${JOBDIR}"

trap 'echo "[$(date -u +%FT%TZ)] [INFO] SIGTERM/INT diterima, keluar..."; exit 0' INT TERM

LAUNCH_SRC="${WEBJOBS_NAME:+webjob:${WEBJOBS_NAME}}"
LAUNCH_SRC="${LAUNCH_SRC:-manual}"
echo "[$(date -u +%FT%TZ)] [BOOT] run.sh start (${LAUNCH_SRC})"

cd "$JOBDIR"

# ==== Pastikan deps terpasang ====
NEED_INSTALL=0
CURHASH=""

if [[ -f "$REQ" ]]; then
  CURHASH="$(sha256sum "$REQ" | awk '{print $1}')"
  OLDHASH="$(cat "$STAMP" 2>/dev/null || true)"
  [[ "$CURHASH" != "$OLDHASH" ]] && NEED_INSTALL=1
else
  echo "[$(date -u +%FT%TZ)] [WARN] requirements.txt tidak ditemukan di $REQ"
fi

# Jika modul belum ada, paksa install meski hash sama
python - <<'PY' || NEED_INSTALL=1
import importlib.util as iu
ok = iu.find_spec("azure.storage.queue") is not None
print("[CHK] azure.storage.queue:", ok)
raise SystemExit(0 if ok else 1)
PY

if [[ "$NEED_INSTALL" -eq 1 ]]; then
  echo "[$(date -u +%FT%TZ)] [DEPS] installing to ${SITEPKG} ..."
  mkdir -p "$SITEPKG"
  if [[ -f "$REQ" ]]; then
    python -m pip install --upgrade --no-cache-dir -r "$REQ" -t "$SITEPKG"
    [[ -n "$CURHASH" ]] && echo "$CURHASH" > "$STAMP"
  else
    # fallback minimal kalau requirements.txt tidak ada
    python -m pip install --upgrade --no-cache-dir -t "$SITEPKG" \
      azure-storage-queue azure-core azure-identity
  fi

  # verifikasi lagi
  python - <<'PY'
import importlib.util as iu
assert iu.find_spec("azure.storage.queue"), "azure.storage.queue masih tidak ditemukan setelah pip install"
print("[DEPS] OK")
PY
fi

# ==== Info sys.path untuk debug singkat ====
python - <<'PY'
import sys, importlib.util
print(f"[CHK] sys.path[:3] = {sys.path[:3]}")
print(f"[CHK] worker available? {importlib.util.find_spec('worker') is not None}")
PY

# ==== Jalankan worker ====
echo "[$(date -u +%FT%TZ)] [RUN] starting: python -u -m worker.worker"
if python -u -m worker.worker 2>/tmp/worker_mod.err; then
  exit 0
fi

echo "[$(date -u +%FT%TZ)] [RUN] fallback: python -u worker/worker.py"
exec python -u worker/worker.py
