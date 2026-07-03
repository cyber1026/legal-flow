"""
第四层语料「标准合同库」爬取、筛选与条款切分公共模块。

目标来源是国家市场监督管理总局合同示范文本库（htsfwb.samr.gov.cn）：
- 部委合同示范文本：/National
- 地方合同示范文本：/Local
- 详情页：/View?id=<uuid>

本模块只做公开示范文本的「抓全 / 抓准 / 存好 / 切条款」。更高阶的 LLM 结构化抽取
（标准立场、风险等级、退让空间）应放在后续入库管线里，与其它层语料统一处理。
"""

import os
import re
import shutil
from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from legal_crawl_common import (
    clean_line,
    clean_text,
    ensure_dirs,
    md5_text,
    read_jsonl,
    safe_filename,
    sha256_text,
    write_csv,
    write_jsonl,
)

try:
    # guide_crawl_common 里的附件下载/抽文本逻辑已经覆盖 PDF/DOC/DOCX，第四层复用即可。
    from guide_crawl_common import download_file, extract_file_text
except Exception:  # pragma: no cover - 只在脚本路径异常时兜底
    download_file = None
    extract_file_text = None


SOURCE_LAYER = "第四层：标准合同库（合同示范文本与条款变体）"
SOURCE_SITE = "htsfwb.samr.gov.cn"

VIEW_RE = re.compile(r"/View\?id=([0-9a-fA-F-]{36})")
DOC_NO_RE = re.compile(r"\bGF[—\-－]\s*\d{4}\s*[—\-－]\s*\d{3,5}\b")
YEAR_RE = re.compile(r"(19|20)\d{2}")


def ensure_standard_clause_dirs(out_dir: str) -> Dict[str, str]:
    """标准合同库输出目录。兼容 legal_crawl_common.ensure_dirs 的 html/all_md/logs 命名。"""
    paths = ensure_dirs(out_dir)
    paths["attachments"] = os.path.join(out_dir, "attachments")
    paths["clauses_md"] = os.path.join(out_dir, "markdown", "clauses")
    os.makedirs(paths["attachments"], exist_ok=True)
    os.makedirs(paths["clauses_md"], exist_ok=True)
    return paths


def normalize_doc_id(url: str) -> str:
    m = re.search(r"id=([0-9a-fA-F-]{36})", url or "")
    return m.group(1).lower() if m else md5_text(url)


def normalize_scope(scope: str) -> str:
    if scope == "national":
        return "部委合同示范文本"
    if scope == "local":
        return "地方合同示范文本"
    return scope


# =========================================================================
# 一、索引页解析
# =========================================================================


def _title_from_anchor(a) -> str:
    title = clean_line(a.get_text(" ", strip=True))
    if title:
        return title
    for attr in ("title", "aria-label"):
        if a.get(attr):
            return clean_line(a.get(attr))
    return ""


