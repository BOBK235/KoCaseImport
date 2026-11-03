from kocaseimport.utils import normalize_case_number, sanitize_filename, text_is_usable
from kocaseimport.wikisource import WikisourceBuilder


def test_case_number():
    assert normalize_case_number("2016 헌나 1") == "2016헌나1"
    assert normalize_case_number("24헌마123등") == "2024헌마123등"


def test_filename():
    assert sanitize_filename('2016헌나1: 대통령/탄핵?') == '2016헌나1 대통령 탄핵'


def test_text_quality():
    assert text_is_usable("대한민국 헌법재판소 결정 " * 10, 20)
    assert not text_is_usable("x", 20)


def test_proofread_serialization():
    text = WikisourceBuilder.serialize_page("본문", "ExampleBot")
    assert '<pagequality level="1" user="ExampleBot" />' in text
    assert "</noinclude>본문<noinclude>" in text
