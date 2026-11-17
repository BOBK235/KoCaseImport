"""Transactional row-byte accounting for application-owned SQL, without triggers.

Only these fixed INSERT/UPDATE/DELETE forms are supported. Identifiers never
come from HTTP input. The caller holds mutation_lock until commit/rollback.
Primary keys are immutable, so only affected rows need to be measured.
"""
import re

from .database import ConfigurationError, DatabaseFull

FIELDS = {
    'jobs': 'id case_number status step live pdf_url_override payload log error created_at updated_at owner_id auth_id summary reuse_pdf worker_id'.split(),
    'admissions': 'job_id owner_id kind created_at'.split(),
    'oauth_grants': 'id owner_id username token expires login_state'.split(),
    'web_sessions': 'id csrf owner_id username auth_id pending_state pending_verifier pending_at expires'.split(),
}
KEYS = {t: ('job_id', 'kind') if t == 'admissions' else ('id',) for t in FIELDS}


def _invalid():
    raise ConfigurationError('지원되지 않는 저장소 변경 형식입니다. 운영자에게 알려 주세요.')


def _size(table):
    return '64+' + '+'.join(f'COALESCE(OCTET_LENGTH(`{c}`),0)' for c in FIELDS[table])


def _measure(con, table, keys):
    total = 0
    columns = ','.join('`'+c+'`' for c in KEYS[table])
    for offset in range(0, len(keys), 500):
        batch = keys[offset:offset+500]
        placeholders = ','.join('('+','.join('?' for _ in KEYS[table])+')' for _ in batch)
        row = con._execute(f'SELECT COALESCE(SUM({_size(table)}),0) FROM `{table}` '
                           f'WHERE ({columns}) IN ({placeholders})',
                           tuple(value for key in batch for value in key)).fetchone()
        total += int(row[0])
    return total


def mutate(con, sql, params):
    # Deliberately fail closed for unsupported forms on tracked tables.
    match = re.match(r'\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+`?(\w+)`?(?=\s|\(|$)', sql, re.I)
    if not match:
        _invalid()
    verb, table = match[1].upper().split()[0], match[2]
    if table not in FIELDS:
        return None
    params = tuple(params)
    old = 0
    if verb == 'INSERT':
        # No literal values, multi-value lists or upserts on tracked tables.
        insert = re.fullmatch(r'\s*INSERT\s+INTO\s+`?'+table+r'`?\s*(?:\(([^)]+)\))?\s*VALUES\s*\(([?,\s]+)\)\s*', sql, re.I)
        if not insert:
            _invalid()
        columns = [s.strip().strip('`') for s in insert[1].split(',')] if insert[1] else FIELDS[table]
        if len(set(columns)) != len(columns) or not set(columns) <= set(FIELDS[table]) or len(columns) != len(params):
            _invalid()
        if insert[2].count('?') != len(params) or not set(KEYS[table]) <= set(columns):
            _invalid()
        keys = [tuple(params[columns.index(c)] for c in KEYS[table])]
    else:
        body = sql[match.end():]
        where = re.search(r'\bWHERE\b', body, re.I)
        tail = body[where.start():] if where else ''
        count = tail.count('?')
        tail_params = params[-count:] if count else ()
        if verb == 'UPDATE':
            if re.search(r'\bLIMIT\b', body, re.I):
                _invalid()
            assignments = body[:where.start()] if where else body
            if any(re.search(r'\b`?'+key+r'`?\s*=', assignments, re.I) for key in KEYS[table]):
                _invalid()
        elif not where and body.strip():
            _invalid()
        columns = ','.join('`'+c+'`' for c in KEYS[table])
        rows = con._execute(f'SELECT {columns}, {_size(table)} FROM `{table}` {tail}', tail_params).fetchall()
        keys = [tuple(row[i] for i in range(len(KEYS[table]))) for row in rows]
        old = sum(int(row[len(KEYS[table])]) for row in rows)
    if verb == 'DELETE':
        # SELECT/DELETE with LIMIT need not choose the same query plan/rows.
        # Delete precisely the primary keys measured under mutation_lock.
        columns = ','.join('`'+c+'`' for c in KEYS[table])
        result = None
        count = 0
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset+500]
            placeholders = ','.join('('+','.join('?' for _ in KEYS[table])+')' for _ in batch)
            result = con._execute(f'DELETE FROM `{table}` WHERE ({columns}) IN ({placeholders})',
                                  tuple(v for key in batch for v in key))
            count += result.rowcount
        if result is None:
            result = con._execute(f'DELETE FROM `{table}` WHERE 1=0')
        result.rowcount = count
    else:
        result = con._execute(sql, params)
    delta = (0 if verb == 'DELETE' else _measure(con, table, keys)) - old
    if delta:
        updated = con._execute('UPDATE storage_budget SET used_bytes=used_bytes+? '
                               'WHERE id=1 AND used_bytes+?>=0 AND (used_bytes+?<=max_bytes OR ?<0)',
                               (delta, delta, delta, delta))
        if updated.rowcount != 1:
            raise DatabaseFull()
    return result
