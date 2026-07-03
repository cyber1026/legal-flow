"""SAMR 合同示范文本 DOCX → 干净易读 Markdown 转换器。

为什么自研而不用 docling/python-docx 裸转换：
- 这些示范合同（尤其 1999-2009 老版）大量用「表格做表单布局」，含横向合并(w:gridSpan)与
  纵向合并(w:vMerge)。python-docx 会把合并单元格文本**复制 N 遍**；docling 虽保结构，但把
  纯表单合同渲染成超宽空表，可读性极差（也是参考目录 samr_national「字号/排版差」的根源）。

本模块按 Word 文档 XML 顺序遍历段落与表格，核心做三件事：
1. 还原真实逻辑网格：按 gridSpan/vMerge 把合并单元格内容**只保留一份**（锚点单元格），
   其余置空，得到一个矩形矩阵。
2. 清洗：删除全空行/全空列、折叠多余空白。
3. **表格分段渲染**：把一张表拆成「整宽文本行」段（如逐条条款）与「真正多列数据」段
   （如标的明细表）——文本行渲染成普通段落，多列数据才渲染成 Markdown 表格。
   这样老式表单合同既忠实又易读。

段落侧：合同标题输出为单个 `#`；「第X条/数字条」加粗但不做成大号标题（避免字号过大）；
保留下划线填空位（过长的折叠）。
"""

import re
from typing import List, Optional

from bs4 import BeautifulSoup, NavigableString, Tag
from docx import Document
from docx.oxml.ns import qn


# ============================ 文本清洗 ============================

# 章节级标题（用于二级标题与分段）。
_SECTION_RE = re.compile(
    r"^(使用说明|说\s*明|合同协议书|协议书|通用合同条款|通用条款|专用合同条款|专用条款|"
    r"附\s*[件录]\s*[一二三四五六七八九十0-9]*\s*[:：]?.*|签署页|填写说明|重要提示|"
    r"(?:专业)?(?:术语|名词)解释|前\s*言)$"
)
# 「第X条」条款头：捕获「第X条」与其后标题。
_ARTICLE_RE = re.compile(r"^(第[一二三四五六七八九十百千万零〇0-9]+条)(?:\s*(.*))?$")
# 「第X章/节」标题。
_CHAPTER_RE = re.compile(r"^(第[一二三四五六七八九十百千万零〇0-9]+[章节篇部分])(?:\s*(.*))?$")
# 纯条款号（单独成段，需与下一段合并）。
_BARE_NO_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇0-9]+条$")


def _norm_ws(text: str) -> str:
    """折叠多余空白：制表符→空格、全角空格→空格、压缩连续空格，并裁剪超长下划线/横线。"""
    if not text:
        return ""
    text = _CTRL_RE.sub("", text)  # 剥离控制字符/DEL（解析残留）
    text = text.replace("\t", " ").replace("　", " ").replace("\xa0", " ")
    text = re.sub(r"[ ]{2,}", " ", text)
    # 填空位下划线/全角下划线/连续点过长时裁剪，避免破坏排版。
    text = re.sub(r"_{6,}", "______", text)
    text = re.sub(r"＿{4,}", "______", text)
    text = re.sub(r"·{6,}|‧{6,}|\.{8,}", "……", text)
    return text.strip()


def _para_text(p_el) -> str:
    """提取一个 <w:p> 段落的纯文本（含制表符占位）。"""
    parts: List[str] = []
    for node in p_el.iter():
        tag = node.tag
        if tag == qn("w:t"):
            parts.append(node.text or "")
        elif tag == qn("w:tab"):
            parts.append("\t")
        elif tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
    return "".join(parts)


def _cell_text(tc_el) -> str:
    """提取一个 <w:tc> 单元格文本：单元格内多段落用换行连接，再做清洗。"""
    lines = []
    for p in tc_el.findall(qn("w:p")):
        lines.append(_para_text(p))
    text = "\n".join(lines)
    # 单元格内换行折叠为单空格（Markdown 表格单元格不能换行）。
    text = re.sub(r"\s*\n\s*", " ", text)
    return _norm_ws(text)


