from __future__ import annotations

import time
import logging
import multiprocessing
import resource

from .pipeline import Pipeline
from .auth import access_token, cleanup_auth, init_auth
from .store import claim_next_job, update_job, init_db, cleanup_records, _connect
from .config import settings
from .resources import cleanup_files, remove_partial_files


def run_forever() -> None:
    init_db()
    init_auth()
    # Deployment uses one queue supervisor. A container restart interrupts its child.
    with _connect() as con:
        con.execute("UPDATE jobs SET status='failed',step='failed' WHERE status='running'")
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
        update_job(job['id'], status='failed', step='failed', error='자원 또는 실행 시간 한도로 작업이 중단되었습니다.')
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
        update_job(
            job["id"],
            status="failed",
            step="failed",
            error=f"{type(e).__name__}: 작업 실패 (운영자 확인 필요)",
        )


if __name__ == "__main__":
    run_forever()
