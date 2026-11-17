"""SQLite development / private ToolsDB production, with no silent fallback."""
from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import sqlite3
import time

import pymysql


class DatabaseUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('저장소를 사용할 수 없습니다. 작업을 새로 접수하지 않았으니 잠시 후 다시 확인해 주세요.')


class DatabaseFull(DatabaseUnavailable):
    def __init__(self):
        RuntimeError.__init__(self, '작업 저장 용량 한도에 도달했습니다. 보관 기록 정리 후 다시 시도해 주세요.')


class ConfigurationError(DatabaseUnavailable):
    def __init__(self, message):
        RuntimeError.__init__(self, message)


def validate_storage(cfg):
    if cfg.db_backend not in {'sqlite', 'mariadb'}:
        raise RuntimeError('KOCASE_DB_BACKEND는 mariadb 또는 sqlite여야 합니다.')
    if cfg.toolforge and (cfg.db_backend != 'mariadb' or not cfg.tool_data_dir):
        raise RuntimeError('Toolforge에서는 영속 ToolsDB(mariadb)를 사용해야 합니다.')
    if cfg.db_backend == 'sqlite':
        return
    if not cfg.state_dir or not cfg.state_dir.is_absolute():
        raise RuntimeError('KOCASE_STATE_DIR에 비공개 영속 저장 폴더를 지정하세요.')
    root = cfg.state_dir.resolve()
    if root == root.parent or 'public_html' in root.parts or cfg.state_dir.is_symlink():
        raise RuntimeError('영속 저장 폴더는 비공개 전용 디렉터리여야 합니다.')
    work = cfg.work_dir.resolve()
    if root not in work.parents or cfg.work_dir.is_symlink():
        raise RuntimeError('KOCASE_WORK_DIR는 KOCASE_STATE_DIR 아래의 전용 폴더여야 합니다.')
    if cfg.tool_data_dir:
        tool = cfg.tool_data_dir.resolve()
        mounted = False
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left, right = line.split(' - ', 1)
            mount = Path(left.split()[4])
            if right.split()[0] in {'nfs', 'nfs4'} and (mount == tool or mount in tool.parents):
                mounted = True
        if not mounted or not tool.is_dir() or tool not in root.parents:
            raise RuntimeError('Toolforge NFS 마운트(--mount=all)와 KOCASE_STATE_DIR를 확인하세요.')


def validate_database(cfg):
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', cfg.db_name or '') or cfg.db_name.lower().endswith('_p'):
        raise RuntimeError('공개 접미사 _p가 없는 비공개 KOCASE_DB_NAME이 필요합니다.')
    if not cfg.db_user or not cfg.db_password:
        raise RuntimeError('TOOL_TOOLSDB_USER / TOOL_TOOLSDB_PASSWORD 또는 DB 접속 설정이 필요합니다.')
    if cfg.db_host == 'tools.db.svc.wikimedia.cloud' and not cfg.db_name.startswith(cfg.db_user + '__'):
        raise RuntimeError('ToolsDB 이름은 DB 접속 사용자와 두 개의 밑줄로 시작해야 합니다.')
    if cfg.db_host not in {'tools.db.svc.wikimedia.cloud', 'localhost', '127.0.0.1', '::1'} and not cfg.db_ssl_ca:
        raise RuntimeError('외부 DB에는 KOCASE_DB_SSL_CA를 통한 TLS 인증서 검증이 필요합니다.')


def mysql_connection(cfg, *, timeout=5, database=True):
    validate_database(cfg)
    options = dict(host=cfg.db_host, port=cfg.db_port, user=cfg.db_user, password=cfg.db_password,
                   database=cfg.db_name if database else None, charset='utf8mb4', autocommit=False,
                   connect_timeout=max(1, min(int(timeout), 10)), read_timeout=10, write_timeout=10,
                   local_infile=False)
    if cfg.db_socket:
        options['unix_socket'] = cfg.db_socket
    if cfg.db_ssl_ca:
        options.update(ssl_ca=cfg.db_ssl_ca, ssl_verify_cert=True, ssl_verify_identity=True)
    return pymysql.connect(**options)


class Row:
    def __init__(self, keys, values):
        self._keys, self._values = keys, values
    def keys(self):
        return self._keys
    def __getitem__(self, key):
        return self._values[key if isinstance(key, int) else self._keys.index(key)]
    def __iter__(self):
        return iter(self._values)


