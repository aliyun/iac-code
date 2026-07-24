# tests/i18n/test_html_extractor.py
import io

from iac_code.i18n.html_extractor import extract_html


def _run(html: str):
    return [msg for _lineno, _func, msg, _c in extract_html(io.BytesIO(html.encode("utf-8")), [], [], {})]


def test_extracts_data_i18n_text():
    assert _run('<button data-i18n="Send">发送</button>') == ["Send"]


def test_extracts_data_i18n_attr_pairs():
    html = '<input data-i18n-attr="placeholder:Describe needs;aria-label:Prompt" />'
    assert _run(html) == ["Describe needs", "Prompt"]


def test_multiple_on_one_line_and_ignores_plain_text():
    html = '<a data-i18n="New chat">新对话</a><a data-i18n="Search">搜索</a><b>x</b>'
    assert _run(html) == ["New chat", "Search"]
