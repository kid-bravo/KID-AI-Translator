# app/services/resize.py
from __future__ import annotations

import io, os, re, zipfile, tempfile, shutil
from pathlib import Path
from typing import Tuple, Optional, Dict

# --- Public API --------------------------------------------------------------

def ensure_under_size(
    file_bytes: bytes,
    filename: str,
    *,
    target_mb: float = 20.0,
    max_image_px: int = 2200,
    jpeg_quality: int = 72,
    png_max_compress: bool = True,
    allow_png_to_jpeg: bool = True,
) -> Tuple[bytes, str, Dict]:
    """
    Shrink in-memory. Support: PDF & OOXML (docx, pptx, xlsx, docm, pptm, xlsm).
    Return (new_bytes, new_filename, info).
    """
    name = os.path.basename(filename)
    ext = name.split('.')[-1].lower() if '.' in name else ''
    info: Dict[str, object] = {
        "original_size": len(file_bytes),
        "final_size": None,
        "changed": False,
        "strategy": None
    }

    if ext in ('pdf',):
        out, _ = _shrink_pdf(file_bytes)
        if out:
            info["strategy"] = "pdf"
            info["final_size"] = len(out)
            info["changed"] = len(out) < len(file_bytes)
            return out, name, info
        return file_bytes, name, info

    if ext in ('docx','pptx','xlsx','docm','pptm','xlsm'):
        out = _shrink_ooxml(
            file_bytes,
            ooxml_kind=ext.split('x')[-1],
            max_image_px=max_image_px,
            jpeg_quality=jpeg_quality,
            allow_png_to_jpeg=allow_png_to_jpeg,
            png_max_compress=png_max_compress
        )
        if out:
            info["strategy"] = "ooxml"
            info["final_size"] = len(out)
            info["changed"] = len(out) < len(file_bytes)
            return out, name, info
        return file_bytes, name, info

    info["strategy"] = "noop"
    info["final_size"] = len(file_bytes)
    return file_bytes, name, info


# --- OOXML (DOCX/PPTX/XLSX/… ) ----------------------------------------------

_OOXML_MEDIA_DIRS = {
    "doc": ["word/media"],
    "ppt": ["ppt/media"],
    "xls": ["xl/media"],
}

_IMAGE_EXTS = ('.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.gif')

def _is_photographic(img) -> bool:
    # Heuristik sederhana: banyak warna & tanpa alpha => foto (lebih baik JPEG)
    try:
        if img.mode in ('RGBA','LA','P') and getattr(img, "info", {}).get("transparency") is not None:
            return False
        if img.mode == 'P' and getattr(img, "palette", None):
            return False
        try:
            colors = img.getcolors(maxcolors=512)
            if colors is not None and len(colors) <= 64:
                return False
        except Exception:
            pass
        return True
    except Exception:
        return True

def _resample_image_bytes(data: bytes, *, max_image_px: int, jpeg_quality: int,
                          allow_png_to_jpeg: bool, orig_ext: str) -> Tuple[bytes, str, bool]:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None

    with Image.open(io.BytesIO(data)) as im:
        # Downscale
        w, h = im.size
        max_dim = max(w, h)
        if max_dim > max_image_px:
            scale = max_image_px / float(max_dim)
            im = im.resize((max(1, int(w*scale)), max(1, int(h*scale))), Image.LANCZOS)

        # Pilih format
        has_alpha = ('A' in im.getbands())
        photographic = _is_photographic(im)
        ext = orig_ext.lower()

        out = io.BytesIO()
        if not has_alpha and photographic and (allow_png_to_jpeg or ext in ('.jpg','.jpeg','.webp')):
            im = im.convert('RGB')
            im.save(out, format='JPEG', quality=jpeg_quality, optimize=True, progressive=True, subsampling="4:2:0")
            return out.getvalue(), '.jpg', True
        else:
            params = {}
            if ext == '.png':
                # kompresi maksimal PNG (tetap lossless)
                params = {"optimize": True, "compress_level": 9}
            im.save(out, format=im.format or 'PNG', **params)
            return out.getvalue(), orig_ext, False

