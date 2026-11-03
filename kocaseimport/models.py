from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CaseMetadata:
    case_number: str
    case_name: str = ""
    decision_date: str = ""
    result: str = ""
    court: str = "대한민국 헌법재판소"
    source_page_url: str = ""
    pdf_url: str = ""
    doc_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImportManifest:
    case: CaseMetadata
    pdf_path: str = ""
    pdf_sha1: str = ""
    page_count: int = 0
    commons_filename: str = ""
    commons_title: str = ""
    commons_url: str = ""
    index_title: str = ""
    main_title: str = ""
    page_titles: list[str] = field(default_factory=list)
    text_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data