def _md_escape_cell(text: str) -> str:
    """Markdown 表格单元格转义：竖线转义、换行已在 _cell_text 折叠。"""
    return text.replace("|", "\\|")


# ============================ 表格 → 逻辑矩阵 ============================


def _table_to_matrix(tbl_el) -> List[List[str]]:
    """按 gridSpan / vMerge 还原表格逻辑网格，合并单元格内容只保留锚点一份。

    返回一个矩形矩阵（每行等长），合并产生的「占位」单元格为空字符串。
    """
    grid = tbl_el.find(qn("w:tblGrid"))
    ncols = len(grid.findall(qn("w:gridCol"))) if grid is not None else 0
    rows = tbl_el.findall(qn("w:tr"))

    matrix: List[List[str]] = []
    for tr in rows:
        cells = tr.findall(qn("w:tc"))
        # 估算本行总列宽，兼容 tblGrid 缺失或不一致。
        row_width = sum(_grid_span(tc) for tc in cells)
        width = max(ncols, row_width)
        row = [""] * width
        col = 0
        for tc in cells:
            span = _grid_span(tc)
            vmerge, vval = _v_merge(tc)
            if col >= width:
                break
            if vmerge and vval != "restart":
                # 纵向合并的延续单元格：内容继承自上方，这里留空（去重）。
                col += span
                continue
            row[col] = _cell_text(tc)  # 横向合并：仅锚点列写内容，其余留空
            col += span
        matrix.append(row)

    # 统一行宽。
    w = max((len(r) for r in matrix), default=0)
    for r in matrix:
        if len(r) < w:
            r.extend([""] * (w - len(r)))
    return matrix


def _grid_span(tc_el) -> int:
    tcPr = tc_el.find(qn("w:tcPr"))
    if tcPr is None:
        return 1
    gs = tcPr.find(qn("w:gridSpan"))
    if gs is None:
        return 1
    try:
        return max(1, int(gs.get(qn("w:val"))))
    except (TypeError, ValueError):
        return 1


def _v_merge(tc_el):
    """返回 (是否纵向合并, val)；val 为 'restart' 或 'continue'(含 val 缺省)。"""
    tcPr = tc_el.find(qn("w:tcPr"))
    if tcPr is None:
        return False, None
    vm = tcPr.find(qn("w:vMerge"))
    if vm is None:
        return False, None
    val = vm.get(qn("w:val")) or "continue"
    return True, val


_BARE_SERIAL_RE = re.compile(r"^\d{1,3}$")


def _trim_matrix(matrix: List[List[str]]) -> List[List[str]]:
    """删除全空行、全空列，以及「唯一非空单元格只是纯序号」的表单空行。

    表单类合同（如产品明细表）常预留若干空白填写行，去空列后只剩序号 1/2/3…，
    若不处理会变成一串浮动数字，故直接丢弃这些纯序号空行（表头仍保留，列结构可见）。
    """
    cleaned = []
    for r in matrix:
        nonempty = [c for c in r if c.strip()]
        if len(nonempty) == 1 and _BARE_SERIAL_RE.match(nonempty[0].strip()):
            continue
        if nonempty:
            cleaned.append(r)
    matrix = cleaned
    if not matrix:
        return []
    ncol = len(matrix[0])
    keep = [c for c in range(ncol) if any(row[c].strip() for row in matrix)]
    return [[row[c] for c in keep] for row in matrix]


# ============================ 表格 → Markdown（分段） ============================


def _render_matrix(matrix: List[List[str]]) -> List[str]:
    """把清洗后的逻辑矩阵渲染为 Markdown 块列表。

    分段策略：把「有效内容只占 1 列」的行视为整宽文本行（如条款、备注），渲染成段落；
    连续的「多列」行才组成一张 Markdown 表格。
    """
    matrix = _trim_matrix(matrix)
    if not matrix:
        return []

    blocks: List[str] = []
    buf: List[List[str]] = []
    buf_kind: Optional[str] = None  # 'text' | 'table'

    def flush():
        nonlocal buf, buf_kind
        if not buf:
            return
        if buf_kind == "text":
            for row in buf:
                line = next((c.strip() for c in row if c.strip()), "")
                if line:
                    blocks.append(line)
        else:
            blocks.append(_render_table_block(buf))
        buf = []
        buf_kind = None

    for row in matrix:
        nonempty = [c for c in row if c.strip()]
        kind = "text" if len(nonempty) <= 1 else "table"
        if kind != buf_kind:
            flush()
            buf_kind = kind
        buf.append(row)
    flush()
    return blocks


