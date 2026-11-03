from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

CASE_RE = re.compile(r"^(?P<year>\d{2,4})\s*(?P<type>헌[가나다라마바사아]|헌마|헌바|헌사|헌아)\s*(?P<num>\d+)(?P<etc>등)?$")


def normalize_case_number(value: str) -> str:
    value = re.sub(r"[\s\-]", "", value.strip())
    m = CASE_RE.match(value)
    if not m:
        raise ValueError("헌법재판소 사건번호 형식이 아닙니다. 예: 2016헌나1, 2024헌마123등")
    year = m.group("year")
    if len(year) == 2:
        year = "19" + year if int(year) >= 80 else "20" + year
    return f"{year}{m.group('type')}{m.group('num')}{m.group('etc') or ''}"


def sanitize_filename(value: str, max_len: int = 180) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r"[\\/:*?\"<>|#\[\]{}]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    if not value:
        value = "헌법재판소 결정문"
    return value[:max_len].rstrip()


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_official_ccourt_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "ccourt.go.kr" or host.endswith(".ccourt.go.kr")


def clean_extracted_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def text_is_usable(text: str, min_chars: int = 80) -> bool:
    s = re.sub(r"\s+", "", text or "")
    if len(s) < min_chars:
        return False
    printable = sum(ch.isprintable() for ch in s) / max(len(s), 1)
    semantic = sum(
        ("가" <= ch <= "힣") or ch.isascii() and ch.isalnum()
        for ch in s
    ) / max(len(s), 1)
    return printable > 0.95 and semantic > 0.55