def extract_samr_list_items(list_url: str, html: str, *, scope: str) -> List[Dict]:
    """从 SAMR 列表页提取 /View?id=... 条目。

    页面主体是 Vue/SSR 混合形态；生产环境和搜索缓存均能看到 /View 链接。这里按链接抽取，
    并用附近文本补 publish_year / category / region。
    """
    soup = BeautifulSoup(html, "lxml")
    items, seen = [], set()
    scope_label = normalize_scope(scope)

    for a in soup.find_all("a", href=VIEW_RE):
        href = a.get("href") or ""
        detail_url = urljoin(list_url, href)
        doc_id = normalize_doc_id(detail_url)
        if doc_id in seen:
            continue
        seen.add(doc_id)

        parent = a.find_parent(["li", "div", "tr", "article"]) or a.parent
        ptext = clean_line(parent.get_text(" ", strip=True)) if parent else ""
        title = _title_from_anchor(a)
        if not title:
            title = _guess_title_from_near_text(ptext)
        if not title:
            continue

        items.append({
            "source_layer": SOURCE_LAYER,
            "source_site": SOURCE_SITE,
            "scope": scope_label,
            "title_from_list": title,
            "url": detail_url,
            "list_url": list_url,
            "publish_year_from_list": _extract_year(title) or _extract_year(ptext),
            "category_from_list": _extract_category(ptext),
            "region_from_list": _extract_region(title, scope=scope),
            "crawl_time": datetime.now().isoformat(timespec="seconds"),
        })

    # 兜底：如果站点把链接放在脚本 JSON 中，BeautifulSoup 的 a 抽不到，则从 HTML 正则补。
    if not items:
        for m in VIEW_RE.finditer(html):
            detail_url = urljoin(list_url, m.group(0))
            doc_id = normalize_doc_id(detail_url)
            if doc_id in seen:
                continue
            seen.add(doc_id)
            items.append({
                "source_layer": SOURCE_LAYER,
                "source_site": SOURCE_SITE,
                "scope": scope_label,
                "title_from_list": "",
                "url": detail_url,
                "list_url": list_url,
                "publish_year_from_list": None,
                "category_from_list": None,
                "region_from_list": None,
                "crawl_time": datetime.now().isoformat(timespec="seconds"),
            })

    return items


def merge_list_items(items: Iterable[Dict]) -> List[Dict]:
    merged: Dict[str, Dict] = {}
    for item in items:
        key = normalize_doc_id(item.get("url", ""))
        if key not in merged:
            merged[key] = item
            continue
        old = merged[key]
        # 保留更完整的列表标题/分类。
        for field in ("title_from_list", "publish_year_from_list", "category_from_list", "region_from_list"):
            if not old.get(field) and item.get(field):
                old[field] = item[field]
    return list(merged.values())


def _guess_title_from_near_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    stop_words = ["甲方", "乙方", "发布机关", "发布年份", "发布编号", "下载Word", "下载PDF"]
    for stop in stop_words:
        if stop in text:
            text = text.split(stop, 1)[0]
    return clean_line(text)[:120]


def _extract_year(text: str) -> Optional[str]:
    m = YEAR_RE.search(text or "")
    return m.group(0) if m else None


def _extract_category(text: str) -> Optional[str]:
    for cat in ("生活消费", "农资农业", "生产经营", "建设工程", "其他"):
        if cat in (text or ""):
            return cat
    return None


_REGION_NAMES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
    "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏",
    "新疆", "京津冀",
]


def _extract_region(title: str, *, scope: str) -> Optional[str]:
    if scope != "local":
        return None
    for region in _REGION_NAMES:
        if title.startswith(region) or f"（{region}" in title or f"({region}" in title:
            return region
    return None


# =========================================================================
# 二、详情页解析
# =========================================================================


def parse_samr_detail(item: Dict, html: str) -> Dict:
    soup = BeautifulSoup(html, "lxml")
    lines = _clean_page_lines(soup)
    title = _extract_detail_title(lines, item.get("title_from_list") or "")
    doc_no = _extract_doc_no("\n".join(lines))
    publish_agencies = _extract_meta_list(lines, "发布机关")
    if not publish_agencies and item.get("department_from_list"):
        publish_agencies = [x for x in re.split(r"\s+|、|，|,", item["department_from_list"]) if x]
    publish_year = _extract_meta_value(lines, "发布年份") or _extract_year(title) or item.get("publish_year_from_list")
    category = _extract_meta_value(lines, "分类") or item.get("category_from_list")
    region = _extract_meta_value(lines, "地区") or item.get("region_from_list")

    body, risk_tips = _split_contract_body_and_risk_tips(lines, title)
    attachments = _extract_attachments(soup, "https://htsfwb.samr.gov.cn")

    row = {
        "source_layer": SOURCE_LAYER,
        "source_site": SOURCE_SITE,
        "scope": item.get("scope"),
        "doc_id": normalize_doc_id(item.get("url", "")),
        "title": title,
        "title_from_list": item.get("title_from_list"),
        "url": item.get("url"),
        "list_url": item.get("list_url"),
        "category": category,
        "region": region,
        "publish_year": publish_year,
        "publish_agencies": publish_agencies,
        "doc_no": doc_no,
        "attachments": attachments,
        "body": body,
        "body_len": len(body),
        "risk_tips": risk_tips,
        "risk_tips_len": len(risk_tips),
        "html_sha256": sha256_text(html),
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
    }
    row.update(classify_standard_contract_relevance(row))
    return row