def _render_table_block(rows: List[List[str]]) -> str:
    """把一段多列行渲染为 Markdown。

    - 稠密、列数适中的真数据表 → GitHub Markdown 表格；
    - 又宽又稀疏的「表单型」表格（合并单元格错位导致大量空格）→ 每行压缩成
      「非空单元格用 ｜ 连接」的文本行，避免硬撑出巨大空表，更易读。
    """
    ncol = len(rows[0])
    keep = [c for c in range(ncol) if any(r[c].strip() for r in rows)]
    rows = [[r[c] for c in keep] for r in rows]
    width = len(rows[0])
    if width == 1:  # 去列后只剩一列，退化为文本段。
        return "\n\n".join(r[0].strip() for r in rows if r[0].strip())

    total = width * len(rows)
    filled = sum(1 for r in rows for c in r if c.strip())
    fill_ratio = filled / total if total else 0
    if width > 6 and fill_ratio < 0.5:
        # 表单型：逐行压缩为非空单元格连接的文本行（用全角竖线，避免被当成表格语法）。
        lines = []
        for r in rows:
            cells = [c.strip() for c in r if c.strip()]
            if cells:
                lines.append(" ｜ ".join(cells))
        return "\n\n".join(lines)

    def fmt(row):
        return "| " + " | ".join(_md_escape_cell(c.strip()) for c in row) + " |"

    header = rows[0]
    body = rows[1:]
    out = [fmt(header), "| " + " | ".join(["---"] * width) + " |"]
    out.extend(fmt(r) for r in body)
    return "\n".join(out)


# ============================ 段落 → Markdown ============================


_TOC_RE = re.compile(r"^目\s*录$")
_CH_BOUNDARY_RE = re.compile(r"(?=第[一二三四五六七八九十百千万零〇0-9]+[章节])")


def _append_block(blocks: List[str], rendered: str) -> None:
    """追加一个 Markdown 块；独立的填空续行（如「______；」）并入上一段，避免碎成孤立行。"""
    if (rendered.lstrip().startswith("_") and blocks
            and not blocks[-1].startswith(("#", "-", "|"))):
        blocks[-1] = blocks[-1] + rendered
    else:
        blocks.append(rendered)


def _split_toc_entries(text: str) -> List[str]:
    """把一行里拼接的多个章节目录项拆开（双栏目录拍平后常见）。"""
    parts = [p.strip() for p in _CH_BOUNDARY_RE.split(text) if p.strip()]
    return parts or [text]


def _iter_body_blocks(doc):
    """按文档顺序产出 ('para', 文本) 或 ('table', 矩阵)。"""
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield "para", _norm_ws(_para_text(child))
        elif child.tag == qn("w:tbl"):
            yield "table", _table_to_matrix(child)


def _render_paragraph(text: str, title: str) -> Optional[str]:
    """把单个段落渲染为 Markdown 块；标题/空段返回 None（不重复输出标题）。"""
    if not text:
        return None
    if _BARE_SERIAL_RE.match(text):
        return None  # 孤立的纯数字（页码/残留编号）噪声，丢弃
    # 注意：不在此删除与标题相同的行——正文需要保留合同标题，标题去重/注入由抓取脚本统一处理。

    m = _CHAPTER_RE.match(text)
    if m:
        head, rest = m.group(1), (m.group(2) or "").strip()
        return f"## {head}{('　' + rest) if rest else ''}"

    if _SECTION_RE.match(text):
        return f"## {text}"

    m = _ARTICLE_RE.match(text)
    if m:
        head, rest = m.group(1), (m.group(2) or "").strip()
        # 条款头加粗（不做成大号标题），与正文同字号、更易读。
        return f"**{head}**{('　' + rest) if rest else ''}"

    return text


