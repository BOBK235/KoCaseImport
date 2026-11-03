from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .config import settings
from .models import CaseMetadata
from .utils import is_official_ccourt_url, normalize_case_number


class CCourtError(RuntimeError):
    pass


class CCourtClient:
    ISEARCH_BASE = "https://isearch.ccourt.go.kr/"
    SEARCH_URL = urljoin(ISEARCH_BASE, "search.do")

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": settings.user_agent, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"})

    def lookup(self, case_number: str, pdf_url_override: str = "") -> CaseMetadata:
        case_number = normalize_case_number(case_number)
        meta = CaseMetadata(case_number=case_number)
        if settings.ccourt_openapi_url and settings.ccourt_service_key:
            try:
                api_meta = self._lookup_openapi(case_number)
                if api_meta:
                    meta = api_meta
            except Exception:
                # Official web fallback remains available even when the public-data API changes.
                pass

        web_meta = self._lookup_isearch(case_number)
        if web_meta:
            for field in ("case_name", "decision_date", "result", "source_page_url", "pdf_url", "doc_id"):
                if getattr(web_meta, field):
                    setattr(meta, field, getattr(web_meta, field))
            meta.raw.update(web_meta.raw)

        if pdf_url_override:
            if not is_official_ccourt_url(pdf_url_override):
                raise CCourtError("PDF URL override는 헌법재판소 공식 도메인만 허용합니다.")
            meta.pdf_url = pdf_url_override

        if not meta.case_name:
            meta.case_name = f"{case_number} 결정"
        if not meta.pdf_url:
            raise CCourtError(
                "공식 결정문 PDF URL을 찾지 못했습니다. iSearch 구조 변경 가능성이 있습니다. "
                "작업 화면의 PDF URL override에 헌법재판소 공식 PDF 주소를 넣어 다시 시도하세요."
            )
        return meta

    def _lookup_openapi(self, case_number: str) -> CaseMetadata | None:
        params = {
            "serviceKey": settings.ccourt_service_key,
            "pageNo": 1,
            "numOfRows": 100,
            "_type": "json",
            settings.ccourt_case_param: case_number,
        }
        r = self.s.get(settings.ccourt_openapi_url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        candidates = []

        def walk(obj):
            if isinstance(obj, dict):
                text = " ".join(str(v) for v in obj.values() if not isinstance(v, (dict, list)))
                if case_number in re.sub(r"\s+", "", text):
                    candidates.append(obj)
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)

        walk(data)
        if not candidates:
            return None
        item = max(candidates, key=lambda d: len(d))

        def pick(*names: str) -> str:
            low = {str(k).lower(): v for k, v in item.items()}
            for name in names:
                if name.lower() in low and low[name.lower()] not in (None, ""):
                    return str(low[name.lower()]).strip()
            return ""

        return CaseMetadata(
            case_number=case_number,
            case_name=pick("caseName", "eventNm", "caseNm", " 사건명 ".strip()),
            decision_date=pick("finalDate", "closeDate", "decisionDate", "종국일자"),
            result=pick("finalResult", "result", "종국결과"),
            source_page_url=settings.ccourt_openapi_url,
            raw={"openapi": item},
        )

    def _lookup_isearch(self, case_number: str) -> CaseMetadata | None:
        attempts = [
            {"category": "01", "query": case_number},
            {"category": "00", "query": case_number},
            {"category": "01", "searchWord": case_number},
            {"category": "00", "searchWord": case_number},
            {"category": "01", "keyword": case_number},
            {"category": "00", "keyword": case_number},
        ]
        candidate_urls: list[str] = []
        for params in attempts:
            for method in ("get", "post"):
                try:
                    r = getattr(self.s, method)(self.SEARCH_URL, params=params if method == "get" else None,
                                                 data=params if method == "post" else None, timeout=25)
                    if not r.ok:
                        continue
                    candidate_urls.extend(self._extract_view_urls(r.text, case_number))
                    if candidate_urls:
                        break
                except requests.RequestException:
                    continue
            if candidate_urls:
                break

        # As a second official-web strategy, case-number searches occasionally redirect to a view URL.
        seen = set()
        for url in candidate_urls:
            if url in seen:
                continue
            seen.add(url)
            try:
                meta = self._parse_view(url, case_number)
                if meta and meta.case_number == case_number:
                    return meta
            except requests.RequestException:
                continue
        return None

    def _extract_view_urls(self, html: str, case_number: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        scored: list[tuple[int, str]] = []
        for a in soup.select('a[href*="view.do"]'):
            href = a.get("href", "")
            if "docId=" not in href:
                continue
            context = " ".join(filter(None, [a.get_text(" ", strip=True), a.parent.get_text(" ", strip=True) if a.parent else ""]))
            score = 10 if case_number in re.sub(r"\s+", "", context) else 0
            if "판례" in context or "결정" in context:
                score += 1
            scored.append((score, urljoin(self.ISEARCH_BASE, href)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [url for _, url in scored[:10]]

    def _parse_view(self, url: str, case_number: str) -> CaseMetadata | None:
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text("\n", strip=True)
        compact = re.sub(r"\s+", "", text)
        if case_number not in compact:
            return None

        doc_id_match = re.search(r"[?&]docId=([^&]+)", url)
        case_name = ""
        decision_date = ""
        result = ""

        patterns_name = [
            rf"{re.escape(case_number)}\s*([^\n]{{2,120}})",
            r"사건명\s*[:：]?\s*([^\n]{2,120})",
        ]
        for pat in patterns_name:
            m = re.search(pat, text)
            if m:
                candidate = m.group(1).strip(" -|[]")
                if candidate and "종국" not in candidate:
                    case_name = candidate
                    break

        m = re.search(r"종국일자\s*[:：]?\s*(\d{4}[.\-/ ]+\d{1,2}[.\-/ ]+\d{1,2})", text)
        if m:
            decision_date = self._iso_date(m.group(1))
        if not decision_date:
            m = re.search(r"\[(\d{4}[.]\s*\d{1,2}[.]\s*\d{1,2}[.]?)\s*" + re.escape(case_number), text)
            if m:
                decision_date = self._iso_date(m.group(1))
        m = re.search(r"종국결과\s*[:：]?\s*([^\n]{1,40})", text)
        if m:
            result = m.group(1).strip()

        pdf_urls = []
        for tag in soup.find_all(["a", "iframe", "embed", "object"]):
            for attr in ("href", "src", "data"):
                value = tag.get(attr)
                if not value:
                    continue
                abs_url = urljoin(url, value)
                if ".pdf" in abs_url.lower() and is_official_ccourt_url(abs_url):
                    pdf_urls.append(abs_url)
        if not pdf_urls:
            for m in re.finditer(r"https?://[^\"'<>\s]+\.pdf(?:\?[^\"'<>\s]*)?", r.text, re.I):
                if is_official_ccourt_url(m.group(0)):
                    pdf_urls.append(m.group(0))
        if not pdf_urls:
            for m in re.finditer(r"download\.do\?filePath=[^\"'<>\s]+", r.text, re.I):
                pdf_urls.append(urljoin(url, m.group(0).replace("&amp;", "&")))

        return CaseMetadata(
            case_number=case_number,
            case_name=case_name,
            decision_date=decision_date,
            result=result,
            source_page_url=url,
            pdf_url=pdf_urls[0] if pdf_urls else "",
            doc_id=doc_id_match.group(1) if doc_id_match else "",
            raw={"isearch_view": url},
        )

    @staticmethod
    def _iso_date(value: str) -> str:
        nums = re.findall(r"\d+", value)
        if len(nums) >= 3:
            return f"{int(nums[0]):04d}-{int(nums[1]):02d}-{int(nums[2]):02d}"
        return value.strip()

    def download_pdf(self, meta: CaseMetadata, dest: Path) -> Path:
        if not is_official_ccourt_url(meta.pdf_url):
            raise CCourtError("비공식 도메인의 PDF 다운로드는 차단되었습니다.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.s.get(meta.pdf_url, stream=True, timeout=90) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        with dest.open("rb") as f:
            sig = f.read(5)
        if sig != b"%PDF-":
            dest.unlink(missing_ok=True)
            raise CCourtError("다운로드한 파일이 PDF가 아닙니다.")
        return dest
