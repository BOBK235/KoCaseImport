import json
import re
import time
from dataclasses import replace
from urllib.parse import urlsplit, parse_qs
from unittest.mock import Mock

import pytest
import requests
from cryptography.fernet import Fernet

from kocaseimport import auth, app as web, store, pipeline, mediawiki, worker
from kocaseimport.config import settings

BASE = 'https://tool.example'


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = replace(settings, db_path=tmp_path/'jobs.sqlite3', work_dir=tmp_path/'work',
                  secret_key='x'*48, token_key=Fernet.generate_key().decode(),
                  oauth_client_id='client', oauth_client_secret='client-secret',
                  oauth_redirect_uri=BASE+'/oauth/callback', live_edits=False,
                  mw_bearer_token='SHARED-TOKEN-MUST-NOT-BE-USED')
    for module in (auth, web, store, pipeline, mediawiki):
        monkeypatch.setattr(module, 'settings', cfg)
    app = web.create_app()
    app.testing = True
    requests_seen = []

    def send(session, request, **kwargs):
        requests_seen.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        if request.url.endswith('/access_token'):
            form = parse_qs(request.body)
            if form['grant_type'] == ['authorization_code']:
                who = form['code'][0]
                assert form['redirect_uri'] == [BASE+'/oauth/callback']
                assert len(form['code_verifier'][0]) >= 43
            else:
                who = form['refresh_token'][0].removeprefix('refresh-')
            assert form['client_secret'] == ['client-secret']
            data = {'access_token': 'access-'+who, 'refresh_token': 'refresh-'+who,
                    'token_type': 'Bearer', 'expires_in': 3600}
        elif request.url.endswith('/resource/profile'):
            who = request.headers['Authorization'].removeprefix('Bearer access-')
            data = {'sub': '101' if who == 'alice' else '202', 'username': who}
        else:
            raise AssertionError('Unexpected network request: '+request.url)
        response._content = json.dumps(data).encode()
        response.headers['Content-Type'] = 'application/json'
        return response

    monkeypatch.setattr(requests.sessions.Session, 'send', send)
    return app, requests_seen, cfg


def csrf(client):
    page = client.get('/', base_url=BASE)
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def login(client, who):
    token = csrf(client)
    response = client.post('/oauth/login', data={'csrf':token}, base_url=BASE)
    params = parse_qs(urlsplit(response.location).query)
    assert params['code_challenge_method'] == ['S256']
    response = client.get('/oauth/callback', query_string={'code': who, 'state':params['state'][0]}, base_url=BASE)
    assert response.status_code == 302
    with client.session_transaction(base_url=BASE) as session:
        sid = session['sid']
    return auth.get_session(sid), sid


def enqueue(client, **kwargs):
    response = client.post('/jobs', data={'csrf':csrf(client), 'case_number':'2016헌나1', **kwargs}, base_url=BASE)
    assert response.status_code == 302
    return response.location.rsplit('/',1)[-1]


def test_oauth_callback_state_replay_and_encrypted_storage(env):
    app, seen, cfg = env
    c = app.test_client()
    token = csrf(c)
    response = c.post('/oauth/login', data={'csrf':token}, base_url=BASE)
    state = parse_qs(urlsplit(response.location).query)['state'][0]
    assert c.get('/oauth/callback?state=wrong&code=alice', base_url=BASE).status_code == 400
    assert not seen
    result = c.get('/oauth/callback', query_string={'state':state,'code':'alice'}, base_url=BASE)
    assert result.status_code == 302
    assert c.get('/oauth/callback', query_string={'state':state,'code':'alice'}, base_url=BASE).status_code == 400
    assert len(seen) == 2
    cookie = result.headers['Set-Cookie']
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
    with c.session_transaction(base_url=BASE) as session:
        assert set(session) == {'sid'}
        identity = auth.get_session(session['sid'])
    assert identity['owner_id'] == '101'
    raw = cfg.db_path.read_bytes()
    assert b'access-alice' not in raw and b'refresh-alice' not in raw
    assert auth.access_token(identity['auth_id'],'101') == 'access-alice'
    with pytest.raises(auth.AuthError):
        auth.access_token(identity['auth_id'],'202')


def test_only_own_recent_jobs_and_no_raw_details(env):
    app, _, _ = env
    a,b = app.test_client(),app.test_client()
    identity,_ = login(a,'alice'); login(b,'bob')
    aid=enqueue(a, owner_id='202',auth_id='forged')
    bid=enqueue(b)
    assert store.get_job(aid)['owner_id'] == '101'
    assert store.get_job(aid)['auth_id'] == identity['auth_id']
    store.update_job(aid, status='done', step='internal-step', log='PRIVATE-LOG', error='PRIVATE-TRACE', payload={'token':'PRIVATE-JSON'})
    store.update_summary(aid,case_confirmed=True,pdf_downloaded=True,page_count=70,edition='판례집',dry_run=True)
    own=a.get('/',base_url=BASE)
    assert aid in own.text and bid not in own.text
    assert '자신의 최근 작업' in own.text
    details=a.get('/jobs/'+aid,base_url=BASE)
    assert '70쪽' in details.text and '판례집' in details.text and 'Dry-run' in details.text
    for secret in ('PRIVATE-LOG','PRIVATE-TRACE','PRIVATE-JSON','internal-step','<pre'):
        assert secret not in details.text and secret not in own.text
    assert b.get('/jobs/'+aid,base_url=BASE).status_code == 404
    assert app.test_client().get('/jobs/'+aid,base_url=BASE).status_code == 401
    assert own.headers['Cache-Control'] == 'no-store'


