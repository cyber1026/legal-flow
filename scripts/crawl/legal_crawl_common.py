import csv
import hashlib
import json
import os
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 LegalRiskCrawler/1.0 "
        "(low-frequency legal research crawler)"
    )
}

REQUEST_TIMEOUT = 25
RETRY_TIMES = 3
MIN_SLEEP_SECONDS = 1.2
MAX_SLEEP_SECONDS = 2.8


# =========================================================================
# 司法解释「合同相关」筛选 —— 标题驱动（不碰正文）
# =========================================================================
#
# 司法解释是抽象规则文本，没有「案由」，但**标题**直接表明主题
# （「关于审理XX合同纠纷案件适用法律问题的解释」「关于适用《民法典》合同编…的解释」）。
# 旧规则对正文（legal_text/body）做粗粒度关键词匹配——正文里几乎都有「合同/履行/赔偿」字样，
# 导致刑事/程序性解释被大量误收（court 366/394、spp 75/113）。新规则只看标题，治本。
#
# 三步：① 先判定是否「真司法文书」（滤掉被误抓进来的新闻）；
#       ② 标题命中刑事/行政/婚姻家庭/侵权等 → 排除；
#       ③ 标题含「合同/协议」或合同领域关键词 → 保留。

# 文书类型词：标题形如「关于……的解释/规定/批复/意见/纪要/办法/规则/决定/安排…」
_INSTRUMENT_TITLE_RE = re.compile(
    r"关于.{1,}?(解释|规定|批复|意见|答复|纪要|办法|规则|决定|安排|复函|规程|措施)"
)
_INSTRUMENT_SUFFIXES = (
    "解释", "规定", "规则", "办法", "纪要", "安排", "批复", "意见", "决定", "规程", "复函",
)

# 排除类标题关键词（刑事/行政/婚姻家庭/侵权等，非合同审查范畴）。
# 刻意用**具体多字词**，不用裸「行政」（会误伤「特别行政区」）、不用裸「保险」（会误伤「医疗/工伤保险」）。
INTERP_EXCLUDE_KEYWORDS = [
    # 刑事
    "刑事", "刑法", "犯罪", "量刑", "减刑", "假释", "死刑", "缓刑", "自首", "立功", "累犯",
    "毒品", "走私", "贪污", "贿赂", "受贿", "行贿", "洗钱", "黑社会", "恶势力", "恐怖",
    "袭警", "强奸", "猥亵", "卖淫", "赌博", "盗窃", "抢劫", "抢夺", "绑架", "敲诈", "诈骗",
    "非法集资", "集资诈骗", "扫黑除恶", "掩饰、隐瞒", "非法占用", "非法采矿", "破坏性采矿",
    "危害", "侦查", "刑罚", "羁押", "看守所", "在押",
    # 行政 / 国家赔偿（用具体词，避免「特别行政区」误伤）。行政协议属行政法专门领域，非民商事合同审查。
    "行政诉讼", "行政处罚", "行政许可", "行政强制", "行政复议", "行政赔偿", "行政案件",
    "行政机关", "行政协议", "国家赔偿",
    # 婚姻家庭 / 未成年 / 人身侵权
    "婚姻", "离婚", "继承", "收养", "赡养", "抚养", "夫妻", "家事", "未成年", "人身损害", "工伤",
    # 侵权 / 知识产权侵权 / 不正当竞争（许可合同含「合同」会先命中保留）
    "侵权", "侵害", "不正当竞争", "植物新品种", "反垄断",
]

# 合同领域关键词（标题不含「合同/协议」字样、但仍属合同审查范畴）。均为**具体多字词**，
# 避免裸「委托/股权/运输/债权/债务/技术」误收程序性文书（如「委托评估拍卖」「强制执行股权」）。
INTERP_CONTRACT_KEYWORDS = [
    "民间借贷", "担保制度", "担保", "独立保函", "保理", "票据", "信用证",
    "保险法", "海上保险", "保险合同", "物业服务", "货运代理", "融资租赁",
    "不当得利", "无因管理", "让与担保", "债务加入", "缔约过失", "定金",
    "中小企业",  # 大型企业与中小企业款项支付（背靠背）批复
]

