"""使用 Docling 解析 PDF / DOCX 合同文件。

输出统一的 ParsedDoc：
- blocks 保留页码和 bbox（PDF 有；DOCX 无 bbox，page_no 也可能缺）
- title 优先取首个 Title / SectionHeader 文本，兜底取首段
- Docling 自带 OCR 能力（image-only PDF 走 EasyOCR），不再叠加 PaddleOCR

DocumentConverter 的初始化没有线程安全负担，但 Docling 内部会按需加载模型，
因此用进程级单例做缓存避免重复初始化开销。
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from app.contracts.parser.base import ParsedBlock, ParsedDoc

logger = logging.getLogger(__name__)


# Title 兜底匹配关键词：首段如果命中其一就视为合同标题
_TITLE_KEYWORDS = ("合同", "协议", "Agreement", "Contract", "约定书", "意向书", "备忘录")


def _resolve_artifacts_path() -> Optional[Path]:
    """把 settings.docling_artifacts_path 解析为存在的绝对目录，缺失则返回 None。

    返回 None 时 Docling 退化为原行为（按需联网下载到 HF 缓存），并打一条 warning，
    避免静默吞掉「模型没准备好」这种部署问题。
    """
    from app.core.config import settings

    raw = (settings.docling_artifacts_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        # 相对路径相对项目根目录（本文件位于 app/contracts/parser/ 下，向上 3 级到仓库根）
        path = Path(__file__).resolve().parents[3] / path
    if not path.is_dir():
        logger.warning("Docling 本地模型目录不存在：%s（将回退到联网下载）", path)
        return None
    return path


@lru_cache(maxsize=1)
def _get_converter():
    """进程级 DocumentConverter 单例，避免重复初始化。

    若配置了本地模型目录（settings.docling_artifacts_path），则把 artifacts_path 注入
    PDF pipeline，并显式指定 RapidOCR 引擎（预下载的就是 RapidOcr 模型），从而完全离线运行、
    不联网下载任何模型；目录缺失时回退到 Docling 默认（按需联网下载）。
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        RapidOcrOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    artifacts_path = _resolve_artifacts_path()
    if artifacts_path is None:
        # 没有本地模型：保持原有默认行为
        return DocumentConverter()

    # 设为离线，杜绝任何兜底联网请求（即使个别模型缺失也宁可报错而非静默下载）
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    pipeline_options = PdfPipelineOptions(
        artifacts_path=str(artifacts_path),
        # 与预下载的 RapidOcr 模型对应；用 torch 后端（项目已依赖 torch），
        # 避免再引入 onnxruntime，且预下载目录里同时含 torch/ 权重。
        ocr_options=RapidOcrOptions(backend="torch"),
    )
    logger.info("Docling 使用本地模型目录：%s（离线）", artifacts_path)
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )


def _extract_bbox(prov_list) -> tuple[Optional[int], Optional[list[float]]]:
    """从 Docling ProvenanceItem 列表中取第一项的 (page_no, bbox)。"""
    if not prov_list:
        return None, None
    prov = prov_list[0]
    bbox_obj = getattr(prov, "bbox", None)
    page_no = getattr(prov, "page_no", None)
    if bbox_obj is None:
        return page_no, None
    bbox = [
        float(getattr(bbox_obj, "l", 0.0)),
        float(getattr(bbox_obj, "t", 0.0)),
        float(getattr(bbox_obj, "r", 0.0)),
        float(getattr(bbox_obj, "b", 0.0)),
    ]
    return page_no, bbox


def _iter_text_items(doc) -> Iterable[tuple[str, str, list]]:
    """遍历 DoclingDocument，yield (block_type, text, prov_list)。

    block_type ∈ {heading, paragraph}（表格目前归类为 paragraph，正文已平铺到行）
    """
    try:
        items = doc.iterate_items()
    except Exception:
        logger.exception("Docling iterate_items 失败")
        return

    for node, _level in items:
        # 仅处理含 text 字段的节点；其余结构节点跳过
        text = getattr(node, "text", "") or ""
        text = text.strip()
        if not text:
            continue

        cls_name = type(node).__name__
        if cls_name in ("TitleItem", "SectionHeaderItem"):
            block_type = "heading"
        elif cls_name == "TableItem":
            # 表格目前打成段落，保留原始 markdown/cell 字符串
            block_type = "paragraph"
        else:
            block_type = "paragraph"

        prov_list = getattr(node, "prov", None) or []
        yield block_type, text, prov_list


def _detect_title(blocks: list[ParsedBlock]) -> str:
    """从已解析 blocks 中抽取合同标题。

    策略：
    1. 第一个 heading 块（Docling 把 Title/SectionHeader 都归为 heading）
    2. 否则首个段落，且长度 ≤ 30 且命中关键词
    """
    for blk in blocks:
        if blk.block_type == "heading":
            return blk.text.strip()
    for blk in blocks[:5]:
        text = blk.text.strip()
        if len(text) <= 30 and any(kw in text for kw in _TITLE_KEYWORDS):
            return text
    return ""


def parse_with_docling(file_path: Path, doc_type: str, mime: str = "") -> ParsedDoc:
    """用 Docling 解析 PDF/DOCX，返回 ParsedDoc。

    Args:
        file_path: 文件磁盘路径
        doc_type: "pdf" 或 "docx"
        mime: MIME type（仅用于回填 ParsedDoc）
    """
    converter = _get_converter()
    result = converter.convert(str(file_path))
    doc = result.document

    blocks: list[ParsedBlock] = []
    ocr_used = False

    for block_type, text, prov_list in _iter_text_items(doc):
        page_no, bbox = _extract_bbox(prov_list)
        blocks.append(
            ParsedBlock(
                text=text,
                block_type=block_type,  # type: ignore[arg-type]
                page_no=page_no,
                bbox=bbox,
            )
        )

    # Docling 对 image-only PDF 会自动启用 OCR，简单判断：所有 prov 都没 bbox
    # 但 DOCX 本身就没有 bbox，所以仅在 doc_type==pdf 时考虑
    if doc_type == "pdf" and blocks and all(b.bbox is None for b in blocks):
        ocr_used = True

    title = _detect_title(blocks)
    if not title:
        # 兜底：用文件名去后缀
        title = file_path.stem

    return ParsedDoc(
        title=title,
        blocks=blocks,
        source_path=str(file_path),
        mime=mime,
        doc_type=doc_type,  # type: ignore[arg-type]
        ocr_used=ocr_used,
    )


__all__ = ["parse_with_docling"]
