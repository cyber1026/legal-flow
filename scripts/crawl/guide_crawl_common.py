"""
第三层语料「律协业务操作指引」爬取与筛选的公共模块。

与前两层的关系（延续既有分层）：
- legal_crawl_common.py —— 面向「司法解释」（抽象规则文本），并提供**通用 IO**（请求/缓存/落盘/文件名）。
- case_crawl_common.py  —— 面向「裁判案例」，以案由为核心筛选。
- guide_crawl_common.py（本模块）—— 面向「律师业务操作指引」：律协各专业委员会编写、逐业务场景
  给出律师办理要点与风险提示，是最接近「结构化审查点」的公开实务文本（审查系统第三层）。

设计要点：
1. 两站详情页结构不同，但归一到同一 schema：`parse_acla_detail` / `parse_shanghai_detail`。
2. 合同相关筛选**只看短高信号字段**（标题 + 委员会 + 类别），不碰长正文——延续前两层验证过的
   「正文关键词匹配会严重过度命中」教训。
3. 产出形态为「文章语料层」：爬虫只负责抓全/抓准/存好指引全文 + 完整元信息，
   结构化抽取（审查点/风险等级/退让空间）解耦到后续入库管线用 LLM 统一处理。
"""

import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

# 复用通用 IO，保持一套底层实现
from legal_crawl_common import (  # noqa: F401  （部分仅供下游脚本转引）
    HEADERS,
    REQUEST_TIMEOUT,
    RETRY_TIMES,
    clean_line,
    clean_text,
    ensure_dirs,
    fetch_url,
    md5_text,
    polite_sleep,
    read_jsonl,
    safe_filename,
    sha256_text,
    write_jsonl,
)

from bs4 import BeautifulSoup

SOURCE_LAYER = "第三层：实务审查规则（律协业务操作指引）"


# =========================================================================
# 一、目录结构
# =========================================================================

def ensure_guide_dirs(out_dir: str) -> Dict[str, str]:
    """指引输出目录结构。在通用目录基础上增加 attachments（ACLA 的 DOC/PDF 附件）。"""
    paths = ensure_dirs(out_dir)
    paths["attachments"] = os.path.join(out_dir, "attachments")
    os.makedirs(paths["attachments"], exist_ok=True)
    return paths


# =========================================================================
# 二、通用元信息抽取（两站共用）
# =========================================================================

# 「试行」说明：试行一年 / 试行两年 / 试行 6 个月 等
_TRIAL_RE = re.compile(r"试行\s*(?:期)?\s*(一年|两年|三年|半年|\d+\s*年|\d+\s*个月)")
# 通过日期：「（本指引）于 2025年12月30日……（通讯表决）通过」
_PASSED_RE = re.compile(r"(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)[^，。；）)]{0,40}?通过")


def extract_passed_and_trial(text: str) -> Tuple[Optional[str], Optional[str]]:
    """从指引正文（通常开头一段说明）抽取「通过日期」与「试行期」。best-effort，抽不到返回 None。"""
    passed = None
    m = _PASSED_RE.search(text or "")
    if m:
        passed = re.sub(r"\s+", "", m.group(1))
    trial = None
    m = _TRIAL_RE.search(text or "")
    if m:
        trial = "试行" + re.sub(r"\s+", "", m.group(1))
    return passed, trial


def _committee_from_breadcrumb(title_tag_text: str) -> Optional[str]:
    """从东方律师网 <title> 面包屑里取专业委员会。

    形如「{标题} - 业务指引 - 公司与商事专业委员会 - 专业委员会 - 业务研究大厅 - 东方律师网」，
    取第一个以「委员会」结尾且非「专业委员会」裸词的段。
    """
    parts = [p.strip() for p in re.split(r"\s*[-–—]\s*", title_tag_text or "") if p.strip()]
    for p in parts:
        if p.endswith("委员会") and p != "专业委员会":
            return p
    return None


def _title_from_breadcrumb(title_tag_text: str) -> str:
    """东方律师网 <title> 第一段即文档标题。"""
    parts = [p.strip() for p in re.split(r"\s*[-–—]\s*", title_tag_text or "") if p.strip()]
    return parts[0] if parts else ""


# =========================================================================
# 三、详情页解析（两站适配器，输出同一 schema）
# =========================================================================

