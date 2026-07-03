"""解析 data/raw/ 目录下的 docx 法律文件，输出结构化 JSONL 到 data/parsed_chunks/ 目录。

使用方式：
    uv run python scripts/parse_laws.py                    # 解析所有法律
    uv run python scripts/parse_laws.py --file 民法典_20200528.docx  # 解析单个文件
    uv run python scripts/parse_laws.py --law-dir /path/to/law --out-dir /path/to/output

输出：data/parsed_chunks/{法律名称}.jsonl，每行一个 JSON chunk（一条 = 一个 chunk）。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 正则：中文数字层级标题
# ---------------------------------------------------------------------------
_CN_NUM = r"[零○〇一二三四五六七八九十百千]+"

PART_RE = re.compile(rf"^第{_CN_NUM}编\s*\S+")       # 编
CHAPTER_RE = re.compile(rf"^第{_CN_NUM}章\s*\S+")     # 章
SECTION_RE = re.compile(rf"^第{_CN_NUM}节\s*\S+")     # 节
ARTICLE_RE = re.compile(rf"^(第{_CN_NUM}条)\s*(.*)")  # 条（group1=条号, group2=正文首句）

# 目录行特征（目录中章/节标题后紧跟数字页码，或整段都是目录）
TOC_RE = re.compile(r"\d+\s*$")


# ---------------------------------------------------------------------------
# 文件名解析
# ---------------------------------------------------------------------------

def parse_filename(path: Path) -> tuple[str, str, str]:
    """从文件名解析 (law_name, version, effective_date)。

    期望格式：{法律名称}_{YYYYMMDD}.docx
    如无日期后缀，version / effective_date 均返回空字符串。
    """
    stem = path.stem  # e.g. "中华人民共和国数据安全法_20210610"
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and re.fullmatch(r"\d{8}", parts[1]):
        law_name = parts[0]
        version = parts[1]
        effective_date = f"{version[:4]}-{version[4:6]}-{version[6:]}"
    else:
        law_name = stem
        version = ""
        effective_date = ""
    return law_name, version, effective_date


# ---------------------------------------------------------------------------
# Docling 解析
# ---------------------------------------------------------------------------

def extract_text_with_docling(docx_path: Path) -> str:
    """用 Docling 将 docx 转为 Markdown 文本（纯文本模式，不启用 OCR/AI）。"""
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(str(docx_path))
    return result.document.export_to_markdown()


# ---------------------------------------------------------------------------
# 法律结构解析
# ---------------------------------------------------------------------------

def _is_toc_line(line: str) -> bool:
    """判断是否是目录行（目录中章/节后面跟着页码数字）。"""
    return bool(TOC_RE.search(line)) and (
        bool(CHAPTER_RE.match(line)) or bool(SECTION_RE.match(line)) or bool(PART_RE.match(line))
    )


def parse_law_structure(md_text: str, law_name: str) -> list[dict]:
    """解析 Markdown 文本，返回 chunk 列表（每条一个 chunk）。

    状态机逻辑：
    - 遇到编/章/节：更新当前上下文，不生成 chunk
    - 遇到"第N条"：保存上一条（若有），开始新 chunk
    - 其他行：追加到当前 chunk 的 text
    """
    current_part: Optional[str] = None
    current_chapter: Optional[str] = None
    current_section: Optional[str] = None

    current_article_no: Optional[str] = None
    current_text_lines: list[str] = []

    chunks: list[dict] = []

    def flush_chunk() -> None:
        """将当前积累的条文保存为一个 chunk。"""
        if current_article_no is None:
            return
        text = " ".join(current_text_lines).strip()
        if not text:
            return
        chunks.append(
            _build_chunk(
                law_name=law_name,
                part=current_part,
                chapter=current_chapter,
                section=current_section,
                article_no=current_article_no,
                text=text,
            )
        )

    lines = md_text.splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # 跳过目录行
        if _is_toc_line(line):
            continue

        # 编
        if PART_RE.match(line):
            flush_chunk()
            current_part = line
            current_chapter = None
            current_section = None
            current_article_no = None
            current_text_lines = []
            continue

        # 章
        if CHAPTER_RE.match(line):
            flush_chunk()
            current_chapter = line
            current_section = None
            current_article_no = None
            current_text_lines = []
            continue

        # 节
        if SECTION_RE.match(line):
            flush_chunk()
            current_section = line
            current_article_no = None
            current_text_lines = []
            continue

        # 条
        m = ARTICLE_RE.match(line)
        if m:
            flush_chunk()
            current_article_no = m.group(1)  # 如"第二十一条"
            first_text = m.group(2).strip()  # 条号后的首句正文
            current_text_lines = [first_text] if first_text else []
            continue

        # 普通正文行：追加到当前条
        if current_article_no is not None:
            current_text_lines.append(line)

    # 保存最后一条
    flush_chunk()
    return chunks


# ---------------------------------------------------------------------------
# Schema 字段构建
# ---------------------------------------------------------------------------

def _build_chunk(
    law_name: str,
    part: Optional[str],
    chapter: Optional[str],
    section: Optional[str],
    article_no: str,
    text: str,
) -> dict:
    doc_id = law_name
    chunk_id = f"{doc_id}_{article_no}"

    # parent_path：按层级依次加入非 null 的层级
    parent_path: list[str] = [law_name]
    if part:
        parent_path.append(part)
    if chapter:
        parent_path.append(chapter)
    if section:
        parent_path.append(section)

    # embedding_text：按模板拼接，跳过 null 层级
    et_parts = [law_name]
    if part:
        et_parts.append(part)
    if chapter:
        et_parts.append(chapter)
    if section:
        et_parts.append(section)
    et_parts.append(article_no)
    et_parts.append(text)
    embedding_text = "\n\n".join(et_parts)

    citation_text = f"《{law_name}》{article_no}"

    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "doc_type": "law",
        "law_name": law_name,
        "part": part,
        "chapter": chapter,
        "section": section,
        "parent_path": parent_path,
        "article_no": article_no,
        "text": text,
        "embedding_text": embedding_text,
        "keywords": [],
        "citation_text": citation_text,
        "status": "effective",
        "effective_date": "",   # 由调用方填入
        "version": "",          # 由调用方填入
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def process_file(docx_path: Path, out_dir: Path) -> int:
    """解析单个 docx 文件，写入 JSONL，返回 chunk 数量（失败返回 -1）。"""
    law_name, version, effective_date = parse_filename(docx_path)
    logger.info(f"解析：{docx_path.name}  →  law={law_name}  version={version}")

    try:
        md_text = extract_text_with_docling(docx_path)
    except Exception as exc:
        logger.error(f"Docling 解析失败 {docx_path.name}: {exc}")
        return -1

    chunks = parse_law_structure(md_text, law_name)
    if not chunks:
        logger.warning(f"未提取到任何 chunk：{docx_path.name}")
        return 0

    # 填入 version / effective_date
    for chunk in chunks:
        chunk["version"] = version
        chunk["effective_date"] = effective_date

    out_path = out_dir / f"{law_name}.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    logger.info(f"  写入 {len(chunks)} 条 → {out_path}")
    return len(chunks)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="解析 data/raw/ 下的 docx 法律文件，输出 JSONL")
    p.add_argument(
        "--law-dir",
        default=str(REPO_ROOT / "data" / "raw"),
        help="法律 docx 文件所在目录（默认：%(default)s）",
    )
    p.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "data" / "parsed_chunks"),
        help="JSONL 输出目录（默认：%(default)s）",
    )
    p.add_argument(
        "--file",
        default=None,
        help="只解析指定文件名（在 --law-dir 下查找，如：民法典_20200528.docx）",
    )
    return p.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    law_dir = Path(args.law_dir)
    out_dir = Path(args.out_dir)

    if args.file:
        target = law_dir / args.file
        if not target.exists():
            logger.error(f"文件不存在：{target}")
            sys.exit(1)
        files = [target]
    else:
        files = sorted(law_dir.glob("*.docx"))
        if not files:
            logger.error(f"在 {law_dir} 下未找到任何 .docx 文件")
            sys.exit(1)

    total_chunks = 0
    failed = []
    for docx_path in files:
        n = process_file(docx_path, out_dir)
        if n < 0:
            failed.append(docx_path.name)
        else:
            total_chunks += n

    print(f"\n[完成] 共处理 {len(files) - len(failed)} 个文件，{total_chunks} 个 chunk")
    if failed:
        print(f"[失败] {len(failed)} 个文件：{failed}")


if __name__ == "__main__":
    main()
