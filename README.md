# KoCaseImport 0.1.1

헌법재판소 사건번호를 입력해 공식 결정문 PDF를 확보하고, Wikimedia Commons와 한국어 위키문헌의 ProofreadPage 구조까지 만드는 Toolforge용 MVP입니다.

## 처리 흐름

1. 사건번호 정규화 (`2016헌나1` 등)
2. 헌법재판소 공식 자료에서 사건 메타데이터와 PDF 탐색
   - 공공데이터포털 OpenAPI가 설정되어 있으면 이를 우선 사용
   - `dtmPpHp`의 docId로 iSearch 상세 JSON을 조회하고 `pdfFilePath`를 `downloadEX.do`에 전달
   - 결정문 PDF가 없으면 공식 판례집·공보의 해당 사건 PDF를 사용하고 판본을 명시
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

## Toolforge 업데이트

이 패키지 폴더의 내용을 기존 Git 저장소 루트에 반영합니다. 폴더 전체를 저장소 안에 한 단계 더 중첩하지 마세요. `.env`와 자격증명은 커밋하지 않습니다.

이번 변경 파일: `kocaseimport/ccourt.py`, `kocaseimport/config.py`, `kocaseimport/pipeline.py`, `Procfile`, `.env.example`, `tests/test_ccourt.py`, `README.md`, `UPDATE-0.1.1.md`.
`Procfile`은 `web: bash toolforge-start.sh`로 되어 있습니다.

Toolforge 도구 계정에서 아래 두 비밀이 아닌 설정값을 등록/갱신합니다.

```bash
toolforge envvars create CCOURT_CASE_PARAM eventNo
toolforge envvars create CCOURT_ALLOW_PUBLISHED_PDF true
```

기존 `CCOURT_OPENAPI_URL`의 getIncdntDetail 전체 주소와 `CCOURT_SERVICE_KEY`는 유지합니다.
수정 소스를 커밋·푸시하고 다음을 실행합니다. 아래 저장소 주소는 실제 주소로 바꾸세요.

```bash
toolforge build start https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
```

빌드 성공 후에만 재시작합니다.

```bash
toolforge webservice buildservice restart
```

웹 화면에서 `2016헌나1`을 새 작업으로 제출하고 실제 업로드는 선택하지 않습니다.
이 버전은 웹과 워커를 같은 컨테이너에서 실행합니다. SQLite 큐를 공유하지 않는 별도 Job으로 워커를 분리하지 마세요. `/tmp`의 작업 기록은 컨테이너가 교체되면 유실될 수 있습니다.

## 헌법재판소 OpenAPI와 PDF 판본

공공데이터포털의 사건정보 조회 서비스 `/getIncdntDetail`에 `eventNo`, `serviceKey`를 보냅니다. `CCOURT_OPENAPI_URL`에는 활용신청 화면의 전체 요청 URL을 넣습니다. 키를 URL에 직접 붙이지 않습니다. 요청은 `_type=json`을 사용하며 JSON 응답을 요구합니다.

| API 필드 | 사용 |
| --- | --- |
| `header.resultCode` | 0 또는 00을 성공으로 처리 |
| `body.items.item[].eventNo` | 요청한 사건번호와 정확히 대조 |
| `eventNm` | 사건명, 앞뒤 공백 제거 |
| `endDate` / `adjuDate` | 종국일자, 없으면 선고일자; YYYY-MM-DD로 변환 |
| `endRsta` | 종국결과 |
| `dtmPpHp` | 원본 결정문 상세 화면 |
| `dtmPpXml` | XML 출처로 보존; PDF로 취급하지 않음 |
| `pdntList[].pdntHp` | 판례집 수록본 상세 화면 |
| `ogList[].ogHp` | 공보 수록본 상세 화면 |

메타데이터는 사용자 제공 getIncdntDetail 응답 구조에 맞춰 명시적으로 파싱합니다. 인증 실패·HTTP 오류·비JSON 응답·일치하는 사건 없음은 숨기지 않고 중단합니다. 요청 URL/인증키를 예외 메시지에 출력하지 않습니다.

iSearch는 현재 사이트 JavaScript가 사용하는 `api/index/searcher/view`의 `returnObject["00"]`를 읽습니다. `pdfFilePath`는 암호화된 값일 수 있으므로 임의 해석하지 않고 URL 인코딩해 `downloadEX.do`의 `filePath`에 전달합니다. API의 XML 경로를 문자열 치환해서 PDF를 만들지 않습니다.

결정문 PDF가 없으면 기본적으로 판례집, 공보 순서로 PDF를 찾습니다. 선택한 판본은 파일명, 작업 로그, manifest, Commons 설명에 기록합니다. 판례집·공보에는 판시사항·결정요지 등이 함께 있을 수 있으며 원본 결정문의 쪽수·편집과 같다고 가정하지 않습니다. 이 대체 사용을 금지하려면 `CCOURT_ALLOW_PUBLISHED_PDF=false`로 설정하세요.

OpenAPI 두 설정을 모두 생략하면 iSearch의 eventNo 상세 조회를 시도합니다. 한 설정만 입력한 경우에는 설정 오류를 알립니다. `PDF URL override`를 넣으면 자동 PDF 탐색을 건너뛰지만, 설정된 OpenAPI 메타데이터 조회는 수행합니다.

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

iSearch 상세 JSON은 사이트 내부 인터페이스이므로 향후 변경될 수 있습니다. HWP를 PDF로 변환하는 기능은 없습니다. 0.1.1 검증은 getIncdntDetail 사용자 제공 샘플의 파싱 및 실제 iSearch 조회·PDF 다운로드까지이며, 인증키가 필요한 OpenAPI 실호출과 Wikimedia 실게시, Toolforge 재배포는 이 환경에서 수행하지 않았습니다.

## Toolforge 0.1 single-container deployment note

The included `Procfile` and `toolforge-start.sh` run the Flask/Gunicorn web UI and queue worker in the same container. This is intentional for 0.1 because the queue uses SQLite under `/tmp` by default. The queue is therefore ephemeral and will be lost when the webservice container restarts. Before production use, migrate the queue/state database to ToolsDB (or another supported shared service) and run the worker as a separate Toolforge job.