def _base_row(item: Dict, association: str, source_site: str) -> Dict:
    """构造各站共用的 row 骨架（来源/优先种子标记等列表期带来的字段）。"""
    return {
        "source_layer": SOURCE_LAYER,
        "source_site": source_site,
        "association": association,
        "url": item["url"],
        "list_url": item.get("list_url"),
        "category": item.get("category"),  # 列表页 [类别]（上海）
        "is_priority_seed": bool(item.get("is_priority_seed")),
        "title_from_list": item.get("title_from_list"),
        "publish_date_from_list": item.get("publish_date_from_list"),
    }


def parse_acla_detail(item: Dict, html: str) -> Dict:
    """解析全国律协 acla.org.cn `/info/<hash>` 详情页。

    结构：标题 `p.article-content-tit`；元信息 `ul.article-content-date > li`
    （发表时间 / 作者=制定委员会 / 来源）；纯正文 `div.acla-new-right-content`。
    """
    soup = BeautifulSoup(html, "lxml")
    row = _base_row(item, "全国律协", "www.acla.org.cn")

    tit = soup.select_one("p.article-content-tit")
    title = clean_line(tit.get_text(" ", strip=True)) if tit else ""
    if not title and soup.title:
        title = re.split(r"\s*-\s*", clean_line(soup.title.get_text()))[0]
    title = title or item.get("title_from_list", "")

    # 元信息行：发表时间 / 作者（即制定委员会）/ 来源
    publish_date = author = source = None
    date_box = soup.select_one("ul.article-content-date")
    date_text = date_box.get_text("  ", strip=True) if date_box else ""
    m = re.search(r"发表时间[:：]\s*(20\d{2}-\d{1,2}-\d{1,2})", date_text)
    if m:
        publish_date = m.group(1)
    m = re.search(r"作者[:：]\s*([^\s发来]{2,30})", date_text)
    if m:
        author = clean_line(m.group(1))
    m = re.search(r"来源[:：]\s*([^\s发作]{2,30})", date_text)
    if m:
        source = clean_line(m.group(1))

    body_el = soup.select_one("div.acla-new-right-content") or soup.select_one("div.article-content")
    body = clean_text(body_el.get_text("\n", strip=True)) if body_el else ""

    # 附件（部分老指引正文里带 DOC/PDF 链接）
    attachments = _extract_attachments(soup, base_url="https://www.acla.org.cn")

    passed_date, trial = extract_passed_and_trial(body)

    row.update({
        "title": title,
        "committee": author,         # ACLA 的「作者」即制定委员会（如「民事专业委员会」）
        "author": author,
        "source": source,
        "publish_date": publish_date or item.get("publish_date_from_list"),
        "passed_date": passed_date,
        "trial_period": trial,
        "attachments": attachments,
        "body": body,
        "body_len": len(body),
        "html_sha256": sha256_text(html),
    })
    return row


def parse_shanghai_detail(item: Dict, html: str) -> Dict:
    """解析上海律协（东方律师网）lawyers.org.cn `/info/<hash>` 详情页。

    结构：标题 `div.m-info h2`（兜底 <title> 首段）；委员会取自 <title> 面包屑；
    「日期：YYYY-MM-DD」；正文 `div.m-info > div.content`；通过日期/试行期从正文抽。
    """
    soup = BeautifulSoup(html, "lxml")
    row = _base_row(item, "上海律协", "www.lawyers.org.cn")

    title_tag_text = clean_line(soup.title.get_text()) if soup.title else ""
    h = soup.select_one("div.m-info h2") or soup.select_one("div.m-info h1")
    title = clean_line(h.get_text(" ", strip=True)) if h else _title_from_breadcrumb(title_tag_text)
    title = title or item.get("title_from_list", "")

    committee = _committee_from_breadcrumb(title_tag_text)

    mi = soup.select_one("div.m-info")
    mi_text = mi.get_text("\n", strip=True) if mi else soup.get_text("\n", strip=True)
    publish_date = None
    m = re.search(r"日期[:：]\s*(20\d{2}-\d{1,2}-\d{1,2})", mi_text)
    if m:
        publish_date = m.group(1)

    body_el = soup.select_one("div.m-info div.content")
    body = clean_text(body_el.get_text("\n", strip=True)) if body_el else ""

    passed_date, trial = extract_passed_and_trial(body or mi_text)
    attachments = _extract_attachments(soup, base_url="https://www.lawyers.org.cn")
    # 老指引把「下载全文」链接写在正文纯文本里（非 href），从 body 文本补抽
    existing = {a["url"] for a in attachments}
    for u in dict.fromkeys(_BYFILES_RE.findall(body)):
        if u not in existing:
            attachments.append({"name": "下载全文", "url": u, "local_path": None, "text_extracted": False})

    row.update({
        "title": title,
        "committee": committee,
        "author": committee,
        "source": "上海市律师协会",
        "publish_date": publish_date or item.get("publish_date_from_list"),
        "passed_date": passed_date,
        "trial_period": trial,
        "attachments": attachments,
        "body": body,
        "body_len": len(body),
        "html_sha256": sha256_text(html),
    })
    return row


