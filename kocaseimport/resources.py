"""Bounded disk use and worker-only retention maintenance (Toolforge/Linux)."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import shutil
import time

from .store import _connect, ResourceLimit


@contextmanager
def storage_lock(cfg):
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    with (cfg.work_dir / '.storage.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def download_budget(cfg):
    # Serialize reservations, including concurrent processes and partial files.
    with storage_lock(cfg):
        used = sum(p.stat().st_size for p in cfg.work_dir.rglob('*') if p.is_file() and not p.is_symlink())
        available = min(cfg.max_work_bytes - used,
                        shutil.disk_usage(cfg.work_dir).free - 64 * 1024 * 1024)
        if available <= 0:
            raise ResourceLimit('PDF 보관 공간 한도에 도달했습니다. 잠시 후 다시 시도하세요.', code='work_storage_full')
        yield min(cfg.max_pdf_bytes, available)


def cleanup_files(cfg):
    cutoff = time.time() - cfg.pdf_retention
    with storage_lock(cfg):
        for directory in cfg.work_dir.iterdir():
            if not re.fullmatch(r'[0-9a-f]{12}', directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            with _connect() as con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute('SELECT status,updated_at,summary FROM jobs WHERE id=?', (directory.name,)).fetchone()
                if row and (row['status'] in {'pending', 'running'} or row['updated_at'] > cutoff):
                    continue
                if not row and directory.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(directory)
                if row:
                    summary = json.loads(row['summary'] or '{}')
                    summary['pdf_expired'] = True
                    con.execute('UPDATE jobs SET summary=? WHERE id=?', (json.dumps(summary), directory.name))


def remove_partial_files(cfg, job_id):
    if re.fullmatch(r'[0-9a-f]{12}', job_id):
        directory = cfg.work_dir / job_id
        if not directory.is_symlink():
            for path in directory.glob('*.part'):
                path.unlink(missing_ok=True)


def write_manifest(cfg, job_dir, data):
    encoded = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    if len(encoded) > cfg.max_manifest_bytes:
        raise ResourceLimit('작업 결과 보관 크기 한도를 초과했습니다.')
    path = job_dir / 'manifest.json'
    with storage_lock(cfg):
        used = sum(p.stat().st_size for p in cfg.work_dir.rglob('*') if p.is_file() and not p.is_symlink())
        old_size = path.stat().st_size if path.is_file() else 0
        if used - old_size + len(encoded) > cfg.max_work_bytes:
            raise ResourceLimit('작업 파일 보관 공간 한도를 초과했습니다.', code='work_storage_full')
        path.write_bytes(encoded)