# ============================ 顶层入口 ============================


def docx_to_markdown_body(path: str, title: str = "") -> str:
    """把 DOCX 转为 Markdown 正文（不含 frontmatter / 一级标题）。"""
    doc = Document(path)
    blocks: List[str] = []
    pending_no: Optional[str] = None  # 暂存单独成段的「第X条」，与下一段合并
    in_toc = False
    toc_items: List[str] = []

    def flush_toc():
        nonlocal in_toc, toc_items
        if toc_items:
            blocks.append("\n".join(f"- {x}" for x in toc_items))
        in_toc = False
        toc_items = []

    for kind, payload in _iter_body_blocks(doc):
        if kind == "para":
            text = payload
            if not text:
                continue
            if _TOC_RE.match(text):
                flush_toc()
                blocks.append("## 目录")
                in_toc = True
                continue
            if in_toc:
                if _looks_like_toc_entry(text):
                    entries = _split_toc_entries(text)
                    if any(e in toc_items for e in entries):
                        flush_toc()  # 条目重复→目录结束(正文再次以该标题出现)，落到下方按正文处理
                    else:
                        toc_items.extend(entries)
                        continue
                else:
                    flush_toc()
            # 合并「单独一行的条款号」+ 下一行标题/正文。
            if pending_no is not None:
                merged = f"{pending_no} {text}".strip()
                pending_no = None
                rendered = _render_paragraph(merged, title)
                if rendered:
                    _append_block(blocks, rendered)
                continue
            if _BARE_NO_RE.match(text):
                pending_no = text
                continue
            rendered = _render_paragraph(text, title)
            if rendered:
                _append_block(blocks, rendered)
        else:  # table
            flush_toc()
            if pending_no is not None:
                blocks.append(_render_paragraph(pending_no, title) or pending_no)
                pending_no = None
            blocks.extend(_render_matrix(payload))

    flush_toc()
    if pending_no is not None:
        blocks.append(_render_paragraph(pending_no, title) or pending_no)

    # 块之间统一空行分隔，保证 Markdown 渲染为独立段落/表格。
    return "\n\n".join(b for b in blocks if b and b.strip())


# ============================ catdoc 文本（老 .doc） → Markdown ============================
#
# 老式 .doc(OLE2) 用 python-docx 读不了；系统 catdoc 能抽出干净的线性文本（读序正确、无
# PDF 解析那种「按字号误判标题」的问题）。代价是丢失表格网格——表格内容会被拍平成文本行，
# 内容不丢、结构丢。原始 PDF 一并留底，必要时可回溯。

# 合同里合法出现的非 CJK/ASCII 符号（复选框/圈号/度量/标点等），其余非常用字符视为乱码。
_SYMBOL_WL = set(
    "°×÷±²³µ§·…—–‘’“”•　□☑☐■○●◯✓✔√※★☆→←№℃℉‰%¥￥㎡㎞㎏㎜＄$€"
    "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"
)

# C0/C1 控制字符与 DEL（解析残留），任何路径都应剥离。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _is_legit_char(c: str) -> bool:
    """是否为中文合同正文里会出现的正常字符（CJK / ASCII / CJK标点全角 / 白名单符号）。"""
    o = ord(c)
    return (
        0x4E00 <= o <= 0x9FFF        # 基本汉字
        or 0x20 <= o <= 0x7E          # ASCII 可见
        or o in (0x09, 0x0A)          # tab/换行
        or 0x3000 <= o <= 0x303F      # CJK 标点
        or 0xFF00 <= o <= 0xFFEF      # 全角
        or c in _SYMBOL_WL
    )