_ATTACH_EXT_RE = re.compile(r"\.(docx?|pdf|wps|zip|xlsx?|pptx?)(\?|$)", re.I)
# 东方律师网「点击查看文件」走 REST 端点（无扩展名），部分老指引/海外投资篇正文仅在此 PDF 里
_REST_FILE_RE = re.compile(r"/service/rest/tk\.File/[0-9a-f]+/view", re.I)
# 2006/2007 老指引在正文里以**纯文本 URL**给「点击此处下载全文」（非 href），需从文本正则抽
_BYFILES_RE = re.compile(
    r"https?://byfiles\.storage\.lawyers\.org\.cn/file/\?action=download&fileId=[0-9a-f]+", re.I)


def _extract_attachments(soup: BeautifulSoup, base_url: str) -> List[Dict]:
    """抽取正文里的附件下载链接：① 带扩展名的 DOC/PDF/... ② 东方律师网 tk.File 查看端点（无扩展名）。

    仅记录链接，下载与抽取在 download_file / extract_file_text 里做。
    """
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _ATTACH_EXT_RE.search(href) or _REST_FILE_RE.search(href):
            full = urljoin(base_url, href)
            if full in seen:
                continue
            seen.add(full)
            out.append({"name": clean_line(a.get_text(" ", strip=True)) or os.path.basename(full),
                        "url": full, "local_path": None, "text_extracted": False})
    return out


# =========================================================================
# 三'、附件下载 + 文本抽取（PDF / 老二进制 DOC / DOCX），两站共用
# =========================================================================

# Content-Type / magic → 扩展名
_CTYPE_EXT = {"application/pdf": ".pdf", "application/msword": ".doc",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}


def download_file(url: str, dest_dir: str, referer: Optional[str] = None) -> Optional[str]:
    """下载附件到 dest_dir，按 url md5 命名，扩展名据 Content-Type/magic 判定（REST 端点无扩展名）。

    已存在同名（任一扩展）则直接复用。失败返回 None（不抛）。
    """
    key = md5_text(url)
    for ext in (".pdf", ".doc", ".docx", ".bin"):  # 复用已下载
        p = os.path.join(dest_dir, key + ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=max(REQUEST_TIMEOUT, 60))
        r.raise_for_status()
        content = r.content
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in (".pdf", ".doc", ".docx"):
            ext = _CTYPE_EXT.get((r.headers.get("Content-Type") or "").split(";")[0].strip(), "")
        if not ext:  # 再用 magic 兜底
            if content[:4] == b"%PDF":
                ext = ".pdf"
            elif content[:4] == b"\xd0\xcf\x11\xe0":
                ext = ".doc"
            elif content[:2] == b"PK":
                ext = ".docx"
            else:
                ext = ".bin"
        local = os.path.join(dest_dir, key + ext)
        with open(local, "wb") as f:
            f.write(content)
        return local
    except Exception:
        return None


