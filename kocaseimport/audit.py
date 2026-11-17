"""Private, bounded JSONL request audit. Never store tokens, URLs or raw errors.

This is an application-owned server log, not a second history table. It is not
tamper-proof against a tool maintainer or a compromised application process.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
import uuid


class AuditUnavailable(RuntimeError):
    pass


ACTIONS = {
    'job_requested': '사건 준비 요청', 'publication_requested': '후속 게시 요청',
    'job_finished': '작업 종료', 'delete_requested': '기록·파일 삭제 요청',
    'job_deleted': '기록·파일 삭제 완료', 'admin_view': '관리자 기록 열람',
}
LOG_NAME = re.compile(r'audit-[0-9]{8}-[0-9]{20}-[0-9a-f]{32}\.jsonl\Z')


def directory(cfg):
    return cfg.audit_dir or cfg.db_path.parent / 'audit'


def validate_config(cfg):
    path = directory(cfg)
    root = path.resolve()
    forbidden = (cfg.work_dir.resolve(), (Path(__file__).parent / 'static').resolve())
    if (not path.is_absolute() or root == root.parent or 'public_html' in root.parts
            or any(root == p or p in root.parents for p in forbidden)):
        raise RuntimeError('KOCASE_AUDIT_DIR는 작업 폴더·공개 웹 폴더 밖의 비공개 절대 경로여야 합니다.')


@contextmanager
def _locked(cfg):
    try:
        root = directory(cfg)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise OSError('invalid audit directory')
        root.chmod(0o700)
        fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'a+b') as lock:
            os.fchmod(lock.fileno(), 0o600)
            # A failed/busy log must not make a request wait indefinitely.
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AuditUnavailable('요청 기록을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.')
                    time.sleep(0.02)
            try:
                yield root
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        raise AuditUnavailable('요청 기록을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.') from None


def _files(root):
    result = []
    for p in root.iterdir():
        if LOG_NAME.fullmatch(p.name):
            info = p.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError('unsafe audit file')
            result.append((p.name, p))
    return [p for _, p in sorted(result, reverse=True)]


def _prune(cfg, root):
    cutoff = time.time_ns() - cfg.audit_retention * 1_000_000_000
    keep = []
    for p in _files(root):
        # Expire by segment creation, not last append: continuing to append
        # cannot extend the retention of older records in the same segment.
        if int(p.name.split('-')[2]) <= cutoff or len(keep) >= cfg.audit_file_count:
            p.unlink()
        else:
            keep.append(p)
    return keep


def cleanup(cfg):
    with _locked(cfg) as root:
        _prune(cfg, root)


def _text(value, limit):
    # Also remove visual line separators/bidi controls; JSON escaping prevents
    # physical log-line injection, and Jinja escapes HTML on display.
    return ''.join(c for c in str(value or '') if c.isprintable())[:limit]


def record(cfg, action, *, actor_id, actor_name='', job=None, state=''):
    if action not in ACTIONS or not actor_id:
        raise ValueError('invalid audit event')
    job = job or {}
    row = {
        'v': 1, 'action': action, 'actor_id': _text(actor_id, 80),
        'actor_name': _text(actor_name, 150), 'job_id': _text(job.get('id'), 12),
        'case_number': _text(job.get('case_number'), 40),
        'mode': ('live' if job.get('live') else 'dry_run') if job else '',
        'state': state if state in {'pending', 'running', 'done', 'failed'} else '',
    }
    with _locked(cfg) as root:
        files = _prune(cfg, root)
        now = time.time_ns()
        row['at_ns'] = now
        row['at'] = datetime.fromtimestamp(now / 1e9, timezone.utc).isoformat(timespec='seconds')
        encoded = (json.dumps(row, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
        day = datetime.fromtimestamp(now / 1e9, timezone.utc).strftime('%Y%m%d')
        current = files[0] if files and files[0].name.startswith('audit-' + day + '-') else None
        if current is None or current.stat().st_size + len(encoded) + 1 > cfg.audit_file_bytes:
            while len(files) >= cfg.audit_file_count:
                files.pop().unlink()
            current = root / f'audit-{day}-{now:020d}-{uuid.uuid4().hex}.jsonl'
        fd = os.open(current, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'a+b') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b'\n':
                    stream.write(b'\n')
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())


def try_record(cfg, action, **fields):
    """Completion logs must not relabel successful external edits as failed."""
    try:
        record(cfg, action, **fields)
    except AuditUnavailable:
        logging.error('audit completion unavailable; request audit retained')


def read_events(cfg, *, before=None, actor_id='', case_number='', limit=100):
    """Read fixed log names only, at most 20 MiB by default; never arbitrary paths."""
    result = []
    cutoff = time.time_ns() - cfg.audit_retention * 1_000_000_000
    with _locked(cfg) as root:
        for path in _prune(cfg, root):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, 'rb') as stream:
                # Bound memory even if the file was manually replaced/expanded.
                size = os.fstat(stream.fileno()).st_size
                start = max(0, size - cfg.audit_file_bytes)
                stream.seek(start)
                if start:
                    stream.readline(4096)
                data = stream.read(cfg.audit_file_bytes)
            for line in reversed(data.splitlines()):
                if len(line) > 4096:
                    continue
                try:
                    row = json.loads(line)
                    at = row['at_ns']
                    if not isinstance(at, int) or at <= cutoff or (before is not None and at >= before):
                        continue
                    if row.get('action') not in ACTIONS or (actor_id and row.get('actor_id') != actor_id):
                        continue
                    if case_number and row.get('case_number') != case_number:
                        continue
                    # No generic raw JSON rendering, even for administrators.
                    view = {k: _text(row.get(k), n) for k, n in (
                        ('actor_id', 80), ('actor_name', 150), ('job_id', 12),
                        ('case_number', 40), ('at', 40), ('mode', 10), ('state', 10))}
                    view.update(action=ACTIONS[row['action']], at_ns=at)
                    result.append(view)
                    if len(result) > limit:
                        return result[:limit], result[limit - 1]['at_ns']
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    return result, None
