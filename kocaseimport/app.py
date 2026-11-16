from __future__ import annotations
import hmac
import json
import secrets
from pathlib import Path
from flask import Flask, abort, g, redirect, render_template, request, send_file, session, url_for
from .config import settings
from . import auth
from .store import create_job, get_job, init_db, list_jobs, public_job, queue_publication, quota_status, ResourceLimit
from .utils import normalize_case_number, resolve_job_pdf


def create_app() -> Flask:
    auth.validate_config()
    settings.prepare()
    init_db()
    auth.init_auth()
    app = Flask(__name__)
    app.secret_key = settings.secret_key
    app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=16384)

    @app.before_request
    def load_session():
        g.identity = auth.get_session(session.get('sid'))
        if not g.identity:
            if 'sid' in session:
                session.clear()
            g.identity = {'owner_id': None, 'csrf': session.get('csrf', '')}

    def csrf():
        if not g.identity['csrf'] or not hmac.compare_digest(request.form.get('csrf', ''), g.identity['csrf']):
            abort(400)

    def require_user():
        if not g.identity.get('owner_id'):
            abort(401)

    def owned_job(job_id):
        require_user()
        job = get_job(job_id)
        if not job or job.get('owner_id') != g.identity['owner_id']:
            abort(404)
        return job

    def downloaded_pdf(job):
        summary = json.loads(job.get('summary') or '{}')
        if not summary.get('pdf_downloaded'):
            return None
        # Older completed jobs recorded the path only in the final manifest.
        filename = summary.get('pdf_filename') or Path(job['payload'].get('pdf_path', '')).name
        return resolve_job_pdf(settings.work_dir, job['id'], filename)

    def render_job(job, error=None, status=200):
        pdf_available = downloaded_pdf(job) is not None
        completed_dry_run = job['status'] == 'done' and not job['live']
        manifest_ready = all(job['payload'].get(k) for k in ('case', 'pdf_path', 'pdf_sha1', 'page_count'))
        return render_template(
            'job.html', job=public_job(job), identity=g.identity, error=error,
            pdf_available=pdf_available, live_enabled=settings.live_edits,
            completed_dry_run=completed_dry_run,
            can_publish=completed_dry_run and pdf_available and manifest_ready and settings.live_edits,
            quota=quota_status(g.identity['owner_id']),
        ), status

    def home(error=None):
        owner = g.identity.get('owner_id')
        if not owner and not g.identity['csrf']:
            session['csrf'] = secrets.token_urlsafe(32)
            g.identity['csrf'] = session['csrf']
        jobs = [public_job(j) for j in list_jobs(owner)] if owner else []
        return render_template('index.html', jobs=jobs, identity=g.identity,
                               live_enabled=settings.live_edits, error=error,
                               quota=quota_status(owner) if owner else None)

    @app.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    def index():
        return home()

    @app.post('/oauth/login')
    def login():
        csrf()
        url, pending = auth.begin_login()
        session['oauth_pending'] = pending
        return redirect(url)

    @app.get('/oauth/callback')
    def callback():
        try:
            sid = auth.complete_login(session.get('oauth_pending'), request.args.get('state', ''),
                                      request.args.get('code', ''), previous_sid=session.get('sid'))
        except auth.AuthError:
            return home('로그인을 완료하지 못했습니다. 다시 로그인해 주세요.'), 400
        session.clear()
        session['sid'] = sid
        return redirect(url_for('index'))

    @app.post('/logout')
    def logout():
        require_user()
        csrf()
        auth.delete_session(session['sid'], disconnect=True)
        session.clear()
        return redirect(url_for('index'))

    @app.post('/jobs')
    def new_job():
        require_user()
        csrf()
        try:
            case_number = normalize_case_number(request.form.get('case_number', ''))
        except ValueError:
            return home('사건번호를 확인해 주세요. 예: 2016헌나1'), 400
        live = request.form.get('live') == '1'
        if live and not settings.live_edits:
            return home('현재는 Dry-run만 사용할 수 있습니다.'), 400
        # Check the bound grant before queuing, never trust form owner/token fields.
        try:
            auth.access_token(g.identity['auth_id'], g.identity['owner_id'])
        except auth.AuthError:
            return home('로그인이 만료되었습니다. 다시 로그인해 주세요.'), 401
        pdf_url = request.form.get('pdf_url', '').strip()
        if len(pdf_url) > 4096:
            abort(400)
        try:
            job_id = create_job(case_number, live=live, pdf_url_override=pdf_url,
                               owner_id=g.identity['owner_id'], auth_id=g.identity['auth_id'])
        except ResourceLimit as e:
            return home(str(e)), 429, {'Retry-After': str(e.retry_after)}
        return redirect(url_for('job_detail', job_id=job_id))

    @app.get('/jobs/<job_id>')
    def job_detail(job_id):
        return render_job(owned_job(job_id))

    @app.post('/jobs/<job_id>/publish')
    def publish_job(job_id):
        job = owned_job(job_id)
        csrf()
        # Repeat clicks redirect to the one existing publication attempt.
        if job['live'] and job.get('reuse_pdf'):
            return redirect(url_for('job_detail', job_id=job_id))
        if not settings.live_edits:
            return render_job(job, '현재 실제 게시가 비활성화되어 있습니다.', 400)
        if job['live'] or job['status'] != 'done':
            return render_job(job, '완료된 Dry-run 작업만 이어서 게시할 수 있습니다.', 400)
        if downloaded_pdf(job) is None:
            return render_job(job, '확인했던 PDF가 남아 있지 않습니다. 새 작업으로 다시 확인해 주세요.', 400)
        auth.access_token(g.identity['auth_id'], g.identity['owner_id'])
        try:
            queued = queue_publication(job_id, owner_id=g.identity['owner_id'], auth_id=g.identity['auth_id'])
        except ResourceLimit as e:
            html, status = render_job(job, str(e), 429)
            return html, status, {'Retry-After': str(e.retry_after)}
        if not queued:
            return render_job(job, '게시 준비 자료가 없습니다. 새 작업으로 다시 확인해 주세요.', 400)
        return redirect(url_for('job_detail', job_id=job_id))

    @app.get('/jobs/<job_id>/pdf')
    def job_pdf(job_id):
        path = downloaded_pdf(owned_job(job_id))
        if path is None:
            abort(404)
        return send_file(path, mimetype='application/pdf', as_attachment=True,
                         download_name=path.name, conditional=False, etag=False, max_age=0)

    @app.errorhandler(auth.AuthError)
    def auth_failure(error):
        return render_template('error.html', message='로그인 상태를 확인하고 다시 시도해 주세요.'), 401

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def safe_error(error):
        return render_template('error.html', message='요청을 처리할 수 없습니다. 메인 화면에서 다시 시작해 주세요.'), error.code

    return app
