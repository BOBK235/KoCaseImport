"""OCR priority, failure boundaries and bounded native fallback."""
import ctypes.util
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from kocaseimport import ocr
from kocaseimport.auth import AuthError
from kocaseimport.config import Settings
from kocaseimport.database import DatabaseUnavailable
from kocaseimport.errors import PublicJobError, describe_failure, failure_record
from kocaseimport.mediawiki import MediaWikiClient, MediaWikiError
from kocaseimport.store import LeaseLost


IMAGE = 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Test.pdf/page1-1000px-Test.pdf.jpg'


def transcription(monkeypatch, tmp_path, count=1):
    path = tmp_path/'decision.pdf'
    path.write_bytes(b'%PDF-test-reader')
    monkeypatch.setattr(ocr, 'PdfReader', lambda source: SimpleNamespace(pages=[None]*count))
    monkeypatch.setattr(ocr, 'extract_page_text', Mock(return_value=''))
    wiki = Mock(spec=MediaWikiClient)
    wiki.page_exists.return_value = False
    wiki.image_for_page.return_value = IMAGE
    client = ocr.OCRClient()
    client.ocr_wikimedia = Mock(return_value='주문')
    client.ocr_local = Mock(return_value=('대체 본문', 'pyocr-tesseract'))
    return client, wiki, path


def test_remote_and_local_only_for_pages_without_usable_text(monkeypatch, tmp_path):
    client, wiki, path = transcription(monkeypatch, tmp_path, 3)
    client.ocr_wikimedia.side_effect = ['주문', ocr.RemoteOCRFailure('timeout'), '끝']
    logs = []
    assert client.transcribe_pages(path, '판례.pdf', wiki, logs.append) == [
        ('주문', 'wikimedia-ocr'), ('대체 본문', 'pyocr-tesseract'), ('끝', 'wikimedia-ocr')]
    client.ocr_local.assert_called_once_with(path, 2, wiki._authorize)
    assert [call.args[0] for call in wiki.image_for_page.call_args_list] == [
        '페이지:판례.pdf/1', '페이지:판례.pdf/2', '페이지:판례.pdf/3']
    assert len(logs) == 4


def test_mixed_document_keeps_page_numbers_and_existing_corrections(monkeypatch, tmp_path):
    client, wiki, path = transcription(monkeypatch, tmp_path, 4)
    wiki.page_exists.side_effect = [True, False, False, False]
    ocr.extract_page_text.side_effect = ['1. 헌법상 수도의 개념', '', '']
    client.ocr_wikimedia.side_effect = ['주문', ocr.RemoteOCRFailure('timeout')]
    texts = client.transcribe_pages(path, '결정문.pdf', wiki)
    assert texts == [('', 'existing-page'), ('1. 헌법상 수도의 개념', 'pdf-text'),
                     ('주문', 'wikimedia-ocr'), ('대체 본문', 'pyocr-tesseract')]
    assert [c.args[0] for c in wiki.image_for_page.call_args_list] == ['페이지:결정문.pdf/3', '페이지:결정문.pdf/4']
    from kocaseimport.wikisource import WikisourceBuilder
    from kocaseimport import wikisource
    monkeypatch.setattr(wikisource, 'settings', replace(Settings(), overwrite=True))
    wiki.edit_page.side_effect = [MediaWikiError('concurrent creation', code='articleexists'), {}, {}]
    titles = WikisourceBuilder(wiki).create_pages('결정문.pdf', texts)
    assert [c.args[0] for c in wiki.edit_page.call_args_list] == titles[1:]
    assert all(c.kwargs['createonly'] for c in wiki.edit_page.call_args_list)


@pytest.mark.parametrize('lookup', ['', requests.Timeout('private URL'), MediaWikiError('private response')])
def test_image_lookup_failure_uses_local_without_leaking_details(monkeypatch, tmp_path, lookup):
    client, wiki, path = transcription(monkeypatch, tmp_path)
    if isinstance(lookup, Exception):
        wiki.image_for_page.side_effect = lookup
    else:
        wiki.image_for_page.return_value = lookup
    logs = []
    assert client.transcribe_pages(path, '판례.pdf', wiki, logs.append)[0][1] == 'pyocr-tesseract'
    client.ocr_wikimedia.assert_not_called()
    assert 'private' not in '\n'.join(logs)


