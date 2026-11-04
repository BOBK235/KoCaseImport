# KoCaseImport 0.2.0

헌법재판소 공식 자료를 Commons와 한국어 위키문헌으로 가져오는 Toolforge용 Python 도구입니다. 웹 이용자는 Wikimedia OAuth2로 로그인하고 자신의 최근 작업과 간단한 처리 결과만 볼 수 있습니다.

## 변경 내용

- OAuth2 Authorization Code + PKCE(S256), 10분 유효·1회용 state 검증.
- Meta의 OAuth profile에서 확인한 중앙 사용자 ID(sub)로 작업 소유자 구분.
- 사용자별 access/refresh token 암호화 저장. 쿠키에는 임의 세션 식별자만 저장.
- 웹 작업의 양쪽 위키 API 요청은 신청자 토큰 사용. 공용 봇이나 전역 Bearer token으로 대체하지 않음.
- 자신의 최근 30개 작업. 타인 작업 주소 접근은 404, 비로그인은 401.
- 작업 화면: 사건 조회·확인, PDF 다운로드 결과·쪽수, 원문 판본, Dry-run 여부. 완료/후속 실패는 조회 상태에 짧게 표시.
- 전체 JSON, 상세 단계, 내부 경로, 로그, traceback은 사용자 HTML에 전달하지 않음.
- CSRF 검증, Secure/HttpOnly/SameSite 쿠키, 인증 후 세션 교체.
- 기본은 Dry-run. Dry-run도 로그인 필요.

## OAuth 애플리케이션 등록

https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose

- OAuth 2.0, confidential 웹 애플리케이션. owner-only가 아닌 여러 사용자가 승인하는 앱으로 등록.
- Authorization Code 및 Refresh Token 사용.
- 콜백: `https://실제도구이름.toolforge.org/oauth/callback` (정확히 일치).
- Commons와 한국어 위키문헌 모두 사용할 수 있는 프로젝트 범위.
- 사용자 식별, 필요한 문서 생성·편집 및 파일 업로드 권한. 식별 전용(authonly) 앱으로는 게시 불가.
- 등록자 계정으로 먼저 시험 가능. 다른 사용자 이용에는 Wikimedia 측 앱 승인 필요.

OAuth 앱은 직접 등록해야 하며 발급된 인증정보는 소스에 포함되어 있지 않습니다.

## Toolforge 업데이트

프로젝트 폴더 내용을 기존 Git 저장소 루트에 반영합니다. 중첩 폴더를 만들지 마세요. requirements.txt, 새 auth.py, store.py, 모든 templates를 포함해 전체 변경을 반영합니다. `.env`와 인증정보는 커밋하지 않습니다.

기존 CCOURT_OPENAPI_URL, CCOURT_SERVICE_KEY, CCOURT_CASE_PARAM=eventNo, CCOURT_ALLOW_PUBLISHED_PDF 설정은 유지합니다. getIncdntDetail에는 `type=json`을 보냅니다.

도구 계정에서 각각 실행하고 프롬프트에 값을 입력합니다.

```bash
toolforge envvars create MW_OAUTH_CLIENT_ID
toolforge envvars create MW_OAUTH_CLIENT_SECRET
toolforge envvars create MW_OAUTH_REDIRECT_URI
toolforge envvars create KOCASE_TOKEN_KEY
```

앞의 두 값은 앱 등록 시 발급받습니다. MW_OAUTH_REDIRECT_URI 예: `https://kocaseimport.toolforge.org/oauth/callback` (실제 도구 주소로 변경).

KOCASE_TOKEN_KEY 생성에는 표준 Python만 필요합니다.

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

출력값을 KOCASE_TOKEN_KEY 입력 프롬프트에 붙여 넣습니다. 이 키는 OAuth 토큰을 암호화하므로 공개하지 않습니다. 변경하면 기존 토큰을 읽을 수 없어 재로그인이 필요합니다.

