# app/services/path_sanitize.py
from __future__ import annotations

import re, unicodedata
from urllib.parse import unquote

# Karakter yang dilarang OneDrive/Windows (buat jaga-jaga jika dipakai judul file downstream)
_ONEDRIVE_ILLEGAL = set('\"*:<>?/\\|')
_CTRL = ''.join(chr(i) for i in range(0, 32))
_WHITESPACE_RX = re.compile(r'\s+')
_MULTI_SLASH_RX = re.compile(r'/+')

def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")

def _collapse_ws(s: str) -> str:
    # normalisasi whitespace (termasuk non-breaking space)
    s = (s or "").replace("\u00A0"," ")
    return _WHITESPACE_RX.sub(" ", s)

def _strip_problematic_edges(s: str) -> str:
    # hapus spasi/titik di tepi & zero-width chars
    s = (s or "").strip().strip("\u200b\u200c\u200d\u200e\u200f")
    while s.endswith((" ", ".")):
        s = s[:-1]
    return s

def sanitize_blob_path(path: str) -> str:
    """
    Normalisasi path blob agar aman & deterministik TANPA mengganti spasi menjadi underscore.
    Ini dipakai SAAT UPLOAD untuk menentukan NAMA FINAL yang disimpan di blob & DB.
    """
    if not path:
        return path
    p = unquote(path)
    p = _nfc(_collapse_ws(p)).replace("\\", "/")
    p = _MULTI_SLASH_RX.sub("/", p)

    cleaned = []
    rm_tr = {ord(c): None for c in _CTRL}
    for seg in p.split("/"):
        seg = seg.translate(rm_tr)
        seg = _strip_problematic_edges(seg)
        if seg in ("", "."):
            continue
        if seg == "..":
            if cleaned:
                cleaned.pop()
            continue
        cleaned.append(seg)

    return "/".join(cleaned)

def safe_basename_onedrive(name: str) -> str:
    """
    Nama file aman untuk OneDrive/Graph (kalau nanti di-upload).
    Tidak mengubah blob name; ini hanya util terpisah untuk tahap OneDrive.
    """
    name = _strip_problematic_edges(_collapse_ws(_nfc(name or "")))
    name = name.translate({ord(c): None for c in _CTRL})
    name = "".join(ch for ch in name if ch not in _ONEDRIVE_ILLEGAL)
    return name.replace("/", "-").replace("\\", "-") or "file"