def _clean_page_lines(soup: BeautifulSoup) -> List[str]:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [clean_line(x) for x in soup.get_text("\n", strip=True).splitlines()]
    lines = [x for x in lines if x]

    # 去掉站点通用说明和页脚噪声，保留详情正文。
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("首页 >") or line == "首页":
            start = i
            break
    lines = lines[start:]

    footer_markers = [
        "版权所有：国家市场监督管理总局",
        "京ICP备",
        "地址：北京市海淀区",
        "由法天使-中国合同库提供支持",
    ]
    end = len(lines)
    for i, line in enumerate(lines):
        if any(marker in line for marker in footer_markers):
            end = i
            break
    return lines[:end]


def _extract_detail_title(lines: List[str], fallback: str) -> str:
    # 常见形态：`首页 > 合同节水管理项目服务合同（水利部、市场监管总局2026版）`
    for line in lines[:20]:
        if line.startswith("首页 >"):
            title = clean_line(line.split(">")[-1])
            if len(title) >= 4:
                return title
        if line == "首页":
            idx = lines.index(line)
            if idx + 1 < len(lines) and lines[idx + 1].startswith(">"):
                title = clean_line(lines[idx + 1].lstrip("> "))
                if len(title) >= 4:
                    return title

    # 正文标题通常在 GF 编号后几行。
    for line in lines[:80]:
        if _looks_like_title_line(line):
            return line
    return fallback


def _looks_like_title_line(line: str) -> bool:
    if len(line) < 4 or len(line) > 80:
        return False
    if line.startswith(("GF", "合同编号", "发布机关", "发布年份", "下载")):
        return False
    return any(tok in line for tok in ("合同", "协议", "承诺书", "示范文本"))


def _extract_doc_no(text: str) -> Optional[str]:
    m = DOC_NO_RE.search(text or "")
    if not m:
        return None
    return re.sub(r"\s+", "", m.group(0)).replace("-", "—").replace("－", "—")


def _extract_meta_value(lines: List[str], label: str) -> Optional[str]:
    for i, line in enumerate(lines):
        compact = line.replace(" ", "")
        if compact in (f"{label}：", f"{label}:"):
            return _next_meaningful_line(lines, i + 1)
        if compact.startswith(f"{label}：") or compact.startswith(f"{label}:"):
            val = re.split(r"[:：]", line, maxsplit=1)[-1].strip()
            if val:
                return val
            return _next_meaningful_line(lines, i + 1)
    return None


def _extract_meta_list(lines: List[str], label: str) -> List[str]:
    val = _extract_meta_value(lines, label)
    if not val:
        return []
    parts = re.split(r"[、,，/]\s*|\s+", val)
    return [clean_line(p) for p in parts if clean_line(p)]


def _next_meaningful_line(lines: List[str], start: int) -> Optional[str]:
    skip = {"：", ":", "下载Word文档", "下载PDF文档"}
    for line in lines[start:start + 6]:
        if line and line not in skip and not line.startswith(""):
            return line
    return None


