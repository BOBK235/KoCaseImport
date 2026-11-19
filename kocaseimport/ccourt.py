from __future__ import annotations
from .resources import check_storage_lock

from dataclasses import replace
from heapq import heappop, heappush
from itertools import count
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests

from .config import settings
from .errors import PublicJobError, failure_record
from .models import CaseMetadata
from .utils import is_official_ccourt_url, normalize_case_number


class CCourtError(PublicJobError):
    def __init__(self, message='', *, code='lookup_response', **context):
        super().__init__(message, code=code, **context)


class CCourtClient:
    ISEARCH_BASE = "https://isearch.ccourt.go.kr/"
    VIEW_API = ISEARCH_BASE + "api/index/searcher/view"

    def __init__(self, on_case=None) -> None:
        self.on_case = on_case or (lambda: None)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": settings.user_agent,
                               "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"})

    @staticmethod
    def _official(url: str) -> bool:
        p = urlsplit(url)
        return (p.scheme in {"https", "http"} and not p.username and not p.password
                and is_official_ccourt_url(url))

    def _json(self, url: str, params: dict, label: str) -> dict:
        # Request exception URLs can contain serviceKey; never propagate them.
        try:
            with self.s.get(url, params=params, timeout=30, allow_redirects=False) as r:
                if r.status_code != 200:
                    raise CCourtError(f"{label}: HTTP {r.status_code}. 요청 주소와 접근 상태를 확인하세요.",
                                      code='lookup_http', http_status=r.status_code)
                try:
                    data = r.json()
                except ValueError:
                    raise CCourtError(f"{label}: JSON 응답이 아닙니다. API 응답 형식을 확인하세요.") from None
        except requests.RequestException as exc:
            code = ('lookup_timeout' if isinstance(exc, requests.Timeout) else
                    'lookup_tls' if isinstance(exc, requests.exceptions.SSLError) else 'lookup_connection')
            raise CCourtError(f"{label}: 통신 실패 ({type(exc).__name__}).", code=code) from None
        if not isinstance(data, dict):
            raise CCourtError(f"{label}: 응답이 JSON 객체가 아닙니다.")
        return data

    @staticmethod
    def _same_case(value: str, expected: str) -> bool:
        try:
            members = [normalize_case_number(part).removesuffix('등')
                       for part in re.split(r'[/,·ㆍ․]', value)]
            return normalize_case_number(expected).removesuffix('등') in members
        except (ValueError, AttributeError, TypeError):
            return False

    def lookup(self, case_number: str, pdf_url_override: str = "") -> CaseMetadata:
        return next(self.pdf_candidates(case_number, pdf_url_override))

    def pdf_candidates(self, case_number: str, pdf_url_override: str = ""):
        case_number = normalize_case_number(case_number)
        if pdf_url_override and not self._official(pdf_url_override):
            raise CCourtError("PDF URL override는 헌법재판소 공식 HTTP(S) 주소만 허용합니다.", code='pdf_address_blocked')
        if bool(settings.ccourt_openapi_url) != bool(settings.ccourt_service_key):
            raise CCourtError("CCOURT_OPENAPI_URL과 CCOURT_SERVICE_KEY를 함께 설정하세요.", code='lookup_configuration')
        meta = (self._lookup_openapi(case_number) if settings.ccourt_openapi_url
                else CaseMetadata(case_number=case_number))
        if meta.raw.get("openapi"):
            self.on_case()
        if pdf_url_override:
            meta.pdf_url = pdf_url_override
            meta.case_name = meta.case_name or f"{case_number} 결정"
            meta.raw["source_edition"] = "사용자 지정 PDF"
            yield meta
            return
        yield from self._edition_candidates(meta)

    def _lookup_openapi(self, case_number: str) -> CaseMetadata:
        params = {"serviceKey": settings.ccourt_service_key,
                  settings.ccourt_case_param: case_number.removesuffix("등"),
                  "pageNo": 1, "numOfRows": 100, "type": "json"}
        data = self._json(settings.ccourt_openapi_url, params, "헌재 OpenAPI")
        header = data.get("header")
        if not isinstance(header, dict) or "resultCode" not in header:
            raise CCourtError("헌재 OpenAPI: header.resultCode가 없습니다. getIncdntDetail 응답을 확인하세요.")
        code = str(header["resultCode"])
        if code not in {"0", "00"}:
            # Do not echo arbitrary response messages that could contain credentials.
            safe_code = re.sub(r"[^A-Za-z0-9_-]", "", code)[:24]
            raise CCourtError(f"헌재 OpenAPI 오류: resultCode={safe_code}. 인증·요청변수·호출 한도를 확인하세요.",
                              code='api_rejected', api_code=code)
        body = data.get("body")
        if not isinstance(body, dict):
            raise CCourtError("헌재 OpenAPI: body가 없습니다.")
        items = body.get("items") or {}
        if not isinstance(items, dict):
            raise CCourtError("헌재 OpenAPI: body.items 형식이 예상과 다릅니다.")
        rows = items.get("item") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise CCourtError("헌재 OpenAPI: body.items.item 형식이 예상과 다릅니다.")
        matches = [x for x in rows if isinstance(x, dict)
                   and self._same_case(x.get("eventNo", ""), case_number)]
        if len(matches) != 1:
            raise CCourtError(f"헌재 OpenAPI: 요청한 사건과 정확히 일치하는 항목이 {len(matches)}개입니다. eventNo를 확인하세요.",
                              code='case_not_found' if not matches else 'case_ambiguous')
        item = matches[0]
        return CaseMetadata(
            case_number=case_number,
            case_name=str(item.get("eventNm") or "").strip(),
            decision_date=self._iso_date(str(item.get("endDate") or item.get("adjuDate") or "")),
            result=str(item.get("endRsta") or "").strip(),
            source_page_url=str(item.get("dtmPpHp") or "").strip(),
            raw={"openapi": item},
        )

    def _view(self, url: str, case_number: str) -> dict:
        params = {"idx": "00", "reSearch": json.dumps({"flag": "false", "reKeyword": ""})}
        if url:
            p = urlsplit(url)
            if p.scheme not in {"http", "https"} or p.hostname != "isearch.ccourt.go.kr" or p.path != "/view.do":
                raise CCourtError("헌재 상세 화면 주소가 예상한 iSearch view.do 주소가 아닙니다.", code='lookup_address')
            q = parse_qs(p.query)
            doc_id = q.get("docId", [""])[0]
            if not re.fullmatch(r"\d+_010(?:500|300|200)", doc_id) or q.get("idx", ["00"])[0] != "00":
                raise CCourtError("헌재 상세 화면의 docId 또는 idx를 확인하세요.", code='lookup_address')
            params["docId"] = doc_id
        else:
            params["eventNo"] = case_number.removesuffix("등")
        data = self._json(self.VIEW_API, params, "헌재 iSearch 상세 조회")
        obj = data.get("returnObject")
        item = obj.get("00") if isinstance(obj, dict) else None
        if data.get("success") is not True or not isinstance(item, dict):
            raise CCourtError("헌재 iSearch: 결정문 상세 데이터를 찾지 못했습니다.")
        if not self._same_case(item.get("eventNo", ""), case_number):
            raise CCourtError("헌재 iSearch: 응답의 사건번호가 요청과 일치하지 않습니다.", code='case_mismatch')
        if params.get("docId") and item.get("docId") != params["docId"]:
            raise CCourtError("헌재 iSearch: 응답의 문서 식별자가 요청과 일치하지 않습니다.", code='case_mismatch')
        self.on_case()
        return item

    def _resolve_pdf(self, meta: CaseMetadata) -> CaseMetadata:
        return next(self._edition_candidates(meta))

    def _edition_candidates(self, meta: CaseMetadata):
        editions = ('결정문', '판례집', '공보')
        relations = ('relationDecisionInfo', 'relationCaseCollectionInfo', 'relationPublishInfo')
        pending, ready = [], []
        sequence = count()
        scheduled, seen_docs = set(), set()
        errors, unavailable, missing_editions, hwp_editions = [], [], [], []
        yielded = False

        def schedule(rank, url):
            if url not in scheduled:
                scheduled.add(url)
                heappush(pending, (rank, next(sequence), url))

        schedule(0, meta.source_page_url)
        api_item = meta.raw.get('openapi', {})
        if settings.ccourt_allow_published_pdf:
            for rank, (key, link) in enumerate((('pdntList', 'pdntHp'), ('ogList', 'ogHp')), 1):
                rows = api_item.get(key) or []
                if isinstance(rows, dict):
                    rows = [rows]
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and isinstance(row.get(link), str) and row[link]:
                            schedule(rank, row[link])

        while pending or ready:
            # A case-number lookup may return a collection or gazette first.
            # Resolve links to higher priority editions before using its PDF.
            if ready and (not pending or ready[0][0] <= pending[0][0]):
                yielded = True
                yield heappop(ready)[2]
                continue
            _, _, url = heappop(pending)
            try:
                item = self._view(url, meta.case_number)
            except CCourtError as exc:
                errors.append(exc)
                continue
            edition = item.get('name')
            if edition not in editions:
                errors.append(CCourtError("헌재 iSearch: 지원하지 않는 자료 유형입니다."))
                continue
            doc_id = item.get('docId', '')
            if doc_id:
                if doc_id in seen_docs:
                    continue
                seen_docs.add(doc_id)
            for rank, key in enumerate(relations):
                if rank and not settings.ccourt_allow_published_pdf:
                    break
                value = item.get(key)
                related = value.split('||', 1)[0] if isinstance(value, str) else ''
                suffix = ('010500', '010300', '010200')[rank]
                if re.fullmatch(r'[0-9]+_' + suffix, related) and related not in seen_docs:
                    schedule(rank, self.ISEARCH_BASE + 'view.do?' + urlencode({'idx':'00', 'docId':related}))
            if edition != editions[0] and not settings.ccourt_allow_published_pdf:
                continue
            token = item.get("pdfFilePath")
            if isinstance(token, str) and token.strip():
                candidate = replace(meta, doc_id=doc_id,
                    pdf_url=self.ISEARCH_BASE + 'downloadEX.do?' + urlencode({'filePath': token}),
                    source_page_url=self.ISEARCH_BASE + 'view.do?' + urlencode({'idx': '00', 'docId': doc_id}),
                    case_name=meta.case_name or str(item.get('eventName') or meta.case_number).strip(),
                    decision_date=meta.decision_date or self._iso_date(str(item.get('date') or '')),
                    result=meta.result or str(item.get('endRsta') or '').strip(),
                    raw={**meta.raw, 'source_edition': edition,
                         'pdf_resolution': 'isearch pdfFilePath → downloadEX.do',
                         'lookup_warnings': [str(e) for e in errors]})
                heappush(ready, (editions.index(edition), next(sequence), candidate))
            else:
                unavailable.append(f"{edition}: PDF 없음")
                missing_editions.append(edition)
                if item.get('hwpFilePath'):
                    hwp_editions.append(edition)
        if yielded and not errors:
            return
        details = "; ".join(unavailable + [str(e) for e in errors])
        code = ('pdf_lookup_incomplete' if errors else
                'pdf_missing' if settings.ccourt_allow_published_pdf else 'pdf_alternatives_disabled')
        raise CCourtError("공식 PDF를 확보하지 못했습니다. " + details
                          + ". XML/HWP는 PDF로 변환하거나 업로드하지 않았습니다. 공식 PDF URL override를 사용할 수 있습니다.",
                          code=code, missing_editions=missing_editions, hwp_editions=hwp_editions,
                          causes=[failure_record(e.code, **e.context) for e in errors])

    @staticmethod
    def _iso_date(value: str) -> str:
        if not value.strip():
            return ""
        digits = re.sub(r"\D", "", value)
        try:
            if len(digits) != 8:
                raise ValueError
            return datetime.strptime(digits, "%Y%m%d").date().isoformat()
        except ValueError:
            raise CCourtError("헌재 응답의 날짜 형식이 YYYYMMDD가 아니거나 유효하지 않습니다.") from None

    def download_pdf(self, meta: CaseMetadata, dest: Path) -> Path:
        from .resources import check_storage_lock, download_budget
        with download_budget(settings) as budget:
            return self._download_pdf(meta, dest, budget)

    def _download_pdf(self, meta: CaseMetadata, dest: Path, budget: int) -> Path:
        import time
        deadline = time.monotonic() + settings.download_seconds
        url = meta.pdf_url
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_suffix(dest.suffix + ".part")
        size_code = 'pdf_size_limit' if budget >= settings.max_pdf_bytes else 'work_storage_full'
        try:
            for _ in range(6):
                if not self._official(url):
                    raise CCourtError("비공식 도메인 또는 비HTTP(S) PDF 다운로드는 차단되었습니다.", code='pdf_address_blocked')
                with self.s.get(url, stream=True, timeout=90, allow_redirects=False) as r:
                    if r.status_code in {301, 302, 303, 307, 308}:
                        if not r.headers.get("Location"):
                            raise CCourtError("PDF 다운로드: 리디렉션 주소가 없습니다.", code='pdf_redirect')
                        url = urljoin(url, r.headers["Location"])
                        continue
                    if r.status_code != 200:
                        raise CCourtError(f"PDF 다운로드: HTTP {r.status_code}.",
                                          code='pdf_http_missing' if r.status_code in {404, 410} else 'pdf_http',
                                          http_status=r.status_code)
                    length = r.headers.get('Content-Length', '')
                    if length.isdigit() and int(length) > budget:
                        raise CCourtError('PDF 크기 또는 보관 공간 한도를 초과했습니다.', code=size_code)
                    received = 0
                    with temporary.open("wb") as f:
                        for chunk in r.iter_content(256 * 1024):
                            check_storage_lock()
                            if time.monotonic() > deadline:
                                raise CCourtError('PDF 다운로드 시간 한도를 초과했습니다.', code='pdf_timeout')
                            if chunk:
                                received += len(chunk)
                                if received > budget:
                                    raise CCourtError('PDF 크기 또는 보관 공간 한도를 초과했습니다.', code=size_code)
                                f.write(chunk)
                with temporary.open("rb") as f:
                    signature = f.read(5)
                    if signature != b"%PDF-":
                        raise CCourtError("다운로드 응답이 PDF가 아닙니다. HTML 오류화면·XML·HWP는 업로드하지 않습니다.",
                                          code='pdf_not_pdf' if signature else 'pdf_empty')
                temporary.replace(dest)
                return dest
            raise CCourtError("PDF 다운로드: 리디렉션 횟수를 초과했습니다.", code='pdf_redirect')
        except requests.RequestException as exc:
            code = ('pdf_timeout' if isinstance(exc, requests.Timeout) else
                    'pdf_tls' if isinstance(exc, requests.exceptions.SSLError) else 'pdf_connection')
            raise CCourtError(f"PDF 다운로드: 통신 실패 ({type(exc).__name__}).", code=code) from None
        finally:
            temporary.unlink(missing_ok=True)
