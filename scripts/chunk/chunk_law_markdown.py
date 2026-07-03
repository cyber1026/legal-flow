#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法律 Markdown 结构感知 chunking 脚本。

输入为 `data/legal_sources/layer1_law/markdown/*.md`，输出为可直接用于向量入库的 JSONL。
本脚本针对法律文本做优化：以条文或修正案修改项为原子单元，维护编、分编、章、节、附件、
附表等结构路径，按 token 预算贪心合并相邻原子单元，默认不拆半条。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tqdm import tqdm


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_DIR = REPO_ROOT / "data" / "legal_sources" / "layer1_law" / "markdown"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "legal_sources" / "layer1_law" / "chunks" / "layer1_law_chunks.jsonl"

TOKEN_SAFETY_MARGIN = 8
DEFAULT_MAX_TOKENS = 1500
TABLE_CONTEXT_MAX_TOKENS = 256

_CN_NUM = r"[零〇○一二三四五六七八九十百千万两0-9]+"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_ARTICLE_RE = re.compile(rf"^\*\*(第{_CN_NUM}条)\*\*(?:\s*[　 ]?(.*))?$")
_AMENDMENT_ITEM_RE = re.compile(rf"^\*\*({_CN_NUM}、)\*\*(.*)$")
_TABLE_LINE_RE = re.compile(r"^\s*\|")
_VERSION_SUFFIX_RE = re.compile(r"_(\d{8})$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；])")
_INLINE_APPENDIX_TITLE_RE = re.compile(
    rf"^(?:《?中华人民共和国[^《》\n]{{0,120}}?》?)?(附[件录表]{_CN_NUM}.+)$"
)
# 立法性文件标题（全国人大／人大常委会「关于……的决定」「……修正案」）。标题不含句末标点，
# 借此与正文句子区分；这类标题在源 Markdown 中常被拆成多行，需先合并再识别。
_LEGISLATIVE_TITLE_RE = re.compile(r"^全国人民代表大会[^。！？，、；]*?(?:决定|修正案(?:（[^）]*）)?)$")
# 紧随标题之后的颁布／通过日期注，用于区分标题完全相同的不同决定。
_PROMULGATION_NOTE_RE = re.compile(r"^（[^（）]*\d{4}年[^（）]*）$")

logger = logging.getLogger(__name__)


class TokenCounter:
    """封装 BGE-M3 tokenizer，提供不含特殊 token 的 token 计数。"""

    def __init__(self, model_name: str) -> None:
        """加载 tokenizer；优先使用本地缓存，缺失时按 transformers 默认逻辑兜底。"""
        from transformers import AutoTokenizer

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
        except Exception:
            self._tokenizer = AutoTokenizer.from_pretrained(model_name)

    def count(self, text: str) -> int:
        """统计文本 token 数，不计 CLS/SEP 等特殊 token。"""
        if not text:
            return 0
        return len(self._tokenizer.encode(text, add_special_tokens=False))


@dataclass
class LawUnit:
    """法律文档中的最小切分原子：条文、修正项、表格、序言或其他结构文本。"""

    unit_type: str
    body: str
    path: list[str]
    label: str = ""
    order: int = 0
    token_count: int = 0


@dataclass
class LawDoc:
    """一篇法律 Markdown 解析后的结构化结果。"""

    source_file: str
    law_name: str
    title: str
    version: str
    effective_date: str
    preamble: str = ""
    units: list[LawUnit] = field(default_factory=list)


def parse_filename(path: Path) -> tuple[str, str, str]:
    """从文件名解析法律名称、版本日期和 ISO 日期。"""
    stem = path.stem
    match = _VERSION_SUFFIX_RE.search(stem)
    if not match:
        return stem, "", ""
    version = match.group(1)
    law_name = stem[: match.start()]
    effective_date = f"{version[:4]}-{version[4:6]}-{version[6:]}"
    return law_name, version, effective_date


