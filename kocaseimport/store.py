from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config import settings

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


def _connect() -> sqlite3.Connection:
    settings.prepare()
    con = sqlite3.connect(settings.db_path, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _connect() as con:
        con.executescript(SCHEMA)


def create_job(case_number: str, live: bool, pdf_url_override: str = "") -> str:
    init_db()
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _connect() as con:
        con.execute(
            "INSERT INTO jobs(id,case_number,status,step,live,pdf_url_override,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (job_id, case_number, "pending", "queued", int(live), pdf_url_override, now, now),
        )
    return job_id


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


def list_jobs(limit: int = 30) -> list[dict[str, Any]]:
    with _connect() as con:
        rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def claim_next_job() -> dict[str, Any] | None:
    init_db()
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1"
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
