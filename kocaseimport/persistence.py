"""Explicit production initialization, key binding and schema verification."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import uuid

from . import database

SCHEMA_VERSION = '2'
SUPPORTED_VERSIONS = {'1', SCHEMA_VERSION}
SCHEMA = [
"""CREATE TABLE IF NOT EXISTS mutation_lock (name VARCHAR(32) PRIMARY KEY) ENGINE=InnoDB""",
"""CREATE TABLE IF NOT EXISTS app_meta (name VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL) ENGINE=InnoDB""",
"""CREATE TABLE IF NOT EXISTS storage_budget (id INT PRIMARY KEY, used_bytes BIGINT NOT NULL, max_bytes BIGINT NOT NULL) ENGINE=InnoDB""",
"""CREATE TABLE IF NOT EXISTS runtime_lease (name VARCHAR(32) PRIMARY KEY, holder VARCHAR(64) NOT NULL, expires DOUBLE NOT NULL) ENGINE=InnoDB""",
"""CREATE TABLE IF NOT EXISTS jobs (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 case_number VARCHAR(40) NOT NULL, status VARCHAR(16) NOT NULL,
 step VARCHAR(64) NOT NULL DEFAULT '', live INT NOT NULL DEFAULT 0,
 pdf_url_override TEXT NOT NULL DEFAULT '', payload MEDIUMTEXT NOT NULL DEFAULT '{}',
 log TEXT NOT NULL DEFAULT '', error VARCHAR(80) NOT NULL DEFAULT '',
 created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
 owner_id VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin,
 auth_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
 summary TEXT NOT NULL DEFAULT '{}', reuse_pdf INT NOT NULL DEFAULT 0,
 worker_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
 INDEX jobs_status_idx(status,created_at), INDEX jobs_updated_idx(status,updated_at),
 INDEX jobs_owner_idx(owner_id,created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
"""CREATE TABLE IF NOT EXISTS admissions (
 job_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 owner_id VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 kind VARCHAR(16) NOT NULL, created_at DOUBLE NOT NULL,
 PRIMARY KEY(job_id,kind), INDEX admissions_owner_idx(owner_id,kind,created_at),
 INDEX admissions_time_idx(created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
"""CREATE TABLE IF NOT EXISTS oauth_grants (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 owner_id VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 username VARCHAR(255) NOT NULL, token MEDIUMTEXT NOT NULL, expires DOUBLE NOT NULL,
 login_state VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
 INDEX grants_expiry_idx(expires), INDEX grants_owner_idx(owner_id),
 UNIQUE INDEX grants_state_idx(login_state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
"""CREATE TABLE IF NOT EXISTS web_sessions (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 csrf VARCHAR(128) NOT NULL, owner_id VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin,
 username VARCHAR(255), auth_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
 pending_state TEXT, pending_verifier TEXT, pending_at DOUBLE, expires DOUBLE NOT NULL,
 INDEX sessions_expiry_idx(expires), INDEX sessions_owner_idx(owner_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
]


def fingerprints(cfg):
    return {'cookie_key': hashlib.sha256(('cookie:' + cfg.secret_key).encode()).hexdigest(),
            'token_key': hashlib.sha256(('token:' + cfg.token_key).encode()).hexdigest()}


def metadata(con):
    return {row['name']: row['value'] for row in con.execute('SELECT name,value FROM app_meta')}


def check_configuration(con, cfg):
    meta = metadata(con)
    if meta.get('schema_version') not in SUPPORTED_VERSIONS or meta.get('ready') != '1':
        raise database.ConfigurationError('영속 DB 초기화/이전을 먼저 완료하세요. 빈 DB로 자동 대체하지 않습니다.')
    for name, expected in fingerprints(cfg).items():
        if not hmac.compare_digest(meta.get(name, ''), expected):
            raise database.ConfigurationError('저장된 인증 키와 설정이 다릅니다. 기존 키를 유지하거나 운영 문서의 인증 초기화 절차를 따르세요.')
    return meta


def verify_ready(cfg):
    cfg.prepare()
    with database.connect(cfg) as con:
        meta = check_configuration(con, cfg)
    verify_binding(cfg, meta)


def verify_binding(cfg, meta):
    marker = cfg.state_dir / '.storage-id'
    if (meta.get('schema_version') not in SUPPORTED_VERSIONS or marker.is_symlink() or not marker.is_file()
            or marker.stat().st_size > 64 or marker.read_text() != meta.get('storage_id')):
        raise database.ConfigurationError('DB와 영속 파일 폴더가 일치하지 않습니다. 마운트와 KOCASE_STATE_DIR를 확인하세요.')


def _tables(cur):
    cur.execute('SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')
    return dict(cur.fetchall())


def _empty_storage(cfg):
    from .audit import directory
    if (cfg.state_dir/'.storage-id').exists() or (cfg.state_dir/'.storage-id').is_symlink():
        raise RuntimeError('이미 식별자가 있는 파일 폴더입니다. manage diagnose/check로 상태를 확인하세요.')
    allowed = {cfg.work_dir.resolve(), directory(cfg).resolve()}
    if any(p.is_symlink() or not p.is_dir() or not any(
            p.resolve() == target or p.resolve() in target.parents
            for target in allowed) for p in cfg.state_dir.rglob('*')):
        raise RuntimeError('처음 초기화할 때는 비어 있는 전용 저장 폴더가 필요합니다. 기존 파일을 삭제하지 마세요.')


def _resume_empty(cur, cfg, tables):
    """Adopt only a recognisable, entirely unused pre-metadata 0.2.6 schema.

    No DROP/ALTER/TRUNCATE, no token/job deletion, no metadata replacement.
    Existing triggers or incomplete/unrecognised layouts require diagnosis.
    """
    expected, indexes = {}, {}
    for sql in SCHEMA:
        name = re.search(r'CREATE TABLE IF NOT EXISTS (\w+)', sql)[1]
        body = sql[sql.index('(')+1:sql.rindex(') ENGINE')]
        columns = []
        keys = []
        for item in re.split(r',\s*(?![^()]*\))', body):
            index = re.match(r'\s*(UNIQUE\s+)?INDEX\s+(\w+)\(([^)]+)\)', item)
            primary = re.match(r'\s*PRIMARY KEY\(([^)]+)\)', item)
            if index:
                keys.extend((index[2], 0 if index[1] else 1, pos, col.strip())
                            for pos, col in enumerate(index[3].split(','), 1))
            elif primary:
                keys.extend(('PRIMARY', 0, pos, col.strip()) for pos, col in enumerate(primary[1].split(','), 1))
            pair = re.match(r'\s*(\w+)\s+(\w+(?:\(\d+\))?)', item)
            if pair and pair[1].upper() not in {'INDEX', 'PRIMARY', 'UNIQUE'}:
                column_type = pair[2].lower()
                nullable = 'NO' if 'NOT NULL' in item or 'PRIMARY KEY' in item else 'YES'
                collation = ('ascii_bin' if 'CHARACTER SET ascii' in item else 'utf8mb4_bin') if 'char' in column_type or 'text' in column_type else None
                columns.append((pair[1], column_type, nullable, collation))
                if 'PRIMARY KEY' in item:
                    keys.append(('PRIMARY', 0, 1, pair[1]))
        expected[name] = columns
        indexes[name] = sorted(keys)
    if set(tables) != set(expected) or any(engine != 'InnoDB' for engine in tables.values()):
        raise RuntimeError('복구 가능한 빈 초기 스키마와 다릅니다. manage diagnose 결과를 확인하세요.')
    cur.execute('SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()')
    if cur.fetchone()[0]:
        raise RuntimeError('기존 트리거가 있어 자동 복구를 거부합니다. 기존 DB를 삭제하지 마세요.')
    for table, columns in expected.items():
        cur.execute('SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLLATION_NAME FROM information_schema.COLUMNS '
                    'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION', (table,))
        actual = [(name, re.sub(r'\b(int|bigint)\(\d+\)', r'\1', kind), nullable, collation)
                  for name, kind, nullable, collation in cur.fetchall()]
        if actual != columns:
            raise RuntimeError('기존 테이블 구조가 다릅니다. 자동 복구하지 않습니다.')
        cur.execute('SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME FROM information_schema.STATISTICS '
                    'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s', (table,))
        if sorted(cur.fetchall()) != indexes[table]:
            raise RuntimeError('기존 테이블 인덱스가 다릅니다. 자동 복구하지 않습니다.')
        if table not in {'mutation_lock', 'storage_budget'}:
            cur.execute(f'SELECT COUNT(*) FROM `{table}`')
            if cur.fetchone()[0]:
                raise RuntimeError('기존 데이터 또는 초기화 정보가 있어 복구하지 않습니다. manage diagnose/check를 사용하세요.')
    cur.execute('SELECT name FROM mutation_lock')
    if cur.fetchall() not in [(), (('write',),)]:
        raise RuntimeError('초기 잠금 상태가 달라 복구하지 않습니다.')
    cur.execute('SELECT id,used_bytes,max_bytes FROM storage_budget')
    if cur.fetchall() not in [(), ((1, 0, cfg.max_db_bytes),)]:
        raise RuntimeError('초기 저장량 정보가 달라 복구하지 않습니다.')


class InitFailure(RuntimeError):
    def __init__(self, stage, code=None):
        code = code if isinstance(code, int) else None
        hint = {
            1044: 'DB 이름 접두사와 DB 권한을 확인하세요.',
            1045: 'DB 인증정보를 확인하세요.',
            1142: '해당 DB 작업 권한이 없습니다.',
            1205: '잠금 대기 시간이 초과되었습니다.',
            1419: '바이너리 로그 환경에서 트리거 생성 권한이 제한되었습니다.',
            2003: 'DB 서버에 연결할 수 없습니다.',
            2013: 'DB 연결이 중단되었습니다.',
        }.get(code, 'manage diagnose로 현재 상태를 확인하세요. 기존 DB를 삭제하지 마세요.')
        super().__init__(f'초기화 실패: 단계={stage}, DB 코드={code or "없음"}. {hint}')


def initialize(cfg, *, admission_delay=86400, ready=True, resume=False):
    """Operator command only; a failed initial setup can be resumed if empty."""
    cfg.prepare()
    database.validate_database(cfg)
    stage, raw = 'connect', None
    try:
        raw = database.mysql_connection(cfg, database=False)
        with raw.cursor() as cur:
            stage = 'create_database'
            if not resume:
                cur.execute(f'CREATE DATABASE IF NOT EXISTS `{cfg.db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
            cur.execute(f'USE `{cfg.db_name}`')
        stage = 'schema_lock'
        with database.named_lock(cfg, 'schema', timeout=2):
            with raw.cursor() as cur:
                stage = 'inspect'
                tables = _tables(cur)
                _empty_storage(cfg)
                if tables:
                    if not resume:
                        raise RuntimeError('기존 DB를 덮어쓰지 않습니다. manage diagnose로 상태를 확인하세요.')
                    _resume_empty(cur, cfg, tables)
                elif resume:
                    raise RuntimeError('복구할 테이블이 없습니다. 새 DB는 manage init으로 초기화하세요.')
                stage = 'create_tables'
                for sql in SCHEMA:
                    cur.execute(sql)
                stage = 'seed'
                cur.execute("SELECT COUNT(*) FROM mutation_lock WHERE name='write'")
                if not cur.fetchone()[0]:
                    cur.execute("INSERT INTO mutation_lock VALUES('write')")
                cur.execute('SELECT COUNT(*) FROM storage_budget WHERE id=1')
                if not cur.fetchone()[0]:
                    cur.execute('INSERT INTO storage_budget VALUES(1,0,%s)', (cfg.max_db_bytes,))
            raw.commit()
            # Schema 2 enforces byte limits in the same application transaction;
            # no SUPER privileges, triggers, or global DB configuration changes.
            stage = 'storage_marker'
            storage_id = uuid.uuid4().hex
            fd = os.open(cfg.state_dir/'.storage-id', os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(storage_id)
                stream.flush(); os.fsync(stream.fileno())
            stage = 'metadata'
            with raw.cursor() as cur:
                cur.execute('SELECT UNIX_TIMESTAMP(NOW(6))')
                now = float(cur.fetchone()[0])
                values = {'schema_version': SCHEMA_VERSION, 'ready': '1' if ready else '0', 'storage_id': storage_id,
                          'admission_after': str(now + admission_delay), **fingerprints(cfg)}
                cur.executemany('INSERT INTO app_meta(name,value) VALUES(%s,%s)', list(values.items()))
            raw.commit()
            stage = 'release_lock'
    except database.pymysql.MySQLError as e:
        raise InitFailure(stage, e.args[0] if e.args else None) from None
    except database.DatabaseUnavailable as e:
        raise InitFailure(stage, getattr(e, 'code', None)) from None
    except OSError as e:
        raise RuntimeError(f'초기화 실패: 단계={stage}, 파일 오류 코드={e.errno}. 폴더 권한과 마운트를 확인하세요.') from None
    finally:
        if raw:
            raw.close()


def diagnose(cfg):
    """Read-only summary; no credentials, hashes, IDs, tokens or row contents."""
    database.validate_storage(cfg)
    raw = database.mysql_connection(cfg, database=False)
    try:
        with raw.cursor() as cur:
            cur.execute('SELECT @@log_bin,@@log_bin_trust_function_creators')
            logging, trusted = cur.fetchone()
            report = {'binary_log': bool(logging), 'trusted_function_creators': bool(trusted)}
            cur.execute('SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s', (cfg.db_name,))
            report['database_exists'] = bool(cur.fetchone()[0])
            if not report['database_exists']:
                return report
            cur.execute(f'USE `{cfg.db_name}`')
            tables = _tables(cur)
            expected = {re.search(r'CREATE TABLE IF NOT EXISTS (\w+)', sql)[1] for sql in SCHEMA}
            report['unexpected_tables'] = len(set(tables)-expected)
            report['rows'] = {}
            for table in sorted(set(tables) & expected):
                cur.execute(f'SELECT COUNT(*) FROM `{table}`')
                report['rows'][table] = cur.fetchone()[0]
            cur.execute('SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE()')
            report['triggers'] = cur.fetchone()[0]
            report['storage_marker_exists'] = (cfg.state_dir/'.storage-id').exists()
            if 'app_meta' in tables:
                cur.execute("SELECT name,value FROM app_meta WHERE name IN ('schema_version','ready')")
                meta = dict(cur.fetchall())
                report['schema_version'] = meta.get('schema_version') if meta.get('schema_version') in SUPPORTED_VERSIONS else None
                report['ready'] = meta.get('ready') if meta.get('ready') in {'0', '1'} else None
            return report
    finally:
        raw.close()

