from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader

from .ccourt import CCourtClient
from .config import settings
from .mediawiki import MediaWikiClient
from .models import CaseMetadata, ImportManifest
from .ocr import OCRClient
from .store import append_log, update_job, update_summary
from .utils import resolve_job_pdf, sanitize_filename, sha1_file, wiki_page_url
from .wikisource import WikisourceBuilder


class Pipeline:
    def __init__(self, job_id: str | None = None, token_provider=None) -> None:
        self.job_id = job_id
        self.token_provider = token_provider
        self.ccourt = CCourtClient(on_case=lambda: self.summary(case_confirmed=True))

    def summary(self, **fields):
        if self.job_id:
            update_summary(self.job_id, **fields)

    def log(self, msg: str) -> None:
        print(msg, flush=True)
        if self.job_id:
            append_log(self.job_id, msg)

    def step(self, name: str) -> None:
        if self.job_id:
            update_job(self.job_id, step=name)

    def run(self, case_number: str, live: bool = False, pdf_url_override: str = "",
            prepared_manifest: dict | None = None) -> ImportManifest:
        settings.prepare()
        if live and not settings.live_edits:
            raise RuntimeError("실제 게시가 비활성화되어 있습니다.")
        effective_live = bool(live and settings.live_edits)
        self.summary(dry_run=not effective_live)
        if self.job_id and effective_live and self.token_provider is None:
            raise RuntimeError("웹 작업에는 사용자별 OAuth 인증이 필요합니다.")
        if prepared_manifest is not None:
            if not effective_live:
                raise RuntimeError("준비된 PDF는 실제 게시 작업에서만 이어서 사용할 수 있습니다.")
            self.step("verify_pdf")
            meta, pdf_path, sha1, page_count = self._load_prepared(case_number, prepared_manifest)
            base_name = pdf_path.name
            job_dir = pdf_path.parent
            self.log("Dry-run에서 확인한 사건 정보와 PDF 재사용")
        else:
            self.step("case_lookup")
            self.log(f"사건 조회: {case_number}")
            meta = self.ccourt.lookup(case_number, pdf_url_override=pdf_url_override)
            self.log(f"사건 확인: {meta.case_number} / {meta.case_name}")
            edition = meta.raw.get("source_edition", "결정문")
            self.summary(case_confirmed=True, edition=edition)
            suffix = f" ({edition} 수록본)" if edition in {"판례집", "공보"} else ""
            base_name = sanitize_filename(f"{meta.case_number} {meta.case_name} 결정문{suffix}") + ".pdf"
            job_dir = settings.work_dir / (self.job_id or meta.case_number)
            job_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = job_dir / base_name
            self.step("download_pdf")
            self.ccourt.download_pdf(meta, pdf_path)
            sha1 = sha1_file(pdf_path)
            page_count = len(PdfReader(str(pdf_path)).pages)

        edition = meta.raw.get("source_edition", "결정문")
        self.summary(case_confirmed=True, edition=edition)
        self.log(f"원문 판본: {edition} / {meta.source_page_url}")
        for warning in meta.raw.get("lookup_warnings", []):
            self.log(f"조회 알림: {warning}")
        self.summary(pdf_downloaded=True, page_count=page_count, pdf_filename=base_name)
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

        commons = MediaWikiClient(settings.commons_api, token_provider=self.token_provider)
        wikisource = MediaWikiClient(settings.wikisource_api, token_provider=self.token_provider)
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
            result = commons.upload_file(pdf_path, filename, description, "헌법재판소 공식 결정문 업로드 (KoCaseImport)")
            filename = result['upload'].get('filename') or filename
            self.log(f"Commons 업로드 완료: File:{filename}")
        manifest.commons_filename = filename
        manifest.commons_title = f"File:{filename}"
        manifest.commons_url = wiki_page_url(settings.commons_api, manifest.commons_title)
        self.summary(commons_title=manifest.commons_title, commons_reused=bool(existing_name))

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
        self.summary(wikisource_title=main_title)

        self._save_manifest(job_dir, manifest)
        return manifest

    def _load_prepared(self, case_number: str, data: dict):
        """Use only the verified dry-run bytes; never silently fetch a new edition."""
        meta = CaseMetadata(**data['case'])
        if meta.case_number != case_number:
            raise RuntimeError("저장된 사건번호가 작업과 일치하지 않습니다.")
        path = resolve_job_pdf(settings.work_dir, self.job_id or '', Path(data.get('pdf_path', '')).name)
        if path is None:
            raise RuntimeError("확인했던 PDF가 없습니다. 새 Dry-run 작업이 필요합니다.")
        sha1 = sha1_file(path)
        if sha1 != data.get('pdf_sha1'):
            raise RuntimeError("확인했던 PDF와 현재 파일이 다릅니다. 새 Dry-run 작업이 필요합니다.")
        page_count = len(PdfReader(str(path)).pages)
        if not page_count or page_count != data.get('page_count'):
            raise RuntimeError("확인했던 PDF의 쪽수가 일치하지 않습니다.")
        return meta, path, sha1, page_count

    def _save_manifest(self, job_dir: Path, manifest: ImportManifest) -> None:
        (job_dir / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _commons_description(meta) -> str:
        category = f"\n[[Category:{settings.commons_category}]]" if settings.commons_category else ""
        date = meta.decision_date or ""
        source = meta.source_page_url or meta.pdf_url
        edition = meta.raw.get("source_edition", "결정문")
        return f"""{{{{Information
|description={{{{ko|{meta.case_number} {meta.case_name} 헌법재판소 결정문 (사용 판본: {edition})}}}}
|date={date}
|source={source}
|author=대한민국 헌법재판소
|permission=
|other versions=
}}}}

== {{int:license-header}} ==
{{{{PD-KoreaGov}}}}{category}
"""
