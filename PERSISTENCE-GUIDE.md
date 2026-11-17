# KoCaseImport 0.2.6 영속 저장 전환·운영 가이드

이 문서는 도구 이름이 `kocaseimport`인 경우를 설명합니다. 다른 이름이면 경로와 이미지 이름을 바꾸세요. 명령은 PowerShell에서 SSH로 접속한 **Toolforge의 도구 계정 셸**에서 실행합니다. Windows PowerShell에서 바이너리 TAR를 파이프로 저장하지 마세요.

## 유지하는 정보와 보관 기간

| 정보 | 위치 | 정책 |
|---|---|---|
| 로그인 세션·암호화된 OAuth 토큰 | 비공개 ToolsDB | 최초 로그인부터 12시간, 로그아웃 시 무효화 |
| 작업·결과 링크·상세 처리 상태 | 비공개 ToolsDB | 종료 후 7일 또는 사용자의 선택 삭제 |
| 사용자별 사건 준비·실제 게시 집계 | 비공개 ToolsDB | 서버 시각 기준 최근 24시간; 기록 삭제로 줄어들지 않음 |
| PDF·manifest | 영속 폴더의 `work/` | 종료 작업은 24시간, 사용자 선택 삭제 지원 |
| 관리자 요청 로그 | 영속 폴더의 `audit/` | 최대 30일·20MiB; 용량·묶음 단위 정리로 더 일찍 삭제 가능 |
| 키·운영 설정·관리자 ID | Toolforge envvars | 재시작 시 동일 값 주입 |

로그인 만료 시각은 재시작으로 연장되지 않습니다. 브라우저에 기존 쿠키가 남아 있고 서버 세션이 아직 유효해야 로그인이 유지됩니다. API 제공자가 토큰을 취소했거나 세션이 만료되면 다시 로그인해야 합니다.

## 1. 기존 자료를 보존할 때: 먼저 이전 파일 생성

**새 버전으로 재시작하거나 기존 웹서비스를 중지하기 전에** 수행합니다. 0.2.5의 기본 `/tmp`는 다른 shell pod에서 접근할 수 없습니다. `webservice shell` 대신 현재 실행 중인 **실제 web pod**를 선택해야 합니다.

1. 새 버전 소스를 Git 저장소에 반영합니다. 최소한 `scripts/export_legacy.py` 파일을 Toolforge의 체크아웃에 준비합니다. 이 스크립트는 Python 표준 라이브러리만 사용합니다.
2. 작업 화면에서 대기·실행 중 작업이 모두 끝날 때까지 기다립니다.
3. 도구 계정 셸에서 다음을 실행합니다.

```bash
umask 077
mkdir -p /data/project/kocaseimport/kocaseimport-migration
chmod 700 /data/project/kocaseimport/kocaseimport-migration
kubectl get pods
```

목록에서 현재 웹서비스의 Running pod 이름을 확인하고 아래 `실제_WEB_POD_이름`을 교체합니다. 현재 디렉터리는 새 소스의 저장소 루트여야 합니다.

```bash
kubectl exec -i 실제_WEB_POD_이름 -- launcher python - --quiesce \
  < scripts/export_legacy.py \
  > /data/project/kocaseimport/kocaseimport-migration/snapshot.tar
```

스크립트는 같은 컨테이너의 KoCaseImport 웹·워커를 잠시 정지하고, 작업이 진행 중이지 않은지 다시 검사합니다. SQLite 백업, PDF·결과 파일, 관리자 로그를 TAR로 생성하며 키 자체는 포함하지 않습니다. **명령이 성공하고 `SNAPSHOT_COMPLETE`가 표시되어야 다음 단계로 진행합니다.** 실패 시 정지했던 프로세스를 재개합니다. 부분 생성된 TAR는 가져오지 마세요.

성공 후에는 이전 파일과 원본이 달라지지 않도록 기존 서비스를 정지 상태로 둡니다. 이때 웹 화면은 응답하지 않을 수 있습니다. 전환을 취소하려면 출력에 나온 PID들에 대해, 같은 pod 안에서 `kill -CONT`를 실행합니다.