def _extract_doc_olefile(path: str) -> str:
    """纯 Python 抽取老二进制 .doc（OLE）正文，零系统依赖。

    解析 WordDocument 流的 piece table（CLX/Pcdt）逐片解码（压缩片=cp1252，否则 UTF-16LE），
    再按 Word 域代码控制符（0x13 域始 / 0x14 分隔 / 0x15 域终）剥离 TOC/HYPERLINK/PAGEREF 等指令噪声。
    解析失败返回空串（调用方再退到外部工具）。
    """
    import struct
    try:
        import olefile
    except ImportError:
        return ""
    try:
        ole = olefile.OleFileIO(path)
        if not ole.exists("WordDocument"):
            return ""
        doc = ole.openstream("WordDocument").read()
        flags = struct.unpack_from("<H", doc, 0x0A)[0]
        fc_clx = struct.unpack_from("<I", doc, 0x01A2)[0]
        lcb_clx = struct.unpack_from("<I", doc, 0x01A6)[0]
        table_name = "1Table" if (flags & 0x0200) else "0Table"
        if not ole.exists(table_name):
            return ""
        clx = ole.openstream(table_name).read()[fc_clx:fc_clx + lcb_clx]

        i, pcdt = 0, None
        while i < len(clx):  # 在 CLX 里定位 Pcdt(0x02)，跳过前置 Prc(0x01)
            if clx[i] == 0x02:
                lcb = struct.unpack_from("<I", clx, i + 1)[0]
                pcdt = clx[i + 5:i + 5 + lcb]
                break
            if clx[i] == 0x01:
                i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
            else:
                break
        if not pcdt:
            return ""

        n = (len(pcdt) - 4) // 12
        cps = [struct.unpack_from("<I", pcdt, j * 4)[0] for j in range(n + 1)]
        pcd_off = (n + 1) * 4
        parts = []
        for k in range(n):
            fc = struct.unpack_from("<I", pcdt, pcd_off + k * 8 + 2)[0]
            compressed = bool(fc & 0x40000000)
            fc_actual = fc & 0x3FFFFFFF
            cch = cps[k + 1] - cps[k]
            if compressed:
                parts.append(doc[fc_actual // 2: fc_actual // 2 + cch].decode("cp1252", errors="ignore"))
            else:
                parts.append(doc[fc_actual: fc_actual + cch * 2].decode("utf-16-le", errors="ignore"))
        text = "".join(parts)
        text = re.sub("\x13[^\x14\x15]*\x14", "", text)  # 去域指令段
        text = re.sub("[\x13\x14\x15\x07\x01\x02]", "", text)
        return clean_text(text.replace("\r", "\n"))
    except Exception:
        return ""


def _extract_doc_via_tools(path: str) -> str:
    """老 .doc 的外部工具兜底：catdoc > libreoffice > antiword。都没有则空串。"""
    if shutil.which("catdoc"):
        try:
            out = subprocess.run(["catdoc", "-s", "cp936", "-d", "utf-8", path],
                                 check=True, timeout=120, capture_output=True, text=True)
            if out.stdout.strip():
                return clean_text(out.stdout)
        except Exception:
            pass
    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    if soffice:
        try:
            tmp = tempfile.mkdtemp(prefix="guide_doc_")
            subprocess.run([soffice, "--headless", "--convert-to", "txt:Text", "--outdir", tmp, path],
                           check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            txts = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".txt")]
            text = open(txts[0], encoding="utf-8", errors="ignore").read() if txts else ""
            shutil.rmtree(tmp, ignore_errors=True)
            if text.strip():
                return clean_text(text)
        except Exception:
            pass
    if shutil.which("antiword"):
        try:
            out = subprocess.run(["antiword", path], check=True, timeout=120, capture_output=True, text=True)
            if out.stdout.strip():
                return clean_text(out.stdout)
        except Exception:
            pass
    return ""


def extract_pdf_text(path: str) -> str:
    """用 pypdf 抽取文本型 PDF 正文。扫描件（抽不到文本）返回空串（本场景指引多为文本型 PDF）。"""
    try:
        import pypdf
    except ImportError:
        return ""
    try:
        reader = pypdf.PdfReader(path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return clean_text(text)
    except Exception:
        return ""


def extract_file_text(path: Optional[str]) -> str:
    """按文件 magic 分发抽取：PDF→pypdf，老 DOC→olefile(退外部工具)，DOCX→python-docx。失败返回空串。"""
    if not path or not os.path.exists(path):
        return ""
    try:
        head = open(path, "rb").read(8)
    except Exception:
        return ""
    if head[:4] == b"%PDF":
        return extract_pdf_text(path)
    if head[:4] == b"\xd0\xcf\x11\xe0":  # OLE 老二进制 .doc
        return _extract_doc_olefile(path) or _extract_doc_via_tools(path)
    if head[:2] == b"PK":  # zip 容器（.docx）
        try:
            import docx  # python-docx
            return clean_text("\n".join(p.text for p in docx.Document(path).paragraphs))
        except Exception:
            return ""
    return ""


# =========================================================================
# 四、是否为「业务操作指引」（用于 ACLA 栏目噪声过滤）
# =========================================================================

# 指引型标题信号词。ACLA 栏目混有大量实务文章/案例评析/新闻，需先按标题模式滤出真指引。
_GUIDE_TITLE_TOKENS = ("操作指引", "业务指引", "实务指引", "执业指引", "操作规程",
                       "业务规范", "工作指引", "办理指南", "业务操作")
_GUIDE_TITLE_RE = re.compile(r"律师办理.{0,24}(?:指引|指南|规范|规程)")


def looks_like_guide(title: str) -> bool:
    """标题是否像一份「业务操作指引」（用于 ACLA 栏目页过滤，上海 businessguide 全是指引无需此判）。"""
    t = title or ""
    if any(tok in t for tok in _GUIDE_TITLE_TOKENS):
        return True
    return bool(_GUIDE_TITLE_RE.search(t))


# =========================================================================
# 五、合同相关筛选（标题 + 委员会 + 类别驱动，不碰正文）
# =========================================================================

# P0：标题含强合同型业务领域词（「合同/协议」在 classify 里单独判为最强信号）。
# 注意不要把「证券/网络/数据/资本市场/施工」等宽词放进 P0：
# - 「措施工作」会误命中「施工」
# - 「网络公益讲座」「资本市场新闻」本身不是合同审查语料
GUIDE_CONTRACT_KEYWORDS = [
    "买卖", "供货", "供用电", "采购", "招投标", "招标投标", "经销", "代销", "特许经营",
    "租赁", "房屋租赁", "融资租赁", "借款", "借贷", "民间借贷",
    "担保", "保证", "抵押", "质押", "保理", "票据", "信用证", "保函", "独立保函", "让与担保",
    "建设工程", "工程施工", "勘察设计", "房地产", "房屋", "不动产", "PPP", "特许",
    "股权转让", "股权代持", "股权激励", "并购", "重组", "资产收购", "增资", "对赌",
    "关联交易", "信托", "资管",
    "红筹", "新三板", "海外投资",
    "委托", "居间", "行纪", "仓储", "运输", "货运", "物流", "供应链",
    "技术开发", "技术转让", "技术服务", "技术许可", "知识产权许可", "专利许可", "商标许可",
    "企业法律顾问", "常年法律顾问", "保险", "海商", "海事", "船舶", "国际贸易", "进出口",
    "涉外", "外商投资", "中外合资", "中外合作", "联营", "合营", "广告", "旅游", "物业服务",
    "电子商务",
]

# P1：合同邻近域（合规/治理/用工/破产等，多含合同审查要素，列次优先）。
GUIDE_ADJACENT_KEYWORDS = [
    "公司治理", "公司设立", "公司章程", "公司合规", "企业合规", "合规", "尽职调查",
    "劳动", "劳务", "用工", "人力资源", "员工", "竞业限制",
    "税务", "税法", "财务", "破产", "重整", "清算", "债务", "债权",
    "投资", "私募", "基金", "上市", "发行", "股票", "证券", "资本市场",
    "数据", "个人信息", "网络",
]

# 委员会信号（命中即提升优先级，覆盖合同密集型专业委员会）。
GUIDE_COMMITTEE_SIGNALS = [
    "公司与商事", "公司法", "企业法律顾问", "金融", "证券", "银行", "保险",
    "房地产", "建设工程", "海商海事", "国际投资", "国际贸易", "知识产权",
    "民事", "合同", "破产", "并购", "私募", "投资",
]

# 排除域（刑事/婚姻家事/行政/未成年/律师行业管理等，非合同审查范畴）。
# 延续前两层经验：用**具体多字词**，不用裸「行政」「保险」，避免误伤。
GUIDE_EXCLUDE_KEYWORDS = [
    # 刑事
    "刑事", "辩护", "犯罪", "取保候审", "羁押", "认罪认罚", "刑事控告", "刑事附带",
    "毒品", "诈骗罪", "职务犯罪", "死刑", "看守所",
    # 婚姻家事 / 人身
    "婚姻", "离婚", "继承", "遗嘱", "遗产", "赡养", "抚养", "扶养", "收养", "意定监护",
    "家事", "夫妻", "未成年", "人身损害", "人损", "工伤赔偿", "交通事故",
    # 行政 / 国家赔偿
    "行政诉讼", "行政处罚", "行政复议", "行政许可", "国家赔偿", "信访",
    # 纯诉讼/程序事务（与合同实体审查无关）
    "案由选择", "委托手续", "执行异议", "再审", "申诉", "送达",
    # 律师行业管理 / 非业务
    "律师收费", "执业年检", "执业证", "惩戒", "纪律处分", "法律援助", "值班律师",
    "公益", "普法", "党建",
    # 新闻/活动/出版信息，不是可直接入库的实务规则正文
    "召开", "会议", "工作会议", "交流会议", "讲座", "倡议书", "出版发行", "工作情况",
    "贯彻落实", "致劳动者",
]

# ACLA 动态栏目里的新闻/活动标题可能含「协议/涉外/重组」等合同词，但不是规则正文。
GUIDE_NEWS_TITLE_KEYWORDS = [
    "召开", "举办", "会议", "交流会", "讲座", "大讲堂", "培训", "活动", "侧记", "签署",
    "发布", "出版发行", "工作情况", "贯彻落实", "致劳动者", "倡议书", "出席",
]


def classify_guide_contract_relevance(row: Dict) -> Dict:
    """判断单份指引是否与合同（合同审查场景）相关，给出优先级与理由。

    优先级（从高到低）：
      DROP（排除域）  标题/委员会命中刑事/婚姻家事/行政/律师行业管理等
      P0_CONTRACT    标题含「合同/协议」或合同型业务领域词
      P1_ADJACENT    合同邻近域 或 合同密集型委员会信号
      P_SEED         优先种子兜底保留（即便上述均未命中，也强制纳入并打标）
      DROP           无任何合同信号

    只在「标题 + 委员会 + 类别」上匹配，绝不碰正文（前两层验证过的过度命中根因）。

    注意：不再在此判定「是否像指引」——上海 businessguide 是策展过的业务指引库；ACLA 已改为先抓
    栏目候选详情，再用合同信号 + 新闻/活动标题排除词筛选。在分类器里再套 looks_like_guide 会误杀
    「XX评估指引」「建筑施工风险防范指引」「红筹资本市场操作指南」等合同相关材料。
    """
    title = clean_line(row.get("title") or row.get("title_from_list") or "")
    committee = clean_line(row.get("committee") or "")
    category = clean_line(row.get("category") or "")
    match_text = "\n".join([title, committee, category])
    is_seed = bool(row.get("is_priority_seed"))

    title_text = title
    has_hetong = ("合同" in title_text) or ("协议" in title_text)
    p0_hits = [kw for kw in GUIDE_CONTRACT_KEYWORDS if kw in title_text]
    p1_hits = [kw for kw in GUIDE_ADJACENT_KEYWORDS if kw in match_text]
    committee_hits = [kw for kw in GUIDE_COMMITTEE_SIGNALS if kw in (committee + category)]
    exclude_hits = [kw for kw in GUIDE_EXCLUDE_KEYWORDS if kw in match_text]
    news_hits = [kw for kw in GUIDE_NEWS_TITLE_KEYWORDS if kw in title_text]

    keep, priority, reason, matched = False, "DROP", "", []

    if news_hits:
        reason = f"命中新闻/活动标题：{'、'.join(news_hits)}"
        matched = news_hits
    elif exclude_hits and not has_hetong and not p0_hits:
        # 排除域（刑事/婚姻家事/行政等）。但若同时含明确合同信号（如「劳务派遣合同」），合同信号优先。
        reason = f"命中排除域：{'、'.join(exclude_hits)}"
        matched = exclude_hits
    elif has_hetong or p0_hits:
        keep, priority = True, "P0_CONTRACT"
        matched = (["合同/协议"] if has_hetong else []) + p0_hits
        reason = f"标题含合同信号：{'、'.join(matched)}"
    elif p1_hits or committee_hits:
        keep, priority = True, "P1_ADJACENT"
        matched = p1_hits + [f"委员会:{c}" for c in committee_hits]
        reason = f"合同邻近域/委员会信号：{'、'.join(matched)}"
    elif is_seed:
        keep, priority = True, "P_SEED"
        reason = "优先种子，强制纳入"
    else:
        reason = "标题无合同信号"

    # 优先种子无论如何都保留（若上面落入排除/无信号分支，这里翻正并标注）
    if is_seed and not keep:
        keep, priority = True, "P_SEED"
        reason = f"优先种子强制纳入（原判：{reason}）"

    return {
        **row,
        "contract_related": keep,
        "contract_priority": priority,
        "classify_reason": reason,
        "matched_keywords": matched,
    }


_PRIORITY_ORDER = {"P0_CONTRACT": 0, "P1_ADJACENT": 1, "P_SEED": 2, "DROP": 99}


def filter_contract_related_guides(paths: Dict[str, str], rows: List[Dict]) -> List[Dict]:
    """对全量指引做合同相关筛选，落盘分类全集 / 合同相关子集 / 摘要 CSV / Markdown。"""
    classified = [classify_guide_contract_relevance(r) for r in rows]
    related = [r for r in classified if r["contract_related"]]

    related.sort(key=lambda x: (
        _PRIORITY_ORDER.get(x.get("contract_priority"), 99),
        not x.get("is_priority_seed"),          # 种子排前
        x.get("publish_date") or "",            # 同优先级按日期
        x.get("title") or "",
    ))

    classified_path = os.path.join(paths["manifest"], "classified_all.jsonl")
    related_path = os.path.join(paths["manifest"], "contract_related_guides.jsonl")
    summary_csv_path = os.path.join(paths["manifest"], "contract_related_summary.csv")

    write_jsonl(classified_path, classified)
    write_jsonl(related_path, related)
    _write_guide_csv(summary_csv_path, related)

    # 清空合同相关 Markdown 目录后重写，避免规则收紧后陈旧文件残留
    import glob
    for old in glob.glob(os.path.join(paths["contract_md"], "*.md")):
        os.remove(old)

    md_errors = []
    for row in related:
        try:
            md_path = os.path.join(paths["contract_md"], safe_filename(row.get("title"), row["url"]))
            save_guide_markdown(row, md_path)
        except Exception as e:  # 单篇失败不应中断整体
            md_errors.append({"url": row.get("url"), "error": str(e)})

    if md_errors:
        write_jsonl(os.path.join(paths["logs"], "contract_markdown_errors.jsonl"), md_errors)
        print(f"合同相关 Markdown 保存失败：{len(md_errors)} 篇，日志见 logs/")

    from collections import Counter
    dist = Counter(r["contract_priority"] for r in classified)
    seed_kept = sum(1 for r in related if r.get("is_priority_seed"))

    print(f"全部分类结果：{classified_path}")
    print(f"合同相关结果：{related_path}（共 {len(related)} / {len(classified)} 篇，其中优先种子 {seed_kept}）")
    print(f"合同相关摘要 CSV：{summary_csv_path}")
    print(f"合同相关 Markdown：{paths['contract_md']}")
    print(f"优先级分布：{dict(dist)}")
    return related


def _write_guide_csv(path: str, rows: List[Dict]):
    """合同相关指引摘要 CSV（人工复核用）。"""
    import csv
    fields = ["contract_priority", "is_priority_seed", "association", "committee", "category",
              "title", "publish_date", "passed_date", "trial_period", "body_len",
              "matched_keywords", "url", "classify_reason"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            r = dict(row)
            r["matched_keywords"] = "、".join(row.get("matched_keywords") or [])
            writer.writerow({k: r.get(k, "") for k in fields})


# =========================================================================
# 六、Markdown 输出
# =========================================================================

def save_guide_markdown(row: Dict, path: str):
    """把单份指引渲染为 Markdown，保留完整元信息 + 全文，便于后续 LLM 结构化抽取与入库。"""
    attachments = row.get("attachments") or []
    attach_md = "\n".join(
        f"- [{a.get('name')}]({a.get('url')})" + (f"（本地：{a.get('local_path')}）" if a.get("local_path") else "")
        for a in attachments
    ) or "无"

    content = f"""# {row.get("title")}

## 元数据

- 来源层级：{row.get("source_layer")}
- 协会：{row.get("association")}
- 来源网站：{row.get("source_site")}
- 制定委员会：{row.get("committee")}
- 类别：{row.get("category")}
- 发布时间：{row.get("publish_date")}
- 通过日期：{row.get("passed_date")}
- 试行期：{row.get("trial_period")}
- 作者：{row.get("author")}
- 来源：{row.get("source")}
- 是否优先种子：{row.get("is_priority_seed")}
- 正文字数：{row.get("body_len")}
- 原文链接：{row.get("url")}

## 合同相关分类

- 是否合同相关：{row.get("contract_related")}
- 优先级：{row.get("contract_priority")}
- 分类原因：{row.get("classify_reason")}
- 命中关键词：{"、".join(row.get("matched_keywords") or [])}

## 附件

{attach_md}

---

## 指引正文

{row.get("body") or ""}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
