#!/usr/bin/env python3
"""将 layer1 法律 DOCX 原文批量转换为结构化 Markdown。

本脚本面向 `data/legal_sources/layer1_law/raw` 这批全国人大法律原文：
- 不依赖 Word 样式名，因为这些 DOCX 的正文段落样式基本都是 Normal。
- 按 OOXML body 顺序遍历段落与表格，保留目录、编、分编、章、节、条文与附表结构。
- 表格按底层 XML 的 gridSpan/vMerge 还原合并单元格，避免 python-docx 裸读造成重复文本。
- 输出只把 Markdown 文件放入 markdown 目录，转换摘要写入 manifest 目录。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from docx import Document
from docx.oxml.ns import qn


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "legal_sources" / "layer1_law" / "raw"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "legal_sources" / "layer1_law" / "markdown"
DEFAULT_MANIFEST_DIR = REPO_ROOT / "data" / "legal_sources" / "layer1_law" / "manifest"

logger = logging.getLogger(__name__)

# 中文法律层级和编号常见字符集合。
_CN_NUM = r"[零〇○一二三四五六七八九十百千万两0-9]+"

# 文本清洗与乱码检查正则。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"[ \t　\xa0\u2000-\u200a\u202f\u205f\u3000]+")
_GARBAGE_RE = re.compile(r"[\ufffd\ue000-\uf8ff]")

# 法律结构识别正则。注意这些文件的 Word 样式不可用，所以全部从文本判断。
_PART_RE = re.compile(rf"^(第{_CN_NUM}编)(?:\s*(.*))?$")
_SUBPART_RE = re.compile(rf"^(第{_CN_NUM}分编)(?:\s*(.*))?$")
_CHAPTER_RE = re.compile(rf"^(第{_CN_NUM}章)(?:\s*(.*))?$")
_SECTION_RE = re.compile(rf"^(第{_CN_NUM}节)(?:\s*(.*))?$")
_ARTICLE_RE = re.compile(rf"^(第{_CN_NUM}条)(?:\s*(.*))?$")
_APPENDIX_RE = re.compile(rf"^(附[件录表]\s*{_CN_NUM}?)(?:[：:\s]*(.*))?$")
_PREFACE_RE = re.compile(r"^(序\s*言|前\s*言)$")
_CN_ITEM_RE = re.compile(rf"^({_CN_NUM})、(.+)$")
_BARE_PAGE_RE = re.compile(r"^\d{1,4}$")

# 目录行里偶尔会把多个章/节拍到同一段，按下一个结构头拆分。
_TOC_SPLIT_RE = re.compile(
    rf"(?=(?:第{_CN_NUM}(?:编|分编|章|节)|附[件录表]\s*{_CN_NUM}?))"
)

# 具有法律标题性质的尾缀，用于判断首段是否应作为一级标题。
_TITLE_SUFFIXES = (
    "法",
    "法典",
    "条例",
    "决定",
    "解释",
    "规则",
    "办法",
    "规定",
)


def _normalize_text(text: str) -> str:
    """清洗单行文本：去控制字符、统一空白、裁剪超长填空线。"""
    if not text:
        return ""
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"(?<=[\d.])[\u2000-\u200a\u202f\u205f](?=\d)", "", text)
    text = text.replace("\u200b", "")
    text = text.replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text)
    text = re.sub(r"_{8,}", "______", text)
    text = re.sub(r"＿{4,}", "______", text)
    return text.strip()


def _paragraph_text(p_el: Any) -> str:
    """从一个 OOXML 段落节点提取纯文本，保留 tab 和换行的语义占位。"""
    parts: list[str] = []
    for node in p_el.iter():
        if node.tag == qn("w:t"):
            parts.append(node.text or "")
        elif node.tag == qn("w:tab"):
            parts.append("\t")
        elif node.tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
    return "".join(parts)


def _cell_text(tc_el: Any) -> str:
    """提取表格单元格文本，并把单元格内部多段落压缩为 Markdown 可读的一行。"""
    lines: list[str] = []
    for p_el in tc_el.findall(qn("w:p")):
        text = _normalize_text(_paragraph_text(p_el))
        if text:
            lines.append(text)
    return " ".join(lines).strip()


def _grid_span(tc_el: Any) -> int:
    """读取单元格横向合并列数；缺失或异常时按 1 列处理。"""
    tc_pr = tc_el.find(qn("w:tcPr"))
    if tc_pr is None:
        return 1
    grid_span = tc_pr.find(qn("w:gridSpan"))
    if grid_span is None:
        return 1
    try:
        return max(1, int(grid_span.get(qn("w:val"))))
    except (TypeError, ValueError):
        return 1


def _v_merge(tc_el: Any) -> tuple[bool, Optional[str]]:
    """读取单元格纵向合并信息，返回是否合并以及 restart/continue 状态。"""
    tc_pr = tc_el.find(qn("w:tcPr"))
    if tc_pr is None:
        return False, None
    v_merge = tc_pr.find(qn("w:vMerge"))
    if v_merge is None:
        return False, None
    return True, v_merge.get(qn("w:val")) or "continue"


def _table_to_matrix(tbl_el: Any) -> list[list[str]]:
    """把 OOXML 表格转换为逻辑矩阵，合并单元格只在锚点保留文本。"""
    grid = tbl_el.find(qn("w:tblGrid"))
    grid_cols = len(grid.findall(qn("w:gridCol"))) if grid is not None else 0
    rows: list[list[str]] = []

    for tr_el in tbl_el.findall(qn("w:tr")):
        cells = tr_el.findall(qn("w:tc"))
        width = max(grid_cols, sum(_grid_span(tc_el) for tc_el in cells))
        row = [""] * width
        col = 0
        for tc_el in cells:
            span = _grid_span(tc_el)
            has_v_merge, v_merge_state = _v_merge(tc_el)
            if col >= width:
                break
            if has_v_merge and v_merge_state != "restart":
                col += span
                continue
            row[col] = _cell_text(tc_el)
            col += span
        rows.append(row)

    max_width = max((len(row) for row in rows), default=0)
    for row in rows:
        if len(row) < max_width:
            row.extend([""] * (max_width - len(row)))
    return rows


def _trim_matrix(matrix: list[list[str]]) -> list[list[str]]:
    """删除全空行列，减少 Word 附表中的空白占位对 Markdown 表格的污染。"""
    non_empty_rows = [row for row in matrix if any(cell.strip() for cell in row)]
    if not non_empty_rows:
        return []
    keep_cols = [
        idx
        for idx in range(len(non_empty_rows[0]))
        if any(row[idx].strip() for row in non_empty_rows)
    ]
    return [[row[idx] for idx in keep_cols] for row in non_empty_rows]


def _escape_table_cell(text: str) -> str:
    """转义 Markdown 表格单元格里的竖线，避免破坏表格列结构。"""
    return text.replace("|", "\\|")


def _render_table(matrix: list[list[str]]) -> str:
    """把逻辑矩阵渲染为 Markdown 表格或压缩文本块。"""
    matrix = _trim_matrix(matrix)
    if not matrix:
        return ""
    if len(matrix[0]) == 1:
        return "\n\n".join(row[0].strip() for row in matrix if row[0].strip())

    width = len(matrix[0])
    filled = sum(1 for row in matrix for cell in row if cell.strip())
    total = max(1, width * len(matrix))
    if width > 8 and filled / total < 0.45:
        lines = []
        for row in matrix:
            cells = [cell.strip() for cell in row if cell.strip()]
            if cells:
                lines.append(" ｜ ".join(cells))
        return "\n\n".join(lines)

    def fmt(row: list[str]) -> str:
        """把一行表格单元格格式化为 GitHub Markdown 表格行。"""
        return "| " + " | ".join(_escape_table_cell(cell.strip()) for cell in row) + " |"

    header = matrix[0]
    body = matrix[1:]
    lines = [fmt(header), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(fmt(row) for row in body)
    return "\n".join(lines)


def _iter_doc_blocks(doc: Document) -> Iterable[tuple[str, Any]]:
    """按 DOCX 正文顺序产出段落和表格块，保持原文阅读顺序。"""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield "para", _normalize_text(_paragraph_text(child))
        elif child.tag == qn("w:tbl"):
            yield "table", _table_to_matrix(child)


def _parse_filename(path: Path) -> tuple[str, str, str]:
    """从文件名解析法律名称、版本日期字符串和 ISO 日期。"""
    stem = path.stem
    name, sep, version = stem.rpartition("_")
    if sep and re.fullmatch(r"\d{8}", version):
        return name, version, f"{version[:4]}-{version[4:6]}-{version[6:]}"
    return stem, "", ""


def _compact_heading_rest(rest: str) -> str:
    """压缩标题正文里的装饰性空格，例如把“总 则”恢复为“总则”。"""
    rest = rest.strip()
    if not rest:
        return ""
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", rest)


def _format_structural_heading(prefix: str, rest: str, level: int) -> str:
    """把编、章、节、附件等结构标题渲染为指定层级的 Markdown 标题。"""
    title = prefix.strip()
    rest = _compact_heading_rest(rest)
    if rest:
        title = f"{title} {rest}"
    return f"{'#' * level} {title}"


def _is_toc_title(text: str) -> bool:
    """判断一个段落是否是“目录”标题，兼容全角空格拆开的写法。"""
    return re.fullmatch(r"目\s*录", text.replace("　", " ")) is not None


def _classify_structural_heading(text: str) -> Optional[tuple[str, str, str]]:
    """识别编、分编、章、节、附件标题，返回类型、前缀和标题正文。"""
    for kind, regex in (
        ("preface", _PREFACE_RE),
        ("part", _PART_RE),
        ("subpart", _SUBPART_RE),
        ("chapter", _CHAPTER_RE),
        ("section", _SECTION_RE),
        ("appendix", _APPENDIX_RE),
    ):
        match = regex.match(text)
        if match:
            if kind == "preface":
                return kind, _toc_key(match.group(1)), ""
            return kind, match.group(1), (match.group(2) or "").strip()
    return None


def _looks_like_toc_entry(text: str) -> bool:
    """判断目录中的一行是否是可收集的目录项。"""
    return _classify_structural_heading(text) is not None


def _toc_key(text: str) -> str:
    """生成目录项去重键，忽略空白差异。"""
    return re.sub(r"\s+", "", text)


def _split_toc_entries(text: str) -> list[str]:
    """把 DOCX 拍平到同一段里的多个目录项拆成独立条目。"""
    parts = [part.strip() for part in _TOC_SPLIT_RE.split(text) if part.strip()]
    return parts or [text]


def _heading_levels(texts: list[str]) -> dict[str, int]:
    """根据当前文件是否存在编/分编，决定章、节标题的 Markdown 层级。"""
    has_part = any(_PART_RE.match(text) for text in texts)
    has_subpart = any(_SUBPART_RE.match(text) for text in texts)

    chapter_level = 2
    if has_part and has_subpart:
        chapter_level = 4
    elif has_part:
        chapter_level = 3

    return {
        "part": 2,
        "subpart": 3,
        "chapter": chapter_level,
        "section": min(6, chapter_level + 1),
        "appendix": 2,
        "preface": 2,
    }


def _is_title_candidate(text: str, filename_title: str) -> bool:
    """判断首个非空段落是否应作为 Markdown 一级标题。"""
    compact_text = _toc_key(text)
    compact_filename = _toc_key(filename_title)
    if compact_text == compact_filename or compact_text in compact_filename or compact_filename in compact_text:
        return True
    return text.startswith("中华人民共和国") and text.endswith(_TITLE_SUFFIXES)


def _detect_title(blocks: list[tuple[str, Any]], filename_title: str) -> tuple[str, Optional[int]]:
    """从正文首个非空段落识别标题，并返回需要从正文中跳过的块下标。"""
    for idx, (kind, payload) in enumerate(blocks):
        if kind != "para":
            continue
        text = str(payload).strip()
        if not text:
            continue
        if _is_title_candidate(text, filename_title):
            return _compact_heading_rest(text), idx
        return filename_title, None
    return filename_title, None


def _render_article(text: str) -> Optional[str]:
    """渲染“第X条”条文，条号加粗但不升级为大标题。"""
    match = _ARTICLE_RE.match(text)
    if not match:
        return None
    article_no = match.group(1)
    rest = (match.group(2) or "").strip()
    if rest:
        return f"**{article_no}**　{rest}"
    return f"**{article_no}**"


def _render_numbered_item(text: str) -> Optional[str]:
    """渲染刑法修正案等文件里的“一、二、三”修改项。"""
    match = _CN_ITEM_RE.match(text)
    if not match:
        return None
    return f"**{match.group(1)}、**{match.group(2).strip()}"


def _render_paragraph(text: str, levels: dict[str, int], has_articles: bool) -> Optional[str]:
    """把普通段落按法律结构渲染为 Markdown 块。"""
    if not text or _BARE_PAGE_RE.fullmatch(text):
        return None

    classified = _classify_structural_heading(text)
    if classified:
        kind, prefix, rest = classified
        return _format_structural_heading(prefix, rest, levels[kind])

    article = _render_article(text)
    if article:
        return article

    if not has_articles:
        numbered_item = _render_numbered_item(text)
        if numbered_item:
            return numbered_item

    return text


def _append_block(blocks: list[str], block: Optional[str]) -> None:
    """追加 Markdown 块，统一过滤空白块。"""
    if block and block.strip():
        blocks.append(block.strip())


def _format_toc_entry(text: str) -> str:
    """格式化目录项，保留结构前缀和标题正文之间的可读空格。"""
    classified = _classify_structural_heading(text)
    if classified:
        _, prefix, rest = classified
        rest = _compact_heading_rest(rest)
        return f"{prefix} {rest}".strip()
    return _compact_heading_rest(text)


def _flush_toc(markdown_blocks: list[str], toc_items: list[str]) -> None:
    """把已收集的目录项写入 Markdown 块列表。"""
    if toc_items:
        markdown_blocks.append("\n".join(f"- {_format_toc_entry(item)}" for item in toc_items))


def convert_docx_to_markdown(path: Path) -> tuple[str, dict[str, Any]]:
    """转换单个 DOCX 文件，返回 Markdown 文本与转换元信息。"""
    filename_title, version, effective_date = _parse_filename(path)
    doc = Document(path)
    blocks = list(_iter_doc_blocks(doc))
    para_texts = [payload for kind, payload in blocks if kind == "para" and payload]
    title, skip_title_idx = _detect_title(blocks, filename_title)
    levels = _heading_levels(para_texts)
    has_articles = any(_ARTICLE_RE.match(text) for text in para_texts)

    markdown_blocks: list[str] = [f"# {title}"]
    toc_items: list[str] = []
    in_toc = False
    pending_article_no: Optional[str] = None
    table_count = 0
    has_toc = False

    for idx, (kind, payload) in enumerate(blocks):
        if idx == skip_title_idx:
            continue

        if kind == "table":
            _flush_toc(markdown_blocks, toc_items)
            toc_items = []
            in_toc = False
            pending_article_no = _flush_pending_article(markdown_blocks, pending_article_no)
            rendered_table = _render_table(payload)
            if rendered_table:
                table_count += 1
                markdown_blocks.append(rendered_table)
            continue

        text = str(payload).strip()
        if not text:
            continue

        if _is_toc_title(text):
            _flush_toc(markdown_blocks, toc_items)
            toc_items = []
            markdown_blocks.append("## 目录")
            in_toc = True
            has_toc = True
            continue

        if in_toc:
            toc_result = _handle_toc_line(text, toc_items)
            if toc_result == "collected":
                continue
            if toc_result == "end":
                _flush_toc(markdown_blocks, toc_items)
                toc_items = []
                in_toc = False

        article_match = _ARTICLE_RE.match(text)
        if pending_article_no is not None:
            merged = f"{pending_article_no} {text}".strip()
            pending_article_no = None
            _append_block(markdown_blocks, _render_paragraph(merged, levels, has_articles))
            continue
        if article_match and not (article_match.group(2) or "").strip():
            pending_article_no = article_match.group(1)
            continue

        _append_block(markdown_blocks, _render_paragraph(text, levels, has_articles))

    _flush_toc(markdown_blocks, toc_items)
    pending_article_no = _flush_pending_article(markdown_blocks, pending_article_no)
    if pending_article_no is not None:
        logger.warning("未处理的条号残留：%s", pending_article_no)

    markdown = "\n\n".join(markdown_blocks).strip() + "\n"
    metadata = {
        "source_file": path.name,
        "title": title,
        "version": version,
        "effective_date": effective_date,
        "paragraph_blocks": len(para_texts),
        "table_blocks": table_count,
        "has_toc": has_toc,
        "has_articles": has_articles,
        "markdown_chars": len(markdown),
        "garbage_alert": _has_garbage(markdown),
    }
    return markdown, metadata


def _flush_pending_article(markdown_blocks: list[str], pending_article_no: Optional[str]) -> Optional[str]:
    """把尚未与正文合并的单独条号写入 Markdown。"""
    if pending_article_no is None:
        return None
    markdown_blocks.append(f"**{pending_article_no}**")
    return None


def _handle_toc_line(text: str, toc_items: list[str]) -> str:
    """处理目录状态下的一行文本，返回 collected、end 或 passthrough。"""
    if not _looks_like_toc_entry(text):
        return "end"

    entries = [_compact_heading_rest(entry) for entry in _split_toc_entries(text)]
    keys = [_toc_key(entry) for entry in entries]
    if toc_items and keys and keys[0] == _toc_key(toc_items[0]):
        return "end"

    toc_items.extend(entries)
    return "collected"


def _has_garbage(text: str) -> bool:
    """检查 Markdown 文本中是否存在明确乱码占位或私用区字符。"""
    return _GARBAGE_RE.search(text) is not None or _CTRL_RE.search(text) is not None


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """把字典列表写为 UTF-8 JSONL 文件。"""
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_summary(manifest_dir: Path, summary: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    """写入转换摘要和错误列表，方便后续复核。"""
    manifest_dir.mkdir(parents=True, exist_ok=True)
    summary_path = manifest_dir / "layer1_law_markdown_conversion_summary.json"
    errors_path = manifest_dir / "layer1_law_markdown_conversion_errors.jsonl"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_jsonl(errors_path, errors)


def convert_all(raw_dir: Path, out_dir: Path, manifest_dir: Path, only_file: Optional[str]) -> dict[str, Any]:
    """批量转换 raw_dir 下的 DOCX 文件，并返回总摘要。"""
    if only_file:
        files = [raw_dir / only_file]
    else:
        files = sorted(raw_dir.glob("*.docx"))

    if not files:
        raise FileNotFoundError(f"未找到待转换的 .docx 文件：{raw_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for idx, docx_path in enumerate(files, start=1):
        if not docx_path.exists():
            errors.append({"source_file": docx_path.name, "error": "文件不存在"})
            logger.error("文件不存在：%s", docx_path)
            continue

        try:
            markdown, metadata = convert_docx_to_markdown(docx_path)
            out_path = out_dir / f"{docx_path.stem}.md"
            out_path.write_text(markdown, encoding="utf-8")
            metadata["output_file"] = out_path.name
            records.append(metadata)
            logger.info("[%s/%s] 已转换：%s", idx, len(files), docx_path.name)
        except Exception as exc:  # noqa: BLE001
            errors.append({"source_file": docx_path.name, "error": str(exc)})
            logger.exception("转换失败：%s", docx_path.name)

    summary = {
        "raw_dir": str(raw_dir),
        "out_dir": str(out_dir),
        "total_files": len(files),
        "converted_files": len(records),
        "failed_files": len(errors),
        "files_with_toc": sum(1 for row in records if row["has_toc"]),
        "files_with_articles": sum(1 for row in records if row["has_articles"]),
        "files_with_tables": sum(1 for row in records if row["table_blocks"] > 0),
        "files_with_garbage_alert": sum(1 for row in records if row["garbage_alert"]),
        "records": records,
    }
    _write_summary(manifest_dir, summary, errors)
    return summary


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="将 layer1 法律 DOCX 原文转换为 Markdown")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="DOCX 源目录")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Markdown 输出目录")
    parser.add_argument("--manifest-dir", default=str(DEFAULT_MANIFEST_DIR), help="转换摘要输出目录")
    parser.add_argument("--file", default=None, help="只转换指定文件名")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    """脚本入口：执行批量转换并打印摘要。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args(argv)
    summary = convert_all(
        raw_dir=Path(args.raw_dir),
        out_dir=Path(args.out_dir),
        manifest_dir=Path(args.manifest_dir),
        only_file=args.file,
    )
    print(
        f"[完成] 转换 {summary['converted_files']}/{summary['total_files']} 个文件，"
        f"失败 {summary['failed_files']} 个，乱码告警 {summary['files_with_garbage_alert']} 个。"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