class Result:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.keys = [x[0] for x in cursor.description] if cursor.description else []
    def fetchone(self):
        value = self.cursor.fetchone()
        return Row(self.keys, value) if value is not None else None
    def fetchall(self):
        return [Row(self.keys, row) for row in self.cursor.fetchall()]
    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class Connection:
    def __init__(self, raw, cfg, verify=True):
        self.raw, self.cfg, self.active = raw, cfg, False
        self.verify = verify
        self.mysql = cfg.db_backend == 'mariadb'
    def begin(self):
        if self.active:
            return
        if self.mysql:
            self.raw.begin()
            with self.raw.cursor() as cursor:
                cursor.execute("SELECT name FROM mutation_lock WHERE name='write' FOR UPDATE")
                if not cursor.fetchone():
                    raise DatabaseUnavailable()
        else:
            self.raw.execute('BEGIN IMMEDIATE')
        self.active = True
        if self.mysql and self.verify:
            from .persistence import check_configuration
            check_configuration(self, self.cfg)
    def execute(self, sql, params=()):
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            self.begin()
            return self
        if not self.mysql:
            return self.raw.execute(sql, params)
        if sql.lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
            self.begin()
        cursor = self.raw.cursor()
        # Application-owned SQL uses qmark placeholders, never literal '?'.
        cursor.execute(sql.replace('%', '%%').replace('?', '%s'), params)
        return Result(cursor)
    def executemany(self, sql, rows):
        if not self.mysql:
            return self.raw.executemany(sql, rows)
        self.begin()
        cursor = self.raw.cursor()
        cursor.executemany(sql.replace('%', '%%').replace('?', '%s'), rows)
        return Result(cursor)
    def executescript(self, sql):
        if self.mysql:
            raise RuntimeError('SQLite schema cannot be applied to MariaDB')
        return self.raw.executescript(sql)
    def commit(self):
        self.raw.commit()
        self.active = False
    def now(self):
        if self.mysql:
            return float(self.execute('SELECT UNIX_TIMESTAMP(NOW(6))').fetchone()[0])
        return time.time()
    def bounded_delete(self, table, where, args, limit):
        # Table/where are constants supplied by the application, not HTTP input.
        if self.mysql:
            self.execute(f'DELETE FROM {table} WHERE {where} LIMIT {int(limit)}', args)
        else:
            self.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT {int(limit)})', args)


@contextmanager
def connect(cfg, *, timeout=5, verify=True):
    raw = None
    try:
        if cfg.db_backend == 'mariadb':
            raw = mysql_connection(cfg, timeout=timeout)
            with raw.cursor() as cur:
                cur.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
                cur.execute('SET SESSION innodb_lock_wait_timeout=%s', (max(1, min(int(timeout), 10)),))
        else:
            cfg.prepare()
            raw = sqlite3.connect(cfg.db_path, timeout=timeout)
            raw.row_factory = sqlite3.Row
            raw.execute('PRAGMA secure_delete=ON')
            page_size = raw.execute('PRAGMA page_size').fetchone()[0]
            raw.execute(f'PRAGMA max_page_count={cfg.max_db_bytes // page_size}')
        con = Connection(raw, cfg, verify=verify)
        if cfg.db_backend == 'mariadb' and verify:
            from .persistence import check_configuration
            check_configuration(con, cfg)
        yield con
        con.commit()
    except (pymysql.MySQLError, sqlite3.Error) as e:
        if raw:
            try:
                raw.rollback()
            except pymysql.MySQLError:
                pass
        if 'KOCASE_DB_LIMIT' in str(e) or isinstance(e, sqlite3.Error) and getattr(e, 'sqlite_errorcode', 0) == sqlite3.SQLITE_FULL:
            raise DatabaseFull() from None
        raise DatabaseUnavailable() from None
    except BaseException:
        if raw:
            try:
                raw.rollback()
            except pymysql.MySQLError:
                pass
        raise
    finally:
        if raw:
            raw.close()


@contextmanager
def named_lock(cfg, purpose, *, timeout=2):
    """Secret-scoped, connection-bound lock; no transaction during file/network I/O."""
    name = hmac.new(cfg.secret_key.encode(), (cfg.db_name + ':' + purpose).encode(), hashlib.sha256).hexdigest()
    raw = None
    try:
        raw = mysql_connection(cfg)
        with raw.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s,%s)', (name, timeout))
            if cur.fetchone()[0] != 1:
                raise BlockingIOError('lock unavailable')
        def check():
            with raw.cursor() as cur:
                cur.execute('SELECT IS_USED_LOCK(%s)=CONNECTION_ID()', (name,))
                if cur.fetchone()[0] != 1:
                    raise DatabaseUnavailable()
        yield check
        check()
    except pymysql.MySQLError:
        raise DatabaseUnavailable() from None
    finally:
        if raw:
            raw.close()  # Closing releases the advisory lock, never reconnect.
