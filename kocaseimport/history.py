"""Owner-authorized removal of terminal jobs and their local artifacts."""
import re
import shutil
import sqlite3
import stat

from . import audit
from .resources import storage_lock
from .store import _connect


class HistoryError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def selected_ids(values):
    if not values or len(values) > 30 or any(not re.fullmatch(r'[0-9a-f]{12}', x) for x in values):
        raise HistoryError('삭제할 작업을 1개 이상, 최대 30개 선택해 주세요.')
    return list(dict.fromkeys(values))


def _selection(con, ids, owner_id):
    if not owner_id:
        raise HistoryError('로그인이 필요합니다.', 401)
    rows = con.execute(
        f"SELECT id,case_number,status,live,owner_id FROM jobs WHERE owner_id=? AND id IN ({','.join('?' for _ in ids)})",
        (owner_id, *ids),
    ).fetchall()
    if len(rows) != len(ids):
        raise HistoryError('선택한 작업을 찾을 수 없습니다. 목록을 새로 고쳐 주세요.', 404)
    if any(row['status'] not in {'done', 'failed'} for row in rows):
        raise HistoryError('대기·실행 중인 작업은 삭제할 수 없습니다. 작업이 끝난 뒤 다시 선택해 주세요.', 409)
    return [dict(row) for row in rows]


def preview(ids, owner_id):
    ids = selected_ids(ids)
    with _connect(timeout=2) as con:
        return _selection(con, ids, owner_id)


def _remove_directory(root, job_id):
    path = root / job_id  # Validated ID only; never use a payload/pdf filename.
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        path.unlink()  # Remove the link, never its target.
    elif stat.S_ISDIR(info.st_mode) and shutil.rmtree.avoids_symlink_attacks:
        shutil.rmtree(path)
    else:
        raise OSError('unsafe job directory')


def delete_jobs(cfg, ids, *, owner_id, username):
    ids = selected_ids(ids)
    try:
        # Same lock order as retention: storage -> DB -> audit. Do not wait for
        # another user's 120-second download in a web request.
        with storage_lock(cfg, blocking=False):
            with _connect(timeout=2) as con:
                con.execute('BEGIN IMMEDIATE')
                rows = _selection(con, ids, owner_id)
                for row in rows:
                    audit.record(cfg, 'delete_requested', actor_id=owner_id, actor_name=username, job=row, state=row['status'])
                root = cfg.work_dir.resolve()
                for row in rows:
                    _remove_directory(root, row['id'])
                con.executemany('DELETE FROM jobs WHERE id=? AND owner_id=?', [(x, owner_id) for x in ids])
                # admissions intentionally survive: no case/PDF/details there,
                # only the minimum rolling-24-hour quota ledger.
    except BlockingIOError:
        raise HistoryError('파일 처리 중입니다. 잠시 후 삭제를 다시 시도해 주세요.', 409) from None
    except OSError:
        # SQLite rolls back. Some artifacts may already be gone; a retry is
        # safe and can remove the remaining files and rows. Never claim success.
        raise HistoryError('파일 삭제를 완료하지 못했습니다. 기록은 유지되며 다시 삭제할 수 있습니다.', 503) from None
    except sqlite3.DatabaseError:
        raise HistoryError('기록 저장소를 사용할 수 없어 삭제를 완료하지 못했습니다. 목록을 새로 고친 뒤 다시 시도해 주세요.', 503) from None
    for row in rows:
        audit.try_record(cfg, 'job_deleted', actor_id=owner_id, actor_name=username, job=row, state=row['status'])
    return len(rows)
