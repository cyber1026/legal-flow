"""
爬取中华全国律师协会（acla.org.cn）的「律师业务操作指引」，本地筛选合同相关。

全国律协**没有**统一的「操作指引」索引：指引散落在 `/catalog/<hash>` 混合栏目里
（「业务进阶」混实务文章/案例评析，「全国律协动态」混新闻/会议/老指引发布）。故策略为：
  1. 始终抓 6 个优先种子（5 个 /info 页 + 1 个 .DOC 附件）；
  2. 扫描栏目页（`?pageNumber=N` 翻页），默认抓取栏目全部详情，再本地筛合同相关；
  3. 解析详情（正文内联）；DOC 附件下载存档 + 纯 Python 抽文本（olefile，见 guide_crawl_common）。

早期版本只扫 15 页且只保留标题像「操作指引」的条目，会漏掉大量全国律协「业务进阶」里的合同
实务文章，也扫不到较深分页中的历史指引。当前版本会自动读取栏目最大页数，并允许用
`--guide-title-only` 恢复旧的严格标题过滤。

详情页结构见 guide_crawl_common.parse_acla_detail。

用法（仓库根目录执行）：
  python scripts/crawl/crawl_acla_guides.py                  # 种子 + 栏目扫描 + 解析 + 合同筛选
  python scripts/crawl/crawl_acla_guides.py --catalog-pages 20
  python scripts/crawl/crawl_acla_guides.py --seeds-only     # 只抓优先种子
  python scripts/crawl/crawl_acla_guides.py --filter-only
"""

import argparse
import os
import re
from datetime import datetime
from typing import Dict, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from tqdm import tqdm

from legal_crawl_common import (
    clean_line,
    fetch_url,
    md5_text,
    polite_sleep,
    safe_filename,
    write_jsonl,
    read_jsonl,
)
from guide_crawl_common import (
    download_file,
    ensure_guide_dirs,
    extract_file_text,
    extract_passed_and_trial,
    filter_contract_related_guides,
    looks_like_guide,
    parse_acla_detail,
    save_guide_markdown,
)


BASE_URL = "https://www.acla.org.cn"
DEFAULT_OUT_DIR = "data/legal_sources/layer3_playbooks/acla"

# 优先种子：5 个 /info 详情页 + 1 个直链 .DOC 附件。
SEED_INFO_URLS = [
    "https://www.acla.org.cn/info/babca008c2ba43e9829b3192ed5f13a9",
    "https://www.acla.org.cn/info/6ac4bfc62cdf427a8bb9822bfe3ce1f7",
    "https://www.acla.org.cn/info/c8e2a90999394156a9bf57a459e74299",
    "https://www.acla.org.cn/info/dd085166c6ba4f4991a0551944c2cd31",
    "https://www.acla.org.cn/info/505ba22a0d2d4b8fb8c30f63c1192ed7",
]
SEED_DOC_URLS = [
    "https://www.acla.org.cn/upload/media/2017-11-14/1157bbb54a86fd84fb9f7f15d899ecbe.DOC",
]

# 待扫描的栏目（指引散落其中）。业务进阶=实务文章/指引；全国律协动态=动态+老指引发布。
CATALOG_URLS = [
    "https://www.acla.org.cn/catalog/05fb418662344013b9fe273c025db721",  # 业务进阶
    "https://www.acla.org.cn/catalog/f382bd29b5354881a75392192d28b664",  # 全国律协动态
]

_INFO_HASH_RE = re.compile(r"/info/([0-9a-f]{32})")
_DATE_RE = re.compile(r"20\d{2}-\d{1,2}-\d{1,2}")
_MAX_PAGE_RE = re.compile(r"pageNumber\s*>\s*(\d+)")
_PAGE_LINK_RE = re.compile(r"pageNumber=(\d+)")


# =========================================================================
# 栏目页扫描（默认保留全部候选，详情解析后再筛合同相关）
# =========================================================================

