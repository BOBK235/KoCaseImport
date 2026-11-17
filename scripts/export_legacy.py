#!/usr/bin/env python3
"""Run INSIDE the old web pod: freeze an idle 0.2.5 app and stream a snapshot.

Stdlib only. Successful export deliberately leaves services SIGSTOP'ed so
requests cannot be lost between the snapshot and cutover. No credentials are
printed. On failure stopped processes are resumed. Never run on a shared host.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
import uuid


def export(db, work, audit, output, key_hashes, *, quiesced):
    if not quiesced:
        raise RuntimeError('서비스를 정지한 상태에서만 이전할 수 있습니다.')
    checksums = {}
    with tempfile.TemporaryDirectory(prefix='kocase-export-') as tmp:
        lock = sqlite3.connect(db, timeout=5)
        try:
            lock.execute('BEGIN IMMEDIATE')
            if lock.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]:
                raise RuntimeError('대기·실행 중 작업을 모두 마친 뒤 다시 시도하세요.')
            saved = Path(tmp)/'db.sqlite3'
            with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as source, sqlite3.connect(saved) as target:
                source.backup(target)
            with tarfile.open(fileobj=output, mode='w|') as tar:
                def add(path, name):
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise RuntimeError('심볼릭 링크·하드 링크는 이전할 수 없습니다.')
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, 'rb') as f:
                        digest = hashlib.file_digest(f, 'sha256').hexdigest()
                        f.seek(0)
                        member = tarfile.TarInfo(name)
                        member.size = info.st_size; member.mode = 0o600; member.mtime = info.st_mtime
                        tar.addfile(member, f)
                    checksums[name] = digest
                add(saved, 'db.sqlite3')
                if work.exists():
                    for folder in sorted(work.iterdir()):
                        if not re.fullmatch('[0-9a-f]{12}', folder.name):
                            continue
                        if folder.is_symlink() or not folder.is_dir():
                            raise RuntimeError('작업 폴더를 확인하세요.')
                        for path in sorted(folder.iterdir()):
                            if path.is_file() and path.suffix in {'.pdf', '.json', '.part'}:
                                add(path, 'work/'+folder.name+'/'+path.name)
                if audit.exists():
                    for path in sorted(audit.glob('audit-*.jsonl')):
                        add(path, 'audit/'+path.name)
                metadata = json.dumps({'version': 1, 'snapshot_id': uuid.uuid4().hex,
                                       'quiesced': True, 'created': time.time(),
                                       'keys': key_hashes, 'sha256': checksums}).encode()
                if len(metadata) > 8*1024*1024:
                    raise RuntimeError('이전 메타데이터 크기 한도를 초과했습니다.')
                member = tarfile.TarInfo('metadata.json'); member.size = len(metadata); member.mode = 0o600
                tar.addfile(member, io.BytesIO(metadata))
        finally:
            lock.rollback(); lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quiesce', action='store_true', required=True)
    parser.parse_args()
    os.umask(0o077)
    stopped = []
    success = False
    try:
        cookie, token = os.environ.get('KOCASE_SECRET_KEY', ''), os.environ.get('KOCASE_TOKEN_KEY', '')
        if len(cookie) < 32 or not token:
            raise RuntimeError('기존 서비스와 같은 인증 키 환경변수가 필요합니다.')
        db = Path(os.getenv('KOCASE_DB_PATH', '/tmp/kocaseimport/jobs.sqlite3')).resolve()
        if not db.is_file():
            raise RuntimeError('이전 DB가 있는 실제 web pod 안에서 실행하세요.')
        for proc in Path('/proc').iterdir():
            if not proc.name.isdecimal() or int(proc.name) == os.getpid():
                continue
            try:
                if proc.stat().st_uid != os.geteuid():
                    continue
                command = (proc/'cmdline').read_bytes().replace(b'\0', b' ')
                if b'kocaseimport.worker' in command or (b'gunicorn' in command and b'kocaseimport.app' in command):
                    os.kill(int(proc.name), signal.SIGSTOP)
                    stopped.append(int(proc.name))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        if not stopped:
            raise RuntimeError('동작 중인 KoCaseImport 프로세스를 확인하지 못했습니다.')
        hashes = {'cookie_key': hashlib.sha256(('cookie:'+cookie).encode()).hexdigest(),
                  'token_key': hashlib.sha256(('token:'+token).encode()).hexdigest()}
        work = Path(os.getenv('KOCASE_WORK_DIR', '/tmp/kocaseimport/work')).resolve()
        audit = Path(os.getenv('KOCASE_AUDIT_DIR', str(db.parent/'audit'))).resolve()
        export(db, work, audit, sys.stdout.buffer, hashes, quiesced=True)
        sys.stdout.buffer.flush()
        success = True
        print('SNAPSHOT_COMPLETE; 기존 서비스를 정지 상태로 유지합니다. 취소 시 해당 pod에서 kill -CONT ' + ' '.join(map(str, stopped)), file=sys.stderr)
    except BaseException as e:
        print('이전 파일 생성 실패: '+str(e), file=sys.stderr)
        return 1
    finally:
        if not success:
            for pid in stopped:
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
