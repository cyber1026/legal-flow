"""
爬取最高人民法院官网「指导性案例」栏目（https://www.court.gov.cn/shenpan/gengduo/77.html），
全量抓取后在本地筛选与合同（合同审查场景）相关的指导性案例。

栏目结构与「司法解释」栏目同构：
  - 列表页：/shenpan/gengduo/77.html，分页 /shenpan/gengduo/77_{n}.html
  - 详情页：/shenpan/xiangqing/{id}.html，正文带结构化段落
    （关键词 / 裁判要点 / 基本案情 / 裁判结果 / 裁判理由 / 相关法条）

用法（在仓库根目录执行）：
  python scripts/crawl/crawl_court_guiding_cases.py            # 全量抓取 + 合同筛选
  python scripts/crawl/crawl_court_guiding_cases.py --crawl-only
  python scripts/crawl/crawl_court_guiding_cases.py --filter-only   # 仅重跑筛选
  python scripts/crawl/crawl_court_guiding_cases.py --no-cache
"""

import argparse
import math
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from tqdm import tqdm

from legal_crawl_common import (
    clean_line,
    clean_text,
    fetch_url,
    md5_text,
    polite_sleep,
    safe_filename,
    sha256_text,
    write_jsonl,
    read_jsonl,
)
from case_crawl_common import (
    ensure_case_dirs,
    extract_cause_of_action,
    extract_case_type,
    filter_contract_related_cases,
    save_case_markdown,
    split_case_sections,
)


BASE_URL = "https://www.court.gov.cn"
COLUMN_ID = "77"  # 指导性案例栏目号
START_URL = f"{BASE_URL}/shenpan/gengduo/{COLUMN_ID}.html"
DEFAULT_OUT_DIR = "data/legal_sources/layer2_judicial/cases/guiding"


# =========================================================================
# 列表页
# =========================================================================

def get_page_count(html: str) -> int:
    """解析分页总数：优先用「共 N 篇文章」推算，再用页面里 77_{n}.html 链接兜底取最大页。"""
    page_count = 1

    m = re.search(r"共\s*(\d+)\s*篇", html)
    if m:
        page_count = max(page_count, math.ceil(int(m.group(1)) / 20))

    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        m2 = re.search(rf"{COLUMN_ID}_(\d+)\.html", a["href"])
        if m2:
            page_count = max(page_count, int(m2.group(1)))

    return page_count


def build_list_urls(page_count: int) -> List[str]:
    urls = [START_URL]
    for page in range(2, page_count + 1):
        urls.append(f"{BASE_URL}/shenpan/gengduo/{COLUMN_ID}_{page}.html")
    return urls


def extract_list_items(list_url: str, html: str) -> List[Dict]:
    """从列表页提取详情链接（/shenpan/xiangqing/{id}.html）及标题、列表页日期。"""
    soup = BeautifulSoup(html, "lxml")
    items = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = clean_line(a.get_text(" ", strip=True))

        if not title or "xiangqing" not in href:
            continue

        detail_url = urljoin(list_url, href)
        if "court.gov.cn/shenpan/xiangqing/" not in detail_url:
            continue

        parent_text = clean_line(a.parent.get_text(" ", strip=True)) if a.parent else title
        m_date = re.search(r"(20\d{2}-\d{2}-\d{2})", parent_text)

        items.append({
            "title_from_list": title,
            "url": detail_url,
            "list_url": list_url,
            "publish_date_from_list": m_date.group(1) if m_date else None,
        })

    return items


