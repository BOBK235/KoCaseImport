# KoCaseImport 0.1

헌법재판소 사건번호를 입력해 공식 결정문 PDF를 확보하고, Wikimedia Commons와 한국어 위키문헌의 ProofreadPage 구조까지 만드는 Toolforge용 MVP입니다.

## 처리 흐름

1. 사건번호 정규화 (`2016헌나1` 등)
2. 헌법재판소 공식 자료에서 사건 메타데이터와 PDF 탐색
   - 공공데이터포털 OpenAPI가 설정되어 있으면 이를 우선 사용
   - 헌법재판소 지능형 통합검색(iSearch)을 공식 웹 폴백으로 사용
   - 검색 구조가 바뀐 경우 웹 UI에서 `PDF URL override`를 지정할 수 있음
3. PDF 다운로드 및 SHA-1 계산
4. Commons에서 동일 SHA-1 파일 검색 후 재사용, 없으면 업로드
5. 한국어 위키문헌 `색인:<파일명>` 생성
6. PDF 각 쪽의 텍스트 레이어를 우선 추출
7. 텍스트가 없거나 품질이 낮은 쪽은 Wikimedia OCR(Tesseract `kor`) 호출
8. `페이지:<파일명>/N`을 `교정 안 됨` 상태로 생성
9. 사건번호 이름의 본문 문서를 만들고 `<pages>`로 전체 쪽을 합침

## 중요한 운영 원칙

- 자동 OCR 결과는 항상 **교정 안 됨(level 1)** 으로 저장합니다.
- 판결·결정문 자체에만 `{{PD-KoreaGov}}`를 적용하도록 설계했습니다. 보도자료·해설·편집물에는 사용하지 마세요.
- 기본값은 dry-run입니다. 실제 업로드/편집은 `.env`의 `KOCASE_LIVE_EDITS=true`와 작업 화면의 확인 체크가 모두 있어야 수행됩니다.
- 공개 운영 전에 한국어 위키문헌 공동체 합의와 봇 계정 승인을 권장합니다.

## 설치

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env 편집
python -m kocaseimport.init_db
```

웹 UI:

```bash
gunicorn -b 0.0.0.0:8000 'kocaseimport.app:create_app()'
```

워커(별도 프로세스):

```bash
python -m kocaseimport.worker
```

CLI 단일 실행:

```bash
python -m kocaseimport.cli 2016헌나1 --live
```

`--pdf-url`을 주면 사건 검색 단계에서 결정문 PDF URL만 수동 보정할 수 있습니다.

## Toolforge 예시

공유 홈에 설치한 뒤 웹서비스와 지속 워커를 따로 띄우는 구성을 권장합니다.

```bash
webservice --backend=kubernetes python3.11 shell
# venv 설치 후
webservice --backend=kubernetes python3.11 start

toolforge jobs run kocase-worker \
  --command '$HOME/www/python/venv/bin/python -m kocaseimport.worker' \
  --image python3.11 \
  --continuous
```

Toolforge 환경에 따라 `webservice`/`jobs` 명령과 경로는 조정하세요.

## 헌법재판소 OpenAPI

공공데이터포털의 **헌법재판소_사건정보 조회 서비스**는 인증키가 필요합니다. 이 프로젝트는 엔드포인트가 계정의 활용신청 화면에서 확인되는 점을 고려해 `CCOURT_OPENAPI_URL`을 환경변수로 받습니다. API가 없어도 iSearch 폴백을 시도합니다.

API 응답 필드 이름이 바뀌어도 사건번호가 포함된 객체를 재귀적으로 찾아 `사건명`, `종국일자`, `종국결과` 후보 필드를 휴리스틱하게 읽습니다.

## 인증

0.1은 운영을 단순화하기 위해 **전용 봇 계정** 방식입니다. `MW_BOT_USERNAME`과 `MW_BOT_PASSWORD`에 BotPassword 자격을 넣습니다. OAuth2 bearer token도 지원합니다.

공개 다중 사용자 버전에서는 각 사용자의 OAuth2 Authorization Code Flow로 바꾸는 것이 다음 단계입니다.

## 파일 이름

기본 파일명은 다음과 같습니다.

```text
<사건번호> <사건명> 결정문.pdf
```

예: `2016헌나1 대통령(박근혜) 탄핵 결정문.pdf`

## 안전장치

- 기존 Commons 파일 SHA-1 중복 검사
- 기존 색인/페이지/본문은 기본적으로 덮어쓰지 않음
- 실제 편집 이중 확인
- 모든 단계 로그 저장
- PDF 시그니처 검증
- 공식 `ccourt.go.kr` / `isearch.ccourt.go.kr` 다운로드 URL만 자동 허용

## 알려진 한계

헌법재판소 iSearch의 검색 HTML/폼은 공개 API가 아니므로 사이트 개편에 따라 폴백 파서가 깨질 수 있습니다. 운영 버전에서는 공공데이터포털 OpenAPI를 메타데이터의 기준으로 사용하고, 결정문 PDF 발견 로직에 대한 회귀 테스트를 실제 사건 표본으로 추가하는 것이 좋습니다.

## Toolforge 0.1 single-container deployment note

The included `Procfile` and `toolforge-start.sh` run the Flask/Gunicorn web UI and queue worker in the same container. This is intentional for 0.1 because the queue uses SQLite under `/tmp` by default. The queue is therefore ephemeral and will be lost when the webservice container restarts. Before production use, migrate the queue/state database to ToolsDB (or another supported shared service) and run the worker as a separate Toolforge job.
