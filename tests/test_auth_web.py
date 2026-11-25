import json
import re
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from unittest.mock import Mock

import pytest
import requests
from cryptography.fernet import Fernet

from kocaseimport import auth, app as web, store, pipeline, mediawiki, worker, wikisource, ccourt
from kocaseimport.config import settings

BASE = 'https://tool.example'


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = replace(settings, db_path=tmp_path/'jobs.sqlite3', work_dir=tmp_path/'work',
                  secret_key='x'*48, token_key=Fernet.generate_key().decode(),
                  oauth_client_id='client', oauth_client_secret='client-secret',
                  oauth_redirect_uri=BASE+'/oauth/callback', live_edits=False,
                  mw_bearer_token='SHARED-TOKEN-MUST-NOT-BE-USED')
    for module in (auth, web, store, pipeline, mediawiki, wikisource, worker, ccourt):
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


def test_pdf_download_requires_owner_and_completed_download(env):
    from pypdf import PdfWriter
    app, _, cfg = env
    alice, bob = app.test_client(), app.test_client()
    login(alice, 'alice'); login(bob, 'bob')
    job = enqueue(alice)
    path = cfg.work_dir / job / '2016헌나1 결정문.pdf'
    path.parent.mkdir()
    writer = PdfWriter(); writer.add_blank_page(100, 100); writer.write(path)
    url = '/jobs/' + job + '/pdf'
    # A file on disk alone does not make an incomplete download available.
    assert alice.get(url, base_url=BASE).status_code == 404
    assert url not in alice.get('/jobs/' + job, base_url=BASE).text
    store.update_summary(job, pdf_downloaded=True, page_count=1, pdf_filename=path.name)
    response = alice.get(url, base_url=BASE)
    assert response.status_code == 200 and response.data == path.read_bytes()
    assert response.mimetype == 'application/pdf'
    assert response.headers['Content-Disposition'].startswith('attachment;')
    assert "filename*=UTF-8''" in response.headers['Content-Disposition']
    assert response.headers['Cache-Control'] == 'no-store'
    response.close()
    page = alice.get('/jobs/' + job, base_url=BASE)
    assert url in page.text and str(cfg.work_dir) not in page.text
    assert bob.get(url, base_url=BASE).status_code == 404
    assert app.test_client().get(url, base_url=BASE).status_code == 401
    assert alice.get('/jobs/unknown/pdf', base_url=BASE).status_code == 404
    path.unlink()
    assert alice.get(url, base_url=BASE).status_code == 404
    assert url not in alice.get('/jobs/' + job, base_url=BASE).text
    # A symlink cannot expose another job's file.
    other_job = enqueue(bob)
    other_path = cfg.work_dir / other_job / path.name
    other_path.parent.mkdir()
    writer.write(other_path)
    path.symlink_to(other_path)
    assert alice.get(url, base_url=BASE).status_code == 404
    assert url not in alice.get('/jobs/' + job, base_url=BASE).text


def test_pdf_download_supports_existing_completed_job(env):
    from pypdf import PdfWriter
    app, _, cfg = env
    client = app.test_client(); login(client, 'alice')
    job = enqueue(client)
    path = cfg.work_dir / job / 'existing.pdf'
    path.parent.mkdir()
    writer = PdfWriter(); writer.add_blank_page(100, 100); writer.write(path)
    store.update_summary(job, pdf_downloaded=True, page_count=1)
    store.update_job(job, status='done', payload={'pdf_path': str(path)})
    response = client.get('/jobs/' + job + '/pdf', base_url=BASE)
    assert response.status_code == 200 and response.data == path.read_bytes()
    response.close()


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
        def pdf_candidates(self,*args,**kwargs):
            self.on_case()
            yield CaseMetadata(case_number='2016헌나1',case_name='사건',raw={'source_edition':'판례집'})
        def download_pdf(self,meta,dest):
            dest.parent.mkdir(parents=True,exist_ok=True)
            writer=PdfWriter();writer.add_blank_page(100,100);writer.write(dest)
    monkeypatch.setattr(pipeline,'CCourtClient',Court)
    provider=lambda:auth.access_token(ident['auth_id'],'101')
    manifest = pipeline.Pipeline(job,token_provider=provider).run('2016헌나1',live=False)
    summary=store.public_job(store.get_job(job))
    assert summary['page_count']==1 and summary['edition']=='판례집' and summary['dry_run'] is True
    assert 'pdf_filename' not in summary
    url = '/jobs/' + job + '/pdf'
    assert url in c.get('/jobs/' + job, base_url=BASE).text
    with c.get(url, base_url=BASE) as response:
        assert response.status_code == 200 and response.data == Path(manifest.pdf_path).read_bytes()
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
    store.update_job(job, status='failed')
    assert url in c.get('/jobs/' + job, base_url=BASE).text
    with c.get(url, base_url=BASE) as response:
        assert response.status_code == 200


