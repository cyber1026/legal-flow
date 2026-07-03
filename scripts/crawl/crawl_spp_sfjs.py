import argparse
import os
import re
from typing import Dict, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from tqdm import tqdm

from legal_crawl_common import (
    ensure_dirs,
    fetch_url,
    polite_sleep,
    clean_line,
    md5_text,
    write_jsonl,
    read_jsonl,
    safe_filename,
    parse_detail_html,
    save_markdown,
    filter_contract_related,
)


DEFAULT_BASE_DIR = "https://www.spp.gov.cn/spp/sfjs/"
DEFAULT_OUT_DIR = "data/legal_sources/layer2_judicial/interpretations/spp"


def build_spp_index_urls(base_dir: str, max_pages: int) -> List[str]:
    """
    最高检旧版司法解释页常见分页：
    index.shtml
    index_1.shtml
    index_2.shtml
    ...

    你给的 index_2.shtml 属于这一类分页。
    """
    base_dir = base_dir.rstrip("/") + "/"

    urls = [urljoin(base_dir, "index.shtml")]

    for i in range(1, max_pages + 1):
        urls.append(urljoin(base_dir, f"index_{i}.shtml"))

    return urls


def extract_list_items(list_url: str, html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []

    nav_titles = {
        "首页", "最高检新闻", "地方动态", "通知公告", "直播访谈", "图片",
        "专题", "法律规章", "权威发布", "宪法", "法律", "司法解释",
        "规范文件", "内部规章", "办事服务", "法律法规"
    }

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = clean_line(a.get_text(" ", strip=True))

        if not title or title in nav_titles or len(title) < 6:
            continue

        if not href.endswith(".shtml"):
            continue

        if "index" in href and "t20" not in href:
            continue

        detail_url = urljoin(list_url, href)

        if "www.spp.gov.cn" not in detail_url:
            continue

        parent_text = clean_line(a.parent.get_text(" ", strip=True)) if a.parent else title

        publish_date_from_list = None
        m_date = re.search(r"(20\d{2}-\d{2}-\d{2}|20\d{2}年\d{1,2}月\d{1,2}日)", parent_text)
        if m_date:
            publish_date_from_list = m_date.group(1)

        # 列表项通常都有日期；没有日期的，多数是导航链接，保守跳过
        if not publish_date_from_list and "t20" not in detail_url:
            continue

        items.append({
            "title_from_list": title,
            "url": detail_url,
            "list_url": list_url,
            "publish_date_from_list": publish_date_from_list,
            "issue": None,
        })

    return items


def crawl_list_pages(paths: Dict[str, str], base_dir: str, max_pages: int, use_cache: bool) -> List[Dict]:
    urls = build_spp_index_urls(base_dir, max_pages=max_pages)

    all_items = []
    seen_detail_urls = set()
    empty_or_failed_count = 0

    for idx, list_url in enumerate(tqdm(urls, desc="抓取最高检列表页")):
        cache_name = f"list_{md5_text(list_url)}.html"
        cache_path = os.path.join(paths["html"], cache_name)

        try:
            html = fetch_url(list_url, cache_path=cache_path, use_cache=use_cache)
        except Exception as e:
            print(f"[列表页失败] {list_url}: {e}")
            empty_or_failed_count += 1
            if empty_or_failed_count >= 3:
                break
            continue

        items = extract_list_items(list_url, html)

        if not items:
            empty_or_failed_count += 1
        else:
            empty_or_failed_count = 0

        for item in items:
            if item["url"] not in seen_detail_urls:
                seen_detail_urls.add(item["url"])
                all_items.append(item)

        # 连续 3 页没有有效列表项，认为分页结束
        if idx > 0 and empty_or_failed_count >= 3:
            break

        polite_sleep()

    list_path = os.path.join(paths["manifest"], "list_items.jsonl")
    write_jsonl(list_path, all_items)

    print(f"发现详情链接数量：{len(all_items)}")
    print(f"列表保存：{list_path}")

    return all_items


def crawl_detail_pages(paths: Dict[str, str], list_items: List[Dict], use_cache: bool) -> List[Dict]:
    all_rows = []
    error_rows = []

    for item in tqdm(
        list_items,
        desc="抓取最高检司法解释详情页",
        dynamic_ncols=True,
        ascii=True,
    ):
        url = item["url"]
        cache_name = f"detail_{md5_text(url)}.html"
        cache_path = os.path.join(paths["html"], cache_name)

        try:
            html = fetch_url(url, cache_path=cache_path, use_cache=use_cache)
            row = parse_detail_html(
                item=item,
                html=html,
                source_site="最高人民检察院",
                column="司法解释",
            )
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


def main():
    parser = argparse.ArgumentParser(
        description="爬取最高人民检察院司法解释，并筛选合同法律风险审查相关内容。"
    )

    parser.add_argument(
        "--base-dir",
        default=DEFAULT_BASE_DIR,
        help=(
            "最高检司法解释栏目目录。默认旧版："
            "https://www.spp.gov.cn/spp/sfjs/。"
            "也可改为新版："
            "https://www.spp.gov.cn/spp/flfg/sfjs/"
        )
    )

    parser.add_argument(
        "--out",
        default=DEFAULT_OUT_DIR,
        help="输出目录"
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=20,
        help="最多探测分页数量，默认 20"
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="不使用本地 HTML 缓存，重新请求网页"
    )

    parser.add_argument(
        "--crawl-only",
        action="store_true",
        help="只全量爬取，不筛选合同相关"
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

    list_items = crawl_list_pages(
        paths=paths,
        base_dir=args.base_dir,
        max_pages=args.max_pages,
        use_cache=use_cache,
    )

    rows = crawl_detail_pages(
        paths=paths,
        list_items=list_items,
        use_cache=use_cache,
    )

    if args.crawl_only:
        print("已完成全量爬取，未执行合同相关筛选。")
        return

    filter_contract_related(paths, rows)


if __name__ == "__main__":
    main()