from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader

from .ccourt import CCourtClient
from .config import settings
from .mediawiki import MediaWikiClient
from .models import ImportManifest
from .ocr import OCRClient
from .store import append_log, update_job
from .utils import sanitize_filename, sha1_file
from .wikisource import WikisourceBuilder


class Pipeline:
    def __init__(self, job_id: str | None = None) -> None:
        self.job_id = job_id
        self.ccourt = CCourtClient()

    def log(self, msg: str) -> None:
        print(msg, flush=True)
        if self.job_id:
            append_log(self.job_id, msg)

    def step(self, name: str) -> None:
        if self.job_id:
            update_job(self.job_id, step=name)

    def run(self, case_number: str, live: bool = False, pdf_url_override: str = "") -> ImportManifest:
        settings.prepare()
        effective_live = bool(live and settings.live_edits)
        self.step("case_lookup")
        self.log(f"사건 조회: {case_number}")
        meta = self.ccourt.lookup(case_number, pdf_url_override=pdf_url_override)
        self.log(f"사건 확인: {meta.case_number} / {meta.case_name}")

        base_name = sanitize_filename(f"{meta.case_number} {meta.case_name} 결정문") + ".pdf"
        job_dir = settings.work_dir / (self.job_id or meta.case_number)
        job_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = job_dir / base_name

        self.step("download_pdf")
        self.ccourt.download_pdf(meta, pdf_path)
        sha1 = sha1_file(pdf_path)
        page_count = len(PdfReader(str(pdf_path)).pages)
        self.log(f"PDF 다운로드: {page_count}쪽 / SHA1 {sha1}")

        manifest = ImportManifest(
            case=meta,
            pdf_path=str(pdf_path),
            pdf_sha1=sha1,
            page_count=page_count,
            commons_filename=base_name,
            main_title=meta.case_number,
        )

        if not effective_live:
            self.log("DRY RUN: Wikimedia 업로드/편집은 수행하지 않았습니다.")
            self._save_manifest(job_dir, manifest)
            return manifest

        commons = MediaWikiClient(settings.commons_api)
        wikisource = MediaWikiClient(settings.wikisource_api)
        commons.login()
        wikisource.login()

        self.step("commons_upload")
        existing_name = commons.find_file_by_sha1(sha1)
        if existing_name:
            filename = existing_name
            self.log(f"Commons 동일 파일 재사용: {filename}")
        else:
            filename = base_name
            if commons.page_exists(f"File:{filename}"):
                stem = Path(base_name).stem
                filename = f"{stem} ({sha1[:8]}).pdf"
                self.log(f"Commons 파일명 충돌 감지: 새 이름 {filename} 사용")
                if commons.page_exists(f"File:{filename}"):
                    raise RuntimeError(f"Commons에 대상 파일명이 이미 존재합니다: File:{filename}")
            description = self._commons_description(meta)
            commons.upload_file(pdf_path, filename, description, "헌법재판소 공식 결정문 업로드 (KoCaseImport)")
            self.log(f"Commons 업로드 완료: File:{filename}")
        manifest.commons_filename = filename
        manifest.commons_title = f"File:{filename}"
        manifest.commons_url = "https://commons.wikimedia.org/wiki/File:" + filename.replace(" ", "_")

        self.step("wikisource_index")
        builder = WikisourceBuilder(wikisource)
        index_title = builder.create_index(meta, filename, page_count)
        manifest.index_title = index_title
        self.log(f"색인 준비 완료: {index_title}")

        self.step("transcription")
        ocr = OCRClient()
        texts = ocr.transcribe_pages(pdf_path, filename, wikisource, logger=self.log)
        manifest.text_sources = [source for _, source in texts]

        self.step("page_creation")
        page_titles = builder.create_pages(filename, texts, logger=self.log)
        manifest.page_titles = page_titles

        self.step("main_page")
        main_title = builder.create_main(meta, filename, page_count)
        manifest.main_title = main_title
        self.log(f"본문 완료: {main_title}")

        self._save_manifest(job_dir, manifest)
        return manifest

    def _save_manifest(self, job_dir: Path, manifest: ImportManifest) -> None:
        (job_dir / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _commons_description(meta) -> str:
        category = f"\n[[Category:{settings.commons_category}]]" if settings.commons_category else ""
        date = meta.decision_date or ""
        source = meta.source_page_url or meta.pdf_url
        return f"""{{{{Information
|description={{{{ko|{meta.case_number} {meta.case_name} 헌법재판소 결정문}}}}
|date={date}
|source={source}
|author=대한민국 헌법재판소
|permission=
|other versions=
}}}}

== {{int:license-header}} ==
{{{{PD-KoreaGov}}}}{category}
"""