@pytest.mark.parametrize('error', [AuthError('revoked'), LeaseLost('fenced'), DatabaseUnavailable()])
def test_authority_or_database_failures_never_trigger_fallback(monkeypatch, tmp_path, error):
    client, wiki, path = transcription(monkeypatch, tmp_path)
    wiki.image_for_page.side_effect = error
    with pytest.raises(type(error)):
        client.transcribe_pages(path, '판례.pdf', wiki)
    client.ocr_local.assert_not_called()


def test_api_auth_error_stops_with_public_reason(monkeypatch, tmp_path):
    client, wiki, path = transcription(monkeypatch, tmp_path)
    wiki.image_for_page.side_effect = MediaWikiError('sensitive', code='assertuserfailed')
    with pytest.raises(PublicJobError) as failed:
        client.transcribe_pages(path, '판례.pdf', wiki)
    assert failed.value.code == 'auth_expired'
    client.ocr_local.assert_not_called()


def test_both_ocr_failures_report_page_and_stop(monkeypatch, tmp_path):
    client, wiki, path = transcription(monkeypatch, tmp_path, 3)
    client.ocr_wikimedia.side_effect = ['first', ocr.RemoteOCRFailure('no_text')]
    client.ocr_local.side_effect = PublicJobError(code='ocr_local_models')
    with pytest.raises(PublicJobError) as failed:
        client.transcribe_pages(path, '판례.pdf', wiki)
    record = failure_record(failed.value.code, 'transcription', **failed.value.context)
    public = describe_failure(record)
    assert record['page_number'] == 2
    assert '중단된 쪽: 2' in public['details']
    assert public['code'] == 'ocr_local_models'
    assert wiki.image_for_page.call_count == 2
    assert 'page_number' not in failure_record('ocr_empty', page_number='<script>')


def response(raw, status=200):
    value = Mock()
    value.status_code = status
    value.iter_content.return_value = [raw]
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    return value


def test_remote_request_parameters_short_text_and_credential_isolation(monkeypatch):
    monkeypatch.setattr(ocr, 'settings', replace(Settings(), mw_bearer_token='secret', ocr_lang='kor+eng'))
    client = ocr.OCRClient()
    assert 'Authorization' not in client.s.headers and client.s.trust_env is False
    get = Mock(return_value=response(json.dumps({'text': '주문'}, ensure_ascii=False).encode()))
    monkeypatch.setattr(client.s, 'get', get)
    assert client.ocr_wikimedia(IMAGE) == '주문'
    assert get.call_args.args == ('https://ocr.wmcloud.org/api',)
    args = get.call_args.kwargs
    assert args['params']['langs[]'] == ['kor', 'eng']
    assert args['params']['image'] == IMAGE
    assert args['allow_redirects'] is False and args['stream'] is True
    client.s.close()


@pytest.mark.parametrize('raw,status,reason', [
    (b'{"text":""}', 200, 'no_text'),
    (b'{"error":"private response"}', 200, 'invalid_response'),
    (b'<html>private error</html>', 200, 'invalid_response'),
    (b'[]', 200, 'invalid_response'),
    (b'', 503, 'http_error'),
])
def test_remote_failure_responses_are_fixed_codes(monkeypatch, raw, status, reason):
    client = ocr.OCRClient()
    monkeypatch.setattr(client.s, 'get', Mock(return_value=response(raw, status)))
    with pytest.raises(ocr.RemoteOCRFailure, match='^'+reason+'$'):
        client.ocr_wikimedia(IMAGE)
    client.s.close()


def test_remote_timeout_and_response_limits(monkeypatch):
    client = ocr.OCRClient()
    get = Mock(side_effect=requests.Timeout('secret'))
    monkeypatch.setattr(client.s, 'get', get)
    with pytest.raises(ocr.RemoteOCRFailure, match='^timeout$'):
        client.ocr_wikimedia(IMAGE)
    get.side_effect = None
    get.return_value = response(b'x'*33)
    monkeypatch.setattr(ocr, 'settings', replace(Settings(), ocr_response_bytes=32))
    with pytest.raises(ocr.RemoteOCRFailure, match='^response_limit$'):
        client.ocr_wikimedia(IMAGE)
    monkeypatch.setattr(ocr, 'settings', replace(Settings(), ocr_page_text_bytes=3))
    get.return_value = response(b'{"text":"long"}')
    with pytest.raises(ocr.RemoteOCRFailure, match='^response_limit$'):
        client.ocr_wikimedia(IMAGE)
    client.s.close()