def _clean_garbage_line(line: str) -> str:
    """清理 catdoc 因嵌入对象/控件吐出的二进制乱码（谚文/私用区/CJK扩展/控制符等）。

    整行大半是乱码 → 丢弃；以正文为主的混合行 → 删掉非法字符、保留正文片段。
    """
    if not line:
        return line
    legit = sum(1 for c in line if _is_legit_char(c))
    if legit / max(len(line.strip()), 1) < 0.6:
        return ""  # 主要是乱码，整行丢弃
    cleaned = "".join(c for c in line if _is_legit_char(c) or c == " ")
    s = cleaned.strip()
    if s and not any(c.isalnum() or 0x4E00 <= ord(c) <= 0x9FFF for c in s):
        return ""  # 清理后只剩孤立符号(如 ~ $ ¶)，视为噪声丢弃
    return cleaned


def _format_text_line(line: str) -> str:
    """清洗 catdoc 单行：去首尾空白(去掉居中/缩进)、折叠多空格、裁剪超长下划线。

    不把空格转成下划线——.doc 的居中/缩进也是空格，转下划线会污染封面（如「目  录」变
    「目______录」、行首多下划线），故只折叠空白，保留原有下划线填空位。
    """
    line = _CTRL_RE.sub("", line)  # 剥离控制字符/DEL
    line = line.replace("\t", " ").replace("　", " ").replace("\xa0", " ")
    line = line.strip()
    line = re.sub(r"[ ]{2,}", " ", line)
    line = re.sub(r"_{6,}", "______", line)
    return line


def _looks_like_toc_entry(text: str) -> bool:
    """目录条目：含多个章节标记的拼接行，或较短的单个章/节/说明/术语标题词。"""
    if len(re.findall(r"第[一二三四五六七八九十百千万零〇0-9]+[章节]", text)) >= 2:
        return True  # 双栏目录拍平后的多章节拼接行，无视长度
    if len(text) > 28:
        return False
    return bool(
        _CHAPTER_RE.match(text)
        or _SECTION_RE.match(text)
        or text in ("说明", "专业术语解释", "前言", "重要提示", "填写说明")
    )


def catdoc_text_to_markdown_body(text: str, title: str = "") -> str:
    """把 catdoc 抽出的纯文本格式化为 Markdown 正文（不含 frontmatter / 一级标题）。"""
    raw_lines = [_clean_garbage_line(_format_text_line(x)) for x in text.splitlines()]
    blocks: List[str] = []
    in_toc = False
    toc_items: List[str] = []

    def flush_toc():
        nonlocal in_toc, toc_items
        if toc_items:
            blocks.append("\n".join(f"- {x}" for x in toc_items))
        in_toc = False
        toc_items = []

    for line in raw_lines:
        if not line:
            continue
        # 注意：不删除与标题相同的行——正文需保留合同标题，去重/注入由抓取脚本统一处理。

        if _TOC_RE.match(line):
            flush_toc()
            blocks.append("## 目录")
            in_toc = True
            continue

        if in_toc:
            # 目录块：连续章节条目收进列表；条目重复→目录结束(正文再次以该标题出现)；遇长正文/编号即退出。
            if _looks_like_toc_entry(line):
                entries = _split_toc_entries(line)
                if any(e in toc_items for e in entries):
                    flush_toc()  # 落到下方按正文处理
                else:
                    toc_items.extend(entries)
                    continue
            else:
                flush_toc()

        rendered = _render_paragraph(line, title)
        if rendered:
            _append_block(blocks, rendered)

    flush_toc()
    return "\n\n".join(b for b in blocks if b and b.strip())


# ============================ /View 详情页 HTML → Markdown 正文 ============================
#
# 老式 .doc 经 catdoc 抽取常吐二进制乱码并丢失正文；而 SAMR 详情页 /View 的 `.samr-view-content`
# 容器里是结构化、干净、完整的 HTML（h2=章/说明/术语、h3=条款、p=段落/子项、table=真表格、
# 下划线 span=填空位）。故 .doc 一律改用本解析器，文本完整且能还原真表格。

_VIEW_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# 块级标签：用于判断某容器是否含更细的块级子结构（含则递归，否则整体作一段）。
_VIEW_BLOCK_TAGS = {
    "p", "div", "article", "section", "li", "ul", "ol", "table", "tbody",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
}


