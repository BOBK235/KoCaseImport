"""Real MariaDB tests, enabled only against an explicit local disposable server."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import io
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from unittest.mock import Mock

import pytest
import pymysql

from test_auth_web import env, login, csrf, BASE
from test_history_admin import finish, remove
from kocaseimport import auth, app as web, audit, ccourt, database, history, mediawiki, migration, persistence, pipeline, store, worker, wikisource

MODULES = (auth, web, ccourt, mediawiki, pipeline, store, worker, wikisource)


@pytest.fixture
def mysql_env(env, tmp_path, monkeypatch):
    if not os.getenv('KOCASE_TEST_MYSQL_PORT'):
        pytest.skip('Set KOCASE_TEST_MYSQL_PORT for a local disposable MariaDB server')
    _, seen, base = env
    cfg = replace(base, db_backend='mariadb', db_host='127.0.0.1',
                  db_port=int(os.environ['KOCASE_TEST_MYSQL_PORT']), db_socket='',
                  db_user=os.environ.get('KOCASE_TEST_MYSQL_USER', 'kocase'),
                  db_password=os.environ['KOCASE_TEST_MYSQL_PASSWORD'],
                  db_name='kocase__test_' + uuid.uuid4().hex,
                  state_dir=tmp_path/'state', work_dir=tmp_path/'state'/'work', tool_data_dir=None)
    persistence.initialize(cfg, admission_delay=0)
    for module in MODULES:
        monkeypatch.setattr(module, 'settings', cfg)
    app = web.create_app(); app.testing = True
    try:
        yield app, seen, cfg
    finally:
        # Only our generated local test DB; never use application/ToolsDB names.
        raw = database.mysql_connection(cfg, database=False)
        with raw.cursor() as cur:
            cur.execute(f'DROP DATABASE `{cfg.db_name}`')
        raw.close()


def test_real_process_restart_preserves_session_pdf_history_quota_and_logout(mysql_env):
    app, _, cfg = mysql_env
    client = app.test_client(); identity, sid = login(client, 'alice')
    job = finish(client, cfg)
    cookie = client.get_cookie('session', domain='tool.example').value
    child_env = {**os.environ, 'KOCASE_DB_BACKEND':'mariadb', 'KOCASE_DB_HOST':cfg.db_host,
                 'KOCASE_DB_PORT':str(cfg.db_port), 'KOCASE_DB_USER':cfg.db_user,
                 'KOCASE_DB_PASSWORD':cfg.db_password, 'KOCASE_DB_NAME':cfg.db_name,
                 'KOCASE_STATE_DIR':str(cfg.state_dir), 'KOCASE_WORK_DIR':str(cfg.work_dir),
                 'KOCASE_SECRET_KEY':cfg.secret_key, 'KOCASE_TOKEN_KEY':cfg.token_key,
                 'MW_OAUTH_CLIENT_ID':cfg.oauth_client_id, 'MW_OAUTH_CLIENT_SECRET':cfg.oauth_client_secret,
                 'MW_OAUTH_REDIRECT_URI':cfg.oauth_redirect_uri}
    code = '''import json,sys
from kocaseimport import app,auth,store
data=json.load(sys.stdin)
c=app.create_app().test_client();c.set_cookie('session',data['cookie'],domain='tool.example')
r=c.get('/jobs/'+data['job'],base_url='https://tool.example')
pdf=c.get('/jobs/'+data['job']+'/pdf',base_url='https://tool.example')
print(json.dumps({'status':r.status_code,'pdf':pdf.status_code,'quota':store.quota_status('101')['prepare'],'expires':auth.get_session(data['sid'])['expires']}))
'''
    output = subprocess.run([sys.executable, '-c', code], env=child_env, input=json.dumps({'cookie':cookie,'job':job,'sid':sid}),
                            capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(output.stdout) == {'status':200,'pdf':200,'quota':1,'expires':identity['expires']}
    assert client.post('/logout', data={'csrf':csrf(client)}, base_url=BASE).status_code == 302
    restarted = web.create_app().test_client(); restarted.set_cookie('session',cookie,domain='tool.example')
    assert restarted.get('/jobs/'+job,base_url=BASE).status_code == 401
    assert auth.get_session(sid) is None


def test_mariadb_last_quota_slot_and_delete_retains_usage(mysql_env):
    app, _, cfg = mysql_env
    client = app.test_client(); ident, _ = login(client, 'alice')
    with store._connect() as con:
        con.executemany('INSERT INTO admissions VALUES(?,?,?,?)', [(str(i),'101','prepare',con.now()) for i in range(99)])
    barrier = Barrier(2)
    def submit():
        barrier.wait()
        try:
            return store.create_job('97헌가12',False,owner_id='101',auth_id=ident['auth_id'])
        except store.ResourceLimit:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=list(pool.map(lambda _:submit(),range(2)))
    assert len([j for j in jobs if j]) == 1
    job=next(j for j in jobs if j); store.update_job(job,status='done')
    assert remove(client,job).status_code == 302
    assert store.quota_status('101')['prepare'] == 100
    assert store.get_job(job) is None


def test_refresh_cannot_resurrect_concurrent_logout(mysql_env, monkeypatch):
    app, _, _ = mysql_env
    client = app.test_client(); ident, sid = login(client,'alice')
    with store._connect() as con:
        token=auth._decrypt(con.execute('SELECT token FROM oauth_grants WHERE id=?',(ident['auth_id'],)).fetchone()[0])
        token['expires_at']=0
        con.execute('UPDATE oauth_grants SET token=? WHERE id=?',(auth._encrypt(token),ident['auth_id']))
    started, resume = Event(), Event()
    def refresh(*args,**kwargs):
        started.set(); assert resume.wait(5)
        return {'access_token':'fresh','refresh_token':'rotated','token_type':'Bearer','expires_in':3600}
    oauth=Mock(); oauth.__enter__=Mock(return_value=oauth);oauth.__exit__=Mock(return_value=False)
    oauth.refresh_token.side_effect=refresh
    monkeypatch.setattr(auth,'_client',lambda:oauth)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result=pool.submit(auth.access_token,ident['auth_id'],'101')
        assert started.wait(5)
        auth.delete_session(sid,disconnect=True)
        resume.set()
        with pytest.raises(auth.AuthError): result.result()
    with store._connect() as con:
        assert con.execute('SELECT token FROM oauth_grants WHERE id=?',(ident['auth_id'],)).fetchone()[0] == ''


def test_worker_lease_takeover_fences_old_worker_without_requeue(mysql_env):
    app, _, _=mysql_env
    client=app.test_client(); ident,_=login(client,'alice')
    job=store.create_job('97헌가12',True,owner_id='101',auth_id=ident['auth_id'])
    assert store.acquire_worker('first') and not store.acquire_worker('second')
    assert store.claim_next_job('first')['id'] == job
    store.update_summary(job,commons_title='File:already-published.pdf')
    with store._connect() as con: con.execute("UPDATE runtime_lease SET expires=0 WHERE name='worker'")
    assert store.acquire_worker('second')
    assert store.get_job(job)['status'] == 'failed'
    assert store.public_job(store.get_job(job))['commons_url']
    assert store.claim_next_job('second') is None
    ctx=store.execution.set((job,'first'))
    try:
        with pytest.raises(store.LeaseLost): store.assert_execution(job)
        with pytest.raises(store.LeaseLost): store.update_job(job,status='done')
    finally: store.execution.reset(ctx)
    assert not store.renew_worker('first')


def test_database_budget_rollback_and_fail_closed_outage(mysql_env,monkeypatch):
    app,_,cfg=mysql_env
    client=app.test_client();login(client,'alice'); token=csrf(client)
    with store._connect() as con: con.execute('UPDATE storage_budget SET max_bytes=used_bytes WHERE id=1')
    response=client.post('/jobs',data={'csrf':token,'case_number':'97헌가12'},base_url=BASE)
    assert response.status_code == 503 and store.quota_status('101')['prepare']==0 and not store.list_jobs('101')
    original=database.mysql_connection
    def unavailable(*args,**kwargs):
        raise pymysql.OperationalError('PRIVATE-CREDENTIALS')
    with monkeypatch.context() as patch:
        patch.setattr(database,'mysql_connection',unavailable)
        response=client.get('/',base_url=BASE)
        assert response.status_code==503 and 'PRIVATE-CREDENTIALS' not in response.text
    assert database.mysql_connection is original


def test_public_database_key_mismatch_and_missing_files_fail_closed(mysql_env):
    _,_,cfg=mysql_env
    with pytest.raises(RuntimeError): database.validate_database(replace(cfg,db_name='kocase__public_p'))
    with pytest.raises(RuntimeError): database.validate_database(replace(cfg,db_host='untrusted.example'))
    with pytest.raises(RuntimeError): persistence.verify_ready(replace(cfg,secret_key='different-key'*4))
    (cfg.state_dir/'.storage-id').unlink()
    with pytest.raises(RuntimeError): persistence.verify_ready(cfg)


def test_legacy_snapshot_import_preserves_cookie_expiry_and_files(mysql_env,monkeypatch,tmp_path):
    _,_,cfg=mysql_env
    source_cfg=replace(cfg,db_backend='sqlite',state_dir=None,work_dir=tmp_path/'old-work')
    with monkeypatch.context() as patch:
        for m in MODULES: patch.setattr(m,'settings',source_cfg)
        old_app=web.create_app();client=old_app.test_client();identity,sid=login(client,'alice')
        job=finish(client,source_cfg)
        cookie=client.get_cookie('session',domain='tool.example').value
        spec=importlib.util.spec_from_file_location('legacy_export',Path(__file__).parents[1]/'scripts/export_legacy.py')
        exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
        snapshot=tmp_path/'snapshot.tar'
        with snapshot.open('wb') as stream:
            exporter.export(source_cfg.db_path,source_cfg.work_dir,audit.directory(source_cfg),stream,
                            persistence.fingerprints(source_cfg),quiesced=True)
    migration.import_snapshot(cfg,snapshot)
    new_app=web.create_app();client=new_app.test_client();client.set_cookie('session',cookie,domain='tool.example')
    assert client.get('/jobs/'+job+'/pdf',base_url=BASE).status_code==200
    assert auth.get_session(sid)['expires']==identity['expires']
    assert store.quota_status('101')['prepare']==1
    assert audit.read_events(cfg)[0]
    with pytest.raises(RuntimeError): migration.import_snapshot(cfg,snapshot)


def test_mariadb_delete_publish_race(mysql_env,monkeypatch):
    from test_history_admin import test_delete_and_publish_race_has_one_winner
    test_delete_and_publish_race_has_one_winner(mysql_env,monkeypatch)


def test_real_worker_child_and_restore_revocation(mysql_env,monkeypatch):
    app,_,cfg=mysql_env
    client=app.test_client();identity,sid=login(client,'alice')
    job=store.create_job('97헌가12',False,owner_id='101',auth_id=identity['auth_id'])
    assert store.acquire_worker('worker-test')
    claimed=store.claim_next_job('worker-test')
    class FakePipeline:
        def __init__(self,job_id,token_provider): self.provider=token_provider
        def run(self,*args,**kwargs):
            assert self.provider()=='access-alice'
            return Mock(to_dict=lambda:{'verified':True})
    monkeypatch.setattr(worker,'Pipeline',FakePipeline)
    worker.run_isolated(claimed,'worker-test')
    assert store.get_job(job)['status']=='done'
    store.release_worker('worker-test')
    migration.revoke_auth(cfg,restored=True)
    assert auth.get_session(sid) is None
    with pytest.raises(auth.AuthError):auth.access_token(identity['auth_id'],'101')
    login(client,'alice')
    response=client.post('/jobs',data={'csrf':csrf(client),'case_number':'97헌가12'},base_url=BASE)
    assert response.status_code==429 and 86390<=int(response.headers['Retry-After'])<=86400


def test_import_rejects_archive_path_escape_before_mutation(mysql_env,tmp_path):
    _,_,cfg=mysql_env
    path=tmp_path/'bad.tar'
    meta=json.dumps({'version':1,'quiesced':True,'snapshot_id':'a'*32,
                     'keys':persistence.fingerprints(cfg),'sha256':{}}).encode()
    with tarfile.open(path,'w') as tar:
        for name,data in [('metadata.json',meta),('../outside',b'bad')]:
            info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
    with pytest.raises(RuntimeError):migration.import_snapshot(cfg,path)
    assert not (tmp_path/'outside').exists()
    persistence.verify_ready(cfg)