def test_private_or_disguised_image_urls_never_reach_ocr(monkeypatch):
    client = ocr.OCRClient()
    get = Mock()
    monkeypatch.setattr(client.s, 'get', get)
    for url in ('http://127.0.0.1/a', 'file:///private.pdf',
                'https://upload.wikimedia.org.evil.example/a',
                'https://user:secret@upload.wikimedia.org/a',
                'https://upload.wikimedia.org:444/a', IMAGE+'\n', IMAGE+'#fragment'):
        with pytest.raises(ocr.RemoteOCRFailure, match='^image_address$'):
            client.ocr_wikimedia(url)
    get.assert_not_called()
    client.s.close()


def test_total_transcription_size_is_bounded(monkeypatch, tmp_path):
    client, wiki, path = transcription(monkeypatch, tmp_path, 3)
    monkeypatch.setattr(ocr, 'settings', replace(Settings(), ocr_total_text_bytes=8))
    with pytest.raises(PublicJobError) as failed:
        client.transcribe_pages(path, '판례.pdf', wiki)
    assert failed.value.code == 'ocr_result_limit' and failed.value.context['page_number'] == 2


def test_image_url_for_missing_page_and_empty_response(monkeypatch):
    wiki = MediaWikiClient('https://ko.wikisource.org/w/api.php')
    get = Mock(return_value={'query': {'pages': [{'missing': True, 'imagesforpage': {'fullsize': IMAGE[6:]}}]}})
    monkeypatch.setattr(wiki, 'get', get)
    assert wiki.image_for_page('페이지:판례.pdf/1') == IMAGE
    for result in ({'query': {'pages': []}}, {'query': None}, {'query': {'pages': {}}},
                   {'query': {'pages': [{'imagesforpage': []}]}}):
        get.return_value = result
        assert wiki.image_for_page('페이지:판례.pdf/1') == ''
    wiki.s.close()


def run_native(code, *args):
    result = subprocess.run([sys.executable, '-c', code, *map(str, args)],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_real_pyocr_reads_image_pdf_and_blank_page(tmp_path):
    if not ctypes.util.find_library('tesseract') or not Path('/usr/share/tesseract-ocr/5/tessdata/eng.traineddata').is_file():
        pytest.skip('Install Tesseract English data to run native OCR integration')
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (1200, 1600), 'white')
    # Pillow includes a scalable font, so this test needs no system font path.
    ImageDraw.Draw(image).text((80, 100), 'CONSTITUTIONAL COURT\nOfficial decision 2016\nOCR fallback verification',
                               font=ImageFont.load_default(size=40), fill='black', spacing=30)
    path = tmp_path/'image.pdf'
    image.save(path, resolution=150)
    image.close()
    blank = Image.new('RGB', (1200, 1600), 'white')
    blank_path = tmp_path/'blank.pdf'
    blank.save(blank_path, resolution=150)
    blank.close()
    data = json.loads(run_native('''
import json, sys
from dataclasses import replace
from kocaseimport.config import Settings
from kocaseimport.local_ocr import recognize_page
cfg = replace(Settings(), ocr_local_lang='eng', ocr_tessdata_dir='/usr/share/tesseract-ocr/5/tessdata', ocr_timeout=20)
print(json.dumps([recognize_page(p, 1, cfg) for p in sys.argv[1:]]))
''', path, blank_path))
    assert data[0][1] == 'pyocr-tesseract' and 'CONSTITUTIONAL COURT' in data[0][0]
    assert data[1] == ['', 'pyocr-blank']


def test_native_timeout_and_revocation_stop_child():
    run_native('''
import multiprocessing, time
from dataclasses import replace
from kocaseimport import local_ocr
from kocaseimport.config import Settings
from kocaseimport.errors import PublicJobError
from kocaseimport.auth import AuthError
local_ocr._render_and_ocr = lambda *args: time.sleep(20)
cfg = replace(Settings(), ocr_timeout=1)
started = time.monotonic()
try:
    local_ocr.recognize_page('unused.pdf', 1, cfg)
    raise AssertionError('timeout expected')
except PublicJobError as error:
    assert error.code == 'ocr_local_timeout', error.code
assert time.monotonic()-started < 5
assert not multiprocessing.active_children()
calls = [0]
def guard():
    calls[0] += 1
    if calls[0] > 1:
        raise AuthError('revoked')
try:
    local_ocr.recognize_page('unused.pdf', 1, replace(cfg, ocr_timeout=10), guard=guard)
    raise AssertionError('revocation expected')
except AuthError:
    pass
assert not multiprocessing.active_children()
''')
