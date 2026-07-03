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


START_URL = "http://gongbao.court.gov.cn/ArticleList.html?serial_no=sfjs"
DEFAULT_OUT_DIR = "data/legal_sources/layer2_judicial/interpretations/spc_gazette"


def normalize_gongbao_url(url: str) -> str:
    if url.startswith("https://gongbao.court.gov.cn"):
        return url.replace("https://", "http://", 1)
    return url


def extract_list_items(list_url: str, html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = clean_line(a.get_text(" ", strip=True))

        if not title or len(title) < 4:
            continue

        if "Details/" not in href and "/Details/" not in href:
            continue

        detail_url = urljoin(list_url, href)
        detail_url = normalize_gongbao_url(detail_url)

        if "gongbao.court.gov.cn/Details/" not in detail_url:
            continue

        parent_text = clean_line(a.parent.get_text(" ", strip=True)) if a.parent else title

        issue = None
        m_issue = re.search(r"(20\d{2}年\d{1,2}期)", parent_text)
        if m_issue:
            issue = m_issue.group(1)

        publish_date_from_list = None
        m_date = re.search(r"(20\d{2}-\d{2}-\d{2}|20\d{2}年\d{1,2}月\d{1,2}日)", parent_text)
        if m_date:
            publish_date_from_list = m_date.group(1)

        items.append({
            "title_from_list": title,
            "url": detail_url,
            "list_url": list_url,
            "issue": issue,
            "publish_date_from_list": publish_date_from_list,
        })

    return items


def extract_pagination_links(list_url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = []

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "ArticleList.html" not in href:
            continue

        if "serial_no=sfjs" not in href:
            continue

        abs_url = urljoin(list_url, href)
        abs_url = normalize_gongbao_url(abs_url)
        urls.append(abs_url)

    return sorted(set(urls))


def build_probe_urls(max_probe_pages: int) -> List[str]:
    """
    公报站点可能存在分页参数，但不同年份页面结构可能变化。
    这里做保守探测：如果这些参数无效，后续会因为详情链接重复而自动去重。
    """
    urls = [START_URL]

    if max_probe_pages <= 0:
        return urls

    param_names = ["page", "PageIndex", "pageIndex", "currentPage"]

    for p in range(1, max_probe_pages + 1):
        for param in param_names:
            urls.append(f"{START_URL}&{param}={p}")

    return urls


def crawl_list_pages(paths: Dict[str, str], use_cache: bool, max_probe_pages: int) -> List[Dict]:
    to_visit = build_probe_urls(max_probe_pages)
    visited = set()
    seen_detail_urls = set()
    all_items = []

    pbar = tqdm(
        total=len(to_visit),
        desc="抓取最高法公报列表页",
        dynamic_ncols=True,
        ascii=True,
    )

    while to_visit:
        list_url = to_visit.pop(0)
        list_url = normalize_gongbao_url(list_url)

        if list_url in visited:
            pbar.update(1)
            continue

        visited.add(list_url)

        cache_name = f"list_{md5_text(list_url)}.html"
        cache_path = os.path.join(paths["html"], cache_name)

        try:
            html = fetch_url(list_url, cache_path=cache_path, use_cache=use_cache)
        except Exception as e:
            tqdm.write(f"[列表页失败] {list_url}: {e}")
            pbar.update(1)
            continue

        items = extract_list_items(list_url, html)

        old_count = len(all_items)

        for item in items:
            if item["url"] not in seen_detail_urls:
                seen_detail_urls.add(item["url"])
                all_items.append(item)

        new_count = len(all_items) - old_count

        for next_url in extract_pagination_links(list_url, html):
            if next_url not in visited and next_url not in to_visit:
                to_visit.append(next_url)
                pbar.total += 1

        pbar.set_postfix({
            "visited": len(visited),
            "items": len(all_items),
            "new": new_count,
        })

        pbar.update(1)
        polite_sleep()

    pbar.close()

    list_path = os.path.join(paths["manifest"], "list_items.jsonl")
    write_jsonl(list_path, all_items)

    print(f"已访问列表页数量：{len(visited)}")
    print(f"发现详情链接数量：{len(all_items)}")
    print(f"列表保存：{list_path}")

    return all_items


def crawl_detail_pages(paths: Dict[str, str], list_items: List[Dict], use_cache: bool) -> List[Dict]:
    all_rows = []
    error_rows = []

    for item in tqdm(
        list_items,
        desc="抓取最高法公报详情页",
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
                source_site="最高人民法院公报",
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
        description="爬取最高人民法院公报司法解释，并筛选合同法律风险审查相关内容。"
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
        help="只全量爬取，不筛选合同相关"
    )

    parser.add_argument(
        "--filter-only",
        action="store_true",
        help="只基于已有 all_judicial_interpretations.jsonl 重新筛选"
    )

    parser.add_argument(
        "--max-probe-pages",
        type=int,
        default=20,
        help="额外探测分页数量。若公报页结构变化，可适当调大；设为 0 则只抓入口页和页面中发现的分页。"
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
        use_cache=use_cache,
        max_probe_pages=args.max_probe_pages,
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