def completed_dry_run(client, monkeypatch):
    from pypdf import PdfWriter
    from kocaseimport.models import CaseMetadata
    court = Mock()
    court.pdf_candidates.return_value = [CaseMetadata(case_number='2016헌나1', case_name='확인한 사건',
                                             raw={'source_edition': '판례집'})]
    def download(meta, dest):
        writer = PdfWriter(); writer.add_blank_page(100, 100); writer.write(dest)
    court.download_pdf.side_effect = download
    monkeypatch.setattr(pipeline, 'CCourtClient', lambda **kw: court)
    job_id = enqueue(client)
    worker.run_job(store.claim_next_job())
    assert store.get_job(job_id)['status'] == 'done'
    return job_id, court


def enable_publication(cfg, monkeypatch):
    cfg = replace(cfg, live_edits=True)
    for module in (web, store, pipeline, wikisource):
        monkeypatch.setattr(module, 'settings', cfg)


def test_publish_requires_owner_csrf_completed_dry_run_and_live_enabled(env, monkeypatch):
    app, _, cfg = env
    alice, bob = app.test_client(), app.test_client()
    login(alice, 'alice'); login(bob, 'bob')
    job_id, _ = completed_dry_run(alice, monkeypatch)
    url = '/jobs/' + job_id + '/publish'
    assert app.test_client().post(url, base_url=BASE).status_code == 401
    assert bob.post(url, data={'csrf': csrf(bob)}, base_url=BASE).status_code == 404
    assert alice.post(url, base_url=BASE).status_code == 400
    assert alice.post(url, data={'csrf': csrf(alice)}, base_url=BASE).status_code == 400
    assert store.get_job(job_id)['live'] == 0
    enable_publication(cfg, monkeypatch)
    pending = enqueue(alice)
    assert alice.post('/jobs/'+pending+'/publish', data={'csrf': csrf(alice)}, base_url=BASE).status_code == 400
    page = alice.get('/jobs/'+job_id, base_url=BASE)
    assert '내 계정으로 공용·위키문헌에 이어서 게시' in page.text
    assert '공용 업로드 완료' not in page.text and '위키문헌 업로드 완료' not in page.text
    assert '실행하지 않음 (Dry-run)' in page.text
    Path(store.get_job(job_id)['payload']['pdf_path']).unlink()
    assert alice.post(url, data={'csrf': csrf(alice)}, base_url=BASE).status_code == 400
    assert store.get_job(job_id)['live'] == 0


