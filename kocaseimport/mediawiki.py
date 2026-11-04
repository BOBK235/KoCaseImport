from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from .config import settings


class MediaWikiError(RuntimeError):
    pass


class MediaWikiClient:
    def __init__(self, api_url: str, token_provider=None) -> None:
        self.api_url = api_url
        self.token_provider = token_provider
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": settings.user_agent})
        if settings.mw_bearer_token and not token_provider:
            self.s.headers.update({"Authorization": f"Bearer {settings.mw_bearer_token}"})
        self._logged_in = bool(settings.mw_bearer_token) and not token_provider
        self._csrf = ""

    def _authorize(self):
        if self.token_provider is not None:
            token = self.token_provider()
            if not token:
                raise MediaWikiError("사용자 OAuth 인증이 필요합니다.")
            header = f"Bearer {token}"
            if self.s.headers.get("Authorization") != header:
                self._csrf = ""
            self.s.headers["Authorization"] = header

    def login(self) -> None:
        if self.token_provider is not None:
            self._authorize()
            return
        if self._logged_in:
            return
        if not settings.mw_bot_username or not settings.mw_bot_password:
            raise MediaWikiError("MW_BOT_USERNAME / MW_BOT_PASSWORD 또는 MW_BEARER_TOKEN이 필요합니다.")
        token = self.get({"action": "query", "meta": "tokens", "type": "login"})["query"]["tokens"]["logintoken"]
        data = self.post({
            "action": "login",
            "lgname": settings.mw_bot_username,
            "lgpassword": settings.mw_bot_password,
            "lgtoken": token,
        }, raw=True)
        if data.get("login", {}).get("result") not in {"Success", "PASS"}:
            raise MediaWikiError(f"MediaWiki 로그인 실패: {data}")
        self._logged_in = True

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        self._authorize()
        if self.token_provider is not None:
            params = {**params, "assert": "user"}
        params = {**params, "format": "json", "formatversion": 2}
        r = self.s.get(self.api_url, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise MediaWikiError(str(data["error"]))
        return data

    def post(self, data: dict[str, Any], files=None, raw: bool = False) -> dict[str, Any]:
        self._authorize()
        if self.token_provider is not None:
            data = {**data, "assert": "user"}
        payload = {**data, "format": "json", "formatversion": 2}
        r = self.s.post(self.api_url, data=payload, files=files, timeout=180)
        r.raise_for_status()
        result = r.json()
        if "error" in result and not raw:
            raise MediaWikiError(str(result["error"]))
        return result

    def csrf_token(self) -> str:
        self.login()
        if not self._csrf:
            self._csrf = self.get({"action": "query", "meta": "tokens"})["query"]["tokens"]["csrftoken"]
        return self._csrf

    def page_exists(self, title: str) -> bool:
        page = self.get({"action": "query", "titles": title})["query"]["pages"][0]
        return not page.get("missing", False)

    def get_page_source(self, title: str) -> str:
        data = self.get({"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main", "titles": title})
        page = data["query"]["pages"][0]
        if page.get("missing"):
            return ""
        return page["revisions"][0]["slots"]["main"].get("content", "")

    def edit_page(self, title: str, text: str, summary: str, contentformat: str | None = None,
                  createonly: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": "edit",
            "title": title,
            "text": text,
            "summary": summary,
            "token": self.csrf_token(),
        }
        if self.token_provider is None:
            data["bot"] = 1
        if createonly:
            data["createonly"] = 1
        if contentformat:
            data["contentformat"] = contentformat
        result = self.post(data)
        time.sleep(settings.edit_delay)
        return result

    def find_file_by_sha1(self, sha1_hex: str) -> str:
        try:
            data = self.get({"action": "query", "list": "allimages", "aisha1": sha1_hex, "ailimit": 10})
            images = data.get("query", {}).get("allimages", [])
            if images:
                return images[0]["name"]
        except MediaWikiError:
            if self.token_provider is not None:
                raise
            return ""
        return ""

    def upload_file(self, path: Path, filename: str, description: str, comment: str) -> dict[str, Any]:
        with path.open("rb") as f:
            files = {"file": (filename, f, "application/pdf")}
            data = {
                "action": "upload",
                "filename": filename,
                "text": description,
                "comment": comment,
                "token": self.csrf_token(),
            }
            result = self.post(data, files=files)
            upload = result.get("upload", {})
            if upload.get("result") == "Warning":
                raise MediaWikiError(f"Commons 업로드 경고로 중단: {upload.get('warnings', {})}")
            return result

    def image_for_page(self, page_title: str) -> str:
        data = self.get({"action": "query", "prop": "imageforpage", "titles": page_title})
        page = data["query"]["pages"][0]
        info = page.get("imagesforpage", {})
        url = info.get("fullsize") or info.get("thumbnail") or ""
        if url.startswith("//"):
            url = "https:" + url
        return url

    def user_name(self) -> str:
        try:
            data = self.get({"action": "query", "meta": "userinfo"})
            name = data.get("query", {}).get("userinfo", {}).get("name")
            if self.token_provider is not None and not name:
                raise MediaWikiError("OAuth 사용자 이름을 확인하지 못했습니다.")
            return name or settings.mw_bot_username.split("@", 1)[0]
        except Exception:
            if self.token_provider is not None:
                raise
            return settings.mw_bot_username.split("@", 1)[0]
