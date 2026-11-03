import copy
from dataclasses import replace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

import kocaseimport.ccourt as module
from kocaseimport.ccourt import CCourtClient, CCourtError

# Minimal fixture preserving the schema supplied by the user, not API credentials.
API = {"header": {"resultCode": "0", "resultMsg": "NORMAL SERVICE"},
       "body": {"items": {"item": [{"eventNo": "2016헌나1", "eventNm": "대통령(박근혜)탄핵   ",
           "endDate": "20170310", "endRsta": "인용(파면)", "adjuDate": "20170310",
           "dtmPpHp": "https://isearch.ccourt.go.kr/view.do?idx=00&docId=48728_010500",
           "dtmPpXml": "https://isearch.ccourt.go.kr/download.do?filePath=/a.xml",
           "pdntList": [{"pdntHp": "https://isearch.ccourt.go.kr/view.do?idx=00&docId=48728_010300"}],
           "ogList": []}]}}}


def client(monkeypatch):
    monkeypatch.setattr(module, 'settings', replace(module.settings,
        ccourt_openapi_url='https://api.example.test/getIncdntDetail',
        ccourt_service_key='secret-example', ccourt_case_param='eventNo',
        ccourt_allow_published_pdf=True))
    return CCourtClient()


def view(doc, edition, **kwargs):
    return {'success': True, 'returnObject': {'00': {
        'eventNo': '2016헌나1', 'docId': doc, 'name': edition, **kwargs}}}


def test_api_mapping_and_published_pdf_token(monkeypatch):
    c = client(monkeypatch)
    c._json = Mock(side_effect=[API,
        view('48728_010500', '결정문', hwpFilePath='hwp-only'),
        view('48728_010300', '판례집', pdfFilePath='a+b/c==')])
    meta = c.lookup('2016 헌나 1')
    assert meta.case_name == '대통령(박근혜)탄핵'
    assert meta.decision_date == '2017-03-10'
    assert meta.result == '인용(파면)'
    params = c._json.call_args_list[0].args[1]
    assert params['eventNo'] == '2016헌나1' and params['serviceKey'] == 'secret-example'
    assert 'caseNo' not in params
    assert meta.raw['source_edition'] == '판례집'
    assert meta.doc_id == '48728_010300'
    assert urlsplit(meta.pdf_url).path == '/downloadEX.do'
    assert parse_qs(urlsplit(meta.pdf_url).query)['filePath'] == ['a+b/c==']


def test_api_error_and_exact_case_rejection(monkeypatch):
    c = client(monkeypatch)
    c._json = Mock(return_value={'header': {'resultCode': '30', 'resultMsg': 'secret-example'}})
    with pytest.raises(CCourtError, match='resultCode=30') as err:
        c.lookup('2016헌나1')
    assert 'secret-example' not in str(err.value)
    wrong = copy.deepcopy(API)
    wrong['body']['items']['item'][0]['eventNo'] = '2016헌나10'
    c._json = Mock(return_value=wrong)
    with pytest.raises(CCourtError, match='일치하는 항목이 0개'):
        c.lookup('2016헌나1')


def test_isearch_wrong_case_rejected(monkeypatch):
    c = client(monkeypatch)
    wrong = view('48728_010500', '결정문', pdfFilePath='anything')
    wrong['returnObject']['00']['eventNo'] = '2016헌나10'
    c._json = Mock(return_value=wrong)
    with pytest.raises(CCourtError, match='사건번호'):
        c._view(API['body']['items']['item'][0]['dtmPpHp'], '2016헌나1')


def test_strict_edition_and_override(monkeypatch):
    c = client(monkeypatch)
    monkeypatch.setattr(module, 'settings', replace(module.settings, ccourt_allow_published_pdf=False))
    c._json = Mock(side_effect=[API, view('48728_010500', '결정문', hwpFilePath='hwp')])
    with pytest.raises(CCourtError, match='PDF 없음'):
        c.lookup('2016헌나1')
    assert c._json.call_count == 2
    c._json = Mock(return_value=API)
    meta = c.lookup('2016헌나1', 'https://www.ccourt.go.kr/example.pdf')
    assert meta.pdf_url.endswith('example.pdf') and c._json.call_count == 1


def test_network_error_does_not_expose_key(monkeypatch):
    c = client(monkeypatch)
    c.s.get = Mock(side_effect=requests.ConnectionError('https://example/?serviceKey=secret-example'))
    with pytest.raises(CCourtError) as err:
        c.lookup('2016헌나1')
    assert 'secret-example' not in str(err.value)
    assert err.value.__suppress_context__


def test_non_pdf_download_does_not_replace_file(monkeypatch, tmp_path):
    c = client(monkeypatch)
    c._json = Mock(return_value=API)
    meta = c.lookup('2016헌나1', 'https://www.ccourt.go.kr/example.pdf')
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = [b'<html>error</html>']
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    c.s.get = Mock(return_value=response)
    dest = tmp_path / 'document.pdf'
    dest.write_bytes(b'existing')
    with pytest.raises(CCourtError, match='PDF가 아닙니다'):
        c.download_pdf(meta, dest)
    assert dest.read_bytes() == b'existing'
    assert not dest.with_suffix('.pdf.part').exists()
