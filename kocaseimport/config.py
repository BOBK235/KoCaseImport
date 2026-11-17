from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_tool_data = os.getenv('TOOL_DATA_DIR', '')
_state = os.getenv('KOCASE_STATE_DIR', str(Path(_tool_data) / 'kocaseimport-data') if _tool_data else '')
_db_user = os.getenv('KOCASE_DB_USER', os.getenv('TOOL_TOOLSDB_USER', ''))
_toolforge = bool(_tool_data or os.getenv('TOOL_NAME') or os.getenv('TOOL_TOOLSDB_USER'))


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_backend: str = os.getenv('KOCASE_DB_BACKEND', 'mariadb' if _toolforge else 'sqlite')
    toolforge: bool = _toolforge
    db_host: str = os.getenv('KOCASE_DB_HOST', 'tools.db.svc.wikimedia.cloud')
    db_port: int = int(os.getenv('KOCASE_DB_PORT', '3306'))
    db_user: str = _db_user
    db_password: str = field(default=os.getenv('KOCASE_DB_PASSWORD', os.getenv('TOOL_TOOLSDB_PASSWORD', '')), repr=False)
    db_name: str = os.getenv('KOCASE_DB_NAME', _db_user + '__kocaseimport' if _db_user else '')
    db_ssl_ca: str = os.getenv('KOCASE_DB_SSL_CA', '')
    db_socket: str = os.getenv('KOCASE_DB_SOCKET', '')
    state_dir: Path | None = Path(_state) if _state else None
    tool_data_dir: Path | None = Path(_tool_data) if _tool_data else None
    worker_lease_seconds: int = 60
    worker_heartbeat_seconds: int = 5
    db_path: Path = Path(os.getenv("KOCASE_DB_PATH", "/tmp/kocaseimport/jobs.sqlite3"))
    work_dir: Path = Path(os.getenv("KOCASE_WORK_DIR", str(Path(_state)/'work') if _state else "/tmp/kocaseimport/work"))
    secret_key: str = field(default=os.getenv("KOCASE_SECRET_KEY", "dev-only-change-me"), repr=False)
    live_edits: bool = _bool("KOCASE_LIVE_EDITS", False)
    user_agent: str = os.getenv(
        "KOCASE_USER_AGENT",
        "KoCaseImport/0.2.6 (https://ko.wikisource.org/)"
    )

    ccourt_service_key: str = field(default=os.getenv("CCOURT_SERVICE_KEY", ""), repr=False)
    ccourt_openapi_url: str = os.getenv("CCOURT_OPENAPI_URL", "")
    ccourt_case_param: str = os.getenv("CCOURT_CASE_PARAM", "eventNo")
    ccourt_allow_published_pdf: bool = _bool("CCOURT_ALLOW_PUBLISHED_PDF", True)

    oauth_client_id: str = os.getenv("MW_OAUTH_CLIENT_ID", "")
    oauth_client_secret: str = field(default=os.getenv("MW_OAUTH_CLIENT_SECRET", ""), repr=False)
    oauth_redirect_uri: str = os.getenv("MW_OAUTH_REDIRECT_URI", "")
    token_key: str = field(default=os.getenv("KOCASE_TOKEN_KEY", ""), repr=False)
    admin_user_ids: tuple[str, ...] = tuple(x.strip() for x in os.getenv("KOCASE_ADMIN_USER_IDS", "").split(',') if x.strip())
    audit_dir: Path | None = Path(os.environ['KOCASE_AUDIT_DIR']) if os.getenv('KOCASE_AUDIT_DIR') else None
    audit_retention: int = 30 * 24 * 3600
    audit_file_bytes: int = 2 * 1024 * 1024
    audit_file_count: int = 10

    mw_bot_username: str = os.getenv("MW_BOT_USERNAME", "")
    mw_bot_password: str = field(default=os.getenv("MW_BOT_PASSWORD", ""), repr=False)
    mw_bearer_token: str = field(default=os.getenv("MW_BEARER_TOKEN", ""), repr=False)
    commons_api: str = os.getenv("COMMONS_API", "https://commons.wikimedia.org/w/api.php")
    wikisource_api: str = os.getenv("WIKISOURCE_API", "https://ko.wikisource.org/w/api.php")

    ocr_engine: str = os.getenv("OCR_ENGINE", "tesseract")
    ocr_lang: str = os.getenv("OCR_LANG", "kor")
    ocr_psm: int = int(os.getenv("OCR_PSM", "3"))
    ocr_min_text_chars: int = int(os.getenv("OCR_MIN_TEXT_CHARS", "80"))
    ocr_timeout: int = int(os.getenv("OCR_TIMEOUT", "120"))

    overwrite: bool = _bool("KOCASE_OVERWRITE", False)
    edit_delay: float = float(os.getenv("KOCASE_EDIT_DELAY", "0.6"))
    commons_category: str = os.getenv("KOCASE_COMMONS_CATEGORY", "").strip()

    # Admission counts are per authenticated central Wikimedia user.
    quota_window: int = 24 * 3600
    quota_warn: int = 80
    quota_limit: int = 100
    max_user_active_jobs: int = 3
    max_active_jobs: int = 100
    max_job_records: int = 2000
    max_db_bytes: int = 256 * 1024 * 1024
    max_manifest_bytes: int = 512 * 1024
    max_auth_records: int = 5000
    max_user_sessions: int = 10
    max_pdf_bytes: int = 50 * 1024 * 1024
    max_work_bytes: int = 1024 * 1024 * 1024
    max_pdf_pages: int = 500
    pdf_retention: int = 24 * 3600
    job_retention: int = 7 * 24 * 3600
    download_seconds: int = 120
    job_timeout: int = 30 * 60
    job_memory_bytes: int = 256 * 1024 * 1024

    def prepare(self) -> None:
        from .database import validate_storage
        validate_storage(self)
        if self.db_backend == 'sqlite':
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        elif self.state_dir:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.state_dir.chmod(0o700)
        self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.work_dir.chmod(0o700)


settings = Settings()
