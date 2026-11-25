from __future__ import annotations

import time
import logging
import multiprocessing
import resource
import errno
import requests
import signal
import uuid

from pypdf.errors import PdfReadError, FileNotDecryptedError

from .pipeline import Pipeline
from .auth import access_token, cleanup_auth, init_auth, AuthError
from .store import claim_next_job, update_job, init_db, cleanup_records, _connect, get_job, fail_job, ResourceLimit
from .errors import PublicJobError
from .mediawiki import MediaWikiError
from .config import settings
from . import audit, database, store
from .resources import cleanup_files, remove_partial_files


def run_forever() -> None:
    init_db()
    init_auth()
    holder = uuid.uuid4().hex
    leader = False
    last_cleanup = 0
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            try:
                if not leader:
                    leader = store.acquire_worker(holder)
                    if not leader:
                        time.sleep(settings.worker_heartbeat_seconds)
                        continue
                if not store.renew_worker(holder):
                    raise store.LeaseLost('worker lease expired')
                if time.monotonic() - last_cleanup >= 60:
                    cleanup_auth()
                    cleanup_files(settings)
                    cleanup_records()
                    audit.cleanup(settings)
                    last_cleanup = time.monotonic()
                job = claim_next_job(holder)
                if not job:
                    time.sleep(2)
                    continue
                run_isolated(job, holder)
            except (database.DatabaseUnavailable, audit.AuditUnavailable, store.LeaseLost, BlockingIOError):
                leader = False
                logging.error('worker paused: storage or lease unavailable')
                time.sleep(settings.worker_heartbeat_seconds)
    finally:
        if leader:
            try:
                store.release_worker(holder)
            except database.DatabaseUnavailable:
                pass  # The persistent lease expires without renewal.


def _bounded_job(job):
    if job.get('worker_id'):
        store.execution.set((job['id'], job['worker_id']))
    resource.setrlimit(resource.RLIMIT_AS, (settings.job_memory_bytes, settings.job_memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (settings.job_timeout, settings.job_timeout))
    run_job(job)


def run_isolated(job, holder=None):
    process = multiprocessing.get_context('fork').Process(target=_bounded_job, args=(job,))
    process.start()
    deadline = time.monotonic() + settings.job_timeout
    timed_out = False
    try:
        while process.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            process.join(min(settings.worker_heartbeat_seconds, remaining))
            if holder and not store.renew_worker(holder):
                raise store.LeaseLost('worker lease expired')
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
        process.close()
    # A completed external write must never be relabelled by a stale worker.
    token = store.execution.set((job['id'], holder)) if holder else None
    try:
        current = get_job(job['id'])
        if current and current['status'] == 'running':
            fail_job(job['id'], 'job_timeout' if timed_out else 'worker_stopped')
        remove_partial_files(settings, job['id'])
    finally:
        if token is not None:
            store.execution.reset(token)


def run_job(job):
    try:
        def provider():
            store.assert_execution(job['id'])
            token = access_token(job['auth_id'], job['owner_id'])
            store.assert_execution(job['id'])
            return token
        provider()  # Disconnected/expired jobs cannot run under a shared account.
        manifest = Pipeline(job["id"], token_provider=provider).run(
            job["case_number"],
            live=bool(job["live"]),
            pdf_url_override=job.get("pdf_url_override", ""),
            prepared_manifest=job['payload'] if job.get('reuse_pdf') else None,
        )
        update_job(job["id"], status="done", step="complete", payload=manifest.to_dict())
    except (store.LeaseLost, database.DatabaseUnavailable):
        logging.error('job interrupted: storage or execution authority unavailable')
        return
    except Exception as e:
        logging.error("job=%s failed: %s", job["id"], type(e).__name__)
        if isinstance(e, PublicJobError):
            fail_job(job['id'], e.code, **e.context)
            return
        if isinstance(e, AuthError):
            code = 'auth_expired'
        elif isinstance(e, ResourceLimit):
            code = e.code
        elif isinstance(e, MemoryError):
            code = 'memory_limit'
        elif isinstance(e, FileNotDecryptedError):
            code = 'pdf_encrypted'
        elif isinstance(e, PdfReadError):
            code = 'pdf_invalid'
        elif isinstance(e, requests.RequestException):
            code = 'external_connection'
        elif isinstance(e, OSError):
            code = ('work_storage_full' if e.errno in {errno.ENOSPC, errno.EDQUOT} else
                    'storage_permission' if e.errno in {errno.EACCES, errno.EPERM} else 'storage_error')
        elif isinstance(e, MediaWikiError):
            code = 'auth_expired' if e.authentication_failed else 'wiki_error'
        else:
            current = get_job(job['id']) or {}
            code = 'transcription_failed' if current.get('step') == 'transcription' else 'unexpected'
        fail_job(job['id'], code)


if __name__ == "__main__":
    run_forever()
