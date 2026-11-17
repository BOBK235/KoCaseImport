"""Explicit production initialization, key binding and schema verification."""
import hashlib
import hmac
import os
from pathlib import Path
import uuid

from . import database

SCHEMA_VERSION = '1'
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
    if meta.get('schema_version') != SCHEMA_VERSION or meta.get('ready') != '1':
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
    if (meta.get('schema_version') != SCHEMA_VERSION or marker.is_symlink() or not marker.is_file()
            or marker.stat().st_size > 64 or marker.read_text() != meta.get('storage_id')):
        raise database.ConfigurationError('DB와 영속 파일 폴더가 일치하지 않습니다. 마운트와 KOCASE_STATE_DIR를 확인하세요.')


def _triggers(raw):
    # Enforce a transactional logical byte budget; indexes/engine overhead are
    # separate. All application mutations take mutation_lock before row locks.
    with raw.cursor() as cur:
        for table in ('jobs', 'admissions', 'web_sessions', 'oauth_grants'):
            cur.execute('SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION', (table,))
            columns = [row[0] for row in cur.fetchall()]
            def size(prefix):
                return '64+' + '+'.join(f'COALESCE(OCTET_LENGTH({prefix}.`{c}`),0)' for c in columns)
            for action in ('INSERT', 'UPDATE', 'DELETE'):
                delta = size('NEW') if action == 'INSERT' else '-(' + size('OLD') + ')' if action == 'DELETE' else '(' + size('NEW') + ')-(' + size('OLD') + ')'
                cur.execute(f'''CREATE OR REPLACE TRIGGER budget_{table}_{action.lower()} BEFORE {action} ON `{table}` FOR EACH ROW
                BEGIN
                    DECLARE delta BIGINT;
                    SET delta={delta};
                    IF delta>0 THEN
                        UPDATE storage_budget SET used_bytes=used_bytes+delta WHERE id=1 AND used_bytes+delta<=max_bytes;
                        IF ROW_COUNT()<>1 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='KOCASE_DB_LIMIT'; END IF;
                    ELSEIF delta<0 THEN
                        UPDATE storage_budget SET used_bytes=GREATEST(0,used_bytes+delta) WHERE id=1;
                    END IF;
                END''')


def initialize(cfg, *, admission_delay=86400, ready=True):
    """Operator command only; runtime startup NEVER creates a production DB."""
    cfg.prepare()
    database.validate_database(cfg)
    raw = database.mysql_connection(cfg, database=False)
    try:
        with raw.cursor() as cur:
            cur.execute(f'CREATE DATABASE IF NOT EXISTS `{cfg.db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
            cur.execute(f'USE `{cfg.db_name}`')
        with database.named_lock(cfg, 'schema', timeout=2):
            with raw.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
                if cur.fetchone()[0]:
                    raise RuntimeError('기존 DB를 덮어쓰지 않습니다. 다른 비공개 DB 이름으로 초기화하거나 기존 설정을 확인하세요.')
                if (cfg.state_dir/'.storage-id').exists():
                    raise RuntimeError('이미 사용 중인 파일 폴더입니다. 기존 DB/폴더 조합을 확인하세요.')
                # prepare() may have created an otherwise empty custom work
                # path. Allow only empty configured directory trees, no files.
                from .audit import directory as audit_directory
                allowed = {cfg.work_dir.resolve(), audit_directory(cfg).resolve()}
                if any(p.is_symlink() or not p.is_dir() or not any(
                        p.resolve() == target or p.resolve() in target.parents
                        for target in allowed) for p in cfg.state_dir.rglob('*')):
                    raise RuntimeError('처음 초기화할 때는 비어 있는 전용 저장 폴더가 필요합니다.')
                for sql in SCHEMA:
                    cur.execute(sql)
                cur.execute("INSERT INTO mutation_lock VALUES('write')")
                cur.execute('INSERT INTO storage_budget VALUES(1,0,%s)', (cfg.max_db_bytes,))
            raw.commit()
            _triggers(raw)
            storage_id = uuid.uuid4().hex
            fd = os.open(cfg.state_dir/'.storage-id', os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(storage_id)
                stream.flush(); os.fsync(stream.fileno())
            with raw.cursor() as cur:
                cur.execute('SELECT UNIX_TIMESTAMP(NOW(6))')
                now = float(cur.fetchone()[0])
                values = {'schema_version': SCHEMA_VERSION, 'ready': '1' if ready else '0', 'storage_id': storage_id,
                          'admission_after': str(now + admission_delay), **fingerprints(cfg)}
                cur.executemany('INSERT INTO app_meta(name,value) VALUES(%s,%s)', list(values.items()))
            raw.commit()
    finally:
        raw.close()