KOCASE_SECRET_KEY도 32자 이상의 임의 문자열이어야 합니다. 기존 값이 이 조건을 만족하면 유지합니다. 필요할 때 생성·등록합니다.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
toolforge envvars create KOCASE_SECRET_KEY
```

OAuth 설정이나 키가 누락되면 앱은 기동하지 않고 운영자 로그에 설정 이름을 알립니다. 최초 적용은 KOCASE_LIVE_EDITS=false를 유지합니다.

소스 커밋·푸시 후 실제 저장소 주소로 빌드합니다.

```bash
toolforge build start https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
```

빌드 성공 후:

```bash
toolforge webservice buildservice restart
```

Procfile: `web: bash toolforge-start.sh`. 같은 컨테이너에서 웹과 워커를 실행합니다.

## 최초 확인

1. 홈페이지에서 Wikimedia 계정으로 로그인합니다.
2. `2016헌나1`을 Dry-run으로 제출하고 자신의 목록과 네 항목을 확인합니다.
3. 앱 승인 후 다른 계정에서 앞 계정 작업이 목록과 직접 주소 접근 모두에서 보이지 않는지 확인합니다.
4. 게시 시험 때 KOCASE_LIVE_EDITS=true로 등록하고 재시작합니다. 웹에서 본인 계정 게시 확인란을 선택한 작업만 게시합니다.

웹 작업은 MW_BOT_USERNAME, MW_BOT_PASSWORD, MW_BEARER_TOKEN을 사용하지 않습니다. CLI에는 운영자용 인증 방식이 남아 있습니다.

## 세션과 기록

- 로그인 및 로컬 인증 연결은 최대 12시간. access token은 만료 전에 갱신하며 실패 시 공용 계정으로 전환하지 않습니다.
- 로그아웃은 현재 로그인에 연결된 로컬 토큰을 삭제하고 대기 작업을 중단합니다. 실행 중 작업의 다음 Wikimedia API 요청도 차단합니다. 이미 전송된 요청이나 완료된 편집은 되돌리지 않습니다.
- 로그아웃은 Wikimedia 전체 로그아웃이나 공급자 측 권한 철회가 아닙니다. 앱 권한 철회는 Wikimedia의 연결 앱 관리 화면에서 합니다.
- 같은 계정으로 다시 로그인하면 보존된 자신의 작업을 볼 수 있습니다. 실패 작업 자동 재개는 없으며 새 작업을 제출해야 합니다.
- 기존 0.1.1 작업은 소유자를 확인할 수 없어 누구에게도 배정하지 않습니다. 기록은 내부에 남지만 웹에서 숨기고 소유자 없는 대기 작업은 실행하지 않습니다.
- 상세 처리 기록은 내부 DB와 Toolforge 로그에만 남습니다. OAuth 토큰·인증 응답은 기록하지 않습니다. 예외는 작업 ID와 유형으로만 기록합니다.

## 배포 범위와 한계

이번 요청의 OAuth/사용자별 작업/UI를 구현한 단일 컨테이너 버전입니다. 기본 DB는 `/tmp/kocaseimport/jobs.sqlite3`, 작업 파일은 `/tmp/kocaseimport/work`입니다. 컨테이너 교체 시 세션·토큰·기록·PDF가 유실될 수 있어 재로그인이 필요합니다. 별도 Job으로 워커를 분리하지 마세요. ToolsDB 전환, 장기 기록 보존, 중단 작업 재개와 전체 사용량 제한은 포함하지 않았습니다.

기존 판본 선택은 유지합니다. 결정문 PDF가 없으면 판례집·공보 PDF를 사용할 수 있고 판본을 표시합니다. 자동 전사는 교정 안 됨으로 생성하며 기존 문서는 기본적으로 덮어쓰지 않습니다.

Dry-run 완료는 사건 조회·PDF 다운로드·쪽수 및 SHA-1 확인 완료이며 게시·OCR 성공을 뜻하지 않습니다.

## 개발 및 검증

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
pip install pytest
pytest -q
```

테스트는 로컬 DB·테스트 키·모의 Wikimedia 응답을 사용합니다. 사용자별 접근 격리, state 재사용 차단, CSRF, 토큰 암호화·갱신·로그아웃, 웹 출력 제한, 워커와 양쪽 위키의 사용자 인증 전달을 검증합니다.

실제 client ID/secret이 제공되지 않아 실제 계정 로그인·승인·게시의 종단간 검증은 수행하지 않았습니다. Toolforge 직접 배포도 수행하지 않았습니다.

## 공식 참고 문서

- https://www.mediawiki.org/wiki/OAuth/For_Developers
- https://www.mediawiki.org/wiki/Extension:OAuth
- https://wikitech.wikimedia.org/wiki/Help:Toolforge/Envvars
- https://wikitech.wikimedia.org/wiki/Help:Toolforge/Build_Service
