"""Regression: failed 0.2.6 trigger setup under a restricted MariaDB account."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pymysql
import pytest

from test_persistence import mysql_env
from test_auth_web import env, login
from kocaseimport import database, persistence, store


def unfinished(cfg):
    # Synthetic fixture only: mimic committed 0.2.6 tables before key metadata.
    raw = database.mysql_connection(cfg)
    with raw.cursor() as cur:
        cur.execute('DELETE FROM app_meta')
    raw.commit(); raw.close()
    (cfg.state_dir/'.storage-id').unlink()


def test_restricted_trigger_failure_then_resume_without_dropping_tables(mysql_env):
    _, _, cfg = mysql_env
    unfinished(cfg)
    raw = database.mysql_connection(cfg)
    try:
        with raw.cursor() as cur:
            cur.execute('SELECT @@log_bin,@@log_bin_trust_function_creators')
            log_bin, trusted = cur.fetchone()
            if log_bin and not trusted:
                with pytest.raises(pymysql.MySQLError) as e:
                    cur.execute('CREATE TRIGGER budget_jobs_insert BEFORE INSERT ON jobs FOR EACH ROW SET @unused=1')
                assert e.value.args[0] == 1419
        before = persistence.diagnose(cfg)
        assert before['rows']['app_meta'] == 0 and before['triggers'] == 0
        with pytest.raises(RuntimeError, match='기존 DB'):
            persistence.initialize(cfg)
        persistence.initialize(cfg, resume=True, admission_delay=0)
        persistence.verify_ready(cfg)
        after = persistence.diagnose(cfg)
        assert after['schema_version'] == '2' and after['ready'] == '1'
        assert after['triggers'] == 0 and after['rows']['jobs'] == 0
        assert after['rows']['app_meta'] == 6
        with pytest.raises(RuntimeError):
            persistence.initialize(cfg, resume=True)
        assert persistence.diagnose(cfg) == after
    finally:
        raw.close()


def test_resume_preserves_user_data_and_diagnose_is_read_only(mysql_env):
    app, _, cfg = mysql_env
    client = app.test_client(); identity, _ = login(client, 'alice')
    job = 'a'*12
    with database.connect(cfg) as con:
        con.execute('INSERT INTO jobs(id,case_number,status,created_at,updated_at,owner_id,auth_id) VALUES(?,?,?,?,?,?,?)',
                    (job, '97헌가12', 'done', 0, 0, '101', identity['auth_id']))
    unfinished(cfg)
    before = persistence.diagnose(cfg)
    with pytest.raises(RuntimeError, match='기존 데이터'):
        persistence.initialize(cfg, resume=True)
    assert persistence.diagnose(cfg) == before
    raw = database.mysql_connection(cfg)
    with raw.cursor() as cur:
        cur.execute('SELECT id FROM jobs')
        assert cur.fetchone()[0] == job
    raw.close()
    assert not (cfg.state_dir/'.storage-id').exists()


def test_resume_rejects_unknown_schema_and_existing_files(mysql_env):
    _, _, cfg = mysql_env
    unfinished(cfg)
    path = cfg.work_dir/'keep.txt'; path.write_text('keep')
    with pytest.raises(RuntimeError, match='비어 있는'):
        persistence.initialize(cfg, resume=True)
    assert path.read_text() == 'keep'
    path.unlink()
    raw = database.mysql_connection(cfg)
    with raw.cursor() as cur:
        cur.execute('ALTER TABLE jobs ADD unknown_column INT')
    raw.close()
    with pytest.raises(RuntimeError, match='구조가 다릅니다'):
        persistence.initialize(cfg, resume=True)
    assert persistence.diagnose(cfg)['rows']['app_meta'] == 0


def test_budget_counts_changes_deletes_and_last_slot_atomically(mysql_env):
    _, _, cfg = mysql_env
    def used():
        with database.connect(cfg) as con:
            return con.execute('SELECT used_bytes FROM storage_budget WHERE id=1').fetchone()[0]
    def add(key):
        with database.connect(cfg) as con:
            con.execute('INSERT INTO admissions VALUES(?,?,?,?)', (key, '101', 'prepare', 0))
    add('aa'); size = used()
    with database.connect(cfg) as con:
        con.execute('DELETE FROM admissions')
        con.execute('UPDATE storage_budget SET max_bytes=? WHERE id=1', (size,))
    assert used() == 0
    barrier = Barrier(2)
    def contender(key):
        barrier.wait()
        try:
            add(key); return True
        except database.DatabaseFull:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(contender, ('bb', 'cc'))) == [False, True]
    assert used() == size
    with pytest.raises(database.DatabaseFull):
        with database.connect(cfg) as con:
            con.execute("UPDATE admissions SET owner_id=?", ('x'*50,))
    assert used() == size
    with database.connect(cfg) as con:
        assert con.execute('SELECT owner_id FROM admissions').fetchone()[0] == '101'
        con.execute('UPDATE storage_budget SET max_bytes=? WHERE id=1', (cfg.max_db_bytes,))
        con.executemany('INSERT INTO admissions VALUES(?,?,?,?)', [('d'+str(i), '101', 'prepare', 0) for i in range(5)])
        con.bounded_delete('admissions', 'created_at<=?', (0,), 3)
        assert con.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 3
        con.execute('DELETE FROM admissions')
    assert used() == 0


def test_initialization_error_exposes_only_stage_and_numeric_code(mysql_env, monkeypatch):
    _, _, cfg = mysql_env
    def fail(*args, **kwargs):
        raise pymysql.OperationalError(1045, 'SECRET-password-and-SQL')
    with monkeypatch.context() as patch:
        patch.setattr(database, 'mysql_connection', fail)
        with pytest.raises(persistence.InitFailure) as e:
            persistence.initialize(cfg)
    assert '1045' in str(e.value) and 'connect' in str(e.value)
    assert 'SECRET' not in str(e.value)
