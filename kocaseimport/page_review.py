"""Compare one published ProofreadPage with its retained source PDF."""
import hashlib
import json

from .errors import PublicJobError
from .local_ocr import extract_page_text
from .utils import pdf_wikitext, sha1_file


class ReviewError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def proposed_body(path, manifest, number, cfg):
    if path is None:
        raise ReviewError('보관된 PDF를 찾지 못했습니다. 같은 판본으로 새 작업을 실행해 주세요.', 410)
    try:
        if path.stat().st_size > cfg.max_pdf_bytes or sha1_file(path) != manifest.get('pdf_sha1'):
            raise ReviewError('게시할 때 사용한 PDF와 일치하지 않습니다.', 409)
        text = extract_page_text(path, number, cfg)
    except FileNotFoundError:
        raise ReviewError('보관 기간이 지나 PDF를 찾을 수 없습니다.', 410) from None
    except PublicJobError:
        raise ReviewError('이 쪽의 PDF 본문이 추출 한도를 넘었습니다.', 422) from None
    if not text:
        raise ReviewError('이 쪽에서 교정에 사용할 한글 본문을 추출하지 못했습니다.', 422)
    body = pdf_wikitext(text)
    if len(body.encode()) > cfg.ocr_page_text_bytes:
        raise ReviewError('추출문이 쪽별 본문 한도를 넘었습니다.', 422)
    return body


def read_revision(wiki, title, cfg):
    result = wiki.get({'action': 'query', 'prop': 'revisions', 'titles': title,
                       'rvslots': 'main', 'rvprop': 'ids|content|contentmodel',
                       'rvcontentformat-main': 'application/json', 'rvlimit': 1})
    pages = result.get('query', {}).get('pages', [])
    if not pages or pages[0].get('missing'):
        raise ReviewError('현재 위키문헌에 해당 쪽이 없습니다.', 404)
    try:
        revision = pages[0]['revisions'][0]
        slot = revision['slots']['main']
        if slot['contentmodel'] != 'proofread-page':
            raise ValueError
        content = json.loads(slot['content'])
        if not isinstance(content, dict) or not all(isinstance(content.get(k), str)
                                                    for k in ('body', 'header', 'footer')):
            raise ValueError
        if not isinstance(content.get('level'), dict) or type(revision['revid']) is not int:
            raise ValueError
        if len(content['body'].encode()) > cfg.ocr_page_text_bytes:
            raise ReviewError('기존 본문이 비교 한도를 넘습니다. 위키문헌에서 직접 편집해 주세요.', 422)
        return revision['revid'], content
    except (ValueError, TypeError, KeyError, IndexError):
        raise ReviewError('현재 판의 쪽별 본문과 교정 정보를 읽을 수 없습니다.', 422) from None


def review_stamp(job_id, owner, number, revid, body):
    return [job_id, owner, number, revid, hashlib.sha256(body.encode()).hexdigest()]
