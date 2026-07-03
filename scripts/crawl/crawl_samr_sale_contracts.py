"""抓取国家市场监督管理总局(SAMR)合同示范文本库中「买卖」类示范合同，解析为干净易读的 Markdown。

来源：https://htsfwb.samr.gov.cn/List?key=买卖
- 列表数据：/api/content/SearchTemplates?key=买卖&loc=<false|true>&p=<page>
  loc=false(部委) + loc=true(地方) 列表共 135 行，但**含大量重复 Id**，按 Id 去重后为 **89 个唯一合同**。
- 原件下载：/api/File/DownTemplate?id=<GUID>&type=<1|2>（type=1 Word(.docx/.doc)，type=2 PDF）
- 详情页：/View?id=<GUID>

解析策略（按原件格式分流，统一输出风格一致、带 YAML frontmatter 的 Markdown）：
- 新式 .docx → 自研 samr_docx_to_md.docx_to_markdown_body：还原合并单元格、真实 Markdown 表格、封面/条款。
- 老式 .doc(OLE2，python-docx 读不了) → 系统 catdoc 抽净文本 → catdoc_text_to_markdown_body：
  正确读序、章节/条款结构化，表格内容拍平为文本（结构丢、内容不丢；PDF 原件留底可回溯）。
- 兜底：上述失败时用 docling 解析 PDF，并把按字号误判出的标题降级，避免「字号过大」。

输出：
  data/legal_sources/layer4_standard_contracts/samr/
    sales_contracts/   89 个 <标题>.md（交付物）
    raw/               搜索接口 JSON + originals/<id>.<docx|doc|pdf> 原件
    manifest/          sale_contracts.jsonl + summary.csv
    logs/              errors.jsonl

用法（仓库根目录，用项目 venv 执行；勿用 uv run——会触发依赖重解析失败）：
  .venv/bin/python scripts/crawl/crawl_samr_sale_contracts.py            # 全量89个
  .venv/bin/python scripts/crawl/crawl_samr_sale_contracts.py --limit 5  # 只跑前5个验证
  .venv/bin/python scripts/crawl/crawl_samr_sale_contracts.py --no-cache # 不用本地缓存重抓
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from samr_docx_to_md import (  # noqa: E402
    catdoc_text_to_markdown_body,
    docx_to_markdown_body,
    html_view_to_markdown_body,
)

BASE_URL = "https://htsfwb.samr.gov.cn"
KEYWORD = "买卖"
OUT_ROOT = "data/legal_sources/layer4_standard_contracts/samr"
SOURCE_LAYER = "第四层：标准合同库（合同示范文本与条款变体）"
SOURCE_SITE = "htsfwb.samr.gov.cn"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
# Type 字段（int）到分类名的映射。
TYPE_MAP = {1: "生活消费", 2: "农资农业", 3: "生产经营", 4: "建设工程", 5: "其他"}
# 文件名/正文里的合同编号：GF—2000—0104 / SF-2020-0102 / BF—2023—0142 等。
# 注意：不能用 \b 收尾——中文紧跟数字时(如「0142合同」)二者都是 \w 无词边界，会漏匹配；
# 故首尾改用「非字母数字」的环视。
DOC_NO_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{1,4})[—―\-_－]\s*(\d{4})\s*[—―\-_－]\s*(\d{2,5})(?!\d)"
)
# 文件名中非法/敏感字符。
ILLEGAL_FN_RE = re.compile(r'[/\\:*?"<>|\n\r\t]')


def ensure_dirs() -> Dict[str, str]:
    """建立输出目录骨架。"""
    paths = {
        "root": OUT_ROOT,
        "md": os.path.join(OUT_ROOT, "sales_contracts"),
        "raw": os.path.join(OUT_ROOT, "raw"),
        "orig": os.path.join(OUT_ROOT, "raw", "originals"),
        "view": os.path.join(OUT_ROOT, "raw", "view"),
        "manifest": os.path.join(OUT_ROOT, "manifest"),
        "logs": os.path.join(OUT_ROOT, "logs"),
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    return paths


# ============================ 一、枚举列表（去重） ============================


def enumerate_contracts(paths: Dict[str, str], *, use_cache: bool) -> List[Dict]:
    """枚举 key=买卖 的部委+地方列表，按 Id 去重，返回唯一合同元数据列表。"""
    rows: List[Dict] = []
    for loc in ("false", "true"):
        scope = "部委合同示范文本" if loc == "false" else "地方合同示范文本"
        cache = os.path.join(paths["raw"], f"search_loc_{loc}.json")
        if use_cache and os.path.exists(cache):
            data = json.load(open(cache, encoding="utf-8"))
        else:
            data = _fetch_all_pages(loc)
            json.dump(data, open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        for it in data:
            rows.append({
                "id": (it.get("Id") or "").lower(),
                "title": (it.get("Title") or "").strip(),
                "brief": (it.get("Brief") or "").strip(),
                "department": (it.get("Department") or "").strip(),
                "publish_year": str(it.get("PublishedOn") or "").strip(),
                "region": (it.get("Region") or "").strip(),
                "category": TYPE_MAP.get(it.get("Type"), str(it.get("Type") or "")),
                "scope": scope,
                "is_local": loc == "true",
                "url": f"{BASE_URL}/View?id={(it.get('Id') or '').lower()}",
            })

    # 按 Id 去重（保留首个出现的更完整记录）。
    merged: Dict[str, Dict] = {}
    for r in rows:
        if not r["id"]:
            continue
        if r["id"] not in merged:
            merged[r["id"]] = r
    uniq = list(merged.values())
    print(f"列表枚举：原始 {len(rows)} 行 → 去重后 {len(uniq)} 个唯一合同")
    return uniq


def _fetch_all_pages(loc: str) -> List[Dict]:
    """按页抓取某 loc 的全部搜索结果。"""
    out: List[Dict] = []
    page, total_page = 1, 1
    while page <= total_page:
        resp = requests.get(
            f"{BASE_URL}/api/content/SearchTemplates",
            params={"key": KEYWORD, "loc": loc, "p": page},
            headers={**BROWSER_HEADERS, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                     "Referer": f"{BASE_URL}/List?key={quote(KEYWORD)}"},
            timeout=30,
        )
        resp.raise_for_status()
        d = resp.json()
        out.extend(d.get("Data") or [])
        total_page = int(d.get("TotalPage") or 1)
        page += 1
        time.sleep(0.2)
    return out


# ============================ 二、下载原件 ============================


def download_original(doc_id: str, dtype: int, dest_dir: str, *, use_cache: bool) -> Optional[Tuple[str, str]]:
    """下载原件，按真实格式落盘。返回 (本地路径, kind)；kind ∈ {docx, doc, pdf, other}。"""
    # 已缓存则直接识别（任意已存在的同 id 原件）。
    if use_cache:
        for ext in ("docx", "doc", "pdf", "bin"):
            cached = os.path.join(dest_dir, f"{doc_id}_t{dtype}.{ext}")
            if os.path.exists(cached) and os.path.getsize(cached) > 0:
                return cached, _ext_to_kind(ext)

    resp = requests.get(
        f"{BASE_URL}/api/File/DownTemplate",
        params={"id": doc_id, "type": dtype},
        headers={**BROWSER_HEADERS, "Referer": f"{BASE_URL}/View?id={doc_id}"},
        timeout=90,
    )
    resp.raise_for_status()
    content = resp.content
    if not content:
        return None
    kind = _detect_kind(content)
    ext = {"docx": "docx", "doc": "doc", "pdf": "pdf"}.get(kind, "bin")
    path = os.path.join(dest_dir, f"{doc_id}_t{dtype}.{ext}")
    with open(path, "wb") as f:
        f.write(content)
    # 顺带把 Content-Disposition 里的真实文件名记下（含合同编号）。
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        with open(path + ".name", "w", encoding="utf-8") as f:
            f.write(_filename_from_cd(cd))
    return path, kind


def _detect_kind(content: bytes) -> str:
    if content[:2] == b"PK":
        return "docx"
    if content[:4] == b"\xd0\xcf\x11\xe0":
        return "doc"
    if content[:4] == b"%PDF":
        return "pdf"
    return "other"


def _ext_to_kind(ext: str) -> str:
    return {"docx": "docx", "doc": "doc", "pdf": "pdf"}.get(ext, "other")


def _filename_from_cd(cd: str) -> str:
    """从 Content-Disposition 解析真实文件名（优先 filename*）。"""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        return unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', cd)
    return m.group(1) if m else ""


# ============================ 三、解析为 Markdown ============================


def parse_to_markdown(meta: Dict, word_path: Optional[str], word_kind: Optional[str],
                      pdf_path: Optional[str], view_html: str) -> Tuple[str, str]:
    """把原件解析为 Markdown 正文，返回 (body, parse_source)。

    - 新式 docx：自研转换器（含真表格、合并单元格还原）。
    - 老式 doc：优先用 /View 详情页 HTML（干净完整、有真表格），catdoc 仅作兜底——
      因 catdoc 对含嵌入对象的 .doc 会吐二进制乱码并丢失正文。
    """
    title = meta["title"]
    # 1) 新式 docx：自研转换器
    if word_kind == "docx" and word_path:
        try:
            body = docx_to_markdown_body(word_path, title)
            if body.strip():
                return body, "docx"
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] docx 解析失败({title}): {exc}")
    # 2) 老式 doc：优先 /View HTML，其次 catdoc
    if word_kind == "doc":
        if view_html:
            body = html_view_to_markdown_body(view_html, title)
            if body.strip():
                return body, "doc(view)"
        if word_path:
            text = _catdoc_text(word_path)
            if text and text.strip():
                body = catdoc_text_to_markdown_body(text, title)
                if body.strip():
                    return body, "doc(catdoc)"
        print(f"  [warn] /View 与 catdoc 均解析失败({title})，回退 PDF")
    # 3) 兜底：docling 解析 PDF
    if pdf_path and os.path.exists(pdf_path):
        body = _docling_pdf_markdown(pdf_path)
        if body.strip():
            return body, "pdf(docling)"
    return "", "failed"


def _catdoc_text(path: str) -> str:
    """用 catdoc 抽取 .doc 纯文本（-w 不换行，保留长行便于后处理）。"""
    try:
        out = subprocess.run(["catdoc", "-w", path], capture_output=True, timeout=60)
        return out.stdout.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] catdoc 调用失败: {exc}")
        return ""


_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.*)$")


def _docling_pdf_markdown(pdf_path: str) -> str:
    """docling 解析 PDF（关 OCR，born-digital 够用），并把按字号误判的标题降级为加粗，避免字号过大。"""
    try:
        import logging
        import warnings

        warnings.filterwarnings("ignore")
        logging.disable(logging.CRITICAL)
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opt = PdfPipelineOptions()
        opt.do_ocr = False
        opt.do_table_structure = True
        conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opt)})
        md = conv.convert(pdf_path).document.export_to_markdown()
        # PDF 解析的标题多为字号误判，统一降级为加粗正文（保留表格/列表）。
        md = _HEADING_RE.sub(lambda m: f"**{m.group(1).strip()}**", md)
        md = re.sub(r"(?m)^- \[ \]\s*", "", md)  # 去掉误转的任务清单复选框
        return md
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] docling PDF 解析失败: {exc}")
        return ""


# ============================ 四、组装 Markdown 文件 ============================


def extract_doc_no(cd_name: str, body: str) -> str:
    """优先从原件文件名解析合同编号，否则从正文开头找。"""
    for text in (cd_name, body[:2000]):  # 正文源含封面，编号通常在前部
        m = DOC_NO_RE.search(text or "")
        if m:
            return f"{m.group(1)}—{m.group(2)}—{m.group(3)}"
    return ""


# 正文里出现的文档编号（GF—2000—0104 / SF-2020-0102 / BF—2023—0142…）。
_DOCNO_INLINE_RE = re.compile(r"[A-Z]{1,4}[—―\-_－]\s*\d{4}\s*[—―\-_－]\s*\d{2,5}")


def _is_cover_noise_line(line: str) -> bool:
    """判断是否为封面标识噪声行：整行仅由 文档编号 / 空的「合同编号：」/「（示范文本）」/ 分隔符 组成。

    用于评测数据集——正文要像真实可填写的合同，故剔除这些与元信息表重复或纯模板封面标识；
    「合同编号：」后若带真实编号值（非标准 GF/SF 编号）则保留整行。
    """
    t = _DOCNO_INLINE_RE.sub("", line)
    t = re.sub(r"合同编号\s*[:：]\s*号?", "", t)  # 含「合同编号：号」空字段
    t = re.sub(r"[（(]?\s*示\s*范\s*文\s*本\s*[）)]?", "", t)  # 含空格版「示 范 文 本」
    t = re.sub(r"[\s_＿—―－\-、，,。.：:；;]", "", t)
    return t == ""


def clean_cover_noise(body: str) -> str:
    """剔除正文中的封面标识噪声行（须在 doc_no 提取之后调用，否则会丢掉编号来源）。"""
    kept = [ln for ln in body.split("\n") if not (ln.strip() and _is_cover_noise_line(ln))]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return text.strip()


# ============================ 风险提示（来自 /View 页面） ============================


def fetch_view_html(doc_id: str, view_dir: str, *, use_cache: bool) -> str:
    """抓取并缓存 /View 详情页 HTML（同时用于正文解析与风险提示提取）。"""
    cache = os.path.join(view_dir, f"{doc_id}.html")
    if use_cache and os.path.exists(cache):
        return open(cache, encoding="utf-8").read()
    resp = requests.get(f"{BASE_URL}/View", params={"id": doc_id},
                        headers={**BROWSER_HEADERS, "Referer": f"{BASE_URL}/List?key={quote(KEYWORD)}"},
                        timeout=60)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = resp.apparent_encoding
    html = resp.text
    with open(cache, "w", encoding="utf-8") as f:
        f.write(html)
    time.sleep(0.2)  # 礼貌限速（首次爬 view 详情页）
    return html


def extract_risk_tips(html: str) -> List[Tuple[str, str]]:
    """从 /View HTML 解析「风险提示」（.samr-view-risk-item：.title + .content）。

    风险提示只在网页详情页上、不在 docx/pdf 原件里；每个合同的风险提示不同（条数/内容各异）。
    """
    soup = BeautifulSoup(html, "lxml")
    tips: List[Tuple[str, str]] = []
    for item in soup.select(".samr-view-risk-item"):
        tnode = item.select_one(".title")
        title = tnode.get_text(strip=True) if tnode else ""
        cnode = item.select_one(".content")
        content = (cnode.get_text(" ", strip=True) if cnode
                   else item.get_text(" ", strip=True).replace(title, "", 1).strip())
        content = re.sub(r"\s+", " ", content).strip()
        if title or content:
            tips.append((title, content))
    return tips


def render_risk_md(tips: List[Tuple[str, str]]) -> str:
    """风险提示渲染为 Markdown：每条「**N. 标题**」+ 内容段。"""
    out: List[str] = []
    for i, (title, content) in enumerate(tips, 1):
        out.append(f"**{i}. {title}**" if title else f"**{i}.**")
        if content:
            out.append(content)
    return "\n\n".join(out)


# ============================ 使用说明/说明 抽取（移出正文） ============================

# 说明类标题：「说明/使用说明/填写说明/使用须知/重要提示」，也含「X合同说明/X协议说明」这类
# （如「商品房买卖合同说明」「房地产经纪服务合同说明」）。可能带 `## ` 前缀，也可能是纯文本行。
_INSTR_HEADING_RE = re.compile(
    r"^#{0,3}\s*(?:.{0,18}?(?:合同|协议))?\s*"
    r"(使用说明|填写说明|使用须知|特别说明|特别提示|重要提示|说\s*明)\s*$"
)
# 合同正文起始信号：当事人声明 / 鉴于(根据) / 第X条章 / 标题 / 其它二级标题 / 表格。
_PARTY_RE = re.compile(
    r"^(甲方|乙方|甲|乙|出卖人|买受人|供方|需方|卖方|买方|出租方|承租方|出租人|承租人|"
    r"发包人|承包人|建设单位|物业服务人|借款人|贷款人|委托人|受托人)\s*[（(：:]"
)
_ARTICLE_HEAD_RE = re.compile(r"^\**第[一二三四五六七八九十百千万零〇0-9]+[条章节]")


def _is_contract_start(block: str, titles: set) -> bool:
    """判断某段是否标志合同正文开始（用于界定使用说明段的结束边界）。"""
    b = block.strip()
    if not b:
        return False
    if b in titles:
        return True
    if b.startswith(("## ", "# ", "<h1", "<h2", "|")):
        return True
    if _ARTICLE_HEAD_RE.match(b) or _PARTY_RE.match(b):
        return True
    if b.startswith(("根据《", "依据《", "为了", "为规范", "为明确")):
        return True
    return False


def extract_instruction_sections(body: str, titles: set) -> Tuple[str, str]:
    """从正文抽出「使用说明/说明」段，返回 (去除说明后的正文, 说明 Markdown)。"""
    blocks = re.split(r"\n\n+", body)
    kept: List[str] = []
    instr: List[str] = []
    i = 0
    while i < len(blocks):
        if _INSTR_HEADING_RE.match(blocks[i].strip()):
            i += 1
            while i < len(blocks) and not _is_contract_start(blocks[i], titles):
                if blocks[i].strip():
                    instr.append(blocks[i].strip())
                i += 1
            continue
        kept.append(blocks[i])
        i += 1
    return "\n\n".join(kept).strip(), "\n\n".join(instr).strip()


# ============================ 正文标题（确保正文带合同标题） ============================

# 去掉标题末尾的「（…局/部/厅/委/年/版…）」版本/机关后缀，得到干净合同名。
_TITLE_SUFFIX_RE = re.compile(r"（[^（）]*(?:版|局|部|厅|委|办|年|\d{4})[^（）]*）\s*$")


def clean_contract_title(title: str) -> str:
    """从带版本/机关后缀的标题得到干净合同名，作为正文标题。"""
    t = _TITLE_SUFFIX_RE.sub("", title).strip()
    return t or title


# 「合同当事人」章/段锚点（含「合同双方当事人」、可带「第X章」前缀与冒号）。
_PARTY_CHAPTER_RE = re.compile(
    r"^(?:#+\s*)?(?:第[一二三四五六七八九十0-9]+章[　\s]*)?合同(?:双方)?当事人[：:]?\s*$"
)
# 预备段标题：目录/索引/专业术语解释/术语解释/名词解释。
_PREAMBLE_HEAD_RE = re.compile(r"^#+\s*(?:目\s*录|索\s*引|专业术语解释|术语解释|名词解释)\s*$")
# 合同标题行（地名+「买卖合同/协议」等，较短、单独成行）。
_TITLE_LINE_RE = re.compile(r"^[一-鿿（）()【】市省区县自治州盟旗·\s]{4,32}(?:合同|协议)$")


def _is_recital(block: str) -> bool:
    """判断是否为合同「鉴于/缔约」段（应保留），区别于封面行与术语定义。"""
    s = block.strip()
    if len(s) < 40 or "是指" in s:  # 太短 / 术语定义 → 非鉴于段
        return False
    return any(k in s for k in (
        "签订本", "根据《", "依据《", "出卖人向买受人", "卖方向买方",
        "买卖双方", "双方当事人", "甲乙双方", "甲、乙", "现就", "经协商一致", "为明确",
    ))


def trim_to_contract_body(body: str) -> str:
    """裁掉合同正文前的封面/目录/专业术语解释/索引等预备页，使正文从合同标题处开始。

    策略1：有「合同(双方)当事人」章/段(取最后一个，避开目录式重复) → 之前皆预备页，仅留鉴于段。
    策略2：无当事人章但有目录/术语预备段 → 丢到「预备段之后首个合同标题行」(含)为止。
    其余(标题已在顶部的简单合同) → 不裁剪。
    """
    blocks = re.split(r"\n\n+", body)
    party_idxs = [i for i, b in enumerate(blocks) if _PARTY_CHAPTER_RE.match(b.strip())]
    if party_idxs:
        anchor = party_idxs[-1]
        recital = [b for b in blocks[:anchor] if _is_recital(b)]
        return "\n\n".join(recital + blocks[anchor:]).strip()

    pre_idxs = [i for i, b in enumerate(blocks) if _PREAMBLE_HEAD_RE.match(b.strip())]
    if pre_idxs:
        for i in range(pre_idxs[-1] + 1, len(blocks)):
            if _TITLE_LINE_RE.match(blocks[i].strip()):
                return "\n\n".join(blocks[i + 1:]).strip()  # 标题(含)前全丢，标题由注入补回
    return body


def inject_body_title(body: str, titles: set, display_title: str) -> str:
    """正文去重所有裸标题行，再在开头注入一个居中的合同标题，确保正文始终带标题。"""
    lines = [ln for ln in body.split("\n") if ln.strip() not in titles]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return f'<h2 align="center">{display_title}</h2>\n\n{body}'


def build_markdown(meta: Dict, body: str, doc_no: str, parse_source: str,
                   risk_md: str = "", instr_md: str = "") -> str:
    """生成「一级标题 + 元信息表格 + 正文」的 Markdown。

    不用 YAML frontmatter（`---` 包裹）：不支持 frontmatter 的预览器会把结尾的 `---`
    当成 setext 标题下划线，把整块元信息渲染成超大标题（即「字号过大」）。改用普通
    Markdown 表格，任何预览器都是正常字号。完整机器可读元信息见 manifest/sale_contracts.jsonl。
    """
    rows = [
        ("合同编号", doc_no),
        ("发布机关", meta["department"]),
        ("发布年份", meta["publish_year"]),
        ("适用地区", meta["region"]),
        ("分类", meta["category"]),
        ("文本范围", meta["scope"]),
        ("来源网址", meta["url"]),
        ("来源网站", SOURCE_SITE),
        ("语料层级", SOURCE_LAYER),
    ]
    # 标题：用居中、字号略大的 HTML 标题，更醒目、更像真实合同抬头（# 已是 Markdown 最大级，
    # 再大只能用 HTML；不支持 style 的渲染器会回退为居中 H1，仍醒目）。
    title_html = f'<h1 align="center" style="font-size: 2.4em">{meta["title"]}</h1>'
    lines = [title_html, "", "| 元信息 | 内容 |", "| --- | --- |"]
    for key, val in rows:
        val = str(val or "").strip().replace("|", "\\|")
        if val:
            lines.append(f"| {key} | {val} |")
    # 元信息区附带「风险提示」「使用说明」，与正文以 --- 显著隔开。
    if risk_md:
        lines += ["", "## 风险提示", "", risk_md]
    if instr_md:
        lines += ["", "## 使用说明", "", instr_md]
    lines += ["", "---", "", body]
    return "\n".join(lines).rstrip() + "\n"


def safe_filename(title: str) -> str:
    """把标题清洗为安全文件名（89 个标题已确认唯一）。"""
    name = ILLEGAL_FN_RE.sub("_", title).strip().strip(".")
    return (name or "untitled")[:120] + ".md"


# ============================ 五、主流程 ============================


def process_one(meta: Dict, paths: Dict[str, str], *, skip_pdf: bool, use_cache: bool) -> Dict:
    """处理单个合同：下载原件 + /View → 解析 → 标题/说明/裁剪/风险 → 写 md，返回 manifest 记录。

    供主流程（key=买卖 枚举）与补录脚本（national/local 指定合同）共用，确保格式完全一致。
    """
    title, doc_id = meta["title"], meta["id"]
    word = download_original(doc_id, 1, paths["orig"], use_cache=use_cache)
    pdf = None if skip_pdf else download_original(doc_id, 2, paths["orig"], use_cache=use_cache)
    word_path, word_kind = (word or (None, None))
    pdf_path = pdf[0] if pdf else None
    cd_name = ""
    if word_path and os.path.exists(word_path + ".name"):
        cd_name = open(word_path + ".name", encoding="utf-8").read()

    # /View 详情页：doc 正文源 + 风险提示来源，一次抓取复用。
    try:
        view_html = fetch_view_html(doc_id, paths["view"], use_cache=use_cache)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] /View 抓取失败: {exc}")
        view_html = ""

    body, parse_source = parse_to_markdown(meta, word_path, word_kind, pdf_path, view_html)
    if not body.strip():
        raise RuntimeError("解析结果为空")

    # 编号提取：doc 正文来自 /View(常无编号)，故额外并入 catdoc 文本与文件名一起搜。
    no_src = body
    if word_kind == "doc" and word_path:
        no_src = _catdoc_text(word_path) + "\n" + body
    doc_no = extract_doc_no(cd_name, no_src)
    body = clean_cover_noise(body)  # 剔除封面标识噪声(重复编号/空合同编号/示范文本)

    # 标题处理 + 抽出使用说明 + 裁剪预备页 + 注入正文标题。
    clean_t = clean_contract_title(title)
    title_variants = {title.strip(), clean_t.strip()}
    body, instr_md = extract_instruction_sections(body, title_variants)
    body = trim_to_contract_body(body)  # 裁掉「合同当事人」章之前的封面/目录/术语预备页
    body = inject_body_title(body, title_variants, clean_t)
    try:
        risk_md = render_risk_md(extract_risk_tips(view_html)) if view_html else ""
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 风险提示解析失败: {exc}")
        risk_md = ""
    n_risk = risk_md.count("**") // 2

    md = build_markdown(meta, body, doc_no, parse_source, risk_md, instr_md)
    md_path = os.path.join(paths["md"], safe_filename(title))
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    return {
        **meta, "doc_no": doc_no, "parse_source": parse_source,
        "word_kind": word_kind, "body_len": len(body),
        "n_risk_tips": n_risk, "has_instructions": bool(instr_md),
        "md_file": os.path.basename(md_path),
    }


def run(args: argparse.Namespace) -> None:
    paths = ensure_dirs()
    use_cache = not args.no_cache
    items = enumerate_contracts(paths, use_cache=use_cache)
    if args.limit:
        items = items[: args.limit]

    manifest: List[Dict] = []
    errors: List[Dict] = []
    src_counter: Dict[str, int] = {}

    for i, meta in enumerate(items, 1):
        title = meta["title"]
        doc_id = meta["id"]
        print(f"[{i}/{len(items)}] {title}")
        try:
            rec = process_one(meta, paths, skip_pdf=args.skip_pdf, use_cache=use_cache)
            src_counter[rec["parse_source"]] = src_counter.get(rec["parse_source"], 0) + 1
            manifest.append(rec)
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] {exc}")
            errors.append({"id": doc_id, "title": title, "error": str(exc)})
        if not use_cache:
            time.sleep(0.3)

    _write_jsonl(os.path.join(paths["manifest"], "sale_contracts.jsonl"), manifest)
    _write_jsonl(os.path.join(paths["logs"], "errors.jsonl"), errors)
    _write_summary(os.path.join(paths["manifest"], "summary.csv"), manifest)
    print(f"\n完成：成功 {len(manifest)} / 失败 {len(errors)}；解析来源分布 {src_counter}")
    if errors:
        print("失败清单：", [e["title"] for e in errors])


def _write_jsonl(path: str, rows: List[Dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_summary(path: str, rows: List[Dict]) -> None:
    fields = ["title", "scope", "region", "category", "publish_year", "department",
              "doc_no", "word_kind", "parse_source", "body_len", "url"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def main() -> None:
    ap = argparse.ArgumentParser(description="抓取并解析 SAMR 买卖类示范合同(去重后89个)。")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（验证用）")
    ap.add_argument("--no-cache", action="store_true", help="不使用本地缓存，全部重抓")
    ap.add_argument("--skip-pdf", action="store_true", help="不下载 PDF（不保留 PDF 原件，也失去 PDF 兜底）")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