def test_csrf_and_live_disabled_and_logout(env):
    app,_,_ = env
    c=app.test_client()
    assert c.post('/jobs',data={'case_number':'2016헌나1'},base_url=BASE).status_code == 401
    ident,sid=login(c,'alice')
    assert c.post('/jobs',data={'case_number':'2016헌나1'},base_url=BASE).status_code == 400
    assert c.post('/jobs',data={'csrf':csrf(c),'case_number':'2016헌나1','live':'1'},base_url=BASE).status_code == 400
    job=enqueue(c)
    assert c.post('/logout',data={'csrf':csrf(c)},base_url=BASE).status_code == 302
    assert auth.get_session(sid) is None
    with pytest.raises(auth.AuthError):
        auth.access_token(ident['auth_id'],'101')
    assert store.get_job(job)['status'] == 'failed'


def test_refresh_rotation_and_session_expiry(env):
    app, seen, _ = env
    c=app.test_client(); ident,sid=login(c,'alice')
    with store._connect() as con:
        row=con.execute('SELECT token FROM oauth_grants WHERE id=?',(ident['auth_id'],)).fetchone()
        token=auth._decrypt(row[0]); token['expires_at']=time.time()-1
        con.execute('UPDATE oauth_grants SET token=? WHERE id=?',(auth._encrypt(token),ident['auth_id']))
    assert auth.access_token(ident['auth_id'],'101') == 'access-alice'
    assert parse_qs(seen[-1].body)['grant_type'] == ['refresh_token']
    count=len(seen)
    assert auth.access_token(ident['auth_id'],'101') == 'access-alice' and len(seen)==count
    with store._connect() as con:
        con.execute('UPDATE web_sessions SET expires=0 WHERE id=?',(ident['id'],))
    assert auth.get_session(sid) is None
    assert 'Wikimedia 계정으로 로그인' in c.get('/',base_url=BASE).text


def test_per_user_mediawiki_token_and_no_shared_fallback(env,monkeypatch):
    provider=Mock(return_value='USER-TOKEN')
    client=mediawiki.MediaWikiClient('https://commons.wikimedia.org/w/api.php',token_provider=provider)
    response=Mock()
    response.json.return_value={'query':{'tokens':{'csrftoken':'csrf-token'}}}
    client.s.get=Mock(return_value=response)
    posted=Mock();posted.json.return_value={'edit':{'result':'Success'}}
    client.s.post=Mock(return_value=posted)
    monkeypatch.setattr(mediawiki.time,'sleep',lambda _: None)
    client.edit_page('test','text','summary')
    assert client.s.headers['Authorization']=='Bearer USER-TOKEN'
    payload=client.s.post.call_args.kwargs['data']
    assert payload['assert']=='user' and 'bot' not in payload
    provider.side_effect=auth.AuthError('expired')
    with pytest.raises(auth.AuthError):
        client.edit_page('test','text','summary')
    assert client.s.post.call_count==1


def test_worker_passes_job_owner_token(env,monkeypatch):
    app,_,_=env
    c=app.test_client();login(c,'alice');job=enqueue(c)
    got=[]
    class FakePipeline:
        def __init__(self,job_id,token_provider): got.append((job_id,token_provider()))
        def run(self,*args,**kwargs): return Mock(to_dict=lambda: {'complete':True})
    monkeypatch.setattr(worker,'Pipeline',FakePipeline)
    worker.run_job(store.claim_next_job())
    assert got==[(job,'access-alice')]
    assert store.get_job(job)['status']=='done'


def test_legacy_jobs_remain_unowned_and_unrunnable(env):
    with store._connect() as con:
        con.execute("INSERT INTO jobs(id,case_number,status,created_at,updated_at) VALUES('legacy','2016헌나1','pending',0,0)")
    store.init_db()
    assert store.get_job('legacy')['status']=='failed'
    assert store.list_jobs('101')==[]
    assert store.claim_next_job() is None


def test_pipeline_summary_and_both_wikis_use_owner(env,monkeypatch):
    from pypdf import PdfWriter
    from kocaseimport.models import CaseMetadata
    app,_,cfg=env
    c=app.test_client();ident,_=login(c,'alice');job=enqueue(c)
    class Court:
        def __init__(self,on_case):self.on_case=on_case
        def lookup(self,*args,**kwargs):
            self.on_case()
            return CaseMetadata(case_number='2016헌나1',case_name='사건',raw={'source_edition':'판례집'})
        def download_pdf(self,meta,dest):
            dest.parent.mkdir(parents=True,exist_ok=True)
            writer=PdfWriter();writer.add_blank_page(100,100);writer.write(dest)
    monkeypatch.setattr(pipeline,'CCourtClient',Court)
    provider=lambda:auth.access_token(ident['auth_id'],'101')
    pipeline.Pipeline(job,token_provider=provider).run('2016헌나1',live=False)
    summary=store.public_job(store.get_job(job))
    assert summary['page_count']==1 and summary['edition']=='판례집' and summary['dry_run'] is True
    monkeypatch.setattr(pipeline,'settings',replace(cfg,live_edits=True))
    clients=[]
    class StopBeforePublish:
        def __init__(self,url,token_provider):clients.append((url,token_provider()))
        def login(self):raise RuntimeError('stop-before-publish')
    monkeypatch.setattr(pipeline,'MediaWikiClient',StopBeforePublish)
    with pytest.raises(RuntimeError,match='stop-before-publish'):
        pipeline.Pipeline(job,token_provider=provider).run('2016헌나1',live=True)
    assert clients==[(cfg.commons_api,'access-alice'),(cfg.wikisource_api,'access-alice')]
    assert store.public_job(store.get_job(job))['dry_run'] is False
