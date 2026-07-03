"""条文号归一化 `article_no` 的单测。"""

from __future__ import annotations

import pytest

from app.retrieval.article_no import normalize_article_no, to_cn_article_no


@pytest.mark.parametrize(
    "raw, key",
    [
        ("第533条", "533"),
        ("第五百三十三条", "533"),
        ("五百三十三", "533"),
        ("533", "533"),
        ("第二十条", "20"),
        ("第十三条", "13"),
        ("第十条", "10"),
        ("第一千二百六十条", "1260"),
        ("第五百三十三条之一", "533之1"),
        ("533之1", "533之1"),
    ],
)
def test_normalize_article_no(raw, key):
    assert normalize_article_no(raw) == key


def test_normalize_等价写法相等():
    assert normalize_article_no("第533条") == normalize_article_no("第五百三十三条") == "533"


def test_normalize_无法解析时原样返回():
    assert normalize_article_no("附则") == "附则"
    assert normalize_article_no("") == ""


@pytest.mark.parametrize(
    "raw, cn",
    [
        ("533", "第五百三十三条"),
        ("第533条", "第五百三十三条"),
        ("13", "第十三条"),
        ("20", "第二十条"),
        ("1260", "第一千二百六十条"),
        ("533之1", "第五百三十三条之一"),
    ],
)
def test_to_cn_article_no(raw, cn):
    assert to_cn_article_no(raw) == cn


def test_往返一致():
    for n in [1, 10, 13, 20, 99, 100, 533, 1260]:
        cn = to_cn_article_no(str(n))
        assert normalize_article_no(cn) == str(n)