def normalize_heading(text: str) -> str:
    """清洗标题文本，去掉目录中可能残留的多余空白。"""
    text = re.sub(r"\s+", " ", text.strip())
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"^(第[零〇○一二三四五六七八九十百千万两0-9]+(?:编|分编|章|节))(.+)$", r"\1 \2", text)
    text = re.sub(r"^(附[件录表])\s*([零〇○一二三四五六七八九十百千万两0-9]+)(.*)$", r"\1\2 \3", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def heading_kind(title: str) -> str:
    """按法律语义判断标题类型，不依赖 Markdown 的视觉层级。"""
    normalized = normalize_heading(title)
    if re.match(rf"^第{_CN_NUM}编(?:\s|$)", normalized):
        return "part"
    if re.match(rf"^第{_CN_NUM}分编(?:\s|$)", normalized):
        return "subpart"
    if re.match(rf"^第{_CN_NUM}章(?:\s|$)", normalized):
        return "chapter"
    if re.match(rf"^第{_CN_NUM}节(?:\s|$)", normalized):
        return "section"
    if re.match(rf"^附[件录表]{_CN_NUM}(?:\s|$)", normalized):
        return "appendix"
    if normalized in {"序言", "前言"}:
        return "preface"
    return "other"


def is_appendix_drop_path(path: list[str]) -> bool:
    """判断 chunk 路径是否属于「应丢弃的附件/附录」。

    依据：路径根节点为「附件X / 附录X」结构标题。「附表X」（如税额表、污染当量值表，含实质数据）保留；
    全国人大「关于……的决定 / ……修正案」等独立法律文件路径不以附件开头，也保留。
    """
    if not path:
        return False
    root = normalize_heading(path[0])
    return re.match(rf"^附[件录]{_CN_NUM}(?:\s|$)", root) is not None


def path_prefix(path: list[str], kinds: set[str]) -> list[str]:
    """从当前路径中保留指定类型的上级结构。"""
    kept: list[str] = []
    for item in path:
        if heading_kind(item) in kinds:
            kept.append(item)
    return kept


def heading_level_to_path(path: list[str], level: int, title: str) -> list[str]:
    """根据法律标题语义更新当前结构路径，Markdown 层级仅作为兜底。"""
    if title == "目录":
        return path
    normalized = normalize_heading(title)
    kind = heading_kind(normalized)
    if kind == "preface":
        return [normalized]
    if kind == "appendix":
        return [normalized]
    if kind == "part":
        return [normalized]
    if kind == "subpart":
        return path_prefix(path, {"part"}) + [normalized]
    if kind == "chapter":
        return path_prefix(path, {"part", "subpart"}) + [normalized]
    if kind == "section":
        return path_prefix(path, {"part", "subpart", "chapter"}) + [normalized]
    path_level = max(0, level - 2)
    new_path = path[:path_level]
    new_path.append(normalized)
    return new_path


def inline_structure_heading(line: str, law_name: str) -> Optional[str]:
    """识别正文中未标成 Markdown 标题、但语义上明显是结构标题的行。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 180:
        return None

    candidates = [stripped]
    if law_name and stripped.startswith(law_name):
        candidates.append(stripped[len(law_name) :].strip())
    if stripped.startswith(f"《{law_name}》"):
        candidates.append(stripped[len(law_name) + 2 :].strip())

    for candidate in candidates:
        match = _INLINE_APPENDIX_TITLE_RE.match(candidate)
        if not match:
            continue
        title = normalize_heading(match.group(1))
        if heading_kind(title) == "appendix":
            return title
    return None


def is_toc_heading(level: int, title: str) -> bool:
    """判断标题是否为目录标题。"""
    return level == 2 and re.fullmatch(r"目\s*录", title.strip()) is not None


def article_label(line: str) -> Optional[str]:
    """识别 `**第X条**` 条文标签。"""
    match = _ARTICLE_RE.match(line.strip())
    if not match:
        return None
    return match.group(1)


def amendment_item_label(line: str) -> Optional[str]:
    """识别刑法修正案类文本的 `**一、**` 修改项标签。"""
    match = _AMENDMENT_ITEM_RE.match(line.strip())
    if not match:
        return None
    return match.group(1)


def legislative_title(text: str) -> Optional[str]:
    """识别「全国人大／人大常委会 关于……的决定 / ……修正案」这类立法性文件标题。

    这类标题在源 Markdown 中常被拆成多行（如「全国人民代表大会常务委员会关于」「增加《……基本法》」
    「附件三所列全国性法律的决定」三行），需先合并再识别。命中返回规范化标题，否则返回 None。
    """
    text = text.strip()
    if not text or len(text) > 80:
        return None
    if not _LEGISLATIVE_TITLE_RE.match(text):
        return None
    return normalize_heading(text)


def merge_legislative_titles(lines: list[str]) -> list[str]:
    """预处理：把跨多行的立法性文件标题合并成单个 Markdown H2 标题，并折叠紧邻的颁布日期注。

    源 Markdown 里「全国人大……的决定／……修正案」标题常占连续多行，其后紧跟「（XXXX年……通过）」
    日期注。若不合并，逐行解析会把标题前缀行漏进上一 chunk，且只截取尾段「附件三所列……的决定」当
    路径，导致多个同名决定路径相同、无法区分。这里把整段标题（含日期注）重写为一行 `## 标题`，交由既有
    标题逻辑当作顶层结构标题切分。非标题块原样保留。
    """
    merged: list[str] = []
    total = len(lines)
    index = 0
    while index < total:
        if not lines[index].strip():
            merged.append(lines[index])
            index += 1
            continue
        # 收集一个以空行为界的连续非空块
        block_end = index
        block: list[str] = []
        while block_end < total and lines[block_end].strip():
            block.append(lines[block_end].strip())
            block_end += 1
        title = legislative_title("".join(block))
        if title is None:
            merged.extend(lines[index:block_end])
            index = block_end
            continue
        # 命中立法标题：向后跳过空行，折叠紧邻的颁布日期注以区分同名决定
        note_index = block_end
        while note_index < total and not lines[note_index].strip():
            note_index += 1
        if note_index < total and _PROMULGATION_NOTE_RE.match(lines[note_index].strip()):
            title = f"{title}{lines[note_index].strip()}"
            index = note_index + 1
        else:
            index = block_end
        merged.append(f"## {title}")
        merged.append("")
    return merged


def parse_markdown(path: Path, counter: TokenCounter, max_tokens: int = DEFAULT_MAX_TOKENS) -> LawDoc:
    """把一篇法律 Markdown 解析为 LawDoc，并计算每个原子单元 token 数。"""
    law_name, version, effective_date = parse_filename(path)
    title = law_name
    current_path: list[str] = []
    units: list[LawUnit] = []
    current_unit: Optional[LawUnit] = None
    free_lines: list[str] = []
    preamble_lines: list[str] = []
    table_lines: list[str] = []
    in_toc = False
    order = 0

    def next_order() -> int:
        """生成当前文档内递增的原子单元序号。"""
        nonlocal order
        order += 1
        return order

    def append_unit(unit: LawUnit) -> None:
        """追加原子单元并补充 token 数。"""
        unit.body = unit.body.strip()
        if not unit.body:
            return
        unit.token_count = counter.count(unit.body)
        units.append(unit)

    def flush_current_unit() -> None:
        """结束当前条文或修改项原子单元。"""
        nonlocal current_unit
        if current_unit is not None:
            append_unit(current_unit)
            current_unit = None

    def flush_free_text(unit_type: str = "section_text") -> None:
        """把结构标题下的普通文本缓冲写成原子单元。"""
        nonlocal free_lines
        text = "\n".join(line for line in free_lines if line.strip()).strip()
        free_lines = []
        if text:
            if not current_path and not units:
                preamble_lines.append(text)
                return
            append_unit(
                LawUnit(
                    unit_type=unit_type,
                    body=text,
                    path=current_path.copy(),
                    label=current_path[-1] if current_path else "正文",
                    order=next_order(),
                )
            )

    def estimate_table_tokens(context_lines: list[str], table_body: str) -> int:
        """估算表格带上下文头后的 token 数。"""
        context = "\n".join(line for line in context_lines if line.strip()).strip()
        body = f"{context}\n\n{table_body}".strip() if context else table_body
        unit = LawUnit(
            unit_type="table",
            body=body,
            path=current_path.copy(),
            label=current_path[-1] if current_path else "表格",
            order=0,
        )
        doc = LawDoc(
            source_file=path.name,
            law_name=law_name,
            title=title,
            version=version,
            effective_date=effective_date,
        )
        return counter.count(f"{build_context_header(doc, [unit])}\n{body}")

    def split_table_context(table_body: str) -> tuple[list[str], list[str]]:
        """把表格前说明拆成“单独说明”和“表格语义上下文”两部分。

        短说明整体并入表格；长说明只取紧邻表格的尾部引导语，避免表格 chunk 超预算。
        """
        candidates = [line.strip() for line in free_lines if line.strip()]
        if not candidates:
            return [], []

        if estimate_table_tokens(candidates, table_body) <= max_tokens - TOKEN_SAFETY_MARGIN:
            return [], candidates

        context: list[str] = []
        for line in reversed(candidates):
            candidate = [line] + context
            context_tokens = counter.count("\n".join(candidate))
            if (
                context_tokens <= TABLE_CONTEXT_MAX_TOKENS
                and estimate_table_tokens(candidate, table_body) <= max_tokens - TOKEN_SAFETY_MARGIN
            ):
                context = candidate
                continue
            break

        if not context:
            last_line = candidates[-1]
            sentence_context: list[str] = []
            for sentence in reversed(split_sentences(last_line)):
                candidate = [sentence] + sentence_context
                if estimate_table_tokens(candidate, table_body) <= max_tokens - TOKEN_SAFETY_MARGIN:
                    sentence_context = candidate
                else:
                    break
            context = sentence_context

        prefix_len = max(0, len(candidates) - len(context))
        return candidates[:prefix_len], context

    def flush_table() -> None:
        """把连续 Markdown 表格行写成表格原子单元，并合并必要表名/引导语。"""
        nonlocal table_lines
        if not table_lines:
            return
        table_body = "\n".join(table_lines).strip()
        table_lines = []
        prefix_lines, context_lines = split_table_context(table_body)
        free_lines.clear()
        if prefix_lines:
            append_unit(
                LawUnit(
                    unit_type="section_text",
                body="\n".join(prefix_lines),
                path=current_path.copy(),
                label=current_path[-1] if current_path else "正文",
                    order=next_order(),
                )
            )
        if context_lines:
            table_body = "\n".join(context_lines) + "\n\n" + table_body
        append_unit(
            LawUnit(
                unit_type="table",
                body=table_body,
                path=current_path.copy(),
                label=current_path[-1] if current_path else "表格",
                order=next_order(),
            )
        )

    raw_lines = [raw_line.rstrip() for raw_line in path.read_text(encoding="utf-8").splitlines()]
    for line in merge_legislative_titles(raw_lines):
        stripped = line.strip()

        if stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:].strip() or title
            continue

        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            flush_table()
            flush_current_unit()
            flush_free_text("section_text")
            level = len(heading_match.group(1))
            heading_title = heading_match.group(2).strip()
            if is_toc_heading(level, heading_title):
                in_toc = True
                continue
            in_toc = False
            current_path = heading_level_to_path(current_path, level, heading_title)
            continue

        if in_toc:
            continue

        inline_heading = inline_structure_heading(stripped, law_name)
        if inline_heading and current_unit is None:
            flush_table()
            flush_free_text("section_text")
            current_path = heading_level_to_path(current_path, 2, inline_heading)
            continue

        if _TABLE_LINE_RE.match(stripped):
            flush_current_unit()
            table_lines.append(line)
            continue

        flush_table()

        label = article_label(stripped)
        if label:
            flush_current_unit()
            flush_free_text("section_text")
            current_unit = LawUnit(
                unit_type="article",
                body=line,
                path=current_path.copy(),
                label=label,
                order=next_order(),
            )
            continue

        label = amendment_item_label(stripped)
        if label:
            flush_current_unit()
            flush_free_text("section_text")
            current_unit = LawUnit(
                unit_type="amendment_item",
                body=line,
                path=current_path.copy(),
                label=label,
                order=next_order(),
            )
            continue

        if current_unit is not None:
            current_unit.body += "\n" + line
        else:
            free_lines.append(line)

    flush_table()
    flush_current_unit()
    flush_free_text("section_text")
    return LawDoc(
        source_file=path.name,
        law_name=law_name,
        title=title,
        version=version,
        effective_date=effective_date,
        preamble="\n\n".join(preamble_lines).strip(),
        units=units,
    )


def build_context_header(doc: LawDoc, units: list[LawUnit]) -> str:
    """构造每个 chunk 注入向量文本的上下文头。"""
    parts = [f"法律：{doc.title}"]
    path = units[0].path if units else []
    if path:
        parts.append("路径：" + " / ".join(path))
    labels = [unit.label for unit in units if unit.label]
    if labels:
        if len(labels) == 1:
            parts.append(f"范围：{labels[0]}")
        else:
            parts.append(f"范围：{labels[0]}-{labels[-1]}")
    return "【" + "｜".join(parts) + "】"


def compatible_for_same_chunk(left: LawUnit, right: LawUnit) -> bool:
    """判断两个相邻原子单元是否允许合并到同一 chunk。"""
    if left.path != right.path:
        return False
    if left.unit_type != right.unit_type:
        return False
    return left.unit_type in {"article", "amendment_item"}


def build_record(doc: LawDoc, units: list[LawUnit], counter: TokenCounter, max_tokens: int) -> dict:
    """把一组原子单元渲染为最终 chunk 记录。"""
    header = build_context_header(doc, units)
    body = "\n\n".join(unit.body for unit in units).strip()
    text = f"{header}\n{body}".strip()
    token_count = counter.count(text)
    labels = [unit.label for unit in units if unit.label]
    metadata = {
        "source_file": doc.source_file,
        "law_name": doc.law_name,
        "title": doc.title,
        "version": doc.version or None,
        "effective_date": doc.effective_date or None,
        "preamble": doc.preamble or None,
        "path": units[0].path if units else [],
        "unit_type": units[0].unit_type if units else "",
        "unit_start": labels[0] if labels else None,
        "unit_end": labels[-1] if labels else None,
        "unit_labels": labels,
        "unit_count": len(units),
        "token_count": token_count,
        "body_token_count": counter.count(body),
        "single_unit_overflow": len(units) == 1 and token_count > max_tokens,
        "split_from_large_table": False,
        "split_from_large_text": False,
    }
    return {
        "text": text,
        "context_header": header,
        "body": body,
        "metadata": metadata,
    }


def split_large_table_unit(doc: LawDoc, unit: LawUnit, counter: TokenCounter, max_tokens: int) -> list[dict]:
    """把超长 Markdown 表格按行拆成多个 chunk，并在每块重复表头。"""
    lines = unit.body.splitlines()
    first_table_idx = next((idx for idx, line in enumerate(lines) if _TABLE_LINE_RE.match(line.strip())), -1)
    if first_table_idx < 0:
        return [build_record(doc, [unit], counter, max_tokens)]

    prefix_lines = [line for line in lines[:first_table_idx] if line.strip()]
    table_lines = [line for line in lines[first_table_idx:] if line.strip()]
    if len(table_lines) <= 3:
        return [build_record(doc, [unit], counter, max_tokens)]

    table_header = table_lines[:2]
    table_rows = table_lines[2:]
    records: list[dict] = []
    current_rows: list[str] = []

    def make_unit(rows: list[str]) -> LawUnit:
        """根据当前行集合构造一个拆分后的表格原子单元。"""
        body_lines = prefix_lines + table_header + rows
        return LawUnit(
            unit_type=unit.unit_type,
            body="\n".join(body_lines),
            path=unit.path.copy(),
            label=unit.label,
            order=unit.order,
        )

    def flush_rows() -> None:
        """把当前累积的表格行写成一条 chunk 记录。"""
        nonlocal current_rows
        if not current_rows:
            return
        split_unit = make_unit(current_rows)
        record = build_record(doc, [split_unit], counter, max_tokens)
        record["metadata"]["split_from_large_table"] = True
        records.append(record)
        current_rows = []

    for row in table_rows:
        candidate_rows = current_rows + [row]
        candidate = build_record(doc, [make_unit(candidate_rows)], counter, max_tokens)
        if current_rows and candidate["metadata"]["token_count"] > max_tokens - TOKEN_SAFETY_MARGIN:
            flush_rows()
            current_rows = [row]
        else:
            current_rows = candidate_rows
    flush_rows()
    return records or [build_record(doc, [unit], counter, max_tokens)]


def split_sentences(text: str) -> list[str]:
    """按中文句末标点切分长文本，保留句末标点。"""
    pieces = [piece.strip() for piece in _SENTENCE_SPLIT_RE.split(text) if piece.strip()]
    return pieces or [text]


def hard_split_text(text: str, counter: TokenCounter, budget: int) -> list[str]:
    """兜底按字符累积拆分，保证单段说明文本不会无限超出预算。"""
    pieces: list[str] = []
    buf = ""
    for ch in text:
        candidate = buf + ch
        if buf and counter.count(candidate) > budget:
            pieces.append(buf)
            buf = ch
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces


def split_large_text_unit(doc: LawDoc, unit: LawUnit, counter: TokenCounter, max_tokens: int) -> list[dict]:
    """把超长非条文说明文本按段落和句子拆成多个 chunk。"""
    header = build_context_header(doc, [unit])
    body_budget = max(64, max_tokens - counter.count(header) - TOKEN_SAFETY_MARGIN)
    paragraphs = [para.strip() for para in re.split(r"\n\s*\n", unit.body) if para.strip()]
    records: list[dict] = []
    current_parts: list[str] = []

    def make_unit(parts: list[str]) -> LawUnit:
        """根据文本片段构造拆分后的说明文本原子单元。"""
        return LawUnit(
            unit_type=unit.unit_type,
            body="\n\n".join(parts).strip(),
            path=unit.path.copy(),
            label=unit.label,
            order=unit.order,
        )

    def flush_parts() -> None:
        """把当前说明文本片段写成一条 chunk 记录。"""
        nonlocal current_parts
        if not current_parts:
            return
        record = build_record(doc, [make_unit(current_parts)], counter, max_tokens)
        record["metadata"]["split_from_large_text"] = True
        records.append(record)
        current_parts = []

    for paragraph in paragraphs:
        paragraph_parts = [paragraph]
        if counter.count(paragraph) > body_budget:
            paragraph_parts = []
            for sentence in split_sentences(paragraph):
                if counter.count(sentence) > body_budget:
                    paragraph_parts.extend(hard_split_text(sentence, counter, body_budget))
                else:
                    paragraph_parts.append(sentence)

        for part in paragraph_parts:
            candidate_parts = current_parts + [part]
            candidate = build_record(doc, [make_unit(candidate_parts)], counter, max_tokens)
            if current_parts and candidate["metadata"]["token_count"] > max_tokens - TOKEN_SAFETY_MARGIN:
                flush_parts()
                current_parts = [part]
            else:
                current_parts = candidate_parts

    flush_parts()
    return records or [build_record(doc, [unit], counter, max_tokens)]


def chunk_law_doc(doc: LawDoc, counter: TokenCounter, max_tokens: int, drop_appendix: bool = True) -> list[dict]:
    """按结构路径和 token 预算把一篇法律文档切成 chunk 记录。

    drop_appendix 为真时，先丢弃路径根节点为「附件/附录」的原子单元（附表与独立决定/修正案保留），
    这些附件内容多为程序性条款、表格或范本，对法律问答/合同审查检索价值低。
    """
    records: list[dict] = []
    current_units: list[LawUnit] = []
    units = [
        unit
        for unit in doc.units
        if not (drop_appendix and is_appendix_drop_path(unit.path))
    ]

    def flush() -> None:
        """把当前缓冲单元写成一个 chunk 记录。"""
        nonlocal current_units
        if current_units:
            records.append(build_record(doc, current_units, counter, max_tokens))
            current_units = []

    for unit in units:
        if not current_units:
            single_record = build_record(doc, [unit], counter, max_tokens)
            if unit.unit_type == "table" and single_record["metadata"]["token_count"] > max_tokens - TOKEN_SAFETY_MARGIN:
                records.extend(split_large_table_unit(doc, unit, counter, max_tokens))
            elif unit.unit_type == "section_text" and single_record["metadata"]["token_count"] > max_tokens - TOKEN_SAFETY_MARGIN:
                records.extend(split_large_text_unit(doc, unit, counter, max_tokens))
            else:
                current_units = [unit]
            continue

        if not compatible_for_same_chunk(current_units[-1], unit):
            flush()
            single_record = build_record(doc, [unit], counter, max_tokens)
            if unit.unit_type == "table" and single_record["metadata"]["token_count"] > max_tokens - TOKEN_SAFETY_MARGIN:
                records.extend(split_large_table_unit(doc, unit, counter, max_tokens))
                current_units = []
            elif unit.unit_type == "section_text" and single_record["metadata"]["token_count"] > max_tokens - TOKEN_SAFETY_MARGIN:
                records.extend(split_large_text_unit(doc, unit, counter, max_tokens))
                current_units = []
            else:
                current_units = [unit]
            continue

        candidate = current_units + [unit]
        candidate_record = build_record(doc, candidate, counter, max_tokens)
        if candidate_record["metadata"]["token_count"] <= max_tokens - TOKEN_SAFETY_MARGIN:
            current_units.append(unit)
        else:
            flush()
            current_units = [unit]

    flush()
    for idx, record in enumerate(records):
        record["id"] = f"{doc.source_file.removesuffix('.md')}__{idx:04d}"
        record["metadata"]["chunk_index"] = idx
        record["metadata"]["chunk_total"] = len(records)
    return records


def select_files(input_dir: Path, filenames: list[str], limit: int) -> list[Path]:
    """根据 CLI 参数选择待处理 Markdown 文件。"""
    if filenames:
        files = [input_dir / name for name in filenames]
    else:
        files = sorted(input_dir.glob("*.md"))
    if limit:
        files = files[:limit]
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise FileNotFoundError("以下输入文件不存在：" + "；".join(missing))
    return files


def write_preview(path: Path, records: list[dict], max_records: int = 40) -> None:
    """写入便于人工查看的 Markdown 预览文件。"""
    lines = ["# 法律 chunk 样本预览", ""]
    for record in records[:max_records]:
        meta = record["metadata"]
        lines.extend(
            [
                f"## {record['id']}",
                "",
                f"- law: {meta['title']}",
                f"- path: {' / '.join(meta['path']) if meta['path'] else '(无)'}",
                f"- type: {meta['unit_type']}",
                f"- range: {meta['unit_start']} -> {meta['unit_end']}",
                f"- unit_count: {meta['unit_count']}",
                f"- token_count: {meta['token_count']}",
                f"- overflow: {meta['single_unit_overflow']}",
                "",
                "```text",
                record["text"],
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_outputs(output: Path, records: list[dict], summary: dict, preview_limit: int) -> None:
    """写入 JSONL、summary JSON 和 preview Markdown。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    preview_path = output.with_suffix(".preview.md")
    write_preview(preview_path, records, preview_limit)


def build_summary(files: list[Path], records: list[dict], max_tokens: int) -> dict:
    """汇总本次 chunking 的关键统计。"""
    token_counts = sorted(record["metadata"]["token_count"] for record in records)
    by_file: dict[str, int] = {}
    by_type: dict[str, int] = {}
    overflow = 0
    for record in records:
        meta = record["metadata"]
        by_file[meta["source_file"]] = by_file.get(meta["source_file"], 0) + 1
        by_type[meta["unit_type"]] = by_type.get(meta["unit_type"], 0) + 1
        overflow += int(bool(meta["single_unit_overflow"]))

    def percentile(ratio: float) -> int:
        """按简单位置法计算 token 分位数。"""
        if not token_counts:
            return 0
        idx = min(len(token_counts) - 1, int(len(token_counts) * ratio))
        return token_counts[idx]

    return {
        "input_files": [path.name for path in files],
        "max_tokens": max_tokens,
        "file_count": len(files),
        "chunk_count": len(records),
        "by_file": by_file,
        "by_type": by_type,
        "single_unit_overflow_count": overflow,
        "token_min": token_counts[0] if token_counts else 0,
        "token_median": percentile(0.5),
        "token_p95": percentile(0.95),
        "token_max": token_counts[-1] if token_counts else 0,
        "over_max_tokens_count": sum(1 for token_count in token_counts if token_count > max_tokens),
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="法律 Markdown 结构感知 chunking")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="输入 Markdown 目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 JSONL 路径")
    parser.add_argument("--files", nargs="*", default=[], help="只处理指定 Markdown 文件名")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件，0 表示不限制")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="单 chunk 目标 token 上限")
    parser.add_argument("--model", default="BAAI/bge-m3", help="token 计量 tokenizer")
    parser.add_argument("--preview-limit", type=int, default=40, help="预览文件最多展示 chunk 数")
    parser.add_argument("--no-progress", action="store_true", help="关闭命令行进度条")
    parser.add_argument(
        "--drop-appendix",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="丢弃路径为「附件/附录」的 chunk（附表与决定/修正案保留）；用 --no-drop-appendix 可关闭",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：解析文件、生成 chunk、写入样本或全量结果。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args(argv)
    input_dir = Path(args.input_dir)
    output = Path(args.output)
    files = select_files(input_dir, args.files, args.limit)
    if not files:
        logger.error("没有可处理的 Markdown 文件")
        return 1

    logger.info("加载 tokenizer：%s", args.model)
    counter = TokenCounter(args.model)
    records: list[dict] = []
    dropped_appendix_units = 0
    for path in tqdm(files, desc="切分法律 Markdown", unit="file", disable=args.no_progress):
        doc = parse_markdown(path, counter)
        if args.drop_appendix:
            dropped_appendix_units += sum(1 for unit in doc.units if is_appendix_drop_path(unit.path))
        doc_records = chunk_law_doc(doc, counter, args.max_tokens, drop_appendix=args.drop_appendix)
        records.extend(doc_records)
        logger.debug("已切分：%s -> %s chunks", path.name, len(doc_records))

    summary = build_summary(files, records, args.max_tokens)
    summary["drop_appendix"] = args.drop_appendix
    summary["dropped_appendix_units"] = dropped_appendix_units
    write_outputs(output, records, summary, args.preview_limit)
    logger.info("输出 JSONL：%s", output)
    if args.drop_appendix:
        logger.info("已丢弃附件/附录原子单元：%s", dropped_appendix_units)
    logger.info("chunk=%s token_max=%s overflow=%s", summary["chunk_count"], summary["token_max"], summary["single_unit_overflow_count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
