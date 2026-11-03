from __future__ import annotations

import json
import re
from typing import Any

from .config import settings
from .mediawiki import MediaWikiClient, MediaWikiError
from .models import CaseMetadata


class WikisourceBuilder:
    def __init__(self, client: MediaWikiClient) -> None:
        self.client = client

    def _index_config(self) -> dict[str, Any]:
        text = self.client.get_page_source("MediaWiki:Proofreadpage index data config.json")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}

    def create_index(self, meta: CaseMetadata, filename: str, page_count: int) -> str:
        title = f"색인:{filename}"
        if self.client.page_exists(title) and not settings.overwrite:
            return title
        config = self._index_config()
        fields: dict[str, str] = {}
        for key, spec in config.items():
            semantic = str(spec.get("data", "")).lower()
            if semantic == "title" or key.lower() == "title":
                fields[key] = meta.case_number
            elif semantic == "author" or key.lower() == "author":
                fields[key] = meta.court
            elif semantic == "year" or key.lower() == "year":
                fields[key] = (meta.decision_date[:4] if meta.decision_date else "")
            elif semantic == "pagelist" or key.lower() in {"pages", "pagelist"}:
                fields[key] = "<pagelist />"
            elif semantic == "progress" or key.lower() == "progress":
                fields[key] = str(spec.get("default", ""))
            elif key.lower() == "source":
                fields[key] = "pdf"
            else:
                fields[key] = str(spec.get("default", ""))
        if not fields:
            fields = {
                "Title": meta.case_number,
                "Author": meta.court,
                "Year": meta.decision_date[:4] if meta.decision_date else "",
                "Source": "pdf",
                "Pages": "<pagelist />",
                "Progress": "",
            }
        payload = json.dumps({"fields": fields, "categories": []}, ensure_ascii=False)
        self.client.edit_page(
            title, payload,
            "헌법재판소 결정문 색인 자동 생성 (KoCaseImport)",
            contentformat="application/json",
            createonly=not settings.overwrite,
        )
        return title

    @staticmethod
    def serialize_page(body: str, username: str) -> str:
        # ProofreadPage's text/x-wiki serialization. Level 1 = Not proofread.
        return (
            f'<noinclude><pagequality level="1" user="{username}" /><div class="pagetext">\n\n\n'
            f'</noinclude>{body}<noinclude>\n\n\n</div></noinclude>'
        )

    def create_pages(self, filename: str, texts: list[tuple[str, str]], logger=lambda s: None) -> list[str]:
        username = self.client.user_name()
        titles = []
        for i, (text, source) in enumerate(texts, start=1):
            title = f"페이지:{filename}/{i}"
            titles.append(title)
            if self.client.page_exists(title) and not settings.overwrite:
                logger(f"{i}/{len(texts)}: 기존 페이지 유지")
                continue
            serialized = self.serialize_page(text, username)
            self.client.edit_page(
                title,
                serialized,
                f"초기 전사 자동 생성 ({source}; KoCaseImport) — 교정 안 됨",
                createonly=not settings.overwrite,
            )
            logger(f"{i}/{len(texts)}: 페이지 생성")
        return titles

    def create_main(self, meta: CaseMetadata, filename: str, page_count: int) -> str:
        title = meta.case_number
        if self.client.page_exists(title) and not settings.overwrite:
            return title
        text = (
            f"<pages index=\"{filename}\" from=\"1\" to=\"{page_count}\" />\n"
            f"\n<!-- 원문: {meta.source_page_url} -->\n"
        )
        self.client.edit_page(
            title,
            text,
            "헌법재판소 결정문 본문 자동 생성 (KoCaseImport)",
            createonly=not settings.overwrite,
        )
        return title