def crawl_list_pages(paths: Dict[str, str], use_cache: bool) -> List[Dict]:
    first_html = fetch_url(
        START_URL,
        cache_path=os.path.join(paths["html"], f"list_{COLUMN_ID}.html"),
        use_cache=use_cache,
    )
    page_count = get_page_count(first_html)
    list_urls = build_list_urls(page_count)
    print(f"分页数量：{page_count}")

    all_items, seen = [], set()
    for idx, list_url in enumerate(tqdm(list_urls, desc="抓取列表页"), start=1):
        cache_name = f"list_{COLUMN_ID}.html" if idx == 1 else f"list_{COLUMN_ID}_{idx}.html"
        cache_path = os.path.join(paths["html"], cache_name)
        hit = use_cache and os.path.exists(cache_path)  # 缓存命中则不限速
        html = fetch_url(list_url, cache_path=cache_path, use_cache=use_cache)
        for item in extract_list_items(list_url, html):
            if item["url"] not in seen:
                seen.add(item["url"])
                all_items.append(item)
        if not hit:
            polite_sleep()

    write_jsonl(os.path.join(paths["manifest"], "list_items.jsonl"), all_items)
    print(f"详情链接数：{len(all_items)}")
    return all_items


# =========================================================================
# 详情页解析
# =========================================================================

def extract_title(soup: BeautifulSoup, fallback: str) -> str:
    """指导性案例详情页标题在 div.title；兜底用 <title>（去掉站点后缀）。"""
    tag = soup.find("div", class_=re.compile("title", re.I))
    if tag:
        t = clean_line(tag.get_text(" ", strip=True))
        if t:
            return t
    if soup.title:
        t = clean_line(soup.title.get_text(strip=True))
        t = re.split(r"\s*[-_]\s*中华人民共和国最高人民法院", t)[0]
        if t:
            return t
    return fallback


def get_body_lines(soup: BeautifulSoup) -> List[str]:
    """取正文行：从「打印本页」之后到「责任编辑/版权」之前。"""
    raw = soup.get_text("\n", strip=True)
    lines = [clean_line(x) for x in raw.splitlines()]
    lines = [x for x in lines if x]

    start = 0
    for i, l in enumerate(lines):
        if l == "打印本页":
            start = i + 1
            break

    end = len(lines)
    for i, l in enumerate(lines):
        if l.startswith("责任编辑") or "版权所有" in l or "京公网安备" in l or "京ICP备" in l:
            end = i
            break

    return lines[start:end] if start < end else lines


