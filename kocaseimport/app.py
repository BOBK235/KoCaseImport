from __future__ import annotations

from flask import Flask, abort, redirect, render_template, request, url_for

from .config import settings
from .store import create_job, get_job, init_db, list_jobs
from .utils import normalize_case_number


def create_app() -> Flask:
    settings.prepare()
    init_db()
    app = Flask(__name__)
    app.secret_key = settings.secret_key

    @app.get("/")
    def index():
        return render_template("index.html", jobs=list_jobs(), live_enabled=settings.live_edits)

    @app.post("/jobs")
    def new_job():
        raw = request.form.get("case_number", "")
        try:
            case_number = normalize_case_number(raw)
        except ValueError as e:
            return render_template("index.html", jobs=list_jobs(), live_enabled=settings.live_edits, error=str(e)), 400
        live = request.form.get("live") == "1"
        pdf_url = request.form.get("pdf_url", "").strip()
        job_id = create_job(case_number, live=live, pdf_url_override=pdf_url)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.get("/jobs/<job_id>")
    def job_detail(job_id: str):
        job = get_job(job_id)
        if not job:
            abort(404)
        return render_template("job.html", job=job)

    return app
