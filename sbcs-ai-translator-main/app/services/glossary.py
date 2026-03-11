# app/services/glossary.py
from __future__ import annotations

import io
import csv
import re
import os
import json
from typing import Iterable, Set, Optional, Tuple, List

# ============================================================
# Util sederhana pemilih target language
# ============================================================
def _pick(target_lang: str, en: str, ja: str, zh: str) -> str:
    t = (target_lang or "").lower()
    if t in ("ja", "jp"):
        return ja
    if t in ("zh", "zh-cn", "zh-hans", "zh-sg", "zh-my"):
        return zh  # default ke simplified
    return en


# ============================================================
# 1) INDOFIX: mapping khusus ID -> (EN/JA/ZH) sesuai permintaan
# ============================================================
def build_indofix_rows(target_lang: str) -> list[tuple[str, str]]:
    pairs = {
        "detik-detik": _pick(target_lang, "moments", "瞬間", "时刻"),
        "saat ini": _pick(target_lang, "currently", "現在", "目前"),
        "triwulan": _pick(target_lang, "quarter", "四半期", "季度"),
        "kuartal": _pick(target_lang, "quarter", "四半期", "季度"),
        "laporan keuangan": _pick(target_lang, "financial report", "財務報告書", "财务报告"),
        "neraca": _pick(target_lang, "balance sheet", "貸借対照表", "资产负债表"),
        "rugi laba": _pick(target_lang, "profit and loss", "損益計算書", "损益表"),
        "pendapatan": _pick(target_lang, "revenue", "収益", "收入"),
        "keuntungan": _pick(target_lang, "profit", "利益", "利润"),
        "investasi": _pick(target_lang, "investment", "投資", "投资"),
        "perusahaan": _pick(target_lang, "company", "会社", "公司"),
        "korporasi": _pick(target_lang, "corporation", "企業", "企业"),
    }
    return [(src, dst) for src, dst in pairs.items()]


# ============================================================
# 2) NO-TRANSLATE terms (currency, ticker, angka, dsb)
# ============================================================
NO_TRANSLATE_TERMS: Set[str] = {
    "USD/IDR", "USD/JPY", "USD/EUR", "EUR/USD", "JPY/USD",
    "IDR", "USD", "EUR", "JPY", "GBP", "CNY", "HKD", "SGD",
    "AUD", "CAD", "CHF", "KRW", "THB", "MYR", "TWD", "INR",
}

NO_TRANSLATE_REGEXES: list[re.Pattern] = [
    re.compile(r"\b[A-Z]{2,5}\d{0,4}\b"),           # simbol saham sederhana (e.g., TSLA, BBCA)
    re.compile(r"\b\d{1,3}(,\d{3})*(\.\d+)?\b"),    # angka dengan koma/titik
]

def build_no_translate_entries() -> list[str]:
    return sorted(NO_TRANSLATE_TERMS)

def looks_no_translate(token: str) -> bool:
    if token in NO_TRANSLATE_TERMS:
        return True
    return any(rgx.search(token) for rgx in NO_TRANSLATE_REGEXES)


