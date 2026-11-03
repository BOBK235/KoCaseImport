from __future__ import annotations

import time
import traceback

from .pipeline import Pipeline
from .store import claim_next_job, update_job


def run_forever() -> None:
    while True:
        job = claim_next_job()
        if not job:
            time.sleep(2)
            continue
        try:
            manifest = Pipeline(job["id"]).run(
                job["case_number"],
                live=bool(job["live"]),
                pdf_url_override=job.get("pdf_url_override", ""),
            )
            update_job(job["id"], status="done", step="complete", payload=manifest.to_dict())
        except Exception as e:
            update_job(
                job["id"],
                status="failed",
                step="failed",
                error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=8)}",
            )


if __name__ == "__main__":
    run_forever()