@pytest.mark.parametrize('scenario', ['upload', 'reuse', 'fail_after_commons'])
def test_dry_run_continues_once_with_same_pdf_and_incremental_links(env, monkeypatch, scenario):
    from urllib.parse import unquote
    app, _, cfg = env
    alice = app.test_client(); first_identity, _ = login(alice, 'alice')
    job_id, court = completed_dry_run(alice, monkeypatch)
    original = store.get_job(job_id)['payload']
    original_bytes = Path(original['pdf_path']).read_bytes()
    # The grant from the dry run may have expired; use the current login.
    identity, _ = login(alice, 'alice')
    assert identity['auth_id'] != first_identity['auth_id']
    enable_publication(cfg, monkeypatch)
    url = '/jobs/' + job_id
    for _ in range(2):
        response = alice.post(url+'/publish', data={'csrf': csrf(alice), 'owner_id': '202'}, base_url=BASE)
        assert response.status_code == 302 and response.location == url
    claimed = store.claim_next_job()
    assert claimed['id'] == job_id and claimed['auth_id'] == identity['auth_id']
    assert claimed['owner_id'] == '101' and claimed['live'] == claimed['reuse_pdf'] == 1
    assert store.claim_next_job() is None
    assert len(store.list_jobs('101')) == 1
    assert '내 계정으로 공용·위키문헌에 이어서 게시' not in alice.get(url, base_url=BASE).text
    uploads, edits, clients = [], [], []
    filename = '확인한 문서 & 결정.pdf'

    class Wiki:
        def __init__(self, api, token_provider):
            clients.append((api, token_provider()))
        def login(self): pass
        def find_file_by_sha1(self, sha1):
            assert sha1 == original['pdf_sha1']
            return filename if scenario == 'reuse' else ''
        def page_exists(self, title): return False
        def upload_file(self, path, name, description, comment):
            assert path.read_bytes() == original_bytes
            uploads.append(name)
            return {'upload': {'result': 'Success', 'filename': filename}}
        def get_page_source(self, title): return ''
        def user_name(self): return 'alice'
        def edit_page(self, title, text, *args, **kwargs):
            if title.startswith('색인:'):
                # This is observed before any Wikisource edit has succeeded.
                page = alice.get(url, base_url=BASE)
                assert '공용 파일 보기' in page.text and '위키문헌 문서 보기' not in page.text
                state = store.public_job(store.get_job(job_id))
                assert unquote(state['commons_url']).endswith('/wiki/File:'+filename.replace(' ', '_'))
                assert state['wikisource_status'] == '처리 중'
                if scenario == 'fail_after_commons':
                    raise RuntimeError('private-provider-error')
            edits.append(title)

    monkeypatch.setattr(pipeline, 'MediaWikiClient', Wiki)
    ocr = Mock()
    ocr.transcribe_pages.return_value = [('확인할 본문', 'pdf')]
    monkeypatch.setattr(pipeline, 'OCRClient', lambda: ocr)
    worker.run_job(claimed)
    court.pdf_candidates.assert_called_once()
    court.download_pdf.assert_called_once()
    assert clients == [(cfg.commons_api, 'access-alice'), (cfg.wikisource_api, 'access-alice')]
    assert len(uploads) == (0 if scenario == 'reuse' else 1)
    result = store.get_job(job_id)
    page = alice.get(url, base_url=BASE)
    assert '공용 파일 보기' in page.text
    assert '동일 파일 재사용' in page.text if scenario == 'reuse' else '공용 업로드 완료' in page.text
    if scenario == 'fail_after_commons':
        assert result['status'] == 'failed'
        assert '위키문헌 업로드 완료' not in page.text and '위키문헌 문서 보기' not in page.text
        assert 'private-provider-error' not in page.text
    else:
        assert result['status'] == 'done'
        assert edits == ['색인:'+filename, '페이지:'+filename+'/1', '초안:2016헌나1']
        assert '위키문헌 업로드 완료' in page.text and '위키문헌 문서 보기' in page.text
        assert unquote(store.public_job(result)['wikisource_url']) == 'https://ko.wikisource.org/wiki/초안:2016헌나1'
    # A stale form cannot queue another attempt, even after completion/failure.
    assert alice.post(url+'/publish', data={'csrf': csrf(alice)}, base_url=BASE).status_code == 302
    assert store.claim_next_job() is None


@pytest.mark.parametrize('change', ['changed_pdf', 'live_disabled'])
def test_queued_publication_stops_before_network_when_preparation_is_invalid(env, monkeypatch, change):
    app, _, cfg = env
    alice = app.test_client(); login(alice, 'alice')
    job_id, _ = completed_dry_run(alice, monkeypatch)
    enable_publication(cfg, monkeypatch)
    assert alice.post('/jobs/'+job_id+'/publish', data={'csrf': csrf(alice)}, base_url=BASE).status_code == 302
    if change == 'changed_pdf':
        path = Path(store.get_job(job_id)['payload']['pdf_path'])
        path.write_bytes(path.read_bytes() + b'changed')
    else:
        monkeypatch.setattr(pipeline, 'settings', cfg)
    wiki = Mock()
    monkeypatch.setattr(pipeline, 'MediaWikiClient', wiki)
    worker.run_job(store.claim_next_job())
    wiki.assert_not_called()
    assert store.get_job(job_id)['status'] == 'failed'


