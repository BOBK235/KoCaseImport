import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from test_auth_web import env, login, csrf, enqueue, completed_dry_run, BASE
from kocaseimport import audit, auth, history, store, app as web
from kocaseimport.resources import storage_lock


def remove(client, *ids, confirmed='yes', token=None):
    return client.post('/jobs/delete', data={
        'csrf': csrf(client) if token is None else token, 'job_ids': list(ids), 'confirmed': confirmed,
    }, base_url=BASE)


def finish(client, cfg, **kwargs):
    job = enqueue(client, **kwargs)
    folder = cfg.work_dir / job
    folder.mkdir()
    (folder / 'test.pdf').write_bytes(b'%PDF-private-content')
    (folder / 'manifest.json').write_text('PRIVATE-MANIFEST')
    store.update_job(job, status='done', payload={'pdf_path': str(folder/'test.pdf'), 'secret': 'PRIVATE-PAYLOAD'}, log='PRIVATE-LOG')
    store.update_summary(job, pdf_downloaded=True, pdf_filename='test.pdf', page_count=1)
    return job


def test_selected_history_deletion_removes_db_artifacts_but_not_quota(env):
    app, _, cfg = env
    alice = app.test_client(); login(alice, 'alice')
    first, second = finish(alice, cfg, pdf_url='https://ccourt.go.kr/PRIVATE-URL'), finish(alice, cfg)
    preview = alice.post('/jobs/delete/confirm', data={'csrf': csrf(alice), 'job_ids': [first, second]}, base_url=BASE)
    assert preview.status_code == 200 and first in preview.text and second in preview.text
    assert store.get_job(first) and (cfg.work_dir/first/'test.pdf').exists()
    assert remove(alice, first, second).status_code == 302
    assert store.get_job(first) is None and store.get_job(second) is None
    assert not (cfg.work_dir/first).exists() and not (cfg.work_dir/second).exists()
    assert store.quota_status('101')['prepare'] == 2
    for suffix in ('', '/pdf'):
        assert alice.get('/jobs/'+first+suffix, base_url=BASE).status_code == 404
    assert alice.post('/jobs/'+first+'/publish', data={'csrf': csrf(alice)}, base_url=BASE).status_code == 404
    assert first not in alice.get('/', base_url=BASE).text
    rows, _ = audit.read_events(cfg)
    assert sum(r['action'] == '기록·파일 삭제 완료' for r in rows) == 2
    assert any(r['actor_name'] == 'alice' and r['case_number'] == '2016헌나1' for r in rows)
    raw = b''.join(p.read_bytes() for p in audit.directory(cfg).glob('*.jsonl'))
    for secret in (b'PRIVATE-', b'access-alice', b'refresh-alice', b'client-secret', b'csrf', b'auth_id'):
        assert secret not in raw


def test_delete_authorization_csrf_confirmation_and_active_state(env):
    app, _, cfg = env
    alice, bob = app.test_client(), app.test_client()
    login(alice, 'alice'); login(bob, 'bob')
    own, other = finish(alice, cfg), finish(bob, cfg)
    active = enqueue(alice)
    assert remove(app.test_client(), own).status_code == 401
    assert remove(alice, own, token='wrong').status_code == 400
    assert remove(alice, own, confirmed='no').status_code == 400
    assert remove(bob, own).status_code == 404
    assert remove(alice, own, other).status_code == 404
    assert remove(alice, own, active).status_code == 409
    assert remove(alice, '../outside').status_code == 400
    assert remove(alice, *([own]*31)).status_code == 400
    assert alice.get('/jobs/delete', base_url=BASE).status_code in (404, 405)
    for job in (own, other):
        assert store.get_job(job) and (cfg.work_dir/job/'test.pdf').exists()
    store.update_job(active, status='running')
    assert remove(alice, active).status_code == 409


