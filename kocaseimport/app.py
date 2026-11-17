from __future__ import annotations
import hmac
import json
import secrets
from pathlib import Path
from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from .config import settings
from . import auth, audit, history
from .store import create_job, get_job, init_db, list_jobs, public_job, queue_publication, quota_status, ResourceLimit, admin_recent_jobs
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

    def require_admin():
        require_user()
        if not auth.is_admin(g.identity, settings):
            abort(403)

    @app.context_processor
    def navigation():
        return {'is_admin': auth.is_admin(getattr(g, 'identity', None), settings)}

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
            if len(request.form.get('case_number', '')) > 40:
                raise ValueError('case number too long')
            case_number = normalize_case_number(request.form.get('case_number', ''))
        except ValueError:
            return home('사건번호를 확인해 주세요. 예: 97헌가12, 2016헌나1'), 400
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
                               owner_id=g.identity['owner_id'], auth_id=g.identity['auth_id'],
                               owner_name=g.identity['username'])
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
            queued = queue_publication(job_id, owner_id=g.identity['owner_id'], auth_id=g.identity['auth_id'],
                                       owner_name=g.identity['username'])
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

    @app.post('/jobs/delete/confirm')
    def confirm_delete():
        require_user()
        csrf()
        rows = history.preview(request.form.getlist('job_ids'), g.identity['owner_id'])
        return render_template('delete.html', jobs=rows, identity=g.identity)

    @app.post('/jobs/delete')
    def delete_history():
        require_user()
        csrf()
        if request.form.get('confirmed') != 'yes':
            abort(400)
        count = history.delete_jobs(settings, request.form.getlist('job_ids'),
                                    owner_id=g.identity['owner_id'], username=g.identity['username'])
        flash(f'{count}개 작업의 조회 기록과 서버에 보관한 파일을 삭제했습니다.')
        return redirect(url_for('index'))

    @app.get('/account')
    def account():
        require_user()
        return render_template('account.html', identity=g.identity)

    @app.get('/admin/requests')
    def admin_requests():
        require_admin()
        before = request.args.get('before', '')
        actor_id = request.args.get('user_id', '').strip()
        case_number = request.args.get('case_number', '').strip()
        if before and (not before.isascii() or not before.isdecimal() or len(before) > 20):
            abort(400)
        if actor_id and (not actor_id.isascii() or not actor_id.isdecimal() or len(actor_id) > 20):
            abort(400)
        if case_number:
            if len(case_number) > 40:
                abort(400)
            try:
                case_number = normalize_case_number(case_number)
            except ValueError:
                abort(400)
        audit.record(settings, 'admin_view', actor_id=g.identity['owner_id'], actor_name=g.identity['username'])
        rows, older = audit.read_events(settings, before=int(before) if before else None,
                                        actor_id=actor_id, case_number=case_number)
        return render_template('admin.html', identity=g.identity, events=rows, older=older,
                               actor_id=actor_id, case_number=case_number,
                               current_jobs=admin_recent_jobs() if not before and not actor_id and not case_number else [])

    @app.errorhandler(history.HistoryError)
    def history_failure(error):
        return render_template('error.html', message=str(error), identity=g.identity), error.status

    @app.errorhandler(audit.AuditUnavailable)
    def audit_failure(error):
        return render_template('error.html', message=str(error), identity=g.identity), 503

    @app.errorhandler(auth.AuthError)
    def auth_failure(error):
        return render_template('error.html', message='로그인 상태를 확인하고 다시 시도해 주세요.'), 401

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def safe_error(error):
        return render_template('error.html', message='요청을 처리할 수 없습니다. 메인 화면에서 다시 시작해 주세요.'), error.code

    return app