def extract_catalog_items(list_url: str, html: str, *, guide_title_only: bool = False) -> List[Dict]:
    """从栏目页 div.article-list-item 提取条目（每项首个 /info 锚点为标题）。

    默认不再做「标题像指引」过滤。全国律协业务进阶栏目里有大量高价值合同实务文章，
    标题并不包含「操作指引/业务指引」，应先抓详情，再用合同相关分类器筛掉无关项。
    """
    soup = BeautifulSoup(html, "lxml")
    items, seen = [], set()

    for box in soup.select("div.article-list-item"):
        a = box.find("a", href=_INFO_HASH_RE)
        if not a:
            continue
        title = clean_line(a.get_text(" ", strip=True))
        detail_url = urljoin(list_url, a["href"])
        h = _INFO_HASH_RE.search(detail_url)
        if not h or h.group(1) in seen:
            continue
        if guide_title_only and not looks_like_guide(title):  # 可选：恢复旧的严格指引标题过滤
            continue
        seen.add(h.group(1))
        m_date = _DATE_RE.search(box.get_text(" ", strip=True))
        items.append({
            "title_from_list": title,
            "url": detail_url,
            "list_url": list_url,
            "publish_date_from_list": m_date.group(0) if m_date else None,
            "is_priority_seed": False,
        })
    return items


def extract_catalog_max_page(html: str, fallback: int) -> int:
    """从分页 HTML 中识别最大页数，识别不到时用 fallback。"""
    nums = [int(x) for x in _PAGE_LINK_RE.findall(html or "")]
    nums.extend(int(x) for x in _MAX_PAGE_RE.findall(html or ""))
    return max(nums) if nums else fallback


def crawl_catalogs(
    paths: Dict[str, str],
    use_cache: bool,
    catalog_pages: int,
    *,
    auto_pages: bool = True,
    guide_title_only: bool = False,
) -> List[Dict]:
    """扫描各栏目页，收集候选条目。

    `catalog_pages` 是防御性上限；`auto_pages=True` 时会从第 1 页分页控件读取栏目真实最大页数。
    """
    found, seen_hash = [], set()
    for cat_url in CATALOG_URLS:
        cat_id = cat_url.rstrip("/").split("/")[-1][:12]
        max_pages = catalog_pages
        if auto_pages:
            first_cache = os.path.join(paths["html"], f"catalog_{cat_id}_p1.html")
            first_html = fetch_url(cat_url, cache_path=first_cache, use_cache=use_cache)
            max_pages = min(extract_catalog_max_page(first_html, catalog_pages), catalog_pages)

        for page in tqdm(range(1, max_pages + 1), desc=f"扫描栏目 {cat_id}"):
            page_url = cat_url if page == 1 else f"{cat_url}?pageNumber={page}"
            cache_path = os.path.join(paths["html"], f"catalog_{cat_id}_p{page}.html")
            hit = use_cache and os.path.exists(cache_path)
            try:
                html = fetch_url(page_url, cache_path=cache_path, use_cache=use_cache)
            except Exception:
                break  # 翻过尾页等
            page_items = extract_catalog_items(page_url, html, guide_title_only=guide_title_only)
            for it in page_items:
                h = _INFO_HASH_RE.search(it["url"]).group(1)
                if h not in seen_hash:
                    seen_hash.add(h)
                    found.append(it)
            if not hit:
                polite_sleep()
    mode = "指引型标题" if guide_title_only else "全部栏目候选"
    print(f"栏目扫描命中{mode}条目：{len(found)}")
    return found


# =========================================================================
# 优先种子条目
# =========================================================================

def seed_info_items() -> List[Dict]:
    return [{"url": u, "list_url": "priority_seed", "is_priority_seed": True} for u in SEED_INFO_URLS]


# =========================================================================
# 优先种子 DOC（直链附件）
# =========================================================================
# 下载与文本抽取（PDF / 老 DOC / DOCX）统一在 guide_crawl_common：download_file / extract_file_text。

def row_from_doc_seed(doc_url: str, paths: Dict[str, str]) -> Dict:
    """把直链 DOC 种子下载并解析为一条指引 row。"""
    local = download_file(doc_url, paths["attachments"])
    text = extract_file_text(local) if local else ""
    if not text:
        print(f"  警告：DOC 抽取失败，已存原文件：{local}")
    # 标题取抽取文本首个像标题的行，兜底用文件名
    title = ""
    for line in (text or "").splitlines():
        line = clean_line(line)
        if len(line) >= 6 and ("指引" in line or "操作" in line or "合同" in line):
            title = line
            break
    if not title:
        title = f"全国律协业务指引（DOC附件 {os.path.basename(doc_url)}）"
    passed_date, trial = extract_passed_and_trial(text)
    return {
        "source_layer": "第三层：实务审查规则（律协业务操作指引）",
        "source_site": "www.acla.org.cn",
        "association": "全国律协",
        "url": doc_url,
        "list_url": "priority_seed",
        "category": None,
        "is_priority_seed": True,
        "title": title,
        "committee": None,
        "author": "全国律协",
        "source": "全国律协",
        "publish_date": None,
        "passed_date": passed_date,
        "trial_period": trial,
        "attachments": [{"name": os.path.basename(doc_url), "url": doc_url,
                         "local_path": local, "text_extracted": bool(text)}],
        "body": text,
        "body_len": len(text),
        "html_sha256": None,
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
    }