def _shrink_ooxml(doc_bytes: bytes, *, ooxml_kind: str, max_image_px: int, jpeg_quality: int,
                  allow_png_to_jpeg: bool, png_max_compress: bool) -> Optional[bytes]:
    bio_in = io.BytesIO(doc_bytes)
    with zipfile.ZipFile(bio_in, 'r') as zin:
        with tempfile.TemporaryDirectory() as tmp:
            zin.extractall(tmp)
            tmp_path = Path(tmp)

            # Cari folder media
            media_dirs = []
            for dirs in _OOXML_MEDIA_DIRS.values():
                for d in dirs:
                    if (tmp_path / d).exists():
                        media_dirs.append(tmp_path / d)

            rename_map: Dict[str, str] = {}

            for media_dir in media_dirs:
                for p in media_dir.glob("*"):
                    if not p.is_file() or p.suffix.lower() not in _IMAGE_EXTS:
                        continue
                    orig_bytes = p.read_bytes()
                    try:
                        new_bytes, new_ext, changed_ext = _resample_image_bytes(
                            orig_bytes,
                            max_image_px=max_image_px,
                            jpeg_quality=jpeg_quality,
                            allow_png_to_jpeg=allow_png_to_jpeg,
                            orig_ext=p.suffix.lower(),
                        )
                    except Exception:
                        continue

                    new_name = p.stem + (new_ext if changed_ext else p.suffix)
                    new_path = p.with_name(new_name)
                    if new_path.name != p.name:
                        rename_map[str(p).replace(str(tmp_path) + os.sep, '').replace('\\\\','/')] = \
                            str(new_path).replace(str(tmp_path) + os.sep, '').replace('\\\\','/')
                        p.unlink(missing_ok=True)
                        new_path.write_bytes(new_bytes)
                    else:
                        p.write_bytes(new_bytes)

            # Hapus thumbnail (sering bikin file bengkak)
            for thumb in [tmp_path / "docProps" / "thumbnail.jpeg", tmp_path / "docProps" / "thumbnail.jpg"]:
                if thumb.exists():
                    thumb.unlink()

            # Update .rels kalau ada rename image
            if rename_map:
                for rel in tmp_path.rglob("*.rels"):
                    txt = rel.read_text(encoding="utf-8")
                    for old, new in rename_map.items():
                        old_rel = old.split("/",1)[-1] if "/media/" in old else old
                        new_rel = new.split("/",1)[-1] if "/media/" in new else new
                        txt = txt.replace(old_rel, new_rel).replace(old, new)
                    rel.write_text(txt, encoding="utf-8")

                # Pastikan Content_Types mengenali jpg
                ct = tmp_path / "[Content_Types].xml"
                if ct.exists():
                    txt = ct.read_text(encoding="utf-8")
                    if "Extension=\"jpg\"" not in txt and "Extension='jpg'" not in txt:
                        insert_at = txt.find("</Types>")
                        if insert_at != -1:
                            txt = txt[:insert_at] + "<Default Extension=\"jpg\" ContentType=\"image/jpeg\"/>" + txt[insert_at:]
                            ct.write_text(txt, encoding="utf-8")

            # Repack ZIP
            bio_out = io.BytesIO()
            with zipfile.ZipFile(bio_out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
                for root, _, files in os.walk(tmp_path):
                    for f in files:
                        abs_p = Path(root) / f
                        arcname = str(abs_p.relative_to(tmp_path)).replace("\\","/")
                        zout.write(abs_p, arcname)
            return bio_out.getvalue()

# --- PDF ---------------------------------------------------------------------

def _shrink_pdf(pdf_bytes: bytes) -> Tuple[Optional[bytes], str]:
    """
    PyMuPDF -> pikepdf -> Ghostscript (CLI). Return (bytes or None, strategy)
    """
    # 1) PyMuPDF
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        out = io.BytesIO()
        doc.save(out, garbage=4, deflate=True, clean=True, linear=True)
        doc.close()
        data = out.getvalue()
        if len(data) < len(pdf_bytes):
            return data, "pymupdf"
    except Exception:
        pass

    # 2) pikepdf
    try:
        import pikepdf
        out = io.BytesIO()
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate, linearize=True)
        data = out.getvalue()
        if len(data) < len(pdf_bytes):
            return data, "pikepdf"
    except Exception:
        pass

    # 3) Ghostscript
    try:
        import subprocess, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "in.pdf"
            outp = Path(tmp) / "out.pdf"
            inp.write_bytes(pdf_bytes)
            cmd = [
                "gs","-sDEVICE=pdfwrite","-dPDFSETTINGS=/screen",
                "-dCompatibilityLevel=1.4",
                "-dColorImageDownsampleType=/Bicubic","-dColorImageResolution=144",
                "-dGrayImageDownsampleType=/Bicubic","-dGrayImageResolution=144",
                "-dMonoImageDownsampleType=/Subsample","-dMonoImageResolution=300",
                "-dNOPAUSE","-dQUIET","-dBATCH",
                f"-sOutputFile={outp}", str(inp)
            ]
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode == 0 and outp.exists():
                data = outp.read_bytes()
                if len(data) < len(pdf_bytes):
                    return data, "ghostscript"
    except Exception:
        pass

    return None, "noop"


# --- MIME helpers ------------------------------------------------------------

MIME_BY_EXT = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.docm': 'application/vnd.ms-word.document.macroEnabled.12',
    '.pptm': 'application/vnd.ms-powerpoint.presentation.macroEnabled.12',
    '.xlsm': 'application/vnd.ms-excel.sheet.macroEnabled.12',
}

def guess_mime(filename: str) -> str:
    ext = (os.path.splitext(filename)[1] or '').lower()
    return MIME_BY_EXT.get(ext, 'application/octet-stream')