def _is_view_garbage(c: str) -> bool:
    """/View HTML 偶含的占位/乱码字符：控制符、私用区、谚文、CJK 扩展A/B+。"""
    o = ord(c)
    return (
        (o < 0x20 and o not in (0x09, 0x0A))
        or o == 0x7F
        or 0xE000 <= o <= 0xF8FF
        or 0xAC00 <= o <= 0xD7AF
        or 0x3400 <= o <= 0x4DBF
        or o >= 0x20000
    )


def _view_clean(text: str) -> str:
    """清洗 /View 文本：去控制符/私用区等乱码占位、全角与不换行空格归一、折叠多空格。"""
    text = "".join(ch for ch in text if not _is_view_garbage(ch))
    text = text.replace("\xa0", " ").replace("　", " ")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _html_table_to_md(table: Tag) -> str:
    """HTML <table> → Markdown 表格：展开 colspan、补齐为矩形（_render_matrix 要求等长行）。"""
    matrix: List[List[str]] = []
    for tr in table.find_all("tr"):
        row: List[str] = []
        for td in tr.find_all(["td", "th"]):
            txt = _view_clean(td.get_text(" ", strip=True))
            try:
                span = max(int(td.get("colspan", 1) or 1), 1)
            except ValueError:
                span = 1
            row.append(txt)
            row.extend([""] * (span - 1))  # colspan 展开为多列
        if any(c.strip() for c in row):
            matrix.append(row)
    if not matrix:
        return ""
    width = max(len(r) for r in matrix)
    matrix = [r + [""] * (width - len(r)) for r in matrix]  # 补齐为矩形
    try:
        return "\n\n".join(_render_matrix(matrix))
    except Exception:  # noqa: BLE001 — 个别畸形表格不致整篇失败
        return "\n\n".join(" ".join(c for c in r if c) for r in matrix)


def _walk_view(node: Tag, blocks: List[str]) -> None:
    """按文档顺序遍历 .samr-view-content：标题→##/加粗，段落→文本，表格→Markdown 表格。"""
    for el in node.children:
        if isinstance(el, NavigableString):
            t = _view_clean(str(el))
            if t:
                blocks.append(t)
            continue
        if not isinstance(el, Tag) or el.name == "br":
            continue
        name = el.name
        if name == "table":
            md = _html_table_to_md(el)
            if md:
                blocks.append(md)
        elif name in _VIEW_HEADINGS:
            t = _view_clean(el.get_text(" ", strip=True))
            if t:
                rendered = _render_paragraph(t, "")  # 第X章→## / 第X条→加粗 / 说明术语→##
                if rendered:
                    blocks.append(rendered)
        elif name in ("p", "li"):
            t = _view_clean(el.get_text(" ", strip=True))
            if t:
                rendered = _render_paragraph(t, "")  # 第X条→加粗 / 第X章→## / 其余原样
                if rendered:
                    blocks.append(rendered)
        elif el.find(_VIEW_BLOCK_TAGS):  # div/section/span 等含块级子 → 递归
            _walk_view(el, blocks)
        else:  # 纯文本容器 → 整体作一段
            t = _view_clean(el.get_text(" ", strip=True))
            if t:
                blocks.append(t)


def html_view_to_markdown_body(html: str, title: str = "") -> str:
    """把 /View 详情页 HTML 的 `.samr-view-content` 解析为干净完整的 Markdown 正文。"""
    box = BeautifulSoup(html, "lxml").select_one(".samr-view-content")
    if box is None:
        return ""
    blocks: List[str] = []
    _walk_view(box, blocks)
    out: List[str] = []
    for b in blocks:
        if b and b.strip() and (not out or out[-1] != b):  # 去连续重复块
            out.append(b)
    return "\n\n".join(out)


if __name__ == "__main__":
    import sys

    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/samr_test1.docx"
    ttl = sys.argv[2] if len(sys.argv) > 2 else ""
    if src.lower().endswith((".txt",)):
        print(catdoc_text_to_markdown_body(open(src, encoding="utf-8").read(), ttl))
    else:
        print(docx_to_markdown_body(src, ttl))