# =========================================================================
# 详情页抓取
# =========================================================================

def crawl_info_details(paths: Dict[str, str], items: List[Dict], use_cache: bool) -> List[Dict]:
    rows, errors = [], []
    for item in tqdm(items, desc="抓取ACLA详情页"):
        url = item["url"]
        cache_path = os.path.join(paths["html"], f"detail_{md5_text(url)}.html")
        hit = use_cache and os.path.exists(cache_path)
        try:
            html = fetch_url(url, cache_path=cache_path, use_cache=use_cache)
            row = parse_acla_detail(item, html)
            row["crawl_time"] = datetime.now().isoformat(timespec="seconds")
            # 下载正文里的附件；正文为空时用附件文本（PDF/DOC）补内容
            for att in row.get("attachments") or []:
                local = download_file(att["url"], paths["attachments"], referer=url)
                att["local_path"] = local
                if local and not row.get("body"):
                    txt = extract_file_text(local)
                    if txt:
                        row["body"], row["body_len"], att["text_extracted"] = txt, len(txt), True
            rows.append(row)
            save_guide_markdown(row, os.path.join(paths["all_md"], safe_filename(row.get("title"), url)))
        except Exception as e:  # 不静默吞错
            errors.append({"url": url, "error": str(e)})
        if not hit:
            polite_sleep()

    write_jsonl(os.path.join(paths["logs"], "detail_errors.jsonl"), errors)
    print(f"/info 详情解析：{len(rows)} 篇，失败 {len(errors)}")
    return rows


# =========================================================================
# 主流程
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="爬取全国律协业务操作指引并筛选合同相关。")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help="输出目录")
    parser.add_argument("--catalog-pages", type=int, default=500, help="每个栏目扫描的最大页数（auto-pages 下作为上限）")
    parser.add_argument("--no-auto-pages", action="store_true", help="不自动读取栏目最大页数，严格按 --catalog-pages 扫")
    parser.add_argument("--guide-title-only", action="store_true", help="只抓标题像正式业务操作指引的栏目条目（旧策略）")
    parser.add_argument("--seeds-only", action="store_true", help="只抓优先种子，不扫栏目")
    parser.add_argument("--no-cache", action="store_true", help="不使用本地 HTML 缓存")
    parser.add_argument("--crawl-only", action="store_true", help="只全量爬取，不筛选")
    parser.add_argument("--filter-only", action="store_true", help="只基于已有 all_guides.jsonl 重新筛选")
    args = parser.parse_args()

    paths = ensure_guide_dirs(args.out)
    use_cache = not args.no_cache
    all_path = os.path.join(paths["manifest"], "all_guides.jsonl")

    if args.filter_only:
        rows = read_jsonl(all_path)
        if not rows:
            raise RuntimeError(f"未找到全量文件：{all_path}")
        filter_contract_related_guides(paths, rows)
        return

    # 1) 汇总 /info 条目：优先种子 + 栏目扫描（去重，种子优先标记）
    items, seen = [], set()
    for it in seed_info_items():
        h = _INFO_HASH_RE.search(it["url"])
        key = h.group(1) if h else it["url"]
        seen.add(key)
        items.append(it)
    if not args.seeds_only:
        for it in crawl_catalogs(
            paths,
            use_cache,
            args.catalog_pages,
            auto_pages=not args.no_auto_pages,
            guide_title_only=args.guide_title_only,
        ):
            h = _INFO_HASH_RE.search(it["url"]).group(1)
            if h not in seen:
                seen.add(h)
                items.append(it)
    write_jsonl(os.path.join(paths["manifest"], "list_items.jsonl"), items)
    print(f"待抓 /info 指引条目：{len(items)}（含优先种子 {len(SEED_INFO_URLS)}）")

    # 2) 抓 /info 详情 + DOC 种子
    rows = crawl_info_details(paths, items, use_cache=use_cache)
    for doc_url in SEED_DOC_URLS:
        rows.append(row_from_doc_seed(doc_url, paths))

    write_jsonl(all_path, rows)
    print(f"全量指引：all_guides.jsonl（{len(rows)} 篇）")

    if args.crawl_only:
        print("已完成全量爬取，未执行合同相关筛选。")
        return

    filter_contract_related_guides(paths, rows)


if __name__ == "__main__":
    main()