def extract_meta(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    full = soup.get_text("\n", strip=True)
    source = None
    m = re.search(r"来源[:：]\s*([^\n]+)", full)
    if m:
        source = clean_line(m.group(1))
    publish_time = None
    m = re.search(r"发布时间[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s+[0-9:]{8})?)", full)
    if m:
        publish_time = m.group(1)
    return {"source": source, "publish_time": publish_time}


def extract_case_no(title: str, body: str) -> Tuple[Optional[str], Optional[int]]:
    """提取「指导性案例N号」。"""
    m = re.search(r"指导(?:性)?案例\s*(\d+)\s*号", title) or re.search(r"指导(?:性)?案例\s*(\d+)\s*号", body)
    if m:
        return f"指导性案例{m.group(1)}号", int(m.group(1))
    return None, None


# 案号：（2018）粤73民初1099号 之类
_COURT_CASE_NO_RE = re.compile(r"[（(]\s*\d{4}\s*[）)][^\s，。；,;]{0,12}?(?:民|刑|行|执|赔|商|知)[^\s，。；,;]{0,8}?\d+号")
# 审理法院：从裁判结果里抓「XX法院」
_COURT_RE = re.compile(r"([一-龥]{2,15}?(?:人民法院|知识产权法院|海事法院|金融法院))")


def extract_court_info(judgment: str, reasoning: str) -> Tuple[Optional[str], Optional[str]]:
    """从裁判结果/理由里抽取案号与审理法院（best-effort）。"""
    text = "\n".join([judgment or "", reasoning or ""])
    case_no = None
    m = _COURT_CASE_NO_RE.search(text)
    if m:
        case_no = m.group(0)
    court = None
    m = _COURT_RE.search(judgment or text)
    if m:
        court = m.group(1)
    return case_no, court


def parse_detail_page(item: Dict, html: str) -> Dict:
    soup = BeautifulSoup(html, "lxml")
    page_title = extract_title(soup, item.get("title_from_list", ""))
    meta = extract_meta(soup)

    body_lines = get_body_lines(soup)
    body = clean_text("\n".join(body_lines))
    sections = split_case_sections(body_lines)

    keywords_text = sections.get("keywords_text", "")
    judgment = sections.get("judgment", "")
    reasoning = sections.get("reasoning", "")

    case_no, case_no_int = extract_case_no(page_title, body)
    court_case_no, court = extract_court_info(judgment, reasoning)
    cause = extract_cause_of_action(page_title)
    case_type = extract_case_type(keywords_text)

    return {
        "source_type": "指导性案例",
        "source_site": "最高人民法院",
        "case_category": "指导性案例",
        "url": item["url"],
        "list_url": item.get("list_url"),
        "title_from_list": item.get("title_from_list"),
        "page_title": page_title,
        "doc_title": page_title,
        "case_no": case_no,
        "case_no_int": case_no_int,
        "case_id": None,
        "court_case_no": court_case_no,
        "court": court,
        "cause_of_action": cause,
        "case_type": case_type,
        "source": meta.get("source"),
        "publish_time": meta.get("publish_time") or item.get("publish_date_from_list"),
        # 结构化字段
        "keywords_text": keywords_text,
        "holding": sections.get("holding", ""),
        "facts": sections.get("facts", ""),
        "judgment": judgment,
        "reasoning": reasoning,
        "relevant_statutes": sections.get("relevant_statutes", ""),
        "body": body,
        "html_sha256": sha256_text(html),
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
    }


def crawl_detail_pages(paths: Dict[str, str], list_items: List[Dict], use_cache: bool) -> List[Dict]:
    all_rows, error_rows = [], []

    for item in tqdm(list_items, desc="抓取详情页"):
        url = item["url"]
        cache_path = os.path.join(paths["html"], f"detail_{md5_text(url)}.html")
        hit = use_cache and os.path.exists(cache_path)  # 缓存命中则不限速（便于改解析后快速重跑）
        try:
            html = fetch_url(url, cache_path=cache_path, use_cache=use_cache)
            row = parse_detail_page(item, html)
            all_rows.append(row)
            save_case_markdown(
                row,
                os.path.join(paths["all_md"], safe_filename(row.get("doc_title"), url)),
            )
        except Exception as e:
            error_rows.append({"url": url, "title_from_list": item.get("title_from_list"), "error": str(e)})
        if not hit:
            polite_sleep()

    write_jsonl(os.path.join(paths["manifest"], "all_cases.jsonl"), all_rows)
    write_jsonl(os.path.join(paths["logs"], "detail_errors.jsonl"), error_rows)
    print(f"全量详情：{os.path.join(paths['root'], 'all_cases.jsonl')}（{len(all_rows)} 篇，失败 {len(error_rows)}）")
    return all_rows


# =========================================================================
# 主流程
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="爬取最高法指导性案例并筛选合同相关。")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help="输出目录")
    parser.add_argument("--no-cache", action="store_true", help="不使用本地 HTML 缓存")
    parser.add_argument("--crawl-only", action="store_true", help="只全量爬取，不筛选")
    parser.add_argument("--filter-only", action="store_true", help="只基于已有 all_cases.jsonl 重新筛选")
    args = parser.parse_args()

    paths = ensure_case_dirs(args.out)
    use_cache = not args.no_cache
    all_path = os.path.join(paths["manifest"], "all_cases.jsonl")

    if args.filter_only:
        rows = read_jsonl(all_path)
        if not rows:
            raise RuntimeError(f"未找到全量文件：{all_path}")
        filter_contract_related_cases(paths, rows)
        return

    list_items = crawl_list_pages(paths, use_cache=use_cache)
    rows = crawl_detail_pages(paths, list_items, use_cache=use_cache)

    if args.crawl_only:
        print("已完成全量爬取，未执行合同相关筛选。")
        return

    filter_contract_related_cases(paths, rows)


if __name__ == "__main__":
    main()