# 一般民事法律（适用于合同的成立/效力/代理/时效等，列为次优先 P1）。
INTERP_GENERAL_CIVIL_KEYWORDS = [
    "合同编", "总则编", "民事法律行为", "民法总则", "诉讼时效", "表见代理", "情势变更",
]


def ensure_dirs(out_dir: str) -> Dict[str, str]:
    paths = {
        "root": out_dir,
        # manifest：清单/分类/摘要 jsonl+csv（list_items/all_*/contract_related_*/summary）
        "manifest": os.path.join(out_dir, "manifest"),
        # html：原始网页/接口缓存（不入库），目录名为 raw
        "html": os.path.join(out_dir, "raw"),
        # markdown 产物按 all/contract 分层
        "all_md": os.path.join(out_dir, "markdown", "all"),
        "contract_md": os.path.join(out_dir, "markdown", "contract"),
        "logs": os.path.join(out_dir, "logs"),
    }
    for path in paths.values():
        os.makedirs(path, exist_ok=True)
    return paths


def clean_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u3000", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def md5_text(s: str) -> str:
    return hashlib.md5(s.encode("utf-8", errors="ignore")).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def truncate_utf8_bytes(s: str, max_bytes: int) -> str:
    """
    按 UTF-8 字节截断，避免中文文件名超过 Linux 单文件名 255 bytes 限制。
    """
    b = s.encode("utf-8", errors="ignore")

    if len(b) <= max_bytes:
        return s

    return b[:max_bytes].decode("utf-8", errors="ignore").rstrip("_- ，。；、")


def safe_filename_stem(title: str) -> str:
    """把标题净化为稳定的文件名主干（不含 url 哈希与后缀）。

    同一案例标题永远得到同一主干，因此可作为**跨次运行 / 跨来源**的稳定去重键——
    url 里的 id token 每次抓取都变，绝不能用 url 当键（这正是历史 markdown 重复的根因）。
    """
    title = title or "untitled"

    name = re.sub(r'[\\/:*?"<>|]', "_", title)
    name = re.sub(r"\s+", "_", name).strip("_")
    name = name.strip("._- ")

    # 防止标题里混入“已于……通过”等公告说明
    name = re.split(
        r"(已于|已经|由最高人民法院审判委员会|由最高人民检察院|现予公布|自\d{4}年|法释〔|高检发释字〔)",
        name
    )[0]

    name = name.strip("《》“”，。；;：:._- ")

    if not name:
        name = "untitled"

    # 中文 1 个字通常 3 bytes。90 bytes 大约 30 个中文字符。
    return truncate_utf8_bytes(name, max_bytes=90)


def safe_filename(title: str, url: str, suffix: str = ".md") -> str:
    """案例/文档落盘文件名：稳定主干 + url 哈希（区分同名不同篇）+ 后缀。"""
    return f"{safe_filename_stem(title)}_{md5_text(url)[:12]}{suffix}"


def polite_sleep():
    time.sleep(random.uniform(MIN_SLEEP_SECONDS, MAX_SLEEP_SECONDS))


