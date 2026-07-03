"""合同解析中间表示。

不同 parser（Docling / PaddleOCR）输出的结果统一收敛到 ParsedDoc，
供下游 ClauseSplitter 消费，避免业务层依赖具体解析器实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

BlockType = Literal["heading", "paragraph", "table"]


@dataclass
class ParsedBlock:
    """解析后的最小文本单元。

    一个 block 通常对应原文中的「一行 / 一个段落 / 一个标题」。
    bbox 在原文上的坐标为 [x1, y1, x2, y2]，无坐标信息时为 None。
    """

    text: str
    block_type: BlockType = "paragraph"
    page_no: Optional[int] = None
    bbox: Optional[list[float]] = None


@dataclass
class ParsedDoc:
    """统一的合同解析结果。

    title:      解析阶段抽取的合同标题（如「房屋租赁合同」），未抽到则为空串
    blocks:     按文档顺序排列的所有文本块
    source_path:原始文件磁盘路径
    mime:       MIME type（如 image/png、application/pdf）
    doc_type:   归一化文档类型，便于下游分支
    ocr_used:   是否实际触发了 OCR（仅图片/扫描 PDF 时为 True）
    """

    title: str = ""
    blocks: list[ParsedBlock] = field(default_factory=list)
    source_path: str = ""
    mime: str = ""
    doc_type: Literal["image", "pdf", "docx"] = "pdf"
    ocr_used: bool = False

    def full_text(self) -> str:
        """拼接所有 block 文本（保留顺序），方便兜底使用。"""
        return "\n".join(b.text for b in self.blocks if b.text)