def _split_contract_body_and_risk_tips(lines: List[str], title: str) -> Tuple[str, str]:
    if not lines:
        return "", ""

    start = 0
    for i, line in enumerate(lines):
        if line == title or line.endswith(title):
            start = i + 1
            break
        if DOC_NO_RE.search(line):
            start = i
            break

    risk_idx = None
    for i, line in enumerate(lines):
        if "风险提示" in line:
            risk_idx = i
            break

    meta_idx = None
    for i, line in enumerate(lines):
        if line.replace(" ", "").startswith(("发布机关", "发布年份", "发布编号")):
            meta_idx = i
            break

    end_candidates = [x for x in (risk_idx, meta_idx) if x is not None and x > start]
    end = min(end_candidates) if end_candidates else len(lines)
    body_lines = _drop_download_lines(lines[start:end])

    risk_lines: List[str] = []
    if risk_idx is not None:
        risk_end = meta_idx if meta_idx is not None and meta_idx > risk_idx else len(lines)
        risk_lines = _drop_download_lines(lines[risk_idx:risk_end])

    return clean_text("\n".join(body_lines)), clean_text("\n".join(risk_lines))


def _drop_download_lines(lines: List[str]) -> List[str]:
    drop_tokens = ("下载Word文档", "下载PDF文档", "")
    return [line for line in lines if not any(tok in line for tok in drop_tokens)]


_ATTACH_EXT_RE = re.compile(r"\.(docx?|pdf|wps)(\?|$)", re.I)


def _extract_attachments(soup: BeautifulSoup, base_url: str) -> List[Dict]:
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        text = clean_line(a.get_text(" ", strip=True))
        if not (_ATTACH_EXT_RE.search(href) or "下载Word" in text or "下载PDF" in text):
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        out.append({
            "name": text or os.path.basename(full),
            "url": full,
            "local_path": None,
            "text_extracted": False,
        })
    return out


def hydrate_attachments(row: Dict, attachments_dir: str, *, referer: Optional[str] = None) -> Dict:
    """下载附件并在正文过短时用附件文本补正文。下载失败不阻断主流程。"""
    if download_file is None or extract_file_text is None:
        return row

    body = row.get("body") or ""
    for att in row.get("attachments") or []:
        local = download_file(att["url"], attachments_dir, referer=referer or row.get("url"))
        att["local_path"] = local
        text = extract_file_text(local) if local else ""
        att["text_extracted"] = bool(text)
        if text and len(text) > len(body):
            # 详情页有时是预览正文，有时附件更完整；保留更长版本。
            body = text
    if body != (row.get("body") or ""):
        row["body"] = body
        row["body_len"] = len(body)
    return row


# =========================================================================
# 三、合同相关筛选
# =========================================================================


STANDARD_CONTRACT_KEYWORDS = [
    "合同", "协议", "承诺书", "订单", "保单", "意向书", "确认书",
    "买卖", "销售", "采购", "供货", "租赁", "借款", "担保", "保理", "融资租赁",
    "建设工程", "施工", "装修", "物业", "服务", "旅游", "运输", "物流", "仓储",
    "技术", "许可", "委托", "承包", "承揽", "加工", "定作", "赠与", "保管",
    "居间", "中介", "经纪", "加盟", "特许经营", "保险", "信托", "供用电",
    "商品房", "房屋", "养老", "家政", "体育健身", "预付式消费",
]

POLICY_EXCLUDE_KEYWORDS = [
    "办法", "规定", "指导意见", "行政监管", "政策文件", "通知", "公告", "解读",
    "工作方案", "实施方案", "管理制度",
]


def classify_standard_contract_relevance(row: Dict) -> Dict:
    title = clean_line(row.get("title") or row.get("title_from_list") or "")
    body_head = clean_line((row.get("body") or "")[:1200])
    doc_no = row.get("doc_no") or ""
    match_text = "\n".join([title, doc_no, body_head])

    if any(k in title for k in POLICY_EXCLUDE_KEYWORDS) and "合同" not in title and "协议" not in title:
        return {
            "contract_related": False,
            "contract_priority": "DROP",
            "matched_keywords": [],
            "classify_reason": "标题像政策/监管文件，非合同示范文本",
        }

    matched = [k for k in STANDARD_CONTRACT_KEYWORDS if k in match_text]
    if doc_no or "示范文本" in match_text:
        matched = sorted(set(matched + (["示范文本"] if "示范文本" in match_text else [])))
        return {
            "contract_related": True,
            "contract_priority": "P0_STANDARD",
            "matched_keywords": matched,
            "classify_reason": "命中官方示范文本编号或正文示范文本信号",
        }

    if "合同" in title or "协议" in title or matched:
        return {
            "contract_related": True,
            "contract_priority": "P1_CONTRACT_FORM",
            "matched_keywords": matched,
            "classify_reason": "标题/正文开头命中合同或合同类型词",
        }

    return {
        "contract_related": False,
        "contract_priority": "DROP",
        "matched_keywords": [],
        "classify_reason": "未命中合同示范文本信号",
    }