def test_delete_and_publish_race_has_one_winner(env, monkeypatch):
    app, _, cfg = env
    alice = app.test_client(); ident, _ = login(alice, 'alice')
    job, _ = completed_dry_run(alice, monkeypatch)
    barrier = Barrier(2)
    def deletion():
        barrier.wait()
        try:
            return history.delete_jobs(cfg, [job], owner_id='101', username='alice')
        except history.HistoryError as e:
            assert e.status == 409
            return 0
    def publication():
        barrier.wait()
        return store.queue_publication(job, owner_id='101', auth_id=ident['auth_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleted, published = pool.submit(deletion), pool.submit(publication)
        deleted, published = deleted.result(), published.result()
    assert bool(deleted) != bool(published)
    if deleted:
        assert store.get_job(job) is None and not (cfg.work_dir/job).exists()
        assert store.quota_status('101')['publish'] == 0
    else:
        assert store.get_job(job)['status'] == 'pending' and (cfg.work_dir/job).is_dir()
        assert store.quota_status('101')['publish'] == 1


def test_deletion_filesystem_failure_busy_lock_and_retry(env, monkeypatch):
    app, _, cfg = env
    alice = app.test_client(); login(alice, 'alice')
    job = finish(alice, cfg)
    with storage_lock(cfg):
        assert remove(alice, job).status_code == 409
    original = history._remove_directory
    def fail(*args):
        raise PermissionError('PRIVATE-PATH')
    monkeypatch.setattr(history, '_remove_directory', fail)
    response = remove(alice, job)
    assert response.status_code == 503 and 'PRIVATE-PATH' not in response.text
    assert store.get_job(job) and (cfg.work_dir/job/'test.pdf').exists()
    assert not any(r['action'] == '기록·파일 삭제 완료' for r in audit.read_events(cfg)[0])
    monkeypatch.setattr(history, '_remove_directory', original)
    assert remove(alice, job).status_code == 302


def test_delete_never_follows_job_or_nested_symlinks(env):
    app, _, cfg = env
    alice, bob = app.test_client(), app.test_client()
    login(alice, 'alice'); login(bob, 'bob')
    other = finish(bob, cfg)
    own = finish(alice, cfg)
    (cfg.work_dir/own/'outside').symlink_to(cfg.work_dir/other, target_is_directory=True)
    assert remove(alice, own).status_code == 302
    assert (cfg.work_dir/other/'test.pdf').exists()
    linked = enqueue(alice); store.update_job(linked, status='done')
    (cfg.work_dir/linked).symlink_to(cfg.work_dir/other, target_is_directory=True)
    assert remove(alice, linked).status_code == 302
    assert (cfg.work_dir/other/'test.pdf').exists() and store.get_job(other)


def test_audit_failure_rolls_back_admission_and_blocks_deletion(env, monkeypatch):
    app, _, cfg = env
    alice = app.test_client(); login(alice, 'alice')
    job = finish(alice, cfg)
    def fail(*args, **kwargs):
        raise audit.AuditUnavailable('요청 기록을 사용할 수 없습니다.')
    monkeypatch.setattr(audit, 'record', fail)
    response = alice.post('/jobs', data={'csrf': csrf(alice), 'case_number': '97헌가12'}, base_url=BASE)
    assert response.status_code == 503
    assert len(store.list_jobs('101')) == 1 and store.quota_status('101')['prepare'] == 1
    assert remove(alice, job).status_code == 503
    assert store.get_job(job) and (cfg.work_dir/job/'test.pdf').exists()


def test_admin_requires_configured_subject_and_escapes_minimal_records(env, monkeypatch):
    app, _, cfg = env
    alice, bob = app.test_client(), app.test_client()
    login(alice, 'alice'); ident, _ = login(bob, 'bob')
    own, other = finish(alice, cfg), finish(bob, cfg)
    url = '/admin/requests'
    assert app.test_client().get(url, base_url=BASE).status_code == 401
    assert alice.get(url, base_url=BASE).status_code == 403  # No default admin.
    monkeypatch.setattr(web, 'settings', replace(cfg, admin_user_ids=('101',)))
    # The same username and client-supplied admin fields cannot grant access.
    with store._connect() as con:
        con.execute("UPDATE web_sessions SET username='alice' WHERE id=?", (ident['id'],))
    with bob.session_transaction(base_url=BASE) as session:
        session['is_admin'] = True
        session['owner_id'] = '101'
    assert bob.get(url+'?user_id=101&is_admin=1', base_url=BASE).status_code == 403
    assert '관리자 요청 기록' not in bob.get('/', base_url=BASE).text
    assert '관리자 요청 기록' in alice.get('/', base_url=BASE).text
    assert '101' in alice.get('/account', base_url=BASE).text
    assert remove(alice, own).status_code == 302
    audit.record(cfg, 'job_requested', actor_id='202', actor_name='<script>alert(1)</script>\nFORGED',
                 job={'id': other, 'case_number': '97헌가12', 'token': 'PRIVATE-TOKEN', 'pdf_url': 'PRIVATE-URL'})
    response = alice.get(url, base_url=BASE)
    assert response.status_code == 200 and own in response.text and other in response.text
    assert '기록·파일 삭제 완료' in response.text
    assert '<script>' not in response.text and '&lt;script&gt;' in response.text
    assert 'PRIVATE-' not in response.text and 'access-bob' not in response.text
    assert response.headers['Cache-Control'] == 'no-store'
    filtered = alice.get(url+'?user_id=202&case_number=97헌가12', base_url=BASE)
    assert other in filtered.text and own not in filtered.text
    assert alice.get(url+'?before='+'9'*5000, base_url=BASE).status_code == 400
    assert audit.directory(cfg).stat().st_mode & 0o777 == 0o700
    for p in audit.directory(cfg).glob('*.jsonl'):
        assert p.stat().st_mode & 0o777 == 0o600
        for line in p.read_text().splitlines():
            json.loads(line)
    # Admin is read-only: it still cannot download/delete another user's job.
    assert alice.get('/jobs/'+other+'/pdf', base_url=BASE).status_code == 404
    assert remove(alice, other).status_code == 404


def test_audit_rotation_retention_pagination_and_anonymous_no_growth(env, monkeypatch):
    app, _, cfg = env
    for _ in range(5):
        visitor = app.test_client()
        for url in ('/', '/admin/requests', '/account'):
            visitor.get(url, base_url=BASE)
    assert not audit.directory(cfg).exists()
    cfg = replace(cfg, audit_file_bytes=4096, audit_file_count=3, audit_retention=30*86400)
    clock = [audit.time.time_ns()]
    def now():
        clock[0] += 1_000_000
        return clock[0]
    monkeypatch.setattr(audit.time, 'time_ns', now)
    for i in range(100):
        audit.record(cfg, 'job_requested', actor_id='101', job={'id': f'{i:012x}', 'case_number': '97헌가12'})
    paths = list(audit.directory(cfg).glob('*.jsonl'))
    assert len(paths) <= 3 and all(p.stat().st_size <= 4096 for p in paths)
    rows, cursor = audit.read_events(cfg, limit=5)
    older, _ = audit.read_events(cfg, before=cursor, limit=5)
    assert len(rows) == len(older) == 5 and rows[0]['job_id'] == f'{99:012x}'
    assert not {r['at_ns'] for r in rows} & {r['at_ns'] for r in older}
    clock[0] += 31*86400*1_000_000_000
    audit.cleanup(cfg)
    assert not list(audit.directory(cfg).glob('*.jsonl'))