def test_success_labels_require_mediawiki_success_response(env, monkeypatch, tmp_path):
    client = mediawiki.MediaWikiClient('https://commons.wikimedia.org/w/api.php', token_provider=lambda: 'token')
    monkeypatch.setattr(client, 'csrf_token', lambda: 'csrf')
    monkeypatch.setattr(client, 'post', lambda *a, **kw: {'edit': {'result': 'Failure', 'captcha': {}}})
    with pytest.raises(mediawiki.MediaWikiError):
        client.edit_page('title', 'body', 'summary')
    path = tmp_path / 'test.pdf'; path.write_bytes(b'pdf')
    monkeypatch.setattr(client, 'post', lambda *a, **kw: {'upload': {'result': 'Continue'}})
    with pytest.raises(mediawiki.MediaWikiError):
        client.upload_file(path, 'test.pdf', 'description', 'comment')


def test_failure_results_show_safe_reason_stage_and_keep_owner_isolation(env, monkeypatch):
    from kocaseimport.ccourt import CCourtError
    app, _, cfg = env
    alice, bob = app.test_client(), app.test_client()
    login(alice, 'alice'); login(bob, 'bob')
    failures = [
        (CCourtError('PRIVATE-TRACE', code='pdf_missing', missing_editions=['결정문'], hwp_editions=['결정문']), 'case_lookup', '공식 PDF 미제공'),
        (CCourtError('PRIVATE-TRACE', code='pdf_http', http_status=503), 'download_pdf', 'HTTP 상태: 503'),
        (CCourtError('PRIVATE-TRACE', code='pdf_timeout'), 'download_pdf', 'PDF 다운로드 시간 초과'),
        (RuntimeError('PRIVATE-TRACE https://secret/?serviceKey=PRIVATE-KEY'), 'verify_pdf', '예상하지 못한 처리 오류'),
    ]
    for exc, phase, expected in failures:
        job_id = enqueue(alice, case_number='97헌가12')
        assert store.get_job(job_id)['case_number'] == '97헌가12'
        class FailingPipeline:
            def __init__(self, job_id, token_provider): self.job_id = job_id
            def run(self, *args, **kw):
                store.update_job(self.job_id, step=phase)
                store.update_summary(self.job_id, case_confirmed=True)
                raise exc
        monkeypatch.setattr(worker, 'Pipeline', FailingPipeline)
        worker.run_job(store.claim_next_job())
        result = alice.get('/jobs/'+job_id, base_url=BASE)
        assert expected in result.text and '중단 단계:' in result.text
        assert 'PRIVATE-TRACE' not in result.text and 'PRIVATE-KEY' not in result.text
        assert str(cfg.work_dir) not in result.text
        assert bob.get('/jobs/'+job_id, base_url=BASE).status_code == 404
        assert app.test_client().get('/jobs/'+job_id, base_url=BASE).status_code == 401
        if phase == 'download_pdf':
            assert '중단 단계: PDF 다운로드' in result.text
            assert '공식 PDF 미제공' not in result.text


def test_corrupt_download_reports_pdf_validation_failure(env, monkeypatch):
    from kocaseimport.models import CaseMetadata
    app, _, _ = env
    alice = app.test_client(); login(alice, 'alice')
    job = enqueue(alice)
    court = Mock()
    court.pdf_candidates.return_value = [CaseMetadata('2016헌나1')]
    court.download_pdf.side_effect = lambda meta, path: path.write_bytes(b'%PDF-corrupt')
    monkeypatch.setattr(pipeline, 'CCourtClient', lambda **kw: court)
    worker.run_job(store.claim_next_job())
    page = alice.get('/jobs/'+job, base_url=BASE)
    assert 'PDF 읽기 실패' in page.text and '중단 단계: PDF 파일·쪽수 검증' in page.text
    assert '공식 PDF 미제공' not in page.text


