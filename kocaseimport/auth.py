"""Wikimedia OAuth2: server-side sessions, PKCE and encrypted per-grant tokens."""
from __future__ import annotations
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from authlib.integrations.requests_client import OAuth2Session
from cryptography.fernet import Fernet
import requests

from .config import settings
from . import audit
from .store import _connect

OAUTH_BASE = "https://meta.wikimedia.org/w/rest.php/oauth2"
SESSION_TTL = 12 * 3600


class AuthError(RuntimeError):
    pass


def validate_config():
    audit.validate_config(settings)
    if any(not re.fullmatch(r'[1-9][0-9]{0,19}', x) for x in settings.admin_user_ids):
        raise RuntimeError('KOCASE_ADMIN_USER_IDS에는 Wikimedia 중앙 사용자 ID 숫자를 쉼표로 구분해 넣으세요.')
    if len(settings.secret_key) < 32 or settings.secret_key == 'dev-only-change-me':
        raise RuntimeError("KOCASE_SECRET_KEY에 32자 이상의 임의 문자열을 설정하세요.")
    if not settings.oauth_client_id or not settings.oauth_client_secret:
        raise RuntimeError("MW_OAUTH_CLIENT_ID와 MW_OAUTH_CLIENT_SECRET이 필요합니다.")
    p = urlsplit(settings.oauth_redirect_uri)
    if p.scheme != 'https' or not p.netloc or p.query or p.fragment or p.path != '/oauth/callback':
        raise RuntimeError("MW_OAUTH_REDIRECT_URI는 HTTPS /oauth/callback 주소여야 합니다.")
    try:
        Fernet(settings.token_key.encode())
    except Exception:
        raise RuntimeError("KOCASE_TOKEN_KEY에 Fernet 키를 설정하세요.") from None


def is_admin(identity, cfg):
    # Only the authenticated OAuth subject and server configuration count.
    return bool(identity and identity.get('owner_id') in cfg.admin_user_ids)


