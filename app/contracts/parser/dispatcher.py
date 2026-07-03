"""按文件类型路由到对应 parser，统一返回 ParsedDoc。

支持类型：
- image: png / jpg / jpeg / bmp / webp / tif / tiff → PaddleOCR
- pdf: pdf → Docling
- docx: docx → Docling
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.contracts.parser.base import ParsedDoc

logger = logging.getLogger(__name__)


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
_PDF_EXTS = {".pdf"}
_DOCX_EXTS = {".docx"}


def detect_doc_type(file_path: Path) -> str:
    """根据扩展名判定文档类型，未识别时抛 ValueError。"""
    ext = file_path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _DOCX_EXTS:
        return "docx"
    raise ValueError(f"不支持的文件类型：{ext}")


def parse_contract_file(file_path: Path, mime: str = "") -> ParsedDoc:
    """根据文件扩展名分派到 Docling 或 PaddleOCR。"""
    doc_type = detect_doc_type(file_path)

    if doc_type == "image":
        from app.contracts.parser.paddleocr_parser import parse_with_paddleocr

        return parse_with_paddleocr(file_path, mime=mime)

    from app.contracts.parser.docling_parser import parse_with_docling

    return parse_with_docling(file_path, doc_type=doc_type, mime=mime)


__all__ = ["parse_contract_file", "detect_doc_type"]
