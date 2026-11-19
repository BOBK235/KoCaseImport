import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from test_auth_web import env, login, csrf, enqueue, completed_dry_run, enable_publication, BASE
from kocaseimport import auth, store, worker, ccourt
from kocaseimport.models import CaseMetadata
from kocaseimport.resources import cleanup_files


def seed_admissions(owner, kind, count, now):
    with store._connect() as con:
        con.executemany('INSERT INTO admissions VALUES(?,?,?,?)',
                        [(f'{owner}-{kind}-{i}', owner, kind, now) for i in range(count)])


def test_anonymous_traffic_and_login_starts_do_not_allocate_database_rows(env):
    app, seen, _ = env
    for _ in range(20):
        client = app.test_client()
        for route in ('/missing', '/jobs/missing', '/oauth/callback?state=bad&code=bad', '/'):
            client.get(route, base_url=BASE)
        token = csrf(client)
        for _ in range(3):
            assert client.post('/oauth/login', data={'csrf': token}, base_url=BASE).status_code == 302
    assert seen == []
    with store._connect() as con:
        for table in ('web_sessions', 'oauth_grants', 'jobs', 'admissions'):
            assert con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    with pytest.raises(auth.AuthError):
        auth.new_session()


def test_copied_prelogin_cookie_cannot_replay_successful_state(env):
    app, seen, _ = env
    client = app.test_client()
    response = client.post('/oauth/login', data={'csrf': csrf(client)}, base_url=BASE)
    state = parse_qs(urlsplit(response.location).query)['state'][0]
    pending_cookie = client.get_cookie('session', domain='tool.example').value
    assert client.get('/oauth/callback', query_string={'state': state, 'code': 'alice'}, base_url=BASE).status_code == 302
    replay = app.test_client()
    replay.set_cookie('session', pending_cookie, domain='tool.example')
    assert replay.get('/oauth/callback', query_string={'state': state, 'code': 'bob'}, base_url=BASE).status_code == 400
    assert len(seen) == 2
    with store._connect() as con:
        assert con.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM oauth_grants').fetchone()[0] == 1
    client.post('/logout', data={'csrf': csrf(client)}, base_url=BASE)
    replay.set_cookie('session', pending_cookie, domain='tool.example')
    assert replay.get('/oauth/callback', query_string={'state': state, 'code': 'bob'}, base_url=BASE).status_code == 400
    assert len(seen) == 2


def test_warning_at_80_reject_after_100_and_rolling_server_window(env, monkeypatch):
    app, _, _ = env
    clock = [time.time()]
    monkeypatch.setattr(store.time, 'time', lambda: clock[0])
    alice = app.test_client(); login(alice, 'alice')
    seed_admissions('101', 'prepare', 79, clock[0])
    assert 'limit 경고' not in alice.get('/', base_url=BASE).text
    job = enqueue(alice); store.update_job(job, status='failed')
    assert 'limit 경고' in alice.get('/jobs/'+job, base_url=BASE).text
    for _ in range(20):
        job = enqueue(alice); store.update_job(job, status='failed')
    assert 'limit 도달' in alice.get('/', base_url=BASE).text
    before = len(store.list_jobs('101'))
    response = alice.post('/jobs', data={'csrf': csrf(alice), 'case_number': '2016헌나1'}, base_url=BASE)
    assert response.status_code == 429 and response.headers['Retry-After'] == '86400'
    assert len(store.list_jobs('101')) == before
    bob = app.test_client(); login(bob, 'bob'); enqueue(bob)
    assert store.quota_status('202')['prepare'] == 1
    clock[0] += 86400
    login(alice, 'alice')
    enqueue(alice)
    assert store.quota_status('101')['prepare'] == 1


def test_concurrent_last_slot_and_transaction_rollback(env):
    seed_admissions('101', 'prepare', 99, time.time())
    barrier = Barrier(2)
    def submit():
        barrier.wait()
        try:
            return store.create_job('2016헌나1', False, owner_id='101', auth_id='test')
        except store.ResourceLimit:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sum(x is not None for x in results) == 1
    assert store.quota_status('101')['prepare'] == 100
    seed_admissions('202', 'prepare', 99, time.time())
    seed_admissions('202', 'publish', 100, time.time())
    with pytest.raises(store.ResourceLimit):
        store.create_job('2016헌나1', True, owner_id='202', auth_id='test')
    assert store.quota_status('202')['prepare'] == 99
    assert store.list_jobs('202') == []


def test_followup_publication_has_separate_quota_and_no_double_charge(env, monkeypatch):
    app, _, cfg = env
    alice = app.test_client(); login(alice, 'alice')
    job, _ = completed_dry_run(alice, monkeypatch)
    enable_publication(cfg, monkeypatch)
    seed_admissions('101', 'publish', 100, time.time())
    response = alice.post('/jobs/'+job+'/publish', data={'csrf': csrf(alice)}, base_url=BASE)
    assert response.status_code == 429
    assert store.get_job(job)['live'] == 0
    with store._connect() as con:
        con.execute("UPDATE admissions SET created_at=0 WHERE job_id='101-publish-0'")
    for _ in range(2):
        assert alice.post('/jobs/'+job+'/publish', data={'csrf': csrf(alice)}, base_url=BASE).status_code == 302
    assert store.quota_status('101')['prepare'] == 1
    assert store.quota_status('101')['publish'] == 100


