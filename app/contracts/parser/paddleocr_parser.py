"""使用 PaddleOCR 解析合同图片（png/jpg/jpeg/bmp/webp）。

- 默认中文，单例懒加载（避免重复加载模型）
- 输出按 bbox 的 y 坐标从上到下排序，模拟「逐行阅读」
- 每行文本作为一个 ParsedBlock，page_no 固定为 1
- 标题抽取：取顶部 1~2 行，且字号（=文本框高度）显著大于中位数

PaddleOCR 3.x 的 `predict()` 返回类 dict 对象，含：
- rec_texts:  List[str]，每个识别文本
- rec_polys:  List[List[List[float]]]，每个文本对应的 4 点多边形 [(x,y) * 4]
- rec_scores: List[float]，置信度
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any

from app.contracts.parser.base import ParsedBlock, ParsedDoc
from app.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_ocr():
    """进程级 PaddleOCR 单例。

    PaddleOCR 3.x 通过构造器参数控制 lang/方向/去畸变，避免重复加载模型。
    GPU/CPU 由环境（CUDA_VISIBLE_DEVICES）决定，新版接口未保留 use_gpu 参数。
    """
    from paddleocr import PaddleOCR

    return PaddleOCR(
        lang=settings.paddleocr_lang,
        # 合同图片大多是扫描件 / 截图，方向识别和 unwarping 开销大且收益小，关掉
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


def _poly_to_bbox(poly: Any) -> list[float]:
    """4 点多边形 → 轴对齐 bbox [x1, y1, x2, y2]。"""
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def _detect_title_from_lines(lines: list[ParsedBlock]) -> str:
    """从顶部行抽取合同标题。

    规则：
    - 至少 1 行
    - 用 bbox 高度作为「字号」代理
    - 字号显著大于行高中位数（≥ 1.5x），且位于顶部前 5 行
    - 取连续满足条件的 1~2 行拼接
    """
    if not lines:
        return ""
    heights = [
        (blk.bbox[3] - blk.bbox[1]) for blk in lines if blk.bbox and len(blk.bbox) == 4
    ]
    if not heights:
        # 没有 bbox 信息：直接取首行兜底
        return lines[0].text.strip()

    base = median(heights)
    threshold = base * 1.5
    head_lines = lines[:5]

    title_parts: list[str] = []
    for blk in head_lines:
        if not blk.bbox:
            continue
        h = blk.bbox[3] - blk.bbox[1]
        if h >= threshold:
            title_parts.append(blk.text.strip())
            if len(title_parts) >= 2:
                break
        elif title_parts:
            break

    if title_parts:
        return " ".join(title_parts)

    # 兜底：首行
    return lines[0].text.strip()


def parse_with_paddleocr(file_path: Path, mime: str = "") -> ParsedDoc:
    """用 PaddleOCR 解析图片文件，返回 ParsedDoc。"""
    ocr = _get_ocr()
    try:
        results = ocr.predict(str(file_path))
    except Exception:
        logger.exception("PaddleOCR 预测失败 path=%s", file_path)
        raise

    if not results:
        return ParsedDoc(
            title=file_path.stem,
            blocks=[],
            source_path=str(file_path),
            mime=mime,
            doc_type="image",
            ocr_used=True,
        )

    page = results[0]
    rec_texts: list[str] = list(page.get("rec_texts") or [])
    rec_polys: list[Any] = list(page.get("rec_polys") or [])

    lines: list[ParsedBlock] = []
    for text, poly in zip(rec_texts, rec_polys):
        text = (text or "").strip()
        if not text:
            continue
        try:
            bbox = _poly_to_bbox(poly)
        except Exception:
            bbox = None
        lines.append(
            ParsedBlock(
                text=text,
                block_type="paragraph",
                page_no=1,
                bbox=bbox,
            )
        )

    # 按 y_top（bbox[1]）从上到下排序；同行按 x_left 排序
    lines.sort(key=lambda b: (b.bbox[1] if b.bbox else 0, b.bbox[0] if b.bbox else 0))

    title = _detect_title_from_lines(lines) or file_path.stem

    return ParsedDoc(
        title=title,
        blocks=lines,
        source_path=str(file_path),
        mime=mime,
        doc_type="image",
        ocr_used=True,
    )


__all__ = ["parse_with_paddleocr"]
