from __future__ import annotations

import time
import logging
import multiprocessing
import resource
import errno
import requests

from pypdf.errors import PdfReadError, FileNotDecryptedError

from .pipeline import Pipeline
from .auth import access_token, cleanup_auth, init_auth, AuthError
from .store import claim_next_job, update_job, init_db, cleanup_records, _connect, get_job, fail_job, ResourceLimit
from .errors import PublicJobError
from .mediawiki import MediaWikiError
from .config import settings
from .resources import cleanup_files, remove_partial_files


def run_forever() -> None:
    init_db()
    init_auth()
    # Deployment uses one queue supervisor. A container restart interrupts its child.
    with _connect() as con:
        interrupted = [row['id'] for row in con.execute("SELECT id FROM jobs WHERE status='running'")]
    for job_id in interrupted:
        fail_job(job_id, 'worker_restarted')
    last_cleanup = 0
    while True:
        if time.monotonic() - last_cleanup >= 60:
            try:
                cleanup_auth()
                cleanup_files(settings)
                cleanup_records()
            except Exception as e:
                logging.error('maintenance failed: %s', type(e).__name__)
            last_cleanup = time.monotonic()
        job = claim_next_job()
        if not job:
            time.sleep(2)
            continue
        run_isolated(job)


def _bounded_job(job):
    resource.setrlimit(resource.RLIMIT_AS, (settings.job_memory_bytes, settings.job_memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (settings.job_timeout, settings.job_timeout))
    run_job(job)


def run_isolated(job):
    process = multiprocessing.get_context('fork').Process(target=_bounded_job, args=(job,))
    process.start()
    process.join(settings.job_timeout)
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
    if timed_out or process.exitcode != 0:
        fail_job(job['id'], 'job_timeout' if timed_out else 'worker_stopped')
    process.close()
    remove_partial_files(settings, job['id'])


def run_job(job):
    try:
        provider = lambda: access_token(job["auth_id"], job["owner_id"])
        provider()  # Disconnected/expired jobs cannot run under a shared account.
        manifest = Pipeline(job["id"], token_provider=provider).run(
            job["case_number"],
            live=bool(job["live"]),
            pdf_url_override=job.get("pdf_url_override", ""),
            prepared_manifest=job['payload'] if job.get('reuse_pdf') else None,
        )
        update_job(job["id"], status="done", step="complete", payload=manifest.to_dict())
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
            code = 'wiki_error'
        else:
            current = get_job(job['id']) or {}
            code = 'transcription_failed' if current.get('step') == 'transcription' else 'unexpected'
        fail_job(job['id'], code)


if __name__ == "__main__":
    run_forever()
