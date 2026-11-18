"""Public error descriptions; never render exception strings, URLs or response bodies."""
import re


class PublicJobError(RuntimeError):
    def __init__(self, message='', *, code='unexpected', **context):
        super().__init__(message or code)
        self.code = code
        self.context = context


MESSAGES = {
    'case_not_found': ('사건 조회 결과 없음', '요청한 사건번호와 일치하는 사건을 API에서 찾지 못했습니다.', '사건번호를 확인하세요. 예: 97헌가12, 2016헌나1.'),
    'case_ambiguous': ('사건 조회 결과 중복', '사건번호에 일치하는 결과가 여러 개여서 자료를 선택하지 못했습니다.', '사건번호와 공식 사건 조회 결과를 확인해 주세요.'),
    'case_mismatch': ('사건 정보 불일치', '응답의 사건번호 또는 문서 식별자가 요청한 자료와 달라 중단했습니다.', '공식 사건 상세 화면을 확인하고 다시 시도하세요.'),
    'lookup_configuration': ('사건 조회 설정 오류', '헌재 API 주소 또는 인증 설정이 올바르지 않습니다.', '운영자가 API 주소와 인증키 설정을 확인해야 합니다.'),
    'api_rejected': ('사건 조회 API 요청 거부', '헌재 API가 오류 코드를 반환했습니다.', '운영자가 API 인증키·요청 변수·호출 한도를 확인해야 합니다.'),
    'lookup_http': ('공식 자료 조회 서버 오류', '공식 자료 조회 서버가 정상 응답을 반환하지 않았습니다.', '잠시 후 다시 시도하세요. 반복되면 운영자가 접속 상태를 확인해야 합니다.'),
    'lookup_timeout': ('공식 자료 조회 시간 초과', '사건 또는 PDF 주소 조회 중 서버 응답을 기다리다가 시간 제한에 도달했습니다.', '잠시 후 다시 시도하세요. PDF가 없다는 뜻은 아닙니다.'),
    'lookup_connection': ('공식 자료 조회 연결 실패', '사건 또는 PDF 주소 조회 중 서버에 연결하지 못했거나 연결이 끊겼습니다.', '잠시 후 다시 시도하세요. PDF가 없다는 뜻은 아닙니다.'),
    'lookup_tls': ('공식 자료 조회 HTTPS 연결 실패', '공식 조회 서버의 인증서 또는 HTTPS 연결을 확인하지 못했습니다.', '운영자가 서버 연결 상태를 확인해야 합니다.'),
    'lookup_response': ('공식 자료 조회 응답 형식 오류', 'JSON 형식 또는 필요한 사건·문서 정보를 확인하지 못했습니다.', '공식 조회 서비스의 장애나 응답 구조 변경 여부를 운영자가 확인해야 합니다.'),
    'lookup_address': ('공식 상세 주소 확인 실패', '응답의 상세 화면 주소 또는 문서 식별자를 처리할 수 없습니다.', '공식 사건 상세 화면과 서비스 변경 여부를 확인해 주세요.'),
    'pdf_missing': ('공식 PDF 미제공', '조회에 성공한 결정문과 허용된 대체 판본에서 PDF 파일 정보를 찾지 못했습니다.', 'HWP·XML은 자동 변환하지 않습니다. 별도의 공식 PDF 주소를 알고 있다면 직접 지정해 새 작업을 시작하세요.'),
    'pdf_alternatives_disabled': ('결정문 PDF 없음 · 대체 판본 사용 꺼짐', '결정문 PDF를 찾지 못했으며 판례집·공보 PDF 대체 사용은 비활성화되어 있습니다.', '공식 PDF 주소를 직접 지정하거나 운영자에게 대체 판본 설정 확인을 요청하세요.'),
    'pdf_lookup_incomplete': ('PDF 제공 여부 확인 실패', '공식 자료 조회 중 오류가 발생해 PDF 제공 여부를 끝까지 확인하지 못했습니다. PDF가 없다고 단정할 수 없습니다.', '아래 조회 오류를 확인하고 다시 시도하세요.'),
    'pdf_http_missing': ('PDF 주소의 파일을 찾을 수 없음', '확인한 PDF 주소에서 서버가 파일 없음 또는 삭제 응답을 반환했습니다.', '공식 상세 화면에서 파일이 열리는지 확인하세요. 주소가 변경되었을 수 있습니다.'),
    'pdf_http': ('PDF 다운로드 서버 오류', 'PDF 다운로드 서버가 정상 응답을 반환하지 않았습니다.', '잠시 후 다시 시도하세요. 접근 거부 응답이 반복되면 운영자 확인이 필요합니다.'),
    'pdf_timeout': ('PDF 다운로드 시간 초과', '파일을 받는 중 응답 대기 또는 다운로드 시간 제한에 도달했습니다.', '공식 사이트에서 파일이 열리는지 확인한 뒤 다시 시도하세요.'),
    'pdf_connection': ('PDF 다운로드 연결 중단', '파일을 받는 중 서버 연결에 실패했거나 전송이 끊겼습니다.', '잠시 후 다시 시도하세요. 불완전한 다운로드 파일은 사용하지 않습니다.'),
    'pdf_tls': ('PDF HTTPS 연결 실패', '다운로드 서버의 인증서 또는 HTTPS 연결을 확인하지 못했습니다.', '운영자가 서버 연결 상태를 확인해야 합니다.'),
    'pdf_redirect': ('PDF 다운로드 주소 이동 오류', '이동할 주소가 없거나 허용된 주소 이동 횟수를 초과했습니다.', '현재의 공식 PDF 주소를 확인해 주세요.'),
    'pdf_address_blocked': ('PDF 주소 사용 불가', '헌법재판소 공식 HTTP(S) 주소로 확인할 수 없어 다운로드를 차단했습니다.', '헌법재판소 공식 PDF 주소를 지정해 주세요.'),
    'pdf_empty': ('빈 다운로드 응답', '서버 응답에 파일 내용이 없어 PDF로 사용할 수 없습니다.', '공식 사이트에서 PDF가 정상적으로 내려받아지는지 확인하세요.'),
    'pdf_not_pdf': ('PDF가 아닌 다운로드 응답', '받은 내용이 PDF가 아닙니다. HTML 오류 화면이나 HWP·XML일 수 있습니다.', '공식 PDF 주소인지 확인해 주세요.'),
    'pdf_invalid': ('PDF 읽기 실패', '파일 응답은 받았지만 PDF 구조가 손상되었거나 읽을 수 없어 쪽수를 확인하지 못했습니다.', '공식 사이트의 원본 파일이 열리는지 확인하고 다시 시도하세요.'),
    'pdf_encrypted': ('암호화된 PDF', 'PDF가 암호화되어 쪽수 또는 본문을 읽을 수 없습니다.', '암호 없이 열리는 공식 PDF가 있는지 확인해 주세요.'),
    'pdf_size_limit': ('PDF 크기 한도 초과', 'PDF가 파일당 허용 크기 50 MiB를 초과했습니다.', '원본 파일 크기를 확인하고 필요한 경우 운영자에게 문의하세요.'),
    'pdf_page_limit': ('PDF 쪽수 한도 초과', 'PDF 쪽수가 처리 범위인 1~500쪽을 벗어났습니다.', '사건별 PDF인지 확인하거나 운영자에게 문의하세요.'),
    'work_storage_full': ('서버 파일 보관 공간 부족', '작업 파일 보관 한도 또는 서버의 남은 디스크 공간 때문에 저장을 중단했습니다.', '만료 자료가 정리된 후 다시 시도하거나 운영자에게 문의하세요.'),
    'storage_permission': ('서버 파일 저장 권한 오류', '서버가 작업 파일을 읽거나 쓸 권한이 없습니다.', '운영자가 작업 디렉터리 권한을 확인해야 합니다.'),
    'storage_error': ('서버 파일 처리 오류', '파일을 저장하거나 읽는 중 운영체제 오류가 발생했습니다.', '운영자가 디스크와 작업 파일 상태를 확인해야 합니다.'),
    'cached_pdf_missing': ('확인한 PDF가 남아 있지 않음', '보관 기간 만료나 서버 재시작 등으로 이전 PDF를 찾지 못했습니다.', '새 Dry-run 작업으로 PDF를 다시 확인해 주세요.'),
    'cached_pdf_changed': ('이전 PDF와 현재 파일 불일치', '파일 해시 또는 쪽수가 Dry-run에서 확인한 값과 달라 게시를 중단했습니다.', '새 Dry-run 작업으로 PDF를 다시 확인해 주세요.'),
    'auth_expired': ('로그인 인증 사용 불가', '로그인 인증을 사용할 수 없어 작업을 중단했습니다.', '다시 로그인한 뒤 새 작업을 시작해 주세요.'),
    'live_disabled': ('실제 게시 비활성화', '서버에서 실제 게시를 허용하지 않아 작업을 중단했습니다.', '운영자가 실제 게시 설정을 확인해야 합니다.'),
    'resource_limit': ('작업 자원 한도 초과', '작업 결과 크기 또는 서버 자원 한도에 도달했습니다.', '운영자에게 작업번호와 중단 단계를 알려 주세요.'),
    'memory_limit': ('작업 메모리 한도 초과', 'PDF 또는 후속 자료를 처리하는 데 허용된 메모리가 부족했습니다.', '운영자에게 작업번호와 PDF 쪽수를 알려 주세요.'),
    'job_timeout': ('전체 작업 시간 초과', '전체 작업 실행 시간이 허용된 30분을 초과해 중단했습니다.', '아래 중단 단계를 확인하세요. 이미 게시된 파일·문서는 남아 있을 수 있습니다.'),
    'worker_restarted': ('서버 재시작으로 작업 중단', '처리 중 서버 또는 워커가 다시 시작되어 작업이 중단되었습니다.', '이미 완료된 게시 결과를 확인한 뒤 새 작업으로 다시 시도하세요.'),
    'worker_stopped': ('작업 프로세스 종료', '작업 프로세스가 정상 종료되지 않았습니다. CPU·메모리 제한 또는 다른 종료 원인일 수 있습니다.', '정확한 종료 원인은 운영자가 확인해야 합니다.'),
    'wiki_error': ('위키 게시 요청 실패', '공용 또는 위키문헌 API 요청을 완료하지 못했습니다.', '중단 단계와 이미 완료된 게시 링크를 확인하고 운영자에게 문의하세요.'),
    'external_connection': ('외부 서비스 연결 실패', '후속 처리 중 외부 서비스 연결 또는 응답에 문제가 발생했습니다.', '중단 단계와 완료된 결과를 확인한 뒤 다시 시도하세요.'),
    'transcription_failed': ('OCR 전사 실패', 'PDF 다운로드 이후 단계인 OCR 전사를 완료하지 못했습니다.', 'PDF 다운로드 결과와 공용 링크를 확인해 주세요.'),
    'ocr_local_engine': ('대체 OCR 엔진 사용 불가', '위키미디어 OCR 이후 사용할 Python OCR 엔진을 불러오지 못했습니다.', '운영자가 새 requirements.txt와 project.toml을 반영해 다시 빌드해야 합니다.'),
    'ocr_local_models': ('대체 OCR 언어 자료 없음', '로컬 OCR에 필요한 언어 자료 또는 경로 설정을 확인할 수 없습니다.', '운영자가 한국어·영어 Tesseract 자료와 OCR_LOCAL_LANG 설정을 확인해야 합니다.'),
    'ocr_local_render': ('OCR용 쪽 이미지 생성 실패', '다운로드한 PDF의 해당 쪽을 OCR 이미지로 변환하지 못했습니다.', 'PDF에서 해당 쪽이 정상적으로 열리는지 확인해 주세요.'),
    'ocr_local_timeout': ('대체 OCR 시간 초과', '위키미디어 OCR 실패 후 시도한 로컬 OCR도 쪽별 시간 제한에 도달했습니다.', '해당 쪽을 확인하고 필요하면 위키문헌에서 직접 전사해 주세요.'),
    'ocr_local_failed': ('대체 OCR 실패', '위키미디어 OCR 이후의 로컬 OCR에서도 본문을 얻지 못했습니다.', '해당 쪽의 이미지와 서버 OCR 설정을 확인해 주세요.'),
    'ocr_empty': ('OCR 본문 인식 실패', '이미지가 있지만 두 OCR 처리에서 사용할 본문을 얻지 못했습니다.', '빈 쪽으로 단정하지 않았습니다. 해당 쪽을 확인해 주세요.'),
    'ocr_result_limit': ('OCR 결과 크기 한도 초과', 'OCR 결과가 쪽별 또는 전체 본문 크기 제한을 초과했습니다.', '운영자에게 작업번호와 해당 쪽을 알려 주세요.'),
    'legacy_failure': ('상세 원인이 기록되지 않은 실패', '이전 버전의 작업에는 사용자용 상세 오류 원인이 저장되어 있지 않습니다.', '새 작업을 실행하면 중단 원인이 구분되어 표시됩니다.'),
    'unexpected': ('예상하지 못한 처리 오류', '아래 단계에서 작업이 중단되었으나 정확한 원인을 분류하지 못했습니다. PDF 미제공으로 확인된 것은 아닙니다.', '운영자에게 작업번호와 중단 단계를 알려 주세요.'),
}