def fetch_url(url: str, cache_path: Optional[str] = None, use_cache: bool = True) -> str:
    if use_cache and cache_path and os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read()

    last_err = None

    for attempt in range(1, RETRY_TIMES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()

            if not resp.encoding or resp.encoding.lower() in ["iso-8859-1", "ascii"]:
                resp.encoding = resp.apparent_encoding

            html = resp.text

            if cache_path:
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(html)

            return html

        except Exception as e:
            last_err = e
            wait = min(30, 2 ** attempt + random.random())
            time.sleep(wait)

    raise RuntimeError(f"fetch failed: {url}, error={last_err}")


def write_jsonl(path: str, rows: List[Dict]):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> List[Dict]:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_csv(path: str, rows: List[Dict]):
    fields = [
        "contract_priority",
        "matched_keywords",
        "doc_title",
        "doc_no",
        "publish_time",
        "effective_date",
        "source_site",
        "url",
        "classify_reason",
    ]

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        for row in rows:
            writer.writerow({
                "contract_priority": row.get("contract_priority"),
                "matched_keywords": "、".join(row.get("matched_keywords", [])),
                "doc_title": row.get("doc_title"),
                "doc_no": row.get("doc_no"),
                "publish_time": row.get("publish_time") or row.get("publish_date_from_list"),
                "effective_date": row.get("effective_date"),
                "source_site": row.get("source_site"),
                "url": row.get("url"),
                "classify_reason": row.get("classify_reason"),
            })


def get_all_lines(soup: BeautifulSoup) -> List[str]:
    raw_text = soup.get_text("\n", strip=True)
    lines = [clean_line(x) for x in raw_text.splitlines()]
    return [x for x in lines if x]


def extract_title(soup: BeautifulSoup, fallback: str = "") -> str:
    for selector in ["h1", "h2", "h3"]:
        tag = soup.find(selector)
        if tag:
            title = clean_line(tag.get_text(" ", strip=True))
            if title and len(title) >= 4:
                return title

    for cls in ["title", "article-title", "content-title", "detail-title"]:
        tag = soup.find(class_=re.compile(cls, re.I))
        if tag:
            title = clean_line(tag.get_text(" ", strip=True))
            if title and len(title) >= 4:
                return title

    return fallback


def extract_meta_from_lines(lines: List[str]) -> Dict[str, Optional[str]]:
    full = "\n".join(lines)

    source = None
    m = re.search(r"来源[:：]\s*([^\n]+)", full)
    if m:
        source = clean_line(m.group(1))

    publish_time = None

    patterns = [
        r"发布时间[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s+[0-9]{2}:[0-9]{2}:[0-9]{2})?)",
        r"发布时间[:：]\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)",
        r"发布日期[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
        r"发布日期[:：]\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)",
    ]

    for p in patterns:
        m = re.search(p, full)
        if m:
            publish_time = m.group(1)
            break

    return {
        "source": source,
        "publish_time": publish_time,
    }


def extract_body_lines(lines: List[str]) -> List[str]:
    start_idx = 0

    for i, line in enumerate(lines):
        if line in ["打印本页", "正文", "内容"]:
            start_idx = i + 1
            break

    end_idx = len(lines)

    end_markers = [
        "责任编辑",
        "相关链接",
        "上一篇",
        "下一篇",
        "Copyright",
        "Copyrights",
        "中华人民共和国最高人民法院 版权所有",
        "最高人民检察院 All Rights Reserved",
        "京公网安备",
        "未经本网书面授权",
    ]

    for i, line in enumerate(lines):
        if any(marker in line for marker in end_markers):
            end_idx = i
            break

    if start_idx >= end_idx:
        return lines

    return lines[start_idx:end_idx]


def find_legal_text_start(body: str) -> Optional[int]:
    announcement_patterns = [
        "中华人民共和国最高人民法院 中华人民共和国最高人民检察院公告",
        "中华人民共和国最高人民法院、中华人民共和国最高人民检察院公告",
        "中华人民共和国最高人民法院公告",
        "中华人民共和国最高人民检察院公告",
        "最高人民法院 最高人民检察院公告",
        "最高人民法院公告",
        "最高人民检察院公告",
    ]

    for pattern in announcement_patterns:
        idx = body.find(pattern)
        if idx != -1:
            return idx

    doc_no_match = re.search(r"法释〔\d{4}〕\d+号", body)
    if doc_no_match:
        doc_no_idx = doc_no_match.start()
        window_start = max(0, doc_no_idx - 1600)
        window = body[window_start:doc_no_idx]

        title_patterns = [
            "最高人民法院、最高人民检察院关于",
            "最高人民法院 最高人民检察院关于",
            "最高人民法院关于",
            "最高人民检察院关于",
        ]

        candidates = []
        for p in title_patterns:
            pos = window.rfind(p)
            if pos != -1:
                candidates.append(window_start + pos)

        if candidates:
            return min(candidates)

        return window_start

    title_match = re.search(
        r"(最高人民法院(?:、| )?最高人民检察院|最高人民法院|最高人民检察院)关于",
        body
    )
    if title_match:
        return title_match.start()

    return None


def split_explanation_and_legal_text(body: str) -> Tuple[str, str]:
    body = clean_text(body)
    idx = find_legal_text_start(body)

    if idx is None:
        return "", body

    explanation = body[:idx].strip()
    legal_text = body[idx:].strip()
    return explanation, legal_text


def extract_document_no(text: str) -> Optional[str]:
    patterns = [
        r"法释〔\d{4}〕\d+号",
        r"法释\[\d{4}\]\d+号",
        r"高检发释字〔\d{4}〕\d+号",
        r"高检发〔\d{4}〕\d+号",
        r"法发〔\d{4}〕\d+号",
        r"法办〔\d{4}〕\d+号",
        r"法〔\d{4}〕\d+号",
    ]

    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(0)

    return None


def extract_dates(text: str) -> Dict[str, Optional[str]]:
    passed_date = None
    effective_date = None

    patterns_passed = [
        r"于([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)由最高人民法院审判委员会.*?通过",
        r"由最高人民法院审判委员会.*?([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日).*?通过",
        r"经[^\n]{0,50}([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)[^\n]{0,80}通过",
    ]

    for p in patterns_passed:
        m = re.search(p, text)
        if m:
            passed_date = m.group(1)
            break

    m = re.search(r"自([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)起施行", text)
    if m:
        effective_date = m.group(1)

    return {
        "passed_date": passed_date,
        "effective_date": effective_date,
    }


def extract_doc_title(legal_text: str, page_title: str) -> str:
    text = legal_text or ""
    lines = [clean_line(x) for x in text.splitlines() if clean_line(x)]

    for i, line in enumerate(lines):
        if line in [
            "最高人民法院",
            "最高人民检察院",
            "最高人民法院 最高人民检察院",
            "最高人民法院、最高人民检察院",
            "中华人民共和国最高人民法院",
            "中华人民共和国最高人民检察院",
            "中华人民共和国最高人民法院 中华人民共和国最高人民检察院",
        ]:
            title_parts = [line]

            for j in range(i + 1, min(i + 8, len(lines))):
                next_line = lines[j]

                if re.search(r"法释〔\d{4}〕\d+号", next_line):
                    break
                if next_line.startswith("（") or next_line.startswith("("):
                    break
                if re.match(r"^\d{4}年\d{1,2}月\d{1,2}日$", next_line):
                    continue
                if next_line in ["公告", "现予公布"]:
                    continue

                if next_line.startswith("关于") or "适用" in next_line:
                    title_parts.append(next_line)
                    break

            joined = "".join(title_parts)
            if "关于" in joined and len(joined) > len(line):
                return clean_line(joined)

    patterns = [
        r"(最高人民法院、最高人民检察院关于[^\n]{8,160})",
        r"(最高人民法院 最高人民检察院关于[^\n]{8,160})",
        r"(最高人民法院关于[^\n]{8,160})",
        r"(最高人民检察院关于[^\n]{8,160})",
    ]

    for p in patterns:
        m = re.search(p, text)
        if m:
            title = clean_line(m.group(1))

            # 防止把“已于xx通过”“现予公布”“自xx施行”等公告文字并入标题
            title = re.split(
                r"(已于|已经|由最高人民法院审判委员会|由最高人民检察院|现予公布|自\d{4}年|法释〔|高检发释字〔)",
                title
            )[0]

            title = title.strip("《》“”，。；;：: ")
            title = clean_line(title)

            if title:
                return title

    return page_title


def parse_detail_html(
    item: Dict,
    html: str,
    source_site: str,
    column: str,
) -> Dict:
    soup = BeautifulSoup(html, "lxml")
    lines = get_all_lines(soup)

    page_title = extract_title(soup, item.get("title_from_list", ""))
    meta = extract_meta_from_lines(lines)

    body_lines = extract_body_lines(lines)
    body = clean_text("\n".join(body_lines))

    explanation_text, legal_text = split_explanation_and_legal_text(body)

    text_for_meta = legal_text or body
    doc_title = extract_doc_title(text_for_meta, page_title)
    doc_no = extract_document_no(text_for_meta)
    dates = extract_dates(text_for_meta)

    return {
        "source_layer": "第二层：司法解释/司法政策/指导案例",
        "source_site": source_site,
        "column": column,
        "url": item["url"],
        "list_url": item.get("list_url"),
        "title_from_list": item.get("title_from_list"),
        "page_title": page_title,
        "doc_title": doc_title,
        "doc_no": doc_no,
        "source": meta.get("source"),
        "publish_time": meta.get("publish_time") or item.get("publish_date_from_list"),
        "publish_date_from_list": item.get("publish_date_from_list"),
        "issue": item.get("issue"),
        "passed_date": dates.get("passed_date"),
        "effective_date": dates.get("effective_date"),
        "body": body,
        "explanation_text": explanation_text,
        "legal_text": legal_text,
        "html_sha256": sha256_text(html),
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
    }


def is_real_interpretation(row: Dict) -> bool:
    """
    判定一条记录是否为「真正的司法文书」（解释/规定/批复/意见/纪要/办法/规则/决定/安排…）。
    用于滤掉被误抓进来的新闻、检察官任命公告等噪声（尤其最高检来源约 77/113 是新闻）。

    判据：有法释/高检发文号即必是文书；或标题形如「关于……的解释/规定…」；或以文书类型词结尾。
    """
    if row.get("doc_no"):
        return True
    for t in (row.get("doc_title"), row.get("title_from_list"), row.get("page_title")):
        t = clean_line(t or "")
        if not t:
            continue
        if _INSTRUMENT_TITLE_RE.search(t):
            return True
        if t.endswith(_INSTRUMENT_SUFFIXES):
            return True
    return False


def classify_contract_relevance(row: Dict) -> Dict:
    """
    判断单条司法解释是否与合同（合同审查场景）相关——**只看标题，不碰正文**。

    优先级（从高到低）：
      DROP_NONINSTRUMENT  非司法文书（新闻/公告噪声）
      DROP（刑事/行政等）  标题命中刑事/行政/婚姻家庭/侵权等排除词
      P0_CONTRACT  标题含「合同/协议」或合同领域关键词（民间借贷/担保/票据/保险法…）
      P1_CIVIL     标题含一般民事法律（合同编/总则编/民事法律行为/诉讼时效…）
      DROP         其余（程序性/破产/知识产权侵权等）
    """
    # 匹配文本：仅标题（正式标题 + 网页标题 + 列表标题），不含正文
    match_text = "\n".join([
        clean_line(row.get("doc_title") or ""),
        clean_line(row.get("title_from_list") or ""),
        clean_line(row.get("page_title") or ""),
    ])

    keep, priority, reason = False, "DROP", ""
    matched: List[str] = []

    exclude_hits = [kw for kw in INTERP_EXCLUDE_KEYWORDS if kw in match_text]
    contract_hits = [kw for kw in INTERP_CONTRACT_KEYWORDS if kw in match_text]
    civil_hits = [kw for kw in INTERP_GENERAL_CIVIL_KEYWORDS if kw in match_text]
    has_hetong = ("合同" in match_text) or ("协议" in match_text)

    if not is_real_interpretation(row):
        priority = "DROP_NONINSTRUMENT"
        reason = "非司法文书（疑似新闻/公告噪声）"
    elif exclude_hits:
        # 刑事/行政/婚姻家庭/侵权——非合同审查范畴（优先于「合同」字样，覆盖合同诈骗等刑事解释）
        reason = f"标题命中排除类关键词：{'、'.join(exclude_hits)}"
        matched = exclude_hits
    elif has_hetong or contract_hits:
        keep, priority = True, "P0_CONTRACT"
        matched = (["合同/协议"] if has_hetong else []) + contract_hits
        reason = f"标题含合同信号：{'、'.join(matched)}"
    elif civil_hits:
        keep, priority = True, "P1_CIVIL"
        matched = civil_hits
        reason = f"标题含合同适用的一般民事法律：{'、'.join(civil_hits)}"
    else:
        reason = "标题无合同信号"

    return {
        **row,
        "contract_related": keep,
        "contract_priority": priority,
        "classify_reason": reason,
        "matched_keywords": matched,
    }


def filter_contract_related(paths: Dict[str, str], rows: List[Dict]) -> List[Dict]:
    classified = [classify_contract_relevance(row) for row in rows]
    related = [row for row in classified if row["contract_related"]]

    order = {"P0_CONTRACT": 0, "P1_CIVIL": 1, "DROP": 99}

    related.sort(
        key=lambda x: (
            order.get(x.get("contract_priority"), 99),
            x.get("publish_time") or "",
            x.get("doc_title") or x.get("page_title") or "",
        )
    )

    classified_path = os.path.join(paths["manifest"], "classified_all.jsonl")
    related_path = os.path.join(paths["manifest"], "contract_related_judicial_interpretations.jsonl")
    summary_csv_path = os.path.join(paths["manifest"], "contract_related_summary.csv")

    write_jsonl(classified_path, classified)
    write_jsonl(related_path, related)
    write_csv(summary_csv_path, related)

    # 清空合同相关 Markdown 目录后重写，避免历次运行（规则收紧后）的陈旧文件残留
    import glob
    for old in glob.glob(os.path.join(paths["contract_md"], "*.md")):
        os.remove(old)

    markdown_error_rows = []

    for row in related:
        try:
            md_path = os.path.join(
                paths["contract_md"],
                safe_filename(row.get("doc_title") or row.get("page_title"), row["url"])
            )
            save_markdown(row, md_path)
        except Exception as e:
            markdown_error_rows.append({
                "url": row.get("url"),
                "doc_title": row.get("doc_title"),
                "page_title": row.get("page_title"),
                "error": str(e),
            })

    if markdown_error_rows:
        markdown_error_path = os.path.join(paths["logs"], "contract_markdown_errors.jsonl")
        write_jsonl(markdown_error_path, markdown_error_rows)
        print(f"合同相关 Markdown 保存失败数量：{len(markdown_error_rows)}，日志：{markdown_error_path}")

    from collections import Counter
    dist = Counter(r["contract_priority"] for r in classified)

    print(f"全部分类结果保存：{classified_path}")
    print(f"合同相关结果保存：{related_path}")
    print(f"合同相关摘要 CSV：{summary_csv_path}")
    print(f"合同相关 Markdown 保存：{paths['contract_md']}")
    print(f"合同相关数量：{len(related)} / {len(classified)}")
    print(f"优先级分布：{dict(dist)}")

    return related


def save_markdown(row: Dict, path: str):
    legal_text = row.get("legal_text") or ""
    explanation_text = row.get("explanation_text") or ""

    matched_keywords = row.get("matched_keywords", [])

    content = f"""# {row.get("doc_title") or row.get("page_title")}

## 元数据

- 来源层级：{row.get("source_layer")}
- 来源网站：{row.get("source_site")}
- 栏目：{row.get("column")}
- 网页标题：{row.get("page_title")}
- 正式标题：{row.get("doc_title")}
- 文号：{row.get("doc_no")}
- 来源：{row.get("source")}
- 发布时间：{row.get("publish_time") or row.get("publish_date_from_list")}
- 公报期次：{row.get("issue")}
- 通过日期：{row.get("passed_date")}
- 施行日期：{row.get("effective_date")}
- 原文链接：{row.get("url")}

## 合同相关分类

- 是否合同相关：{row.get("contract_related")}
- 优先级：{row.get("contract_priority")}
- 分类原因：{row.get("classify_reason")}
- 命中关键词：{"、".join(matched_keywords) if matched_keywords else ""}

---

## 正式司法解释 / 批复 / 规定正文

{legal_text}

---

## 新闻解读 / 背景说明

{explanation_text}

---

## 页面正文备份

{row.get("body") or ""}
"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)