def test_global_queue_and_record_capacity(env, monkeypatch):
    _, _, cfg = env
    monkeypatch.setattr(store, 'settings', replace(cfg, max_active_jobs=1, max_job_records=1))
    job = store.create_job('2016헌나1', False, owner_id='101', auth_id='test')
    with pytest.raises(store.ResourceLimit, match='대기 한도'):
        store.create_job('2016헌나1', False, owner_id='202', auth_id='test')
    store.update_job(job, status='done')
    with pytest.raises(store.ResourceLimit, match='보관 한도'):
        store.create_job('2016헌나1', False, owner_id='202', auth_id='test')
    assert store.quota_status('202')['prepare'] == 0


@pytest.mark.parametrize('length', ['', '10000'])
def test_pdf_stream_size_bound_cleans_partial_file(env, monkeypatch, length):
    _, _, cfg = env
    monkeypatch.setattr(ccourt, 'settings', replace(cfg, max_pdf_bytes=10))
    client = ccourt.CCourtClient()
    response = Mock(status_code=200, headers={'Content-Length': length})
    response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [b'%PDF-', b'a' * 20]
    client.s.get = Mock(return_value=response)
    path = cfg.work_dir/'abcdef123456'/'test.pdf'
    with pytest.raises(ccourt.CCourtError, match='한도'):
        client.download_pdf(CaseMetadata('2016헌나1', pdf_url='https://ccourt.go.kr/test.pdf'), path)
    assert not path.exists() and not path.with_suffix('.pdf.part').exists()
    monkeypatch.setattr(ccourt, 'settings', replace(cfg, max_work_bytes=5))
    (cfg.work_dir/'occupied.pdf').write_bytes(b'12345')
    client.s.get.reset_mock()
    with pytest.raises(store.ResourceLimit, match='보관 공간'):
        client.download_pdf(CaseMetadata('2016헌나1', pdf_url='https://ccourt.go.kr/test.pdf'), path)
    client.s.get.assert_not_called()


def test_worker_retention_preserves_running_files_and_bounds_auth_cleanup(env):
    _, _, cfg = env
    expired = store.create_job('2016헌나1', False, owner_id='101', auth_id='a')
    active = store.create_job('2016헌나1', False, owner_id='202', auth_id='b')
    for job in (expired, active):
        folder = cfg.work_dir/job; folder.mkdir(); (folder/'test.pdf').write_bytes(b'%PDF-')
    with store._connect() as con:
        con.execute("UPDATE jobs SET status='done',updated_at=? WHERE id=?", (time.time()-cfg.pdf_retention-1, expired))
        con.execute("INSERT INTO web_sessions(id,csrf,expires) VALUES('old-guest','csrf',0)")
        assert 'sessions_expiry_idx' in {r[1] for r in con.execute('PRAGMA index_list(web_sessions)')}
    cleanup_files(cfg); auth.cleanup_auth()
    assert not (cfg.work_dir/expired).exists()
    assert (cfg.work_dir/active/'test.pdf').exists()
    with store._connect() as con:
        assert con.execute("SELECT 1 FROM web_sessions WHERE id='old-guest'").fetchone() is None


def test_job_timeout_terminates_child_and_removes_partial_file(env, monkeypatch):
    _, _, cfg = env
    monkeypatch.setattr(worker, 'settings', replace(cfg, job_timeout=1))
    job_id = store.create_job('2016헌나1', False, owner_id='101', auth_id='test')
    def slow(job):
        folder = cfg.work_dir/job['id']; folder.mkdir(); (folder/'test.pdf.part').write_bytes(b'partial')
        time.sleep(30)
    monkeypatch.setattr(worker, '_bounded_job', slow)
    worker.run_isolated(store.claim_next_job())
    assert store.get_job(job_id)['status'] == 'failed'
    assert store.public_job(store.get_job(job_id))['failure']['code'] == 'job_timeout'
    assert not (cfg.work_dir/job_id/'test.pdf.part').exists()


def test_admin_exempt_on_dry_run_live_and_followup(env, monkeypatch):
    app, _, cfg = env
    monkeypatch.setattr(store, 'settings', replace(cfg, admin_user_ids=('101',), max_user_active_jobs=1))
    alice = app.test_client()
    login(alice, 'alice')
    job, _ = completed_dry_run(alice, monkeypatch)
    for kind in ['prepare', 'publish']:
        seed_admissions('101', kind, 100, time.time())
    store.create_job('96헌마186', True, owner_id='101', auth_id='test')
    assert store.queue_publication(job, owner_id='101', auth_id='test')
    quota = store.quota_status('101')
    assert quota['exempt'] and not quota['warning'] and not quota['reached']
    assert quota['publish'] == 102
    assert '관리자 개인 한도 면제' in alice.get('/', base_url=BASE).text