PHASES = {
    'queued': '작업 대기', 'starting': '작업 시작·로그인 확인',
    'case_lookup': '사건 조회·PDF 주소 확인', 'download_pdf': 'PDF 다운로드',
    'verify_pdf': 'PDF 파일·쪽수 검증', 'commons_upload': '공용 업로드',
    'wikisource_index': '위키문헌 색인 생성', 'transcription': 'OCR 전사',
    'page_creation': '위키문헌 쪽별 문서 생성', 'main_page': '위키문헌 본문 생성',
    'save_result': '작업 결과 저장',
}


def failure_record(code, stage='', **context):
    record = {'code': code if isinstance(code, str) and code in MESSAGES else 'unexpected',
              'stage': stage if isinstance(stage, str) and stage in PHASES else ''}
    status = context.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        record['http_status'] = status
    api = context.get('api_code')
    if isinstance(api, str) and re.fullmatch(r'[0-9]{1,3}', api):
        record['api_code'] = api
    page_number = context.get('page_number')
    if type(page_number) is int and 1 <= page_number <= 500:
        record['page_number'] = page_number
    for key in ('missing_editions', 'hwp_editions'):
        values = context.get(key, [])
        if isinstance(values, (list, tuple)):
            record[key] = [x for x in ('결정문', '판례집', '공보') if x in values]
    causes = context.get('causes', [])
    if isinstance(causes, (list, tuple)):
        record['causes'] = [failure_record(x.get('code'), http_status=x.get('http_status'))
                            for x in causes[:6] if isinstance(x, dict) and isinstance(x.get('code'), str) and x['code'] in MESSAGES]
    return record


def describe_failure(data):
    data = data if isinstance(data, dict) else {}
    record = failure_record(data.get('code'), data.get('stage'), **{
        k: data[k] for k in ('http_status', 'api_code', 'page_number', 'missing_editions', 'hwp_editions', 'causes') if k in data})
    title, message, action = MESSAGES[record['code']]
    details = []
    if 'page_number' in record:
        details.append(f"중단된 쪽: {record['page_number']}")
    if 'http_status' in record:
        details.append(f"HTTP 상태: {record['http_status']}")
    if 'api_code' in record:
        details.append(f"API 결과 코드: {record['api_code']}")
    if record.get('missing_editions'):
        details.append('PDF 정보 없음: ' + ', '.join(record['missing_editions']))
    if record.get('hwp_editions'):
        details.append('HWP 제공 확인: ' + ', '.join(record['hwp_editions']))
    for cause in record.get('causes', []):
        line = MESSAGES[cause['code']][0]
        if 'http_status' in cause:
            line += f" (HTTP {cause['http_status']})"
        details.append(line)
    return {'code': record['code'], 'title': title, 'message': message, 'action': action,
            'stage': PHASES.get(record['stage'], '작업 처리'), 'details': details}