def filter_contract_related_standard_contracts(paths: Dict[str, str], rows: List[Dict]) -> List[Dict]:
    classified = []
    for row in rows:
        row = dict(row)
        row.update(classify_standard_contract_relevance(row))
        classified.append(row)

    related = [r for r in classified if r.get("contract_related")]
    related.sort(key=lambda r: (
        r.get("contract_priority") != "P0_STANDARD",
        r.get("scope") or "",
        r.get("publish_year") or "",
        r.get("title") or "",
    ))

    write_jsonl(os.path.join(paths["manifest"], "classified_all.jsonl"), classified)
    write_jsonl(os.path.join(paths["manifest"], "contract_related_standard_contracts.jsonl"), related)
    _write_standard_summary(os.path.join(paths["manifest"], "contract_related_summary.csv"), related)
    _rewrite_markdown_dir(paths["contract_md"], related)

    print(f"合同相关筛选：{len(related)}/{len(classified)} 篇")
    return related


def _write_standard_summary(path: str, rows: List[Dict]) -> None:
    fields = [
        "contract_priority", "matched_keywords", "title", "doc_no", "scope",
        "publish_year", "publish_agencies", "category", "region", "body_len", "url", "classify_reason",
    ]
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "contract_priority": row.get("contract_priority"),
                "matched_keywords": "、".join(row.get("matched_keywords", [])),
                "title": row.get("title"),
                "doc_no": row.get("doc_no"),
                "scope": row.get("scope"),
                "publish_year": row.get("publish_year"),
                "publish_agencies": "、".join(row.get("publish_agencies") or []),
                "category": row.get("category"),
                "region": row.get("region"),
                "body_len": row.get("body_len"),
                "url": row.get("url"),
                "classify_reason": row.get("classify_reason"),
            })


def _rewrite_markdown_dir(md_dir: str, rows: List[Dict]) -> None:
    shutil.rmtree(md_dir, ignore_errors=True)
    os.makedirs(md_dir, exist_ok=True)
    for row in rows:
        filename = safe_filename(row.get("title") or row.get("doc_id"), row.get("url") or "")
        save_standard_contract_markdown(row, os.path.join(md_dir, filename))


# =========================================================================
# 四、条款切分与条款变体
# =========================================================================


ARTICLE_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇0-9]+条(?:[ 　]*(.*))?$")
SECTION_HEAD_RE = re.compile(r"^(使用说明|说明|合同协议书|协议书|通用合同条款|通用条款|专用合同条款|专用条款|附件|附录|签署页)$")
CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇0-9]+[章节部分篇](?:[ 　]*(.*))?$")
NUMERIC_RE = re.compile(r"^\d+(?:\.\d+){0,3}[ 　、.．]+(.{0,80})$")