# ============================================================
# 3) Export glossary CSV (kompatibel format lama) – header: source,target
# ============================================================
def build_glossary_csv_bytes(
    target_lang: str,
    extra_pairs: Iterable[tuple[str, str]] | None = None
) -> bytes:
    """
    Kembalikan konten CSV: "source,target" (comma-separated).
    Jika butuh TSV untuk Document Translation, lihat compose_glossary_tsv() di bawah.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["source", "target"])
    for src, dst in build_indofix_rows(target_lang):
        w.writerow([src, dst])
    if extra_pairs:
        for src, dst in extra_pairs:
            if src and dst:
                w.writerow([src, dst])
    return buf.getvalue().encode("utf-8")


# ============================================================
# 4) Auto-glossary via Azure OpenAI (opsional)
#    Mengembalikan pairs (source, target_en) bila tersedia
# ============================================================
async def auto_glossary_pairs_from_text(
    sample_text: str,
    target_lang: str,
    *,
    timeout: float = 30.0
) -> list[tuple[str, str]]:
    """
    Gunakan Azure OpenAI untuk ekstraksi istilah → target (default EN).
    Aman bila ENV tidak di-set; akan mengembalikan [].
    """
    import httpx

    _AZ_OAI_KEY = os.getenv("AZURE_OPENAI_API_KEY")
    _AZ_OAI_BASE = (
        os.getenv("AZURE_OPENAI_ENDPOINT")
        or os.getenv("AZURE_OPENAI_BASE")
        or os.getenv("AZURE_OPENAI_API_BASE")
    )
    _AZ_OAI_DEPLOYMENT = (
        os.getenv("AZURE_OPENAI_DEPLOYMENT")
        or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")
        or "gpt-4o-mini"
    )
    _AZ_OAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

    if not (_AZ_OAI_KEY and _AZ_OAI_BASE):
        return []

    prompt = (
        "You are a translation terminologist. From the given text, extract up to 50 domain terms "
        "that should be consistently translated. Return JSON list of objects: "
        '[{"source":"...","target_en":"..."}]. '
        "Avoid currencies, stock tickers, numbers, dates, or items that should remain untranslated."
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        url = f"{_AZ_OAI_BASE}/openai/deployments/{_AZ_OAI_DEPLOYMENT}/chat/completions?api-version={_AZ_OAI_API_VERSION}"
        headers = {"api-key": _AZ_OAI_KEY, "Content-Type": "application/json"}
        payload = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": (sample_text or "")[:8000]},
            ],
            "temperature": 0.2,
            "max_tokens": 1000,
        }
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        try:
            arr = json.loads(content)
        except Exception:
            return []

    pairs: list[tuple[str, str]] = []
    for it in arr:
        s = (it.get("source") or "").strip()
        t = (it.get("target_en") or "").strip()
        if not s or not t:
            continue
        if looks_no_translate(s):
            continue
        pairs.append((s, t))
    return pairs


# ============================================================
# 5) TSV Glossary ala Streamlit (selalu dibuat)
#    - base no-translate (currency/tech/quarter/company)
#    - indofix (ID→EN/JA/ZH) & jafix (JA→EN/ID/ZH)
#    - + auto pairs dari Azure OpenAI (kalau ada)
# ============================================================

# Basis daftar yang tidak diterjemahkan (X -> X)
_BASE_CURRENCIES = [
    "USD","EUR","JPY","IDR","GBP","AUD","CAD","CHF","CNY","HKD",
    "SGD","KRW","THB","MYR","PHP","VND","TWD","INR","BRL","ZAR"
]
_BASE_TECH = [
    "API","URL","HTTP","HTTPS","JSON","XML","CSV","PDF","HTML",
    "CEO","CFO","CTO","CMO","VP","SVP","MD","GM","PM",
    "AI","ML","IoT","SaaS","PaaS","IaaS","CRM","ERP","ROI",
    "KPI","SLA","NDA","IPO","M&A","B2B","B2C","P&L"
]
_BASE_TIME = ["Q1","Q2","Q3","Q4","FY","YTD","MTD","QTD","1Q","2Q","3Q","4Q","H1","H2"]
_BASE_COMPANIES = [
    "SBCS","PT SBCS","SMBC","Bank Indonesia","BI",
    "Microsoft","Google","Apple","Amazon","Meta","Toyota","Honda","Nissan","Sony","Samsung"
]

def _id_indofix_pairs(target_lang: str) -> list[Tuple[str, str]]:
    """ID → (EN/JA/ZH) mapping khusus, sama seperti build_indofix_rows() tapi untuk compose TSV."""
    return build_indofix_rows(target_lang)

def _ja_indofix(target_lang: str) -> list[Tuple[str, str]]:
    """JA → (EN/ID/ZH) mapping tambahan (opsional)."""
    t = (target_lang or "").lower()
    def pick(en: str, id_: str, zh: str) -> str:
        if t in ("en","en-us","en-gb"):
            return en
        if t in ("id","in"):
            return id_
        return zh
    pairs = {
        "数秒": pick("moments","beberapa saat","片刻"),
        "瞬間": pick("moments","saat-saat","瞬间"),
        "時点": pick("point in time","titik waktu","时点"),
        "四半期": pick("quarter","triwulan","季度"),
        "年度": pick("fiscal year","tahun fiskal","年度"),
        "会社": pick("company","perusahaan","公司"),
        "企業": pick("corporation","korporasi","企业"),
    }
    return list(pairs.items())

def _always_pairs(source_lang: str, target_lang: str) -> list[Tuple[str, str]]:
    pairs: list[Tuple[str, str]] = []
    # No-translate base
    for x in _BASE_CURRENCIES + _BASE_TECH + _BASE_TIME + _BASE_COMPANIES:
        pairs.append((x, x))
    # Indo → (en/ja/zh)
    if (source_lang or "").lower() in ("id", "in"):
        pairs.extend(_id_indofix_pairs(target_lang))
    # Jepang → (en/id/zh)
    if (source_lang or "").lower().startswith("ja"):
        pairs.extend(_ja_indofix(target_lang))
    return pairs

def _to_tsv(pairs: list[Tuple[str, str]]) -> bytes:
    """Konversi pairs ke TSV (header: source\ttarget), unik & non-kosong."""
    seen = set()
    rows: list[str] = ["source\ttarget"]
    for s, t in pairs:
        s = (s or "").strip()
        t = (t or "").strip()
        if not s or not t:
            continue
        key = (s.lower(), t.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(f"{s}\t{t}")
    return ("\n".join(rows)).encode("utf-8")

async def build_auto_pairs_with_openai(sample_text: str, target_lang: str) -> list[Tuple[str, str]]:
    """
    Versi lain (langsung target_lang), kompatibel dengan compose_glossary_tsv().
    Mengembalikan (source, target) sesuai target_lang. Jika OpenAI tidak tersedia -> [].
    """
    import httpx

    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    base = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE_OPENAI_BASE") or os.getenv("AZURE_OPENAI_API_BASE")
    dep = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT") or "gpt-4o-mini"
    ver = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")
    if not (api_key and base):
        return []

    prompt = (
        "From the given text, extract up to 50 domain terms needing consistent translation. "
        f"Translate to target language '{target_lang}'. "
        "Return JSON array like: [{\"source\":\"...\",\"target\":\"...\"}]. "
        "Avoid currencies/tickers/numbers/dates."
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        url = f"{base}/openai/deployments/{dep}/chat/completions?api-version={ver}"
        r = await client.post(url, headers={"api-key": api_key, "Content-Type": "application/json"}, json={
            "messages": [
                {"role":"system","content": prompt},
                {"role":"user","content": (sample_text or "")[:8000]}
            ],
            "temperature": 0.2,
            "max_tokens": 1000
        })
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        try:
            arr = json.loads(content)
        except Exception:
            return []

    pairs: list[Tuple[str, str]] = []
    for it in arr:
        s = (it.get("source") or "").strip()
        t = (it.get("target") or "").strip()
        if s and t and not looks_no_translate(s):
            pairs.append((s, t))
    return pairs

async def compose_glossary_tsv(
    source_lang: str,
    target_lang: str,
    sample_text: str = ""
) -> bytes:
    """
    Selalu mengembalikan TSV glossary yang siap dipakai Document Translation (format="TSV"):
      - base no-translate + kode umum
      - indofix/jafix mapping (sesuai arah bahasa)
      - + auto-pairs dari Azure OpenAI (jika tersedia)
    """
    base = _always_pairs(source_lang, target_lang)
    auto_extra: list[Tuple[str, str]] = []
    try:
        if sample_text:
            # Pakai versi target langsung (lebih akurat untuk bahasa non-EN)
            auto_extra = await build_auto_pairs_with_openai(sample_text, target_lang)
    except Exception:
        auto_extra = []
    return _to_tsv(base + auto_extra)
