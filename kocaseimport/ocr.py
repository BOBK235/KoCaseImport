"""Per-page Wikimedia OCR first, then real local PyOCR/Tesseract."""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

import requests
from pypdf import PdfReader

from .config import settings
from .errors import PublicJobError
from .mediawiki import MediaWikiClient, MediaWikiError
from .utils import clean_extracted_text


class RemoteOCRFailure(RuntimeError):
    """A fixed reason code, never a URL, raw response or exception message."""
    def __init__(self, reason):
        allowed = {'image_address', 'http_error', 'timeout', 'response_limit',
                   'invalid_response', 'no_text', 'connection', 'image_unavailable'}
        super().__init__(reason if reason in allowed else 'invalid_response')


class OCRClient:
    endpoint = 'https://ocr.wmcloud.org/api'

    def __init__(self) -> None:
        # Public OCR traffic must not inherit the user's Wikimedia OAuth token.
        self.s = requests.Session()
        self.s.trust_env = False
        self.s.headers.update({'User-Agent': settings.user_agent})

    def ocr_wikimedia(self, image_url: str) -> str:
        try:
            parsed = urlsplit(image_url)
            allowed = (len(image_url) <= 4096 and parsed.scheme == 'https'
                       and not any(ord(c) <= 32 or ord(c) == 127 for c in image_url)
                       and parsed.hostname in {'upload.wikimedia.org', 'thumb.wikimedia.org'}
                       and parsed.port in {None, 443} and not parsed.username
                       and not parsed.password and not parsed.fragment)
        except (TypeError, ValueError):
            allowed = False
        if not allowed:
            raise RemoteOCRFailure('image_address')
        languages = settings.ocr_lang.replace('+', ',').split(',')
        params = {'engine': settings.ocr_engine, 'image': image_url,
                  'psm': max(3, min(settings.ocr_psm, 13)),
                  'langs[]': [x.strip() for x in languages if x.strip()], 'uselang': 'ko'}
        deadline = time.monotonic() + max(1, min(settings.ocr_timeout, 120))
        try:
            with self.s.get(self.endpoint, params=params, stream=True, allow_redirects=False,
                            timeout=(10, max(1, min(settings.ocr_timeout, 120)))) as response:
                if response.status_code != 200:
                    raise RemoteOCRFailure('http_error')
                raw = bytearray()
                for chunk in response.iter_content(16384):
                    if time.monotonic() > deadline:
                        raise RemoteOCRFailure('timeout')
                    if len(raw) + len(chunk) > settings.ocr_response_bytes:
                        raise RemoteOCRFailure('response_limit')
                    raw.extend(chunk)
                data = json.loads(raw)
            if not isinstance(data, dict) or data.get('error') or not isinstance(data.get('text'), str):
                raise RemoteOCRFailure('invalid_response')
            text = clean_extracted_text(data['text'])
            if not text:
                raise RemoteOCRFailure('no_text')
            if len(text.encode('utf-8')) > settings.ocr_page_text_bytes:
                raise RemoteOCRFailure('response_limit')
            return text
        except requests.Timeout:
            raise RemoteOCRFailure('timeout') from None
        except requests.RequestException:
            raise RemoteOCRFailure('connection') from None
        except (ValueError, RecursionError):
            raise RemoteOCRFailure('invalid_response') from None

    def ocr_local(self, pdf_path, page_number, guard):
        from .local_ocr import recognize_page
        return recognize_page(pdf_path, page_number, settings, guard=guard)

    def transcribe_pages(self, pdf_path, filename: str, wikisource: MediaWikiClient, logger=lambda s: None):
        # Page count only. A text layer must never bypass the primary OCR.
        with open(pdf_path, 'rb') as source:
            page_count = len(PdfReader(source).pages)
        if not 0 < page_count <= settings.max_pdf_pages:
            raise PublicJobError(code='pdf_page_limit')
        results: list[tuple[str, str]] = []
        total_bytes = 0
        try:
            for number in range(1, page_count + 1):
                wikisource._authorize()  # Stop revoked/expired or fenced jobs.
                try:
                    image_url = wikisource.image_for_page(f'페이지:{filename}/{number}')
                    if not image_url:
                        raise RemoteOCRFailure('image_unavailable')
                    text = self.ocr_wikimedia(image_url)
                    origin = 'wikimedia-ocr'
                except (RemoteOCRFailure, requests.RequestException, MediaWikiError, ValueError) as error:
                    if isinstance(error, MediaWikiError) and error.authentication_failed:
                        raise PublicJobError(code='auth_expired', page_number=number) from None
                    if isinstance(error, requests.HTTPError) and error.response is not None and error.response.status_code in {401, 403}:
                        raise PublicJobError(code='auth_expired', page_number=number) from None
                    # Never catch AuthError, LeaseLost, DatabaseUnavailable,
                    # MemoryError, or process cancellation as OCR fallback.
                    reason = str(error) if isinstance(error, RemoteOCRFailure) else 'image_lookup'
                    logger(f'{number}/{page_count}: Wikimedia OCR 실패({reason}), PyOCR로 전환')
                    wikisource._authorize()
                    try:
                        text, origin = self.ocr_local(pdf_path, number, wikisource._authorize)
                    except PublicJobError as local_error:
                        raise PublicJobError(code=local_error.code, page_number=number) from None
                wikisource._authorize()
                total_bytes += len(text.encode('utf-8'))
                if total_bytes > settings.ocr_total_text_bytes:
                    raise PublicJobError(code='ocr_result_limit', page_number=number)
                results.append((text, origin))
                label = 'Wikimedia OCR' if origin == 'wikimedia-ocr' else 'PyOCR (빈 쪽)' if origin == 'pyocr-blank' else 'PyOCR/Tesseract'
                logger(f'{number}/{page_count}: {label} 사용')
            return results
        finally:
            self.s.close()
