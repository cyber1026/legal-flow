"""条文号归一化。

法库里 article_no 存中文汉字形式（如「第五百三十三条」），但 LLM 可能输出
「第533条」「533」「第五百三十三条之一」等多种写法。本模块提供：

- `normalize_article_no`：归一化为统一比较键（阿拉伯数字形式，如「533」「533之1」），
  供核验时两侧（模型输出 vs 法库返回）对齐比较。
- `to_cn_article_no`：转成法库的标准中文形式「第X条」，供精确查询拼 expr。

article 号通常 1~9999，偶有「第X条之一」补充条文。无法解析时原样返回。
"""

from __future__ import annotations

_CN_DIGITS = {
    "零": 0, "○": 0, "〇": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_DIGITS_CN = "零一二三四五六七八九"
_UNITS_CN = ["", "十", "百", "千"]


def _cn_to_int(s: str) -> int | None:
    """中文数字转 int；含非数字字符返回 None。"""
    total = 0
    section = 0
    number = 0
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
        else:
            return None
    return total + section + number


def _int_to_cn(n: int) -> str:
    """int 转中文数字（支持 0~9999，足够覆盖条文号）。"""
    if n == 0:
        return "零"
    s = ""
    str_n = str(n)
    length = len(str_n)
    zero_flag = False
    for i, c in enumerate(str_n):
        d = int(c)
        pos = length - i - 1
        if d == 0:
            zero_flag = True
            continue
        if zero_flag:
            s += "零"
            zero_flag = False
        s += _DIGITS_CN[d] + _UNITS_CN[pos]
    if s.startswith("一十"):  # 13 -> 十三，而非 一十三
        s = s[1:]
    return s


def _parse_int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    return _cn_to_int(s)


def _split_core_suffix(raw: str) -> tuple[str, str]:
    """剥掉「第/条/书名号/空格」，拆出主条号与「之X」补充号。"""
    s = (raw or "").strip().replace(" ", "")
    for ch in ("《", "》", "第", "条"):
        s = s.replace(ch, "")
    if "之" in s:
        core, _, suffix = s.partition("之")
        return core, suffix
    return s, ""


def normalize_article_no(raw: str) -> str:
    """归一化为比较键（阿拉伯形式）。无法解析时返回去空格的原串。

    例：「第533条」「五百三十三」「第五百三十三条」→ "533"；
        「第五百三十三条之一」→ "533之1"。
    """
    if not raw:
        return ""
    core, suffix = _split_core_suffix(raw)
    core_int = _parse_int(core)
    if core_int is None:
        return (raw or "").strip()
    key = str(core_int)
    if suffix:
        suf_int = _parse_int(suffix)
        key += f"之{suf_int}" if suf_int is not None else f"之{suffix}"
    return key


def to_cn_article_no(raw: str) -> str:
    """转成法库标准中文形式「第X条」。无法解析时返回去空格的原串。

    例：「533」「第533条」→「第五百三十三条」；「533之1」→「第五百三十三条之一」。
    """
    if not raw:
        return ""
    core, suffix = _split_core_suffix(raw)
    core_int = _parse_int(core)
    if core_int is None:
        return (raw or "").strip()
    out = f"第{_int_to_cn(core_int)}条"
    if suffix:
        suf_int = _parse_int(suffix)
        out += f"之{_int_to_cn(suf_int)}" if suf_int is not None else f"之{suffix}"
    return out


__all__ = ["normalize_article_no", "to_cn_article_no"]