def test_dry_run_uses_next_edition_when_pdf_download_is_missing(env, monkeypatch):
    from kocaseimport.models import CaseMetadata
    from pypdf import PdfWriter
    app, _, _ = env
    alice = app.test_client()
    login(alice, 'alice')
    court = Mock()
    court.pdf_candidates.return_value = [
        CaseMetadata('96헌마186', raw={'source_edition': edition})
        for edition in ('결정문', '판례집', '공보')
    ]
    def download(meta, path):
        if meta.raw['source_edition'] == '결정문':
            raise ccourt.CCourtError(code='pdf_http_missing', http_status=404)
        writer = PdfWriter()
        writer.add_blank_page(100, 100)
        writer.write(path)
    court.download_pdf.side_effect = download
    monkeypatch.setattr(pipeline, 'CCourtClient', lambda **kw: court)
    job = enqueue(alice, case_number='96헌마186')
    worker.run_job(store.claim_next_job())
    result = store.get_job(job)
    assert result['status'] == 'done' and court.download_pdf.call_count == 2
    assert result['payload']['case']['raw']['source_edition'] == '판례집'
    assert '판례집 수록본' in result['payload']['pdf_path']
    assert result['payload']['main_title'] == '초안:96헌마186'


def test_review_requires_owner_preview_and_current_revision(env, monkeypatch):
    app, _, cfg = env
    alice, bob = app.test_client(), app.test_client()
    login(alice, 'alice')
    login(bob, 'bob')
    job, _ = completed_dry_run(alice, monkeypatch)
    url = f'/jobs/{job}/page-review'
    form = {'page': 1, 'csrf': csrf(alice)}
    assert alice.post(url, data=form, base_url=BASE).status_code == 404
    assert bob.post(url, data={'page': 1, 'csrf': csrf(bob)}, base_url=BASE).status_code == 404
    manifest = store.get_job(job)['payload']
    manifest['commons_filename'] = '공용 이름.pdf'
    enable_publication(cfg, monkeypatch)
    assert alice.post(f'/jobs/{job}/publish', data=form, base_url=BASE).status_code == 302
    assert store.claim_next_job()['id'] == job
    store.update_job(job, status='done', payload=manifest)
    assert alice.post(url, data={'page': 1}, base_url=BASE).status_code == 400
    assert alice.post(url, data={**form, 'page': 2}, base_url=BASE).status_code == 400
    wiki = Mock(spec=mediawiki.MediaWikiClient)
    wiki.s = Mock()
    monkeypatch.setattr(web, 'MediaWikiClient', lambda api, token_provider: wiki if token_provider() == 'access-alice' else None)
    monkeypatch.setattr(web.page_review, 'extract_page_text', lambda *args: '헌법상 수도의 개념')
    original = {'header': '머리글', 'footer': '각주', 'body': '현법상 수도의 개념',
                'level': {'level': 3, 'user': 'Other'}}
    revision = {'revid': 11, 'slots': {'main': {'contentmodel': 'proofread-page',
                                                'content': json.dumps(original)}}}
    wiki.get.return_value = {'query': {'pages': [{'revisions': [revision]}]}}
    compare = alice.post(url, data=form, base_url=BASE)
    assert compare.status_code == 200 and '-현법상' in compare.text and '+헌법상' in compare.text
    wiki.edit_page.assert_not_called()
    form.update(action='apply', review=re.search(r'name="review" value="([^"]+)"', compare.text)[1])
    revision['revid'] = 12
    assert alice.post(url, data=form, base_url=BASE).status_code == 409
    wiki.edit_page.assert_not_called()
    compare = alice.post(url, data={**form, 'action': 'compare'}, base_url=BASE)
    form['review'] = re.search(r'name="review" value="([^"]+)"', compare.text)[1]
    assert alice.post(url, data=form, base_url=BASE).status_code == 302
    edit = wiki.edit_page.call_args
    assert edit.args[0] == '페이지:공용 이름.pdf/1' and edit.kwargs['baserevid'] == 12
    assert json.loads(edit.args[1]) == {**original, 'body': '헌법상 수도의 개념',
                                        'level': {'level': 1, 'user': 'alice'}}
