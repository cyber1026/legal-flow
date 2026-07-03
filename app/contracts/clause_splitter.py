"""合同条款切分。

输入：ParsedDoc（按文档顺序的 block 列表）
输出：List[Clause]，每个 Clause 是一条「条款」，带 section_path / page_no / 合并 bbox

切分策略（按优先级降序）：
1. 显式主条款编号：`第X条`
2. 没有出现 `第X条` 主条款风格时，允许 `X.Y`、`(一)/（一）/1./1)` 等数字提纲作为主条款
3. 没有编号时按 heading 块作为章节分界
4. 兜底：用 langchain RecursiveCharacterTextSplitter 限长切

中文条款编号枚举较多，正则保持简单：覆盖主流形态，剩下交给兜底分割。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.contracts.parser.base import ParsedBlock, ParsedDoc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Clause:
    """一条切分后的合同条款。"""

    clause_id: str             # 系统生成的稳定 ID，如 "c1-5" / "p3" / "f12"
    clause_no: str             # 原文条款号，如 "第五条" / "5.2"；无则空串
    title: str                 # 条款短标题（同行/紧跟正文）；无则空串
    section_path: str          # 层级路径，如 "合同正文 / 第三章 / 第5条"
    text: str                  # 条款全文（拼接多行/段落）
    page_no: Optional[int] = None
    bbox: Optional[list[float]] = None  # 合并后的 union bbox
    chunk_index: int = 0
    # 内部使用：原始 block 列表的范围
    _block_idx: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 条款/章节正则
# ---------------------------------------------------------------------------

_CN_NUM = r"[零○〇一二三四五六七八九十百千]+"
_ARTICLE_NUM = rf"(?:{_CN_NUM}|\d+)"

# 「第X条」（最常见的合同条款编号形态）
_RE_ARTICLE_CN = re.compile(rf"^(第{_ARTICLE_NUM}条)\s*(.*)")

# 「第X章/编/节」
_RE_CHAPTER = re.compile(rf"^(第{_ARTICLE_NUM}章)\s*(.*)")
_RE_PART    = re.compile(rf"^(第{_ARTICLE_NUM}编)\s*(.*)")
_RE_SECTION = re.compile(rf"^(第{_ARTICLE_NUM}节)\s*(.*)")

# 「1.」「1、」「1)」「(一)」「（一）」
_RE_NUM_DOT  = re.compile(r"^(\d+)\s*[\.\、．]\s*(.*)")
_RE_NUM_PAR  = re.compile(r"^(\d+)\s*[\)\)]\s*(.*)")
_RE_NUM_DEEP = re.compile(r"^(\d+(?:\.\d+){1,3})\s*[\、\.\s]?\s*(.*)")  # 1.2 / 1.2.3
_RE_CN_PAR   = re.compile(rf"^[\(\（]\s*({_CN_NUM})\s*[\)\）]\s*(.*)")
_RE_CN_DOT   = re.compile(rf"^({_CN_NUM})\s*[、．\.]\s*(.*)")

_ARTICLE_KIND_CN = "cn_article"
_ARTICLE_KIND_OUTLINE = "outline"


def _match_article_with_kind(text: str) -> tuple[str, str, str] | None:
    """匹配条款编号，并区分主条款编号和普通提纲编号。"""
    text = text.lstrip()
    m = _RE_ARTICLE_CN.match(text)
    if m:
        return _ARTICLE_KIND_CN, m.group(1), (m.group(2) or "").strip()

    for pat in (_RE_NUM_DEEP, _RE_NUM_DOT, _RE_NUM_PAR, _RE_CN_PAR, _RE_CN_DOT):
        m = pat.match(text)
        if m:
            return _ARTICLE_KIND_OUTLINE, m.group(1), (m.group(2) or "").strip()
    return None


def _match_article(text: str) -> tuple[str, str] | None:
    """匹配条款编号，命中则返回 (clause_no, 行内剩余正文)。"""
    matched = _match_article_with_kind(text)
    if not matched:
        return None
    _, clause_no, rest = matched
    return clause_no, rest


def _match_section(text: str) -> tuple[str, str, str] | None:
    """匹配章节标题。返回 (level, 编号, 标题)。level ∈ {part, chapter, section}。"""
    text = text.lstrip()
    for level, pat in (("part", _RE_PART), ("chapter", _RE_CHAPTER), ("section", _RE_SECTION)):
        m = pat.match(text)
        if m:
            return level, m.group(1), (m.group(2) or "").strip()
    return None


# ---------------------------------------------------------------------------
# bbox 合并 & 工具
# ---------------------------------------------------------------------------

def _merge_bbox(boxes: list[list[float] | None]) -> Optional[list[float]]:
    """对一组 bbox 求轴对齐 union，全 None 时返回 None。"""
    valid = [b for b in boxes if b and len(b) == 4]
    if not valid:
        return None
    x1 = min(b[0] for b in valid)
    y1 = min(b[1] for b in valid)
    x2 = max(b[2] for b in valid)
    y2 = max(b[3] for b in valid)
    return [x1, y1, x2, y2]


def _first_page(pages: list[Optional[int]]) -> Optional[int]:
    """取第一个非 None 的页码。"""
    for p in pages:
        if p is not None:
            return p
    return None


# ---------------------------------------------------------------------------
# 核心切分流程
# ---------------------------------------------------------------------------

class ClauseSplitter:
    """状态机式切分：扫一遍 blocks，遇编号/章节即 flush。"""

    def __init__(
        self,
        *,
        max_clause_chars: int = 1200,
        split_outline_under_cn_article: bool = False,
    ) -> None:
        """初始化拆分器。

        `split_outline_under_cn_article=False` 是线上合同审查默认值：`第X条`
        内部的 1./（一）等子项留在父条款正文里。评测标准合同抽样器可显式改为
        True，把表格式字段编号拆成独立候选样本。
        """
        # 单条款超过该长度时触发兜底再切（避免一条几千字喂给 LLM 失控）
        self.max_clause_chars = max_clause_chars
        self.split_outline_under_cn_article = split_outline_under_cn_article

    def split(self, parsed: ParsedDoc) -> list[Clause]:
        if not parsed.blocks:
            return []

        # 当前章节层级（最多三层）
        path_part: str = ""
        path_chapter: str = ""
        path_section: str = ""

        # 当前条款累积状态
        cur_no: str = ""
        cur_title: str = ""
        cur_lines: list[str] = []
        cur_pages: list[Optional[int]] = []
        cur_bboxes: list[Optional[list[float]]] = []
        cur_block_idx: list[int] = []
        primary_article_kind = ""

        clauses: list[Clause] = []

        def section_path() -> str:
            parts = [s for s in (path_part, path_chapter, path_section) if s]
            return " / ".join(parts)

        def flush(force_id_prefix: str = "c") -> None:
            nonlocal cur_no, cur_title, cur_lines, cur_pages, cur_bboxes, cur_block_idx
            text = "\n".join(s for s in cur_lines if s).strip()
            if not text:
                cur_no = ""
                cur_title = ""
                cur_lines = []
                cur_pages = []
                cur_bboxes = []
                cur_block_idx = []
                return

            idx = len(clauses)
            clauses.append(
                Clause(
                    clause_id=f"{force_id_prefix}{idx + 1}",
                    clause_no=cur_no,
                    title=cur_title,
                    section_path=section_path(),
                    text=text,
                    page_no=_first_page(cur_pages),
                    bbox=_merge_bbox(cur_bboxes),
                    chunk_index=idx,
                    _block_idx=list(cur_block_idx),
                )
            )
            cur_no = ""
            cur_title = ""
            cur_lines = []
            cur_pages = []
            cur_bboxes = []
            cur_block_idx = []

        for i, blk in enumerate(parsed.blocks):
            text = (blk.text or "").strip()
            if not text:
                continue

            # 1. 章节
            sec = _match_section(text)
            if sec:
                flush()
                level, no, title = sec
                heading = f"{no} {title}".strip()
                if level == "part":
                    path_part = heading
                    path_chapter = ""
                    path_section = ""
                elif level == "chapter":
                    path_chapter = heading
                    path_section = ""
                else:
                    path_section = heading
                continue

            # 2. 条款编号行
            art = _match_article_with_kind(text)
            if art:
                art_kind, matched_no, rest = art
                # 合同一旦使用「第X条」作为主条款，后续 1./（一）等编号通常是该条内部子项，
                # 不能再提升为同级条款；否则前端会把子项展示成合同主结构。
                if (
                    not self.split_outline_under_cn_article
                    and primary_article_kind == _ARTICLE_KIND_CN
                    and art_kind == _ARTICLE_KIND_OUTLINE
                    and cur_lines
                ):
                    cur_lines.append(text)
                    cur_pages.append(blk.page_no)
                    cur_bboxes.append(blk.bbox)
                    cur_block_idx.append(i)
                    continue

                flush()
                cur_no = matched_no
                cur_title = ""
                if rest:
                    # 编号同行的剩余文本视为正文首句（条款标题用第一句的前 30 字）
                    cur_title = rest[:30]
                    cur_lines.append(rest)
                cur_pages.append(blk.page_no)
                cur_bboxes.append(blk.bbox)
                cur_block_idx.append(i)
                if art_kind == _ARTICLE_KIND_CN:
                    primary_article_kind = _ARTICLE_KIND_CN
                elif not primary_article_kind:
                    primary_article_kind = _ARTICLE_KIND_OUTLINE
                continue

            # 3. heading 块（无显式编号）：作为隐式段落分界
            if blk.block_type == "heading":
                flush()
                # heading 本身作为新条款的标题
                cur_no = ""
                cur_title = text[:30]
                cur_lines.append(text)
                cur_pages.append(blk.page_no)
                cur_bboxes.append(blk.bbox)
                cur_block_idx.append(i)
                continue

            # 4. 正文行：追加到当前条款
            cur_lines.append(text)
            cur_pages.append(blk.page_no)
            cur_bboxes.append(blk.bbox)
            cur_block_idx.append(i)

        flush()

        # 兜底：若一条 clause 都没切出来，就把整篇按长度切
        if not clauses:
            clauses = self._fallback_split(parsed)

        # 兜底：超长条款再切
        clauses = self._split_oversize(clauses)

        # 重新编号 chunk_index
        for idx, c in enumerate(clauses):
            c.chunk_index = idx
            if not c.clause_id:
                c.clause_id = f"c{idx + 1}"

        return clauses

    # ------------------------------------------------------------------
    # 兜底逻辑
    # ------------------------------------------------------------------

    def _fallback_split(self, parsed: ParsedDoc) -> list[Clause]:
        """没有可识别条款时，把全文按段落 + 长度兜底切。"""
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter
        except Exception:
            logger.warning("langchain_text_splitters 未安装，兜底直接整篇为一条")
            full = parsed.full_text()
            if not full:
                return []
            return [
                Clause(
                    clause_id="c1",
                    clause_no="",
                    title="",
                    section_path="",
                    text=full,
                    page_no=_first_page([b.page_no for b in parsed.blocks]),
                    bbox=_merge_bbox([b.bbox for b in parsed.blocks]),
                )
            ]

        full = parsed.full_text()
        if not full:
            return []
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.max_clause_chars,
            chunk_overlap=80,
            separators=["\n\n", "\n", "。", "；", ";", ".", " ", ""],
        )
        pieces = splitter.split_text(full)
        return [
            Clause(
                clause_id=f"f{idx + 1}",
                clause_no="",
                title="",
                section_path="",
                text=piece,
                page_no=_first_page([b.page_no for b in parsed.blocks]),
                bbox=None,
            )
            for idx, piece in enumerate(pieces)
        ]

    def _split_oversize(self, clauses: list[Clause]) -> list[Clause]:
        """对超长条款再用长度切，bbox/page_no 沿用父条款（粗略但够用）。"""
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter
        except Exception:
            return clauses

        result: list[Clause] = []
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.max_clause_chars,
            chunk_overlap=80,
            separators=["\n\n", "\n", "。", "；", ";", ".", " ", ""],
        )
        for c in clauses:
            if len(c.text) <= self.max_clause_chars:
                result.append(c)
                continue
            pieces = splitter.split_text(c.text)
            for j, piece in enumerate(pieces):
                result.append(
                    Clause(
                        clause_id=f"{c.clause_id}-{j + 1}",
                        clause_no=c.clause_no,
                        title=c.title,
                        section_path=c.section_path,
                        text=piece,
                        page_no=c.page_no,
                        bbox=c.bbox,
                    )
                )
        return result


def split_clauses(
    parsed: ParsedDoc,
    *,
    max_clause_chars: int = 1200,
    split_outline_under_cn_article: bool = False,
) -> list[Clause]:
    """方便外部直接调用的薄包装。"""
    return ClauseSplitter(
        max_clause_chars=max_clause_chars,
        split_outline_under_cn_article=split_outline_under_cn_article,
    ).split(parsed)


__all__ = ["Clause", "ClauseSplitter", "split_clauses"]
