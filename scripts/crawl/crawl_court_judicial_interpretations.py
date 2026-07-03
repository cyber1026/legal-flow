import argparse
import hashlib
import json
import math
import os
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


# =========================
# 基础配置
# =========================

BASE_URL = "https://www.court.gov.cn"
START_URL = "https://www.court.gov.cn/fabu/gengduo/16.html"

DEFAULT_OUT_DIR = "data/legal_sources/layer2_judicial/interpretations/spc_court"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 ContractRiskResearchCrawler/1.0 "
        "(legal research; respectful low-frequency crawling)"
    )
}

REQUEST_TIMEOUT = 20
RETRY_TIMES = 3

# 温和爬取：每次请求随机等待
MIN_SLEEP_SECONDS = 1.2
MAX_SLEEP_SECONDS = 2.8


# 合同相关筛选统一复用 legal_crawl_common（标题驱动版），避免与公报/最高检爬虫各写一套。
from legal_crawl_common import (
    classify_contract_relevance,
    filter_contract_related,
    save_markdown,
)


# =========================
# 工具函数
# =========================

def ensure_dirs(out_dir: str) -> Dict[str, str]:
    paths = {
        "root": out_dir,
        # manifest：清单/分类/摘要 jsonl+csv（与 legal_crawl_common.ensure_dirs 保持一致）
        "manifest": os.path.join(out_dir, "manifest"),
        "html": os.path.join(out_dir, "raw"),
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
    按 UTF-8 字节数截断，避免中文文件名超过 Linux 单文件名 255 bytes 限制。
    """
    b = s.encode("utf-8", errors="ignore")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore").rstrip("_- ，。；、")


def safe_filename(title: str, url: str, suffix: str = ".md") -> str:
    title = title or "untitled"

    name = re.sub(r'[\\/:*?"<>|]', "_", title)
    name = re.sub(r"\s+", "_", name).strip("_")
    name = name.strip("._- ")

    if not name:
        name = "untitled"

    url_id = md5_text(url)[:12]

    # 单个文件名通常限制 255 bytes。
    # 这里把标题部分控制在 120 bytes 内，给 hash 和后缀留空间。
    name = truncate_utf8_bytes(name, max_bytes=120)

    return f"{name}_{url_id}{suffix}"


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


# =========================
# 列表页抓取
# =========================

def get_total_articles_and_page_count(html: str) -> Tuple[Optional[int], Optional[int]]:
    total_articles = None
    page_count = None

    m = re.search(r"共\s*(\d+)\s*篇文章", html)
    if m:
        total_articles = int(m.group(1))
        page_count = math.ceil(total_articles / 20)

    soup = BeautifulSoup(html, "lxml")
    max_page = 1

    for a in soup.find_all("a", href=True):
        href = a["href"]

        m2 = re.search(r"/fabu/gengduo/16_(\d+)\.html", href)
        if m2:
            max_page = max(max_page, int(m2.group(1)))

        m3 = re.search(r"16_(\d+)\.html", href)
        if m3:
            max_page = max(max_page, int(m3.group(1)))

    if max_page > 1:
        page_count = max_page

    return total_articles, page_count


def build_list_urls(page_count: int) -> List[str]:
    urls = [START_URL]
    for page in range(2, page_count + 1):
        urls.append(f"https://www.court.gov.cn/fabu/gengduo/16_{page}.html")
    return urls


def extract_list_items(list_url: str, html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = clean_line(a.get_text(" ", strip=True))

        if not title:
            continue

        if "/fabu/xiangqing/" not in href and "xiangqing" not in href:
            continue

        detail_url = urljoin(list_url, href)

        if "court.gov.cn/fabu/xiangqing/" not in detail_url:
            continue

        parent_text = clean_line(a.parent.get_text(" ", strip=True))
        date_match = re.search(r"(20\d{2}-\d{2}-\d{2}|19\d{2}-\d{2}-\d{2})", parent_text)
        publish_date_from_list = date_match.group(1) if date_match else None

        items.append({
            "title_from_list": title,
            "url": detail_url,
            "list_url": list_url,
            "publish_date_from_list": publish_date_from_list,
        })

    return items


def crawl_list_pages(paths: Dict[str, str], use_cache: bool = True) -> List[Dict]:
    first_cache = os.path.join(paths["html"], "list_16.html")
    first_html = fetch_url(START_URL, cache_path=first_cache, use_cache=use_cache)

    total_articles, page_count = get_total_articles_and_page_count(first_html)

    if not page_count:
        raise RuntimeError("无法解析分页数量，请检查最高法栏目页面结构是否变化。")

    list_urls = build_list_urls(page_count)

    print(f"栏目文章数：{total_articles}")
    print(f"分页数量：{page_count}")

    all_items = []
    seen = set()

    for idx, list_url in enumerate(tqdm(list_urls, desc="抓取列表页"), start=1):
        cache_name = "list_16.html" if idx == 1 else f"list_16_{idx}.html"
        cache_path = os.path.join(paths["html"], cache_name)

        html = fetch_url(list_url, cache_path=cache_path, use_cache=use_cache)
        items = extract_list_items(list_url, html)

        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                all_items.append(item)

        polite_sleep()

    list_path = os.path.join(paths["manifest"], "list_items.jsonl")
    write_jsonl(list_path, all_items)

    print(f"详情链接数：{len(all_items)}")
    print(f"列表保存：{list_path}")

    return all_items


# =========================
# 详情页解析
# =========================

def extract_title(soup: BeautifulSoup, fallback: str) -> str:
    h1 = soup.find("h1")
    if h1:
        title = clean_line(h1.get_text(" ", strip=True))
        if title:
            return title

    candidates = [
        soup.find("div", class_=re.compile("title", re.I)),
        soup.find("h2"),
        soup.find("h3"),
    ]

    for tag in candidates:
        if tag:
            title = clean_line(tag.get_text(" ", strip=True))
            if title:
                return title

    return fallback


def get_all_lines(soup: BeautifulSoup) -> List[str]:
    raw_text = soup.get_text("\n", strip=True)
    lines = [clean_line(x) for x in raw_text.splitlines()]
    return [x for x in lines if x]


def extract_meta_from_lines(lines: List[str]) -> Dict[str, Optional[str]]:
    full = "\n".join(lines)

    source = None
    m = re.search(r"来源[:：]\s*([^\n]+)", full)
    if m:
        source = clean_line(m.group(1))

    publish_time = None
    m = re.search(
        r"发布时间[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s+[0-9]{2}:[0-9]{2}:[0-9]{2})?)",
        full
    )
    if m:
        publish_time = m.group(1)

    return {
        "source": source,
        "publish_time": publish_time,
    }


def extract_body_lines(lines: List[str]) -> List[str]:
    start_idx = 0

    for i, line in enumerate(lines):
        if line == "打印本页":
            start_idx = i + 1
            break

    end_idx = len(lines)

    for i, line in enumerate(lines):
        if line.startswith("责任编辑"):
            end_idx = i
            break
        if "中华人民共和国最高人民法院 版权所有" in line:
            end_idx = i
            break
        if "京公网安备" in line:
            end_idx = i
            break

    if start_idx >= end_idx:
        return lines

    return lines[start_idx:end_idx]


def find_legal_text_start(body: str) -> Optional[int]:
    """
    优先从“公告”开始切正式文本；
    如果没有公告，再根据“法释〔xxxx〕xx号”附近向前找正式标题。
    """
    announcement_patterns = [
        "中华人民共和国最高人民法院公告",
        "最高人民法院公告",
        "中华人民共和国最高人民法院 中华人民共和国最高人民检察院公告",
        "最高人民法院 最高人民检察院公告",
    ]

    for pattern in announcement_patterns:
        idx = body.find(pattern)
        if idx != -1:
            return idx

    doc_no_match = re.search(r"法释〔\d{4}〕\d+号", body)
    if doc_no_match:
        doc_no_idx = doc_no_match.start()

        window_start = max(0, doc_no_idx - 1500)
        window = body[window_start:doc_no_idx]

        title_patterns = [
            "最高人民法院关于",
            "最高人民法院、最高人民检察院关于",
            "最高人民法院 最高人民检察院关于",
        ]

        candidates = []
        for p in title_patterns:
            pos = window.rfind(p)
            if pos != -1:
                candidates.append(window_start + pos)

        if candidates:
            return min(candidates)

        return window_start

    # 部分较早页面可能没有法释文号，保守处理：不强切
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

    m = re.search(r"于([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)由最高人民法院审判委员会.*?通过", text)
    if m:
        passed_date = m.group(1)

    m = re.search(r"经[^\n]{0,30}([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)[^\n]{0,30}通过", text)
    if not passed_date and m:
        passed_date = m.group(1)

    m = re.search(r"自([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)起施行", text)
    if m:
        effective_date = m.group(1)

    return {
        "passed_date": passed_date,
        "effective_date": effective_date,
    }


def extract_doc_title(legal_text: str, page_title: str) -> str:
    lines = [clean_line(x) for x in legal_text.splitlines() if clean_line(x)]

    for i, line in enumerate(lines):
        if line in [
            "最高人民法院",
            "最高人民法院 最高人民检察院",
            "最高人民法院、最高人民检察院",
            "中华人民共和国最高人民法院",
        ]:
            title_parts = [line]

            for j in range(i + 1, min(i + 6, len(lines))):
                next_line = lines[j]

                if re.search(r"法释〔\d{4}〕\d+号", next_line):
                    break
                if next_line.startswith("（") or next_line.startswith("("):
                    break
                if re.match(r"^\d{4}年\d{1,2}月\d{1,2}日$", next_line):
                    continue
                if next_line in ["公告", "现予公布"]:
                    continue

                if next_line.startswith("关于") or title_parts[-1].startswith("关于") or "适用" in next_line:
                    title_parts.append(next_line)

            joined = "".join(title_parts)
            if "关于" in joined and len(joined) > len(line):
                return clean_line(joined)

    patterns = [
        r"(最高人民法院、最高人民检察院关于[^\n]{8,180})",
        r"(最高人民法院 最高人民检察院关于[^\n]{8,180})",
        r"(最高人民法院关于[^\n]{8,180})",
        r"(最高人民检察院关于[^\n]{8,180})",
    ]

    for p in patterns:
        m = re.search(p, legal_text)
        if m:
            title = clean_line(m.group(1))

            # 防止把“已于xx通过”“现予公布”等公告文字并入标题
            title = re.split(
                r"(已于|已经|由最高人民法院审判委员会|现予公布|自\d{4}年|法释〔)",
                title
            )[0]

            title = title.strip("《》 “”，。；;：:")
            title = clean_line(title)

            if title:
                return title

    return page_title


def parse_detail_page(item: Dict, html: str) -> Dict:
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
        "source_site": "最高人民法院",
        "column": "司法解释",
        "url": item["url"],
        "list_url": item.get("list_url"),
        "title_from_list": item.get("title_from_list"),
        "page_title": page_title,
        "doc_title": doc_title,
        "doc_no": doc_no,
        "source": meta.get("source"),
        "publish_time": meta.get("publish_time"),
        "publish_date_from_list": item.get("publish_date_from_list"),
        "passed_date": dates.get("passed_date"),
        "effective_date": dates.get("effective_date"),
        "body": body,
        "explanation_text": explanation_text,
        "legal_text": legal_text,
        "html_sha256": sha256_text(html),
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
    }


def crawl_detail_pages(paths: Dict[str, str], list_items: List[Dict], use_cache: bool = True) -> List[Dict]:
    all_rows = []
    error_rows = []

    for item in tqdm(list_items, desc="抓取详情页"):
        url = item["url"]
        html_cache_name = f"detail_{md5_text(url)}.html"
        html_cache_path = os.path.join(paths["html"], html_cache_name)

        try:
            html = fetch_url(url, cache_path=html_cache_path, use_cache=use_cache)
            row = parse_detail_page(item, html)
            all_rows.append(row)

            md_path = os.path.join(
                paths["all_md"],
                safe_filename(row.get("doc_title") or row.get("page_title"), url)
            )
            save_markdown(row, md_path)

        except Exception as e:
            error_rows.append({
                "url": url,
                "title_from_list": item.get("title_from_list"),
                "error": str(e),
            })

        polite_sleep()

    all_path = os.path.join(paths["manifest"], "all_judicial_interpretations.jsonl")
    error_path = os.path.join(paths["logs"], "detail_errors.jsonl")

    write_jsonl(all_path, all_rows)
    write_jsonl(error_path, error_rows)

    print(f"全量详情保存：{all_path}")
    print(f"全量 Markdown 保存：{paths['all_md']}")
    print(f"失败数量：{len(error_rows)}，错误日志：{error_path}")

    return all_rows



# =========================
# 主流程
# =========================

def main():
    parser = argparse.ArgumentParser(
        description="爬取最高人民法院司法解释栏目，并筛选合同法律风险审查相关司法解释。"
    )

    parser.add_argument(
        "--out",
        default=DEFAULT_OUT_DIR,
        help="输出目录"
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="不使用本地 HTML 缓存，重新请求网页"
    )

    parser.add_argument(
        "--crawl-only",
        action="store_true",
        help="只全量爬取，不执行合同相关筛选"
    )

    parser.add_argument(
        "--filter-only",
        action="store_true",
        help="只基于已有 all_judicial_interpretations.jsonl 重新筛选"
    )

    args = parser.parse_args()

    paths = ensure_dirs(args.out)
    use_cache = not args.no_cache

    all_path = os.path.join(paths["manifest"], "all_judicial_interpretations.jsonl")

    if args.filter_only:
        rows = read_jsonl(all_path)
        if not rows:
            raise RuntimeError(f"未找到全量文件：{all_path}")
        filter_contract_related(paths, rows)
        return

    # 1. 抓列表页
    list_items = crawl_list_pages(paths, use_cache=use_cache)

    # 2. 抓全部详情页
    rows = crawl_detail_pages(paths, list_items, use_cache=use_cache)

    # 3. 只全量下载，不筛选
    if args.crawl_only:
        print("已完成全量爬取，未执行合同相关筛选。")
        return

    # 4. 本地筛选合同相关司法解释
    filter_contract_related(paths, rows)


if __name__ == "__main__":
    main()