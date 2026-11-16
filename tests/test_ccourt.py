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
    assert params['type'] == 'json' and '_type' not in params
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


def test_historical_event_number_preserved_in_query_and_matching(monkeypatch):
    c = client(monkeypatch)
    response = copy.deepcopy(API)
    response['body']['items']['item'][0]['eventNo'] = '97헌가12'
    for entered in ('97헌가12', '1997헌가12'):
        c._json = Mock(return_value=response)
        meta = c.lookup(entered, 'https://ccourt.go.kr/test.pdf')
        assert c._json.call_args.args[1]['eventNo'] == '97헌가12'
        assert meta.case_number == '97헌가12'
    assert c._same_case('1997헌가12', '97헌가12')
    assert not c._same_case('97헌가123', '97헌가12')


def test_missing_pdf_is_distinct_from_failed_lookup(monkeypatch):
    from kocaseimport.errors import describe_failure, failure_record
    c = client(monkeypatch)
    c._json = Mock(return_value=API)
    meta = c._lookup_openapi('2016헌나1')
    c._view = Mock(side_effect=[{'name': '결정문', 'hwpFilePath': 'private-token'}, {'name': '판례집'}])
    with pytest.raises(CCourtError) as missing:
        c._resolve_pdf(meta)
    assert missing.value.code == 'pdf_missing'
    assert missing.value.context['hwp_editions'] == ['결정문']
    c._view = Mock(side_effect=[{'name': '결정문'}, CCourtError('private-token', code='lookup_timeout')])
    with pytest.raises(CCourtError) as failed:
        c._resolve_pdf(meta)
    assert failed.value.code == 'pdf_lookup_incomplete'
    shown = describe_failure(failure_record(failed.value.code, **failed.value.context))
    assert 'PDF가 없다고 단정할 수 없습니다' in shown['message']
    assert '공식 자료 조회 시간 초과' in shown['details']
    assert 'private-token' not in str(shown)


@pytest.mark.parametrize('status,body,expected', [
    (404, b'', 'pdf_http_missing'), (503, b'', 'pdf_http'),
    (200, b'<html>private-token</html>', 'pdf_not_pdf'), (200, b'', 'pdf_empty'),
])
def test_download_failure_codes(monkeypatch, tmp_path, status, body, expected):
    from kocaseimport.models import CaseMetadata
    c = client(monkeypatch)
    response = Mock(status_code=status, headers={})
    response.iter_content.return_value = [body]
    response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
    c.s.get = Mock(return_value=response)
    with pytest.raises(CCourtError) as error:
        c.download_pdf(CaseMetadata('97헌가12', pdf_url='https://ccourt.go.kr/test.pdf'), tmp_path/'test.pdf')
    assert error.value.code == expected
    assert not (tmp_path/'test.pdf.part').exists()


def test_network_failure_codes_do_not_include_request_urls(monkeypatch, tmp_path):
    from kocaseimport.models import CaseMetadata
    from kocaseimport.errors import failure_record, describe_failure
    c = client(monkeypatch)
    for error, code in ((requests.Timeout, 'pdf_timeout'),
                        (requests.exceptions.SSLError, 'pdf_tls'),
                        (requests.ConnectionError, 'pdf_connection')):
        c.s.get = Mock(side_effect=error('https://ccourt.go.kr/?serviceKey=PRIVATE-KEY'))
        with pytest.raises(CCourtError) as caught:
            c.download_pdf(CaseMetadata('97헌가12', pdf_url='https://ccourt.go.kr/test.pdf'), tmp_path/'test.pdf')
        assert caught.value.code == code
        visible = describe_failure(failure_record(caught.value.code, **caught.value.context))
        assert 'PRIVATE-KEY' not in str(visible)
