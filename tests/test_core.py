from kocaseimport.utils import normalize_case_number, sanitize_filename, text_is_usable
from kocaseimport.wikisource import WikisourceBuilder


def test_case_number():
    assert normalize_case_number("2016 헌나 1") == "2016헌나1"
    assert normalize_case_number("24헌마123등") == "2024헌마123등"
    assert normalize_case_number('97헌가12') == '97헌가12'
    assert normalize_case_number('97 헌가 12') == '97헌가12'
    assert normalize_case_number('1997헌가12') == '97헌가12'
    assert normalize_case_number('99헌바1등') == '99헌바1등'
    import pytest
    with pytest.raises(ValueError):
        normalize_case_number('997헌가12')


def test_filename():
    assert sanitize_filename('2016헌나1: 대통령/탄핵?') == '2016헌나1 대통령 탄핵'


def test_text_quality():
    assert text_is_usable("대한민국 헌법재판소 결정 " * 10, 20)
    assert not text_is_usable("x", 20)


def test_proofread_serialization():
    text = WikisourceBuilder.serialize_page("본문", "ExampleBot")
    assert '<pagequality level="1" user="ExampleBot" />' in text
    assert "</noinclude>본문<noinclude>" in text


def test_main_header_license_and_only_requested_category():
    from unittest.mock import Mock
    from kocaseimport.models import CaseMetadata
    client = Mock(); client.page_exists.return_value = True
    meta = CaseMetadata('2016헌나1', case_name='대통령(박근혜)탄핵', decision_date='2017-03-10',
                        raw={'source_edition': '판례집'})
    WikisourceBuilder(client).create_main(meta, '결정문.pdf', 70)
    assert client.edit_page.call_args.args[0] == '초안:2016헌나1'
    assert client.edit_page.call_args.kwargs['createonly'] is False
    text = client.edit_page.call_args.args[1]
    assert text.startswith('{{머리말\n')
    assert '| 제목 = 2016헌나1\n' in text and '| 부제 = 대통령(박근혜)탄핵\n' in text
    assert '| 저자 = 대한민국 헌법재판소\n' in text and '| 연도 = 2017\n' in text
    assert '| 분류없음 = true' in text
    assert text.index('}}') < text.index('<pages index=') < text.index('== 라이선스 ==')
    assert '{{PD-South Korea-exempt}}' in text
    assert text.count('[[분류:') == 1 and text.endswith('[[분류:대한민국 헌법재판소 판례]]\n')