def split_standard_contract_clauses(row: Dict) -> List[Dict]:
    body = row.get("body") or ""
    lines = [clean_line(x) for x in body.splitlines()]
    lines = [x for x in lines if x]
    clauses: List[Dict] = []
    section_stack: List[str] = []
    current: Optional[Dict] = None

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = clean_text("\n".join(current.pop("_lines", [])))
        if len(text) < 8:
            current = None
            return
        current["clause_text"] = text
        current["clause_len"] = len(text)
        current["normalized_clause_type"] = normalize_clause_type(
            " ".join([current.get("clause_title") or "", text[:300]])
        )
        current["contract_domain"] = infer_contract_domain(row.get("title") or "")
        clauses.append(current)
        current = None

    for line in lines:
        if current and current.get("clause_role") == "使用说明" and line.startswith("根据《"):
            flush()
            section_stack = ["合同正文"]

        if SECTION_HEAD_RE.match(line) or CHAPTER_RE.match(line):
            flush()
            section_stack.append(line)
            section_stack = section_stack[-4:]
            continue

        article = ARTICLE_RE.match(line)
        numeric = NUMERIC_RE.match(line)
        is_article = bool(article)
        is_numeric = bool(numeric and len(line) <= 120)

        if is_numeric and current and (current.get("clause_no") or "").startswith("第"):
            current["_lines"].append(line)
            continue

        if is_article or is_numeric:
            flush()
            clause_no = line.split(" ", 1)[0]
            title = ""
            if is_article:
                clause_no = re.match(r"^第[一二三四五六七八九十百千万零〇0-9]+条", line).group(0)
                title = clean_line(article.group(1) or "")
            elif is_numeric:
                clause_no = re.match(r"^\d+(?:\.\d+){0,3}", line).group(0)
                title = clean_line(numeric.group(1) or "")

            current = _new_clause(row, len(clauses) + 1, clause_no, title, section_stack)
            current["_lines"].append(line)
            continue

        if current is None:
            # 使用说明、前言等没有明确条号时按小段兜底。
            if len(line) >= 20 and any(k in line for k in ("本合同", "本示范文本", "当事人", "甲方", "乙方")):
                current = _new_clause(row, len(clauses) + 1, "", "", section_stack or ["未编号条款"])
                current["_lines"].append(line)
            continue
        current["_lines"].append(line)

    flush()

    if not clauses and clean_text(body):
        fallback = _new_clause(row, 1, "", row.get("title") or "未编号文本", ["未编号文本"])
        text = clean_text(body)
        fallback["clause_text"] = text
        fallback["clause_len"] = len(text)
        fallback["normalized_clause_type"] = normalize_clause_type(
            " ".join([fallback.get("clause_title") or "", text[:300]])
        )
        fallback["contract_domain"] = infer_contract_domain(row.get("title") or "")
        fallback.pop("_lines", None)
        clauses.append(fallback)

    for idx, clause in enumerate(clauses, start=1):
        clause["clause_index"] = idx
        clause["clause_id"] = f"{row.get('doc_id')}_c{idx:04d}"
    return clauses


def _new_clause(row: Dict, idx: int, clause_no: str, title: str, sections: List[str]) -> Dict:
    return {
        "source_layer": SOURCE_LAYER,
        "source_site": SOURCE_SITE,
        "doc_id": row.get("doc_id"),
        "contract_title": row.get("title"),
        "doc_no": row.get("doc_no"),
        "scope": row.get("scope"),
        "publish_year": row.get("publish_year"),
        "publish_agencies": row.get("publish_agencies") or [],
        "category": row.get("category"),
        "region": row.get("region"),
        "url": row.get("url"),
        "clause_id": f"{row.get('doc_id')}_c{idx:04d}",
        "clause_index": idx,
        "clause_no": clause_no,
        "clause_title": title,
        "section_path": " / ".join(sections) if sections else "合同正文",
        "clause_role": infer_clause_role(sections),
        "_lines": [],
    }


def infer_clause_role(sections: List[str]) -> str:
    joined = " / ".join(sections)
    if "使用说明" in joined or joined == "说明":
        return "使用说明"
    if "专用" in joined:
        return "专用条款"
    if "通用" in joined:
        return "通用条款"
    if "附件" in joined or "附录" in joined:
        return "附件"
    if "协议书" in joined:
        return "协议书"
    return "合同正文"


