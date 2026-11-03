from __future__ import annotations

import requests
from pypdf import PdfReader

from .config import settings
from .mediawiki import MediaWikiClient
from .utils import clean_extracted_text, text_is_usable


class OCRClient:
    endpoints = ("https://ocr.wmcloud.org/api", "https://ocr.wmcloud.org/api.php")

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": settings.user_agent})

    def extract_pdf_text(self, pdf_path) -> list[str]:
        reader = PdfReader(str(pdf_path))
        out = []
        for page in reader.pages:
            try:
                out.append(clean_extracted_text(page.extract_text() or ""))
            except Exception:
                out.append("")
        return out

    def ocr_wikimedia(self, image_url: str) -> str:
        params = {
            "engine": settings.ocr_engine,
            "image": image_url,
            "psm": settings.ocr_psm,
            "langs[]": settings.ocr_lang,
            "uselang": "ko",
        }
        last_error = None
        for endpoint in self.endpoints:
            try:
                r = self.s.get(endpoint, params=params, timeout=settings.ocr_timeout)
                r.raise_for_status()
                data = r.json()
                text = clean_extracted_text(data.get("text", ""))
                if text:
                    return text
            except Exception as e:
                last_error = e
        raise RuntimeError(f"Wikimedia OCR 실패: {last_error}")

    def transcribe_pages(self, pdf_path, filename: str, wikisource: MediaWikiClient, logger=lambda s: None):
        extracted = self.extract_pdf_text(pdf_path)
        results: list[tuple[str, str]] = []
        for i, text in enumerate(extracted, start=1):
            if text_is_usable(text, settings.ocr_min_text_chars):
                results.append((text, "pdf-text"))
                logger(f"{i}/{len(extracted)}: PDF 텍스트 레이어 사용")
                continue
            page_title = f"페이지:{filename}/{i}"
            image_url = wikisource.image_for_page(page_title)
            if not image_url:
                raise RuntimeError(f"{page_title}의 스캔 이미지 URL을 찾지 못했습니다.")
            ocr_text = self.ocr_wikimedia(image_url)
            results.append((ocr_text, "wikimedia-ocr"))
            logger(f"{i}/{len(extracted)}: Wikimedia OCR 사용")
        return results