```bash
kubectl exec 실제_WEB_POD_이름 -- sh -c 'kill -CONT 출력에_표시된_PID들'
```

이미 컨테이너가 교체되어 원본 `/tmp`가 사라졌다면 복구할 수 없습니다. 이 경우 3절의 **이전 파일이 없는 경우**를 사용합니다. 로그인과 과거 기록은 새로 시작하지만 이후 재시작부터는 유지됩니다.

## 2. 새 소스 빌드와 저장 설정

기존 `KOCASE_SECRET_KEY`, `KOCASE_TOKEN_KEY`, OAuth 클라이언트 정보, CCOURT 키와 관리자 ID를 **새로 생성하거나 변경하지 않습니다.** 이전 파일은 기존 키로만 가져올 수 있습니다.

```bash
toolforge build start https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
```

빌드 성공을 확인한 뒤 기존 웹서비스를 중지합니다. 이전 파일을 보존할 경우 반드시 앞 단계의 생성이 성공한 다음 실행하세요.

```bash
toolforge webservice stop
toolforge envvars create KOCASE_DB_BACKEND
```

입력값은 `mariadb`입니다. `TOOL_TOOLSDB_USER`, `TOOL_TOOLSDB_PASSWORD`는 Toolforge가 제공합니다. 기본 DB 이름은 `<TOOL_TOOLSDB_USER>__kocaseimport`입니다. 직접 지정하려면 `KOCASE_DB_NAME`을 설정하되, **이름이 `_p`로 끝나면 안 됩니다.** ToolsDB의 `_p` DB는 공개 읽기를 허용하며 프로그램에서도 이를 거부합니다.

기본 영속 폴더는 `$TOOL_DATA_DIR/kocaseimport-data`입니다. 다른 위치를 사용하려면 다음 환경변수에 실제 절대 경로를 입력합니다. `$TOOL_DATA_DIR`라는 문자를 값에 그대로 넣지 마세요.

```bash
toolforge envvars create KOCASE_STATE_DIR
```

입력 예: `/data/project/kocaseimport/kocaseimport-data`

이 폴더는 처음 초기화할 때 비어 있는 전용 폴더여야 합니다. 공개 웹 폴더나 다른 도구의 자료 폴더를 사용하지 마세요. 코드와 설정은 빌드 이미지·envvars에 두고 NFS에는 데이터 파일만 둡니다.

예전에 `KOCASE_WORK_DIR=/tmp/...` 또는 `KOCASE_AUDIT_DIR=/tmp/...`를 등록했다면 해당 envvar를 제거하여 새 기본값을 사용합니다. 등록하지 않았다면 아래 두 명령은 필요 없습니다.

```bash
toolforge envvars delete KOCASE_WORK_DIR
toolforge envvars delete KOCASE_AUDIT_DIR
```

운영 상태에서 `KOCASE_DB_PATH`는 사용하지 않습니다. SQLite 파일을 NFS로 옮겨 운영하는 구성은 지원하지 않습니다.

## 3. DB 초기화·이전

아래 작업은 웹서비스를 시작하기 전에 실행합니다. 이미지 이름은 **빌드 결과에 표시된 이미지**를 사용합니다. 보통 `tool-kocaseimport/tool-kocaseimport:latest`입니다. 작업 이름이 이미 존재하면 새 이름을 사용하세요.

```bash
image_ref='tool-kocaseimport/tool-kocaseimport:latest'
```

### 이전 파일이 있는 경우

```bash
toolforge jobs run kocase-db-init --wait --mount=all --image "$image_ref" \
  --command 'manage init --for-import'

toolforge jobs run kocase-db-import --wait --mount=all --image "$image_ref" \
  --command 'manage import /data/project/kocaseimport/kocaseimport-migration/snapshot.tar'

toolforge jobs run kocase-db-check --wait --mount=all --image "$image_ref" \
  --command 'manage check'
```

각 작업이 성공했는지 확인한 뒤 다음 명령을 실행합니다. `init --for-import`만 완료된 상태에서는 서비스가 시작되지 않습니다. `import`가 끝나야 사용 가능합니다.

