from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import settings
from .utils import wiki_page_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  case_number TEXT NOT NULL,
  status TEXT NOT NULL,
  step TEXT NOT NULL DEFAULT '',
  live INTEGER NOT NULL DEFAULT 0,
  pdf_url_override TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  log TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, created_at);
"""


@contextmanager
def _connect():
    settings.prepare()
    con = sqlite3.connect(settings.db_path, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db() -> None:
    with _connect() as con:
        con.executescript(SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        for name, definition in (("owner_id", "TEXT"), ("auth_id", "TEXT"),
                                 ("summary", "TEXT NOT NULL DEFAULT '{}'"),
                                 ("reuse_pdf", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        con.execute("CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs(owner_id, created_at)")
        # Legacy jobs have no authenticated owner; never expose or execute them.
        con.execute("UPDATE jobs SET status='failed', step='failed' WHERE owner_id IS NULL AND status='pending'")


def create_job(case_number: str, live: bool, pdf_url_override: str = "", *, owner_id: str, auth_id: str) -> str:
    if not owner_id or not auth_id:
        raise ValueError("인증된 작업 소유자가 필요합니다.")
    init_db()
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _connect() as con:
        con.execute(
            "INSERT INTO jobs(id,case_number,status,step,live,pdf_url_override,created_at,updated_at,owner_id,auth_id,summary) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, case_number, "pending", "queued", int(live), pdf_url_override, now, now, owner_id, auth_id, json.dumps({"dry_run": not live})),
        )
    return job_id


def queue_publication(job_id: str, *, owner_id: str, auth_id: str) -> bool:
    """Atomically promote one completed dry run; repeated submissions are no-ops."""
    if not owner_id or not auth_id:
        return False
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, owner_id)).fetchone()
        if not row:
            return False
        if row['live']:
            return bool(row['reuse_pdf'])  # Already queued by an earlier click.
        if row['status'] != 'done':
            return False
        summary = json.loads(row['summary'] or '{}')
        payload = json.loads(row['payload'] or '{}')
        if not summary.get('pdf_downloaded') or not all(
                payload.get(k) for k in ('case', 'pdf_path', 'pdf_sha1', 'page_count')):
            return False
        summary['dry_run'] = False
        con.execute(
            "UPDATE jobs SET live=1,reuse_pdf=1,status='pending',step='queued',auth_id=?,"
            "summary=?,error='',updated_at=? WHERE id=?",
            (auth_id, json.dumps(summary, ensure_ascii=False), time.time(), job_id),
        )
    return True


def get_job(job_id: str) -> dict[str, Any] | None:
    with _connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d["payload"] or "{}")
    except json.JSONDecodeError:
        d["payload"] = {}
    return d


def list_jobs(owner_id: str, limit: int = 30) -> list[dict[str, Any]]:
    with _connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?", (owner_id, limit)).fetchall()
    return [dict(r) for r in rows]


def claim_next_job() -> dict[str, Any] | None:
    init_db()
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM jobs WHERE status='pending' AND owner_id IS NOT NULL ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            con.commit()
            return None
        now = time.time()
        con.execute(
            "UPDATE jobs SET status='running', step='starting', updated_at=? WHERE id=? AND status='pending'",
            (now, row["id"]),
        )
        con.commit()
    return get_job(row["id"])


def update_job(job_id: str, **fields: Any) -> None:
    allowed = {"status", "step", "payload", "log", "error"}
    values = []
    sets = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "payload" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key}=?")
        values.append(value)
    sets.append("updated_at=?")
    values.append(time.time())
    values.append(job_id)
    with _connect() as con:
        con.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", values)


def append_log(job_id: str, message: str) -> None:
    with _connect() as con:
        row = con.execute("SELECT log FROM jobs WHERE id=?", (job_id,)).fetchone()
        old = row[0] if row else ""
        text = (old + ("\n" if old else "") + message)[-120000:]
        con.execute("UPDATE jobs SET log=?,updated_at=? WHERE id=?", (text, time.time(), job_id))


def update_summary(job_id: str, **fields) -> None:
    allowed = {"case_confirmed", "pdf_downloaded", "page_count", "pdf_filename", "edition", "dry_run",
               "commons_title", "commons_reused", "wikisource_title"}
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT summary FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return
        data = json.loads(row[0] or "{}")
        data.update({k: v for k, v in fields.items() if k in allowed})
        con.execute("UPDATE jobs SET summary=?, updated_at=? WHERE id=?",
                    (json.dumps(data, ensure_ascii=False), time.time(), job_id))


def public_job(job: dict) -> dict:
    """Explicit display allowlist. Never pass raw jobs to templates."""
    data = json.loads(job.get("summary") or "{}")
    status = job["status"]
    # Previous releases saved publication results only in completed manifests.
    payload = job.get('payload') or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    published = bool(job['live']) and status == 'done'
    commons_title = data.get('commons_title') or (payload.get('commons_title', '') if published else '')
    wikisource_title = data.get('wikisource_title') or (
        payload.get('main_title', '') if published and payload.get('index_title') else '')
    dry_run = data.get('dry_run', not bool(job['live']))
    waiting = '실행하지 않음 (Dry-run)' if dry_run else ('완료되지 않음' if status == 'failed' else '대기 중')
    case = "확인됨" if data.get("case_confirmed") else {"pending": "조회 대기 중", "running": "조회 중", "failed": "조회 실패"}.get(status, "확인되지 않음")
    if status == "failed" and data.get("case_confirmed"):
        case += " · 후속 작업 실패"
    elif status == "done":
        case += " · 작업 완료"
    pdf = "다운로드 완료" if data.get("pdf_downloaded") else ("완료되지 않음" if status == "failed" else "대기 중")
    return {"id": job["id"], "case_number": job["case_number"], "case_status": case,
            "pdf_status": pdf, "page_count": data.get("page_count"),
            "edition": data.get("edition") or "확인 전",
            "dry_run": dry_run,
            "commons_status": ('공용 업로드 완료' if not data.get('commons_reused') else '공용 파일 준비 완료 · 동일 파일 재사용') if commons_title else waiting,
            "commons_url": wiki_page_url(settings.commons_api, commons_title) if commons_title else '',
            "wikisource_status": '위키문헌 업로드 완료' if wikisource_title else ('처리 중' if commons_title and status == 'running' else waiting),
            "wikisource_url": wiki_page_url(settings.wikisource_api, wikisource_title) if wikisource_title else '',
            "refresh": status in {"pending", "running"}}
