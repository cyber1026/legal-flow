"""标准合同 Markdown 正文抽取与条款切分。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.contracts.clause_splitter import Clause, split_clauses
from app.contracts.parser.base import ParsedBlock, ParsedDoc

_ARTICLE_BOLD_RE = re.compile(r"^\*\*(第[零○〇一二三四五六七八九十百千]+条)\*\*\s*(.*)$")
_INLINE_ARTICLE_RE = re.compile(r"(第[零○〇一二三四五六七八九十百千]+条)")
_INLINE_CN_ORDER_RE = re.compile(r"(?<!第)([一二三四五六七八九十]{1,3})、")
_HTML_HEADING_RE = re.compile(r"^<h[1-6][^>]*>(.*?)</h[1-6]>$", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_SEPARATOR_RE = re.compile(r"^[\s\|\-:]+$")
_LEGAL_TABLE_FIELDS = (
    "违约责任",
    "争议解决方式",
    "质量标准",
    "质量要求",
    "质量保证期",
    "验收方法及期限",
    "验收标准",
    "结算方式及期限",
    "结算方式",
    "包装要求及费用",
    "包装要求",
    "包装标准",
    "运输方式及到达站",
    "运输方式及费用负担",
    "费用负担",
    "其他约定事项",
    "其他约定",
)
_TAIL_META_KEYWORDS = (
    "鉴（公）证意见",
    "鉴证意见",
    "此合同一式",
    "合同签订地点",
    "合同签订时间",
    "监制部门",
    "印制单位",
    "承包单位（章）",
    "双方商定的其他事项",
    "本合同附件",
    "本合同未尽事宜",
    "本合同共",
    "合同编号",
    "附表",
)


def contract_id_from_path(path: Path) -> str:
    """根据文件路径生成稳定合同 ID。"""
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def extract_contract_body(markdown: str) -> str:
    """只抽取第一个 `---` 之后的合同正文。"""
    lines = markdown.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[idx + 1 :]).strip()
    return markdown.strip()


def _strip_markdown_emphasis(text: str) -> str:
    """去掉常见 Markdown 强调符号。"""
    text = text.replace("**", "")
    text = text.replace("__", "")
    return text.strip()


def _normalize_markdown_line(line: str, *, force_heading: bool = False) -> tuple[str, str] | None:
    """把 Markdown 行规范成 `(block_type, text)`。"""
    raw = line.strip().replace("｜", "|")
    if not raw:
        return None
    if raw.startswith("<!--"):
        return None

    html_heading = _HTML_HEADING_RE.match(raw)
    if html_heading:
        text = _TAG_RE.sub("", html_heading.group(1)).strip()
        return ("heading", text) if text else None

    if raw.startswith("#"):
        text = raw.lstrip("#").strip()
        return ("heading", _strip_markdown_emphasis(text)) if text else None

    article = _ARTICLE_BOLD_RE.match(raw)
    if article:
        text = f"{article.group(1)} {article.group(2).strip()}".strip()
        return ("paragraph", _strip_markdown_emphasis(text))

    if set(raw.replace(" ", "")) <= {"|", "-", ":"}:
        return None

    text = _TAG_RE.sub("", raw).strip()
    text = _strip_markdown_emphasis(text)
    if force_heading and text:
        return ("heading", text)
    return ("paragraph", text) if text else None


def _split_visual_parts(line: str) -> list[str]:
    """拆开 OCR/表格转 Markdown 后常见的全角竖线并列片段。"""
    parts = [part.strip() for part in line.split("｜") if part.strip()]
    return parts if len(parts) > 1 else [line]


def _compact_field(text: str) -> str:
    """压缩字段名空白，便于识别表格里的“质 量 标 准”等写法。"""
    return re.sub(r"\s+", "", text or "")


def _is_legal_table_field(cell: str) -> bool:
    """判断表格单元格是否是合同风险评测相关字段。"""
    compact = _compact_field(cell).strip("：:")
    for field in _LEGAL_TABLE_FIELDS:
        if compact == field:
            return True
        if compact.startswith(f"{field}：") or compact.startswith(f"{field}:"):
            return True
        if field in compact and len(compact) <= len(field) + 8:
            return True
    return False


def _clean_table_cell(cell: str) -> str:
    """清理表格单元格文本。"""
    return re.sub(r"\s+", " ", cell).strip(" ：:")


def _extract_legal_table_fragments(line: str) -> list[str]:
    """从 Markdown 表格行里抽取带实质内容的法律字段片段。"""
    raw = line.strip()
    if "|" not in raw or _TABLE_SEPARATOR_RE.match(raw):
        return []
    cells = [_clean_table_cell(cell) for cell in raw.strip("|").split("|")]
    cells = [cell for cell in cells if cell and not _TABLE_SEPARATOR_RE.match(cell)]
    if len(cells) < 2:
        return []

    fragments: list[str] = []
    for idx, cell in enumerate(cells):
        if not _is_legal_table_field(cell):
            continue
        next_cell = cells[idx + 1] if idx + 1 < len(cells) else ""
        compact_cell = _compact_field(cell)
        compact_next = _compact_field(next_cell)
        if ("：" in cell or ":" in cell) and len(compact_cell) > 8:
            fragments.append(cell)
        elif len(compact_next) >= 12 and not _is_legal_table_field(next_cell):
            fragments.append(f"{cell}：{next_cell}")
    return fragments


def _is_tail_meta_line(line: str) -> bool:
    """判断是否是合同尾部签署、鉴证、附件份数等非评测条款行。"""
    compact = _compact_field(_TAG_RE.sub("", line))
    return any(keyword in compact for keyword in _TAIL_META_KEYWORDS)


def _expand_markdown_line(line: str) -> list[tuple[str, bool]]:
    """把原始 Markdown 行展开为 `(文本, 是否强制作为分界标题)`。"""
    table_fragments = _extract_legal_table_fragments(line)
    if table_fragments:
        return [(fragment, True) for fragment in table_fragments]
    if _is_tail_meta_line(line):
        return []
    return [(part, False) for part in _split_visual_parts(line)]


def _starts_explicit_clause(line: str) -> bool:
    """判断原始行是否以显式条款编号开头。"""
    text = _TAG_RE.sub("", line).strip()
    return bool(
        _INLINE_ARTICLE_RE.match(text)
        or re.match(r"^\d+\s*[\.\、．\)]", text)
        or re.match(r"^[一二三四五六七八九十]{1,3}、", text)
    )


def _split_inline_articles(text: str) -> list[str]:
    """把同一行里串联的多个显式编号条款拆成独立逻辑行。"""
    matches = list(_INLINE_ARTICLE_RE.finditer(text))
    is_cn_order = False
    if len(matches) <= 1:
        matches = list(_INLINE_CN_ORDER_RE.finditer(text))
        is_cn_order = bool(matches)
    if len(matches) <= 1:
        return [text]
    pieces: list[str] = []
    prefix = text[: matches[0].start()].strip()
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        piece = text[start:end].strip(" ；;，,")
        if piece:
            pieces.append(piece)
    if prefix and pieces and not is_cn_order:
        pieces[0] = f"{prefix} {pieces[0]}".strip()
    return pieces or [text]


def parse_standard_contract_markdown(path: Path) -> ParsedDoc:
    """把标准合同 Markdown 转为合同解析中间表示。"""
    markdown = path.read_text(encoding="utf-8")
    body = extract_contract_body(markdown)
    blocks: list[ParsedBlock] = []
    suppress_after_table_field = False
    for line in body.splitlines():
        expanded_parts = _expand_markdown_line(line)
        if not expanded_parts:
            continue
        has_forced_table_field = any(force_heading for _, force_heading in expanded_parts)
        if suppress_after_table_field and not has_forced_table_field and not _starts_explicit_clause(line):
            continue
        for visual_part, force_heading in expanded_parts:
            normalized = _normalize_markdown_line(visual_part, force_heading=force_heading)
            if normalized is None:
                continue
            block_type, text = normalized
            if not blocks and block_type == "heading" and "合同" in text and not text.startswith("第"):
                continue
            logical_lines = _split_inline_articles(text) if block_type == "paragraph" else [text]
            for logical_text in logical_lines:
                blocks.append(ParsedBlock(text=logical_text, block_type=block_type))  # type: ignore[arg-type]
        suppress_after_table_field = has_forced_table_field
    title = path.stem
    for block in blocks[:5]:
        if block.block_type == "heading" and block.text:
            title = block.text
            break
    return ParsedDoc(title=title, blocks=blocks, source_path=str(path), mime="text/markdown", doc_type="docx")


def split_standard_contract(path: Path, *, max_clause_chars: int = 1600) -> list[Clause]:
    """解析并切分一份标准合同 Markdown。"""
    parsed = parse_standard_contract_markdown(path)
    return split_clauses(
        parsed,
        max_clause_chars=max_clause_chars,
        split_outline_under_cn_article=True,
    )