이전은 빈 DB에만 허용합니다. 기존 세션 ID 해시, 암호화된 토큰, 만료 시각, 접수 시각을 그대로 가져오며 만료 기간을 새로 부여하지 않습니다. 파일 경로는 새 영속 폴더에 맞게 바꿉니다. 반복 실행으로 기존 데이터가 덮어써지지 않도록 검사합니다.

DB와 파일 이동이 하나의 트랜잭션은 아닙니다. 이전 중 실패하면 준비 상태를 닫아 둡니다. **동일한 이전 파일**로 다시 시도할 수 있습니다. 성공 여부가 불명확하면 먼저 `manage check`와 작업 로그를 확인하세요. 기존 DB를 임의로 삭제하거나 `ready` 값을 SQL로 바꾸지 마세요.

### 이전 파일이 없는 경우

```bash
toolforge jobs run kocase-db-init --wait --mount=all --image "$image_ref" \
  --command 'manage init'
```

과거 사용량을 알 수 없으므로 새 작업 접수에는 **24시간 보호 대기**를 적용합니다. 로그인과 관리자 설정 확인은 가능합니다. 이는 초기화·복원으로 24시간 한도가 우회되는 것을 막기 위한 정책입니다.

## 4. 영속 마운트로 시작·확인

```bash
toolforge webservice buildservice start --mount=all
toolforge webservice buildservice logs
```

`--mount=none`으로 시작하면 영속 파일 저장소를 확인할 수 없어 기동을 거부합니다. 서비스 설정 템플릿을 사용하는 경우에도 `mount: all`을 지정하세요.

확인할 사항:

1. 기존 쿠키가 있고 로그인 후 12시간 이내라면 로그인 상태가 유지되는지 확인합니다.
2. 자신의 작업 목록, PDF 내려받기, 공용·위키문헌 링크를 확인합니다.
3. 관리자 계정에서는 이전 요청·삭제 기록을 확인합니다.
4. 아래 명령으로 한 번 재시작한 뒤 같은 항목과 사용량이 그대로 남는지 확인합니다.

```bash
toolforge webservice buildservice restart
```

전환 확인 후 이전 TAR는 계정 정보와 삭제 전 자료를 계속 보관하지 않도록 제거하세요. 장기 보관이 필요하다면 별도 암호화·접근 제한·보관 기한을 적용해야 합니다.

```bash
rm -- /data/project/kocaseimport/kocaseimport-migration/snapshot.tar
```

이후 일반 업데이트는 **새 빌드 → 재시작**만 합니다. DB 초기화나 가져오기는 반복하지 않습니다.

## 장애·키 교체·백업 복원

- DB 연결 실패 시 새 요청은 HTTP 503으로 거부합니다. 오류를 빈 DB나 임시 SQLite로 대체하지 않습니다. 비밀키·SQL·드라이버 상세 오류는 사용자 화면에 표시하지 않습니다.
- 기존 키와 설정이 다르면 서비스를 시작하지 않습니다. 단순 재시작을 위해 키를 다시 만들지 마세요.
- 진행 중에 끊긴 작업은 자동 재게시하지 않습니다. 이미 전송된 공용 업로드·위키문헌 편집은 성공했을 수 있으므로 실제 게시 결과를 확인합니다. 대기 작업은 토큰이 유효하면 이어서 처리합니다.
- ToolsDB는 별도 오프라인 백업을 제공하지 않습니다. 필요한 백업은 직접 준비하고, 같은 NFS만을 유일한 백업으로 사용하지 마세요. DB·파일 폴더의 식별자와 일관된 시점이 필요합니다. DB 덤프와 암호화 키를 같은 공개 위치에 두지 마세요.
- 백업 복원은 이미 삭제한 자료나 로그아웃한 세션을 되살릴 수 있습니다. 웹서비스를 중지한 상태에서 복원하고, 재시작 전에 아래 인증 무효화를 수행합니다. 보관 기한과 사용자가 요청한 삭제도 별도로 반영해야 합니다.

기존 키를 유지한 상태의 백업 복원 후:

