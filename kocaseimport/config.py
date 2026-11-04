from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path(os.getenv("KOCASE_DB_PATH", "/tmp/kocaseimport/jobs.sqlite3"))
    work_dir: Path = Path(os.getenv("KOCASE_WORK_DIR", "/tmp/kocaseimport/work"))
    secret_key: str = os.getenv("KOCASE_SECRET_KEY", "dev-only-change-me")
    live_edits: bool = _bool("KOCASE_LIVE_EDITS", False)
    user_agent: str = os.getenv(
        "KOCASE_USER_AGENT",
        "KoCaseImport/0.2.0 (https://ko.wikisource.org/)"
    )

    ccourt_service_key: str = os.getenv("CCOURT_SERVICE_KEY", "")
    ccourt_openapi_url: str = os.getenv("CCOURT_OPENAPI_URL", "")
    ccourt_case_param: str = os.getenv("CCOURT_CASE_PARAM", "eventNo")
    ccourt_allow_published_pdf: bool = _bool("CCOURT_ALLOW_PUBLISHED_PDF", True)

    oauth_client_id: str = os.getenv("MW_OAUTH_CLIENT_ID", "")
    oauth_client_secret: str = os.getenv("MW_OAUTH_CLIENT_SECRET", "")
    oauth_redirect_uri: str = os.getenv("MW_OAUTH_REDIRECT_URI", "")
    token_key: str = os.getenv("KOCASE_TOKEN_KEY", "")

    mw_bot_username: str = os.getenv("MW_BOT_USERNAME", "")
    mw_bot_password: str = os.getenv("MW_BOT_PASSWORD", "")
    mw_bearer_token: str = os.getenv("MW_BEARER_TOKEN", "")
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

    def prepare(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
