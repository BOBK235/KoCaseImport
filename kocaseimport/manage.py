"""Operator commands; never exposed as web routes."""
import argparse
import json
import os
import sqlite3
import sys
import tarfile

from cryptography.fernet import InvalidToken

from .config import settings
from . import auth, database, migration, persistence


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--for-import', action='store_true')
    init.add_argument('--resume', action='store_true', help='데이터가 없는 초기화 실패 DB만 이어서 초기화')
    sub.add_parser('import').add_argument('archive')
    sub.add_parser('check')
    sub.add_parser('diagnose')
    for command in ('revoke-auth', 'rotate-auth-keys'):
        p = sub.add_parser(command)
        p.add_argument('--confirm', action='store_true', required=True)
        p.add_argument('--restored-backup', action='store_true')
    args = parser.parse_args()
    try:
        auth.validate_config()
        if settings.db_backend != 'mariadb':
            raise RuntimeError('이 명령은 영속 MariaDB 구성에서 실행하세요.')
        if args.command == 'init':
            persistence.initialize(settings, ready=not args.for_import, resume=args.resume)
            print('비공개 DB 초기화 완료. '+('이전 파일을 import한 뒤 시작하세요.' if args.for_import else '이전 사용량 보호를 위해 24시간 후 새 요청을 접수합니다.'))
        elif args.command == 'diagnose':
            print(json.dumps(persistence.diagnose(settings), ensure_ascii=False, indent=2))
        elif args.command == 'import':
            migration.import_snapshot(settings, args.archive)
            print('DB·파일 이전 완료. 기존 세션 만료 시각과 사용량을 유지했습니다.')
        elif args.command in {'revoke-auth', 'rotate-auth-keys'}:
            migration.revoke_auth(settings, rotate=args.command == 'rotate-auth-keys', restored=args.restored_backup)
            print('기존 인증을 무효화했습니다. 다시 로그인해야 합니다.')
        else:
            persistence.verify_ready(settings)
            with database.connect(settings) as con:
                counts = {t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in migration.TABLES}
                used = con.execute('SELECT used_bytes,max_bytes FROM storage_budget WHERE id=1').fetchone()
                print('DB·키·영속 폴더 확인 완료:', counts, 'logical_bytes=', tuple(used))
        return 0
    except (RuntimeError, ValueError, OSError, KeyError, TypeError, InvalidToken,
            tarfile.TarError, sqlite3.Error, database.pymysql.MySQLError) as e:
        # Driver errors may contain SQL and credentials. Never print their text.
        code = getattr(e, 'code', None)
        if isinstance(e, database.pymysql.MySQLError):
            code = e.args[0] if e.args and isinstance(e.args[0], int) else None
        text = str(e) if isinstance(e, RuntimeError) else '저장소 작업에 실패했습니다. 설정과 이전 파일을 확인하세요.'
        print(text + (f' [DB 코드: {code}]' if code else ''), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