def init_auth():
    with _connect() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS oauth_grants (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, username TEXT NOT NULL,
            token TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS web_sessions (
            id TEXT PRIMARY KEY, csrf TEXT NOT NULL, owner_id TEXT, username TEXT,
            auth_id TEXT, pending_state TEXT, pending_verifier TEXT, pending_at REAL,
            expires REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON web_sessions(expires);
        CREATE INDEX IF NOT EXISTS sessions_owner_idx ON web_sessions(owner_id);
        CREATE INDEX IF NOT EXISTS grants_expiry_idx ON oauth_grants(expires);
        CREATE INDEX IF NOT EXISTS grants_owner_idx ON oauth_grants(owner_id);
        ''')
        con.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in con.execute('PRAGMA table_info(oauth_grants)')}
        if 'login_state' not in columns:
            con.execute('ALTER TABLE oauth_grants ADD COLUMN login_state TEXT')
        con.execute('CREATE UNIQUE INDEX IF NOT EXISTS grants_state_idx ON oauth_grants(login_state)')
    # Files are private even though stored tokens are encrypted.
    settings.db_path.chmod(0o600)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _encrypt(token):
    return Fernet(settings.token_key.encode()).encrypt(json.dumps(token).encode()).decode()


def _decrypt(blob):
    try:
        return json.loads(Fernet(settings.token_key.encode()).decrypt(blob.encode()))
    except Exception:
        raise AuthError("인증정보를 읽을 수 없습니다. 다시 로그인하세요.") from None


def new_session(owner_id=None, username=None, auth_id=None):
    if not owner_id or not auth_id:
        raise AuthError('익명 서버 세션은 생성하지 않습니다.')
    sid = secrets.token_urlsafe(32)
    with _connect() as con:
        con.execute('BEGIN IMMEDIATE')
        now = time.time()
        _check_session_capacity(con, owner_id)
        con.execute("INSERT INTO web_sessions(id,csrf,owner_id,username,auth_id,expires) VALUES(?,?,?,?,?,?)",
                    (_hash(sid), secrets.token_urlsafe(32), owner_id, username, auth_id, now + SESSION_TTL))
    return sid


def _check_session_capacity(con, owner_id):
    if (con.execute('SELECT COUNT(*) FROM web_sessions').fetchone()[0] >= settings.max_auth_records
            or con.execute('SELECT COUNT(*) FROM oauth_grants').fetchone()[0] >= settings.max_auth_records
            or con.execute('SELECT COUNT(*) FROM oauth_grants WHERE owner_id=?', (owner_id,)).fetchone()[0] >= settings.max_user_sessions
            or con.execute('SELECT COUNT(*) FROM web_sessions WHERE owner_id=?', (owner_id,)).fetchone()[0] >= settings.max_user_sessions):
        raise AuthError('로그인 세션 한도에 도달했습니다. 잠시 후 다시 시도하세요.')


def get_session(sid):
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        return None
    with _connect() as con:
        row = con.execute("SELECT * FROM web_sessions WHERE id=? AND owner_id IS NOT NULL AND expires>?", (_hash(sid), time.time())).fetchone()
    return dict(row) if row else None


def delete_session(sid, disconnect=False):
    with _connect() as con:
        row = con.execute("SELECT auth_id FROM web_sessions WHERE id=?", (_hash(sid),)).fetchone()
        if row and disconnect and row['auth_id']:
            # Revoke the token immediately but retain the consumed state until
            # every pre-login cookie for this attempt is necessarily expired.
            con.execute("UPDATE oauth_grants SET token='',expires=MIN(expires,?) WHERE id=?",
                        (time.time()+600, row['auth_id']))
            con.execute("UPDATE jobs SET status='failed',step='failed' WHERE auth_id=? AND status='pending'", (row['auth_id'],))
        con.execute("DELETE FROM web_sessions WHERE id=?", (_hash(sid),))


def _client():
    return OAuth2Session(settings.oauth_client_id, settings.oauth_client_secret,
                         redirect_uri=settings.oauth_redirect_uri,
                         token_endpoint_auth_method='client_secret_post',
                         code_challenge_method='S256', default_timeout=30)


def begin_login():
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    # Encrypted, signed browser cookie only; no unauthenticated database writes.
    pending = _encrypt({'state': state, 'verifier': verifier, 'created': time.time()})
    with _client() as client:
        url, _ = client.create_authorization_url(OAUTH_BASE + '/authorize', state=state, code_verifier=verifier)
    return url, pending


def _valid_token(token):
    if not isinstance(token, dict) or not token.get('access_token') or str(token.get('token_type', '')).lower() != 'bearer':
        raise AuthError("Wikimedia 인증 응답이 올바르지 않습니다.")
    if not token.get('expires_at'):
        token['expires_at'] = time.time() + int(token.get('expires_in', 3600))
    return token


def complete_login(pending, state, code, previous_sid=None):
    if not pending or not state or not code or len(state) > 256 or len(code) > 4096:
        raise AuthError('로그인 요청이 없습니다.')
    attempt = _decrypt(pending)
    age = time.time() - attempt['created']
    if not 0 <= age <= 600 or not hmac.compare_digest(state, attempt['state']):
        raise AuthError('로그인 요청이 만료되었거나 일치하지 않습니다.')
    state_hash = _hash(state)
    # Invalid/replayed requests do not allocate rows. OAuth codes are single-use;
    # a unique state index also closes concurrent callbacks with different codes.
    with _connect() as con:
        if con.execute('SELECT 1 FROM oauth_grants WHERE login_state=?', (state_hash,)).fetchone():
            raise AuthError('이미 사용된 로그인 요청입니다.')
    try:
        with _client() as client:
            token = _valid_token(client.fetch_token(OAUTH_BASE + '/access_token',
                grant_type='authorization_code', code=code, code_verifier=attempt['verifier'],
                redirect_uri=settings.oauth_redirect_uri))
        with requests.get(OAUTH_BASE + '/resource/profile',
                headers={'Authorization': 'Bearer ' + token['access_token'], 'User-Agent': settings.user_agent},
                timeout=30, allow_redirects=False) as response:
            response.raise_for_status()
            profile = response.json()
        uid, name = str(profile.get('sub') or ''), profile.get('username')
        if not uid.isdigit() or not isinstance(name, str) or not name:
            raise AuthError("Wikimedia 사용자 정보를 확인하지 못했습니다.")
    except Exception:
        # Do not log response bodies, codes, token endpoints or provider exceptions.
        raise AuthError("Wikimedia 로그인에 실패했습니다. 다시 시도하세요.") from None
    grant_id = secrets.token_hex(24)
    sid = secrets.token_urlsafe(32)
    with _connect() as con:
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT 1 FROM oauth_grants WHERE login_state=?', (state_hash,)).fetchone():
            raise AuthError('이미 사용된 로그인 요청입니다.')
        if previous_sid:
            con.execute('DELETE FROM web_sessions WHERE id=?', (_hash(previous_sid),))
        _check_session_capacity(con, uid)
        expires = time.time() + SESSION_TTL
        con.execute("INSERT INTO oauth_grants(id,owner_id,username,token,expires,login_state) VALUES(?,?,?,?,?,?)",
                    (grant_id, uid, name, _encrypt(token), expires, state_hash))
        con.execute("INSERT INTO web_sessions(id,csrf,owner_id,username,auth_id,expires) VALUES(?,?,?,?,?,?)",
                    (_hash(sid), secrets.token_urlsafe(32), uid, name, grant_id, expires))
    return sid


def cleanup_auth():
    """Worker maintenance only; indexed, bounded batches, never per request."""
    with _connect() as con:
        now = time.time()
        con.execute('DELETE FROM web_sessions WHERE id IN (SELECT id FROM web_sessions WHERE expires<=? LIMIT 500)', (now,))
        con.execute('DELETE FROM web_sessions WHERE id IN (SELECT id FROM web_sessions WHERE owner_id IS NULL LIMIT 500)')
        con.execute('DELETE FROM oauth_grants WHERE id IN (SELECT id FROM oauth_grants WHERE expires<=? LIMIT 500)', (now,))


def access_token(auth_id, owner_id):
    # Serializes refresh-token rotation between this web process and the worker.
    with _connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT * FROM oauth_grants WHERE id=? AND owner_id=? AND expires>?",
                          (auth_id, owner_id, time.time())).fetchone()
        if not row:
            raise AuthError("로그인이 만료되었거나 연결이 해제되었습니다. 다시 로그인하세요.")
        token = _decrypt(row['token'])
        if float(token.get('expires_at', 0)) < time.time() + 120:
            if not token.get('refresh_token'):
                raise AuthError("인증이 만료되었습니다. 다시 로그인하세요.")
            try:
                with _client() as client:
                    fresh = _valid_token(client.refresh_token(OAUTH_BASE + '/access_token',
                                                              refresh_token=token['refresh_token']))
                fresh.setdefault('refresh_token', token['refresh_token'])
            except Exception:
                raise AuthError("인증 갱신에 실패했습니다. 다시 로그인하세요.") from None
            token = fresh
            con.execute("UPDATE oauth_grants SET token=? WHERE id=?", (_encrypt(token), auth_id))
        return token['access_token']
