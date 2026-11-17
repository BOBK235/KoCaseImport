"""Import a quiesced legacy snapshot into an empty private database only."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tarfile
import tempfile

from cryptography.fernet import Fernet

from . import audit, database, persistence

TABLES = ('jobs', 'admissions', 'oauth_grants', 'web_sessions')


def _set(con, name, value):
    con.execute('INSERT INTO app_meta(name,value) VALUES(?,?) ON DUPLICATE KEY UPDATE value=VALUES(value)', (name, str(value)))


def _empty_target(con):
    if any(con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in TABLES):
        raise RuntimeError('이전 대상 DB가 비어 있지 않습니다. 기존 데이터를 덮어쓰지 않습니다.')
    lease = con.execute("SELECT expires FROM runtime_lease WHERE name='worker'").fetchone()
    if lease and lease[0] > con.now():
        raise RuntimeError('워커를 중지하고 실행권이 만료된 뒤 이전하세요.')


def import_snapshot(cfg, filename):
    cfg.prepare()
    with tarfile.open(filename, 'r:') as tar:
        members = tar.getmembers()
        if len(members) > 20000 or len({x.name for x in members}) != len(members):
            raise RuntimeError('이전 파일의 항목 수 또는 중복 경로를 확인하세요.')
        meta_member = tar.getmember('metadata.json')
        if not meta_member.isfile() or meta_member.size > 8*1024*1024:
            raise RuntimeError('이전 메타데이터를 확인하세요.')
        meta = json.load(tar.extractfile(meta_member))
        if (meta.get('version') != 1 or meta.get('quiesced') is not True
                or not re.fullmatch('[0-9a-f]{32}', meta.get('snapshot_id', ''))
                or meta.get('keys') != persistence.fingerprints(cfg)):
            raise RuntimeError('정지 상태에서 생성한 이전 파일과 기존 인증 키가 필요합니다.')
        permitted = {}
        for m in members:
            p = PurePosixPath(m.name)
            if not m.isfile() or p.is_absolute() or any(x in {'.', '..'} for x in p.parts) or str(p) != m.name:
                raise RuntimeError('이전 파일의 경로 또는 파일 형식을 확인하세요.')
            if m.name == 'metadata.json':
                continue
            if m.name == 'db.sqlite3':
                cap = cfg.max_db_bytes
            elif len(p.parts) == 3 and p.parts[0] == 'work' and re.fullmatch('[0-9a-f]{12}', p.parts[1]) and p.suffix in {'.pdf', '.json', '.part'}:
                cap = cfg.max_manifest_bytes if p.suffix == '.json' else cfg.max_pdf_bytes
            elif len(p.parts) == 2 and p.parts[0] == 'audit' and audit.LOG_NAME.fullmatch(p.name):
                cap = cfg.audit_file_bytes
            else:
                raise RuntimeError('허용되지 않은 이전 파일 항목입니다.')
            if not 0 <= m.size <= cap:
                raise RuntimeError('이전 파일 크기 한도를 초과했습니다.')
            permitted[m.name] = m
        if 'db.sqlite3' not in permitted or set(permitted) != set(meta.get('sha256', {})):
            raise RuntimeError('이전 파일 목록이 일치하지 않습니다.')
        if sum(m.size for k, m in permitted.items() if k.startswith('work/')) > cfg.max_work_bytes:
            raise RuntimeError('작업 파일 보관 한도를 초과했습니다.')
        logs = [m for k, m in permitted.items() if k.startswith('audit/')]
        if len(logs) > cfg.audit_file_count:
            raise RuntimeError('요청 로그 보관 한도를 초과했습니다.')
        if shutil.disk_usage(cfg.state_dir).free < sum(m.size for m in permitted.values()) + 64*1024*1024:
            raise RuntimeError('이전 파일을 검증할 공간이 부족합니다.')
        with tempfile.TemporaryDirectory(prefix='.import-', dir=cfg.state_dir) as folder:
            stage = Path(folder)
            for name, m in permitted.items():
                dest = stage/name
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with tar.extractfile(m) as source, dest.open('xb') as target:
                    os.fchmod(target.fileno(), 0o600)
                    while data := source.read(1024*1024):
                        target.write(data); digest.update(data)
                if digest.hexdigest() != meta['sha256'][name]:
                    raise RuntimeError('이전 파일의 무결성 검증에 실패했습니다.')
            source = sqlite3.connect((stage/'db.sqlite3').as_uri()+'?mode=ro', uri=True)
            source.row_factory = sqlite3.Row
            try:
                source.execute('PRAGMA trusted_schema=OFF')
                if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('이전 DB 검사에 실패했습니다.')
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
                if not set(TABLES) <= tables:
                    raise RuntimeError('0.2.5 DB의 필수 테이블이 없습니다.')
                for table, limit in (('jobs', cfg.max_job_records),
                                     ('web_sessions', cfg.max_auth_records),
                                     ('oauth_grants', cfg.max_auth_records)):
                    if source.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] > limit:
                        raise RuntimeError('이전 DB의 보관 건수 한도를 초과했습니다.')
                if source.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]:
                    raise RuntimeError('진행 중 작업이 포함된 이전 파일은 사용할 수 없습니다.')
                with database.connect(cfg, verify=False) as con:
                    con.begin(); _empty_target(con)
                    existing = persistence.metadata(con)
                    persistence.verify_binding(cfg, existing)
                    if existing.get('import_id') not in {None, meta['snapshot_id']}:
                        raise RuntimeError('다른 이전 작업이 진행 중입니다.')
                    if existing.get('cookie_key') != meta['keys']['cookie_key'] or existing.get('token_key') != meta['keys']['token_key']:
                        raise RuntimeError('기존 키와 초기화한 DB의 키가 다릅니다.')
                    _set(con, 'ready', '0'); _set(con, 'import_id', meta['snapshot_id'])
                with database.connect(cfg, verify=False) as con:
                    con.begin(); _empty_target(con)
                    for table in TABLES:
                        columns = [r[0] for r in con.execute('SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? ORDER BY ORDINAL_POSITION', (table,))]
                        columns = [c for c in columns if c in {r[1] for r in source.execute(f'PRAGMA table_info({table})')}]
                        for row in source.execute(f'SELECT * FROM {table}'):
                            data = {c: row[c] for c in columns}
                            if table == 'oauth_grants' and data['token']:
                                Fernet(cfg.token_key.encode()).decrypt(data['token'].encode())
                            if table == 'jobs':
                                payload = json.loads(data['payload'] or '{}')
                                if payload.get('pdf_path'):
                                    payload['pdf_path'] = str(cfg.work_dir/data['id']/Path(payload['pdf_path']).name)
                                data['payload'] = json.dumps(payload, ensure_ascii=False)
                            sql = f"INSERT INTO {table} ({','.join('`'+c+'`' for c in columns)}) VALUES({','.join('?' for _ in columns)})"
                            con.execute(sql, tuple(data[c] for c in columns))
                    for name, destination in (('work', cfg.work_dir), ('audit', audit.directory(cfg))):
                        if destination.is_symlink():
                            raise RuntimeError('이전 대상 폴더가 심볼릭 링크입니다.')
                        # Only an empty DB with this same import_id can reach
                        # this point, including retry after interrupted import.
                        if destination.exists():
                            shutil.rmtree(destination)
                        (stage/name).mkdir(mode=0o700, exist_ok=True)
                        (stage/name).rename(destination)
                    _set(con, 'admission_after', '0')
                    _set(con, 'ready', '1')
            finally:
                source.close()
    persistence.verify_ready(cfg)


def revoke_auth(cfg, *, rotate=False, restored=False):
    with database.connect(cfg, verify=not rotate) as con:
        con.begin()
        persistence.verify_binding(cfg, persistence.metadata(con))
        lease = con.execute("SELECT expires FROM runtime_lease WHERE name='worker'").fetchone()
        if lease and lease[0] > con.now():
            raise RuntimeError('서비스를 중지하고 워커 실행권 만료 후 진행하세요.')
        now = con.now()
        con.execute('DELETE FROM web_sessions')
        con.execute("UPDATE oauth_grants SET token='',expires=CASE WHEN expires<? THEN expires ELSE ? END", (now+600, now+600))
        con.execute("UPDATE jobs SET status='failed',step='failed',error='auth_expired',updated_at=? WHERE status IN ('pending','running')", (now,))
        if rotate:
            for name, value in persistence.fingerprints(cfg).items():
                _set(con, name, value)
        if restored:
            _set(con, 'admission_after', now+cfg.quota_window)