CLAUSE_TYPE_RULES = [
    ("主体", ["甲方", "乙方", "当事人", "主体", "法定代表人", "委托代理人"]),
    ("标的", ["标的", "项目概况", "服务内容", "工作内容", "货物", "产品", "房屋", "工程范围"]),
    ("价款", ["价款", "价格", "费用", "报酬", "合同金额", "签约合同价", "租金", "服务费"]),
    ("付款", ["付款", "支付", "结算", "进度款", "预付款", "保证金", "押金"]),
    ("交付验收", ["交付", "交货", "验收", "移交", "交接", "收货", "竣工验收"]),
    ("质量", ["质量", "标准", "保修", "维修", "质保", "质量保证"]),
    ("期限", ["期限", "期间", "有效期", "服务期", "租赁期", "工期", "完成时间"]),
    ("违约", ["违约", "赔偿", "损失", "违约金", "责任承担"]),
    ("解除终止", ["解除", "终止", "暂停", "退出"]),
    ("争议解决", ["争议", "仲裁", "诉讼", "管辖", "法院"]),
    ("保密", ["保密", "商业秘密"]),
    ("知识产权", ["知识产权", "著作权", "专利", "商标", "许可"]),
    ("不可抗力", ["不可抗力"]),
    ("通知", ["通知", "送达", "联系方式"]),
    ("生效", ["生效", "签署", "盖章", "份数"]),
]


def normalize_clause_type(text: str) -> str:
    for typ, keys in CLAUSE_TYPE_RULES:
        if any(k in text for k in keys):
            return typ
    return "其他"


DOMAIN_RULES = [
    ("建设工程", ["建设工程", "施工", "装修", "全过程咨询", "造价咨询"]),
    ("买卖", ["买卖", "销售", "采购", "供货", "农副产品"]),
    ("租赁", ["租赁", "出租", "承租"]),
    ("服务", ["服务", "委托", "咨询", "家政", "养老", "旅游", "健身"]),
    ("运输物流", ["运输", "物流", "仓储", "保管"]),
    ("金融担保", ["借款", "担保", "保理", "融资租赁", "保险", "信托"]),
    ("知识产权", ["技术", "专利", "商标", "许可", "著作权"]),
    ("房地产", ["商品房", "房屋", "物业"]),
]


def infer_contract_domain(title: str) -> str:
    for domain, keys in DOMAIN_RULES:
        if any(k in title for k in keys):
            return domain
    return "通用"


