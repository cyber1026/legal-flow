"""
爬取国家市场监督管理总局「合同示范文本库」，生成第四层标准合同库。

用法（仓库根目录执行）：
  python scripts/crawl/crawl_samr_standard_contracts.py --scope national
  python scripts/crawl/crawl_samr_standard_contracts.py --scope local
  python scripts/crawl/crawl_samr_standard_contracts.py --scope all
  python scripts/crawl/crawl_samr_standard_contracts.py --filter-only --scope national
  python scripts/crawl/crawl_samr_standard_contracts.py --scope national --limit 20 --skip-attachments

输出：
  data/legal_sources/layer4_standard_contracts/{samr_national,samr_local}/
    manifest/   list_items.jsonl / all_standard_contracts.jsonl /
                contract_related_standard_contracts.jsonl /
                all_standard_clauses.jsonl / clause_variants.jsonl / *.csv
    raw/        原始网页与接口缓存
    markdown/   all/ contract/ clauses/
    attachments/ logs/
"""

import argparse
import json
import os
from typing import Dict, List

import requests
from tqdm import tqdm

from legal_crawl_common import md5_text, polite_sleep, safe_filename
from standard_clause_crawl_common import (
    build_clause_outputs,
    ensure_standard_clause_dirs,
    extract_samr_list_items,
    filter_contract_related_standard_contracts,
    hydrate_attachments,
    merge_list_items,
    parse_samr_detail,
    read_jsonl,
    save_standard_contract_markdown,
    write_jsonl,
)