```bash
toolforge jobs run kocase-revoke-after-restore --wait --mount=all --image "$image_ref" \
  --command 'manage revoke-auth --confirm --restored-backup'
```

모든 서버 세션과 사용 가능한 OAuth 토큰을 무효화하며, 과거 사용량 누락에 대비해 24시간 동안 새 작업을 접수하지 않습니다. 이 명령은 Wikimedia 전체 로그인이나 상위 OAuth 애플리케이션 승인을 취소하는 명령은 아닙니다.

키 유출 의심 등으로 `KOCASE_SECRET_KEY`/`KOCASE_TOKEN_KEY`를 교체할 때는 웹서비스를 중지하고 새 키를 envvars에 등록한 뒤 다음을 실행합니다.

```bash
toolforge jobs run kocase-reset-auth-keys --wait --mount=all --image "$image_ref" \
  --command 'manage rotate-auth-keys --confirm'
```

기존 토큰을 새 키로 재암호화하는 기능은 아닙니다. 기존 인증을 모두 무효화하고 새 키 지문을 등록합니다. 사용자 모두 다시 로그인해야 합니다. 실행 중인 워커 실행권이 남아 있으면 거부하므로 중지 후 최대 60초가 지난 뒤 재시도합니다. 백업 복원과 키 교체를 함께 할 때는 `--restored-backup`도 붙이세요.

## 보안 범위와 검증

- DB 이름 공개 여부, 고정 키, 서버 측 세션 만료, 사용자별 소유권, CSRF, 비밀정보 제외 로그를 적용합니다. 기존 관리자 설정은 Wikimedia 고유 사용자 ID 기준입니다.
- 작업·토큰·사용량 변경은 같은 DB의 잠금·트랜잭션으로 처리합니다. 쿼리 시간 초과·용량 초과·예외 시 전체 트랜잭션을 롤백합니다.
- 운영 파일·감사 로그 잠금은 NFS의 파일 잠금에만 의존하지 않고 DB 연결에 종속된 잠금을 사용합니다. 잠금 이름은 비밀키를 사용하여 범위를 나눕니다. 연결이 끊기면 파일 처리와 후속 실행을 중단합니다.
- DB의 논리 저장량 256MiB, 작업 2,000건, 사용자별 24시간 80건 경고·100건 상한, PDF 파일당 50MiB·전체 1GiB 등 기존 상한을 유지합니다. MariaDB 인덱스·엔진 오버헤드는 논리 데이터 상한에 포함되지 않습니다.
- 기본 DB 통신 대상은 Toolforge 내부 ToolsDB입니다. 내부망·플랫폼 운영 권한까지 침해된 상황을 저장 암호화만으로 막을 수는 없습니다. 외부 DB는 신뢰할 CA를 지정해 TLS 인증서·호스트명을 검증해야 하며 검증 실패 시 우회하지 않습니다.
- 로그는 변조 불가능한 보관소가 아닙니다. 같은 도구 계정의 유지관리자나 애플리케이션 실행 권한을 가진 사람은 키와 데이터에 접근할 수 있습니다. 유지관리자 권한을 필요한 사람에게만 부여하세요.
- 기존 검사와 실제 MariaDB 통합 검사를 합쳐 65개가 통과했습니다. 별도 프로세스 재시작, 세션·PDF·사용량 유지, 로그아웃과 갱신 경쟁, 동시 마지막 한도, 워커 실행권 교체, 작업 자식 실행, 저장 한도·장애, 기존 자료 이전과 경로 공격 차단을 포함합니다. 외부 Wikimedia API는 모의 응답이며 실제 Toolforge 배포·공용 게시를 수행하는 검사는 아닙니다.

공식 참고 문서:

- [Toolforge ToolsDB: 비공개 DB 이름·백업·트랜잭션 주의점](https://wikitech.wikimedia.org/wiki/Help:Toolforge/ToolsDB)
- [Build Service: NFS 마운트와 ToolsDB 환경변수](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Building_container_images)
- [Toolforge envvars](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Envvars)
- [OWASP 세션 관리](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP 저장 암호화](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
