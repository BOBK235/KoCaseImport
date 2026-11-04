"""Wikimedia OAuth2: server-side sessions, PKCE and encrypted per-grant tokens."""
from __future__ import annotations
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlsplit

from authlib.integrations.requests_client import OAuth2Session
from cryptography.fernet import Fernet
import requests

from .config import settings
from .store import _connect

OAUTH_BASE = "https://meta.wikimedia.org/w/rest.php/oauth2"
SESSION_TTL = 12 * 3600


class AuthError(RuntimeError):
    pass


def validate_config():
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
        ''')
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
    sid = secrets.token_urlsafe(32)
    with _connect() as con:
        now = time.time()
        con.execute("DELETE FROM web_sessions WHERE expires<?", (now,))
        con.execute("DELETE FROM oauth_grants WHERE expires<?", (now,))
        con.execute("INSERT INTO web_sessions(id,csrf,owner_id,username,auth_id,expires) VALUES(?,?,?,?,?,?)",
                    (_hash(sid), secrets.token_urlsafe(32), owner_id, username, auth_id, now + SESSION_TTL))
    return sid


def get_session(sid):
    if not isinstance(sid, str) or not sid:
        return None
    with _connect() as con:
        row = con.execute("SELECT * FROM web_sessions WHERE id=? AND expires>?", (_hash(sid), time.time())).fetchone()
    return dict(row) if row else None


def delete_session(sid, disconnect=False):
    with _connect() as con:
        row = con.execute("SELECT auth_id FROM web_sessions WHERE id=?", (_hash(sid),)).fetchone()
        if row and disconnect and row['auth_id']:
            con.execute("DELETE FROM oauth_grants WHERE id=?", (row['auth_id'],))
            con.execute("UPDATE jobs SET status='failed',step='failed' WHERE auth_id=? AND status='pending'", (row['auth_id'],))
        con.execute("DELETE FROM web_sessions WHERE id=?", (_hash(sid),))


def _client():
    return OAuth2Session(settings.oauth_client_id, settings.oauth_client_secret,
                         redirect_uri=settings.oauth_redirect_uri,
                         token_endpoint_auth_method='client_secret_post',
                         code_challenge_method='S256', default_timeout=30)


def begin_login(sid):
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    with _connect() as con:
        updated = con.execute("UPDATE web_sessions SET pending_state=?,pending_verifier=?,pending_at=? WHERE id=? AND expires>?",
                    (state, _encrypt(verifier), time.time(), _hash(sid), time.time())).rowcount
    if not updated:
        raise AuthError("로그인 화면을 새로 열어 주세요.")
    with _client() as client:
        url, _ = client.create_authorization_url(OAUTH_BASE + '/authorize', state=state, code_verifier=verifier)
    return url


def _valid_token(token):
    if not isinstance(token, dict) or not token.get('access_token') or str(token.get('token_type', '')).lower() != 'bearer':
        raise AuthError("Wikimedia 인증 응답이 올바르지 않습니다.")
    if not token.get('expires_at'):
        token['expires_at'] = time.time() + int(token.get('expires_in', 3600))
    return token


def complete_login(sid, state, code):
    # Consume the state atomically, once, before exchanging the code.
    with _connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT * FROM web_sessions WHERE id=? AND expires>?", (_hash(sid), time.time())).fetchone()
        if (not row or not state or not row['pending_state']
            or not hmac.compare_digest(state, row['pending_state'])
            or time.time() - row['pending_at'] > 600 or not code):
            raise AuthError("로그인 요청이 만료되었거나 일치하지 않습니다. 다시 시도하세요.")
        verifier = _decrypt(row['pending_verifier'])
        con.execute("UPDATE web_sessions SET pending_state=NULL,pending_verifier=NULL,pending_at=NULL WHERE id=?", (_hash(sid),))
    try:
        with _client() as client:
            token = _valid_token(client.fetch_token(OAUTH_BASE + '/access_token',
                grant_type='authorization_code', code=code, code_verifier=verifier,
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
    with _connect() as con:
        con.execute("INSERT INTO oauth_grants VALUES(?,?,?,?,?)",
                    (grant_id, uid, name, _encrypt(token), time.time() + SESSION_TTL))
    delete_session(sid)
    return new_session(uid, name, grant_id)


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