BASE_URL = "https://htsfwb.samr.gov.cn"
SCOPE_CONFIG = {
    "national": {
        "index_url": f"{BASE_URL}/National",
        "out": "data/legal_sources/layer4_standard_contracts/samr_national",
    },
    "local": {
        "index_url": f"{BASE_URL}/Local",
        "out": "data/legal_sources/layer4_standard_contracts/samr_local",
    },
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

TYPE_MAP = {
    "1": "生活消费",
    "2": "农资农业",
    "3": "生产经营",
    "4": "建设工程",
    "5": "其他",
}


def fetch_text_browser(url: str, cache_path: str, *, use_cache: bool, referer: str, accept_json: bool = False) -> str:
    """用浏览器请求头抓取 SAMR 页面/API。

    SAMR 的 API 对非浏览器请求头会返回 403，普通列表页可访问但没有结果数据，因此这里不用
    legal_crawl_common.fetch_url 的默认 UA。
    """
    if use_cache and os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read()

    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = referer
    if not accept_json:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        headers.pop("X-Requested-With", None)

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = resp.apparent_encoding
    text = resp.text
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def _api_item_to_row(item: Dict, *, scope: str, list_url: str) -> Dict:
    doc_id = (item.get("Id") or "").lower()
    url = f"{BASE_URL}/View?id={doc_id}" if doc_id else ""
    type_value = str(item.get("Type") or "")
    return {
        "source_layer": "第四层：标准合同库（合同示范文本与条款变体）",
        "source_site": "htsfwb.samr.gov.cn",
        "scope": "部委合同示范文本" if scope == "national" else "地方合同示范文本",
        "title_from_list": item.get("Title") or "",
        "brief_from_list": item.get("Brief") or "",
        "url": url,
        "list_url": list_url,
        "publish_year_from_list": item.get("PublishedOn"),
        "category_from_list": TYPE_MAP.get(type_value, type_value),
        "region_from_list": item.get("Region") or None,
        "department_from_list": item.get("Department") or "",
        "tags_from_list": item.get("Tags"),
    }


def crawl_index(paths: Dict[str, str], *, scope: str, index_url: str, use_cache: bool, max_pages: int) -> List[Dict]:
    """调用 SAMR 搜索 API 枚举合同示范文本。

    列表页本身是空容器，真正数据由 `/api/content/SearchTemplates` 返回。max_pages 是防御性上限；
    当 API 返回 TotalPage 时会在到达末页后停止。
    """
    all_items, errors = [], []
    loc = "false" if scope == "national" else "true"
    first_total_page = None

    # 先抓一次页面，让站点下发基础 cookie，也把入口页缓存下来便于复查。
    try:
        html_cache = os.path.join(paths["html"], f"index_page_{md5_text(index_url)}.html")
        fetch_text_browser(index_url, html_cache, use_cache=use_cache, referer=index_url, accept_json=False)
    except Exception as exc:
        errors.append({"_list_url": index_url, "_list_error": str(exc)})

    for page in tqdm(range(1, max_pages + 1), desc=f"枚举{scope}列表"):
        api = f"{BASE_URL}/api/content/SearchTemplates?loc={loc}&p={page}"
        cache_path = os.path.join(paths["html"], f"api_search_{loc}_p{page}.json")
        hit = use_cache and os.path.exists(cache_path)
        try:
            text = fetch_text_browser(api, cache_path, use_cache=use_cache, referer=index_url, accept_json=True)
            response = json.loads(text)
        except Exception as exc:
            errors.append({"_list_url": api, "_list_error": str(exc)})
            continue

        for item in response.get("Data") or []:
            all_items.append(_api_item_to_row(item, scope=scope, list_url=api))
        if first_total_page is None:
            first_total_page = int(response.get("TotalPage") or 1)
        if page >= first_total_page:
            break
        if not hit:
            polite_sleep()

    # 如果 API 将来变更失败，兜底尝试静态页 /View 链接解析。
    if not all_items:
        try:
            html_path = os.path.join(paths["html"], f"index_page_{md5_text(index_url)}.html")
            if os.path.exists(html_path):
                with open(html_path, encoding="utf-8") as f:
                    all_items.extend(extract_samr_list_items(index_url, f.read(), scope=scope))
        except Exception as exc:
            errors.append({"_list_url": index_url, "_list_error": f"static fallback: {exc}"})

    items = merge_list_items(all_items)
    write_jsonl(os.path.join(paths["manifest"], "list_items.jsonl"), items)
    write_jsonl(os.path.join(paths["logs"], "list_errors.jsonl"), errors)
    print(f"列表枚举完成：{len(items)} 条，列表错误 {len(errors)} 条")
    return items


def crawl_details(
    paths: Dict[str, str],
    items: List[Dict],
    *,
    use_cache: bool,
    limit: int = 0,
    skip_attachments: bool = False,
) -> List[Dict]:
    rows, errors = [], []
    target_items = items[:limit] if limit and limit > 0 else items
    for item in tqdm(target_items, desc="抓取示范文本详情"):
        url = item.get("url")
        if not url:
            continue
        cache_path = os.path.join(paths["html"], f"detail_{md5_text(url)}.html")
        hit = use_cache and os.path.exists(cache_path)
        try:
            html = fetch_text_browser(url, cache_path, use_cache=use_cache, referer=item.get("list_url") or url)
            row = parse_samr_detail(item, html)
            if not skip_attachments:
                row = hydrate_attachments(row, paths["attachments"], referer=url)
            rows.append(row)
            save_standard_contract_markdown(
                row,
                os.path.join(paths["all_md"], safe_filename(row.get("title"), url)),
            )
        except Exception as exc:
            errors.append({"url": url, "title_from_list": item.get("title_from_list"), "error": str(exc)})
        if not hit:
            polite_sleep()

    write_jsonl(os.path.join(paths["manifest"], "all_standard_contracts.jsonl"), rows)
    write_jsonl(os.path.join(paths["logs"], "detail_errors.jsonl"), errors)
    print(f"详情抓取完成：{len(rows)} 篇，失败 {len(errors)} 篇")
    return rows


def run_one_scope(args, scope: str) -> None:
    cfg = SCOPE_CONFIG[scope]
    out_dir = args.out or cfg["out"]
    paths = ensure_standard_clause_dirs(out_dir)
    use_cache = not args.no_cache
    all_path = os.path.join(paths["manifest"], "all_standard_contracts.jsonl")

    if args.filter_only:
        rows = read_jsonl(all_path)
        if not rows:
            raise RuntimeError(f"未找到全量文件：{all_path}")
        related = filter_contract_related_standard_contracts(paths, rows)
        build_clause_outputs(paths, related)
        return

    items = crawl_index(
        paths,
        scope=scope,
        index_url=cfg["index_url"],
        use_cache=use_cache,
        max_pages=args.max_pages,
    )
    if args.list_only:
        print("已完成列表枚举，未抓详情。")
        return

    rows = crawl_details(
        paths,
        items,
        use_cache=use_cache,
        limit=args.limit,
        skip_attachments=args.skip_attachments,
    )
    if args.crawl_only:
        print("已完成全量抓取，未执行合同相关筛选和条款切分。")
        return

    related = filter_contract_related_standard_contracts(paths, rows)
    build_clause_outputs(paths, related)


def main() -> None:
    parser = argparse.ArgumentParser(description="爬取 SAMR 合同示范文本库并生成第四层标准合同库。")
    parser.add_argument("--scope", choices=["national", "local", "all"], default="all", help="抓取范围")
    parser.add_argument("--out", default=None, help="单 scope 自定义输出目录；scope=all 时忽略")
    parser.add_argument("--max-pages", type=int, default=100, help="列表 API 最大翻页数（防御性上限）")
    parser.add_argument("--limit", type=int, default=0, help="只抓前 N 个详情，用于验证")
    parser.add_argument("--no-cache", action="store_true", help="不使用本地 HTML 缓存")
    parser.add_argument("--skip-attachments", action="store_true", help="不下载 Word/PDF 附件")
    parser.add_argument("--list-only", action="store_true", help="只枚举列表，不抓详情")
    parser.add_argument("--crawl-only", action="store_true", help="只抓取合同全文，不筛选/切条款")
    parser.add_argument("--filter-only", action="store_true", help="只基于已有 all_standard_contracts.jsonl 重跑筛选和切条款")
    args = parser.parse_args()

    scopes = ["national", "local"] if args.scope == "all" else [args.scope]
    for scope in scopes:
        run_one_scope(args, scope)


if __name__ == "__main__":
    main()
