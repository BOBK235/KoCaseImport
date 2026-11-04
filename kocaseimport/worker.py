from __future__ import annotations

import time
import logging

from .pipeline import Pipeline
from .auth import access_token
from .store import claim_next_job, update_job


def run_forever() -> None:
    while True:
        job = claim_next_job()
        if not job:
            time.sleep(2)
            continue
        run_job(job)


def run_job(job):
    try:
        provider = lambda: access_token(job["auth_id"], job["owner_id"])
        provider()  # Disconnected/expired jobs cannot run under a shared account.
        manifest = Pipeline(job["id"], token_provider=provider).run(
            job["case_number"],
            live=bool(job["live"]),
            pdf_url_override=job.get("pdf_url_override", ""),
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