def build_clause_outputs(paths: Dict[str, str], rows: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    clauses: List[Dict] = []
    for row in rows:
        clauses.extend(split_standard_contract_clauses(row))

    variants = build_clause_variants(clauses)
    write_jsonl(os.path.join(paths["manifest"], "all_standard_clauses.jsonl"), clauses)
    write_jsonl(os.path.join(paths["manifest"], "clause_variants.jsonl"), variants)
    _write_clause_summary(os.path.join(paths["manifest"], "standard_clause_summary.csv"), clauses)
    _rewrite_clause_markdown_dir(paths["clauses_md"], clauses)
    print(f"条款切分：{len(clauses)} 条；变体记录：{len(variants)} 条")
    return clauses, variants


def build_clause_variants(clauses: List[Dict]) -> List[Dict]:
    grouped: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for clause in clauses:
        grouped[(clause.get("contract_domain") or "通用", clause.get("normalized_clause_type") or "其他")].append(clause)

    variants = []
    for (domain, typ), items in grouped.items():
        group_id = f"{_slug(domain)}_{_slug(typ)}"
        for idx, clause in enumerate(items, start=1):
            variants.append({
                "variant_group_id": group_id,
                "variant_index": idx,
                "variant_source": "官方合同示范文本",
                "contract_domain": domain,
                "normalized_clause_type": typ,
                "source_doc_id": clause.get("doc_id"),
                "source_clause_id": clause.get("clause_id"),
                "contract_title": clause.get("contract_title"),
                "doc_no": clause.get("doc_no"),
                "clause_no": clause.get("clause_no"),
                "clause_title": clause.get("clause_title"),
                "clause_text": clause.get("clause_text"),
                "risk_posture": "中性示范",
                "fallback_level": "标准推荐",
                "url": clause.get("url"),
            })
    return variants


def _slug(text: str) -> str:
    return md5_text(text)[:8]


def _write_clause_summary(path: str, clauses: List[Dict]) -> None:
    fields = [
        "contract_domain", "normalized_clause_type", "contract_title", "doc_no",
        "clause_no", "clause_title", "clause_role", "clause_len", "url",
    ]
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in clauses:
            writer.writerow({k: c.get(k) for k in fields})


def _rewrite_clause_markdown_dir(md_dir: str, clauses: List[Dict]) -> None:
    shutil.rmtree(md_dir, ignore_errors=True)
    os.makedirs(md_dir, exist_ok=True)
    for clause in clauses:
        filename = f"{clause.get('clause_id') or md5_text(clause.get('clause_text') or '')}.md"
        save_standard_clause_markdown(clause, os.path.join(md_dir, filename))


# =========================================================================
# 五、Markdown 保存
# =========================================================================


def save_standard_contract_markdown(row: Dict, path: str) -> None:
    lines = [
        "---",
        f"source_layer: {row.get('source_layer')}",
        f"source_site: {row.get('source_site')}",
        f"scope: {row.get('scope') or ''}",
        f"title: {row.get('title') or ''}",
        f"doc_no: {row.get('doc_no') or ''}",
        f"publish_year: {row.get('publish_year') or ''}",
        f"publish_agencies: {'、'.join(row.get('publish_agencies') or [])}",
        f"category: {row.get('category') or ''}",
        f"region: {row.get('region') or ''}",
        f"url: {row.get('url') or ''}",
        f"contract_priority: {row.get('contract_priority') or ''}",
        "---",
        "",
        f"# {row.get('title') or '未命名合同示范文本'}",
        "",
        row.get("body") or "",
    ]
    if row.get("risk_tips"):
        lines.extend(["", "## 风险提示", "", row["risk_tips"]])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def save_standard_clause_markdown(clause: Dict, path: str) -> None:
    lines = [
        "---",
        f"source_layer: {clause.get('source_layer') or ''}",
        f"source_site: {clause.get('source_site') or ''}",
        f"doc_id: {clause.get('doc_id') or ''}",
        f"clause_id: {clause.get('clause_id') or ''}",
        f"contract_title: {clause.get('contract_title') or ''}",
        f"doc_no: {clause.get('doc_no') or ''}",
        f"scope: {clause.get('scope') or ''}",
        f"publish_year: {clause.get('publish_year') or ''}",
        f"publish_agencies: {'、'.join(clause.get('publish_agencies') or [])}",
        f"category: {clause.get('category') or ''}",
        f"region: {clause.get('region') or ''}",
        f"section_path: {clause.get('section_path') or ''}",
        f"clause_no: {clause.get('clause_no') or ''}",
        f"clause_title: {clause.get('clause_title') or ''}",
        f"clause_role: {clause.get('clause_role') or ''}",
        f"normalized_clause_type: {clause.get('normalized_clause_type') or ''}",
        f"contract_domain: {clause.get('contract_domain') or ''}",
        f"url: {clause.get('url') or ''}",
        "---",
        "",
        f"# {clause.get('contract_title') or '标准条款'}",
        "",
        f"- 条款：{clause.get('clause_no') or '未编号'} {clause.get('clause_title') or ''}".rstrip(),
        f"- 类型：{clause.get('normalized_clause_type') or '其他'}",
        f"- 路径：{clause.get('section_path') or '合同正文'}",
        "",
        clause.get("clause_text") or "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


__all__ = [
    "SOURCE_LAYER",
    "SOURCE_SITE",
    "VIEW_RE",
    "ensure_standard_clause_dirs",
    "extract_samr_list_items",
    "merge_list_items",
    "parse_samr_detail",
    "hydrate_attachments",
    "filter_contract_related_standard_contracts",
    "build_clause_outputs",
    "save_standard_contract_markdown",
    "read_jsonl",
    "write_jsonl",
]
