from __future__ import annotations
import hmac
from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from .config import settings
from . import auth
from .store import create_job, get_job, init_db, list_jobs, public_job
from .utils import normalize_case_number


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
            session.clear()
            session['sid'] = auth.new_session()
            g.identity = auth.get_session(session['sid'])

    def csrf():
        if not hmac.compare_digest(request.form.get('csrf', ''), g.identity['csrf']):
            abort(400)

    def require_user():
        if not g.identity.get('owner_id'):
            abort(401)

    def home(error=None):
        owner = g.identity.get('owner_id')
        jobs = [public_job(j) for j in list_jobs(owner)] if owner else []
        return render_template('index.html', jobs=jobs, identity=g.identity,
                               live_enabled=settings.live_edits, error=error)

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
        return redirect(auth.begin_login(session['sid']))

    @app.get('/oauth/callback')
    def callback():
        try:
            sid = auth.complete_login(session['sid'], request.args.get('state', ''), request.args.get('code', ''))
        except auth.AuthError:
            return home('로그인을 완료하지 못했습니다. 다시 로그인해 주세요.'), 400
        session.clear()
        session['sid'] = sid
        return redirect(url_for('index'))

    @app.post('/logout')
    def logout():
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
        job_id = create_job(case_number, live=live, pdf_url_override=pdf_url,
                           owner_id=g.identity['owner_id'], auth_id=g.identity['auth_id'])
        return redirect(url_for('job_detail', job_id=job_id))

    @app.get('/jobs/<job_id>')
    def job_detail(job_id):
        require_user()
        job = get_job(job_id)
        if not job or job.get('owner_id') != g.identity['owner_id']:
            abort(404)
        return render_template('job.html', job=public_job(job), identity=g.identity)

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
