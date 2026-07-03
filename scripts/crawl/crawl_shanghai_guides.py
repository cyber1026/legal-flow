"""
爬取上海市律师协会（东方律师网 www.lawyers.org.cn）「业务指引」全库，本地筛选合同相关。

来源结构（2026-05 实测）：
  - 索引页：/studies/businessguide ，服务端渲染，`?currentPageNo=N`（0 起）翻页，
    每页约 37 条，条目形如「日期 [类别] 标题」并链向 /info/<hash>。
  - 详情页：/info/<hash> ，服务端渲染，标题 div.m-info h2、正文 div.m-info>div.content、
    委员会取自 <title> 面包屑（见 guide_crawl_common.parse_shanghai_detail）。

首页是 Nuxt SPA（无链接），但**索引页与详情页都是服务端渲染**，纯 requests+BS4 即可，无需打 API/登录。

用法（仓库根目录执行）：
  python scripts/crawl/crawl_shanghai_guides.py                 # 全量翻页 + 解析 + 合同筛选
  python scripts/crawl/crawl_shanghai_guides.py --crawl-only
  python scripts/crawl/crawl_shanghai_guides.py --filter-only   # 仅重跑筛选
  python scripts/crawl/crawl_shanghai_guides.py --max-pages 50 --no-cache
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
    filter_contract_related_guides,
    parse_shanghai_detail,
    save_guide_markdown,
)

# 正文短于此阈值（多为「点击查看文件」式 PDF 附件页）则尝试下载附件抽正文补全
_MIN_INLINE_BODY = 300


BASE_URL = "https://www.lawyers.org.cn"
INDEX_URL = f"{BASE_URL}/studies/businessguide"
DEFAULT_OUT_DIR = "data/legal_sources/layer3_playbooks/shanghai_bar"

# 6 个优先种子（用户给的是标题，按**特征子串**匹配索引条目并强制纳入）。
PRIORITY_SEED_TITLE_KEYS = [
    "企业法律顾问业务操作指引",
    "公司关联交易业务操作指引",
    "劳务派遣合同纠纷案件业务操作指引",
    "公司股权代持业务操作指引",
    "企业税务合规业务操作指引",
    "签发律师函业务操作指引",
]

_INFO_HASH_RE = re.compile(r"/info/([0-9a-f]{32})")
_DATE_RE = re.compile(r"20\d{2}-\d{1,2}-\d{1,2}")
_CATEGORY_RE = re.compile(r"\[([^\]]{1,30})\]")


# =========================================================================
# 索引页枚举
# =========================================================================

def extract_index_items(list_url: str, html: str) -> List[Dict]:
    """从索引页提取条目：标题 + /info 链接 + 列表日期 + [类别]。

    只保留「附近文本带 YYYY-MM-DD 日期」的 /info 锚点，借此过滤侧边栏（委员会信息等）噪声。
    """
    soup = BeautifulSoup(html, "lxml")
    items, seen = [], set()

    for a in soup.find_all("a", href=_INFO_HASH_RE):
        title = clean_line(a.get_text(" ", strip=True))
        if len(title) < 6:
            continue
        detail_url = urljoin(list_url, a["href"])
        h = _INFO_HASH_RE.search(detail_url)
        if not h or h.group(1) in seen:
            continue

        parent = a.find_parent(["li", "div", "tr"])
        ptext = parent.get_text(" ", strip=True) if parent else title
        m_date = _DATE_RE.search(ptext)
        if not m_date:  # 列表条目必带日期；无日期者多为侧边栏/导航
            continue
        m_cat = _CATEGORY_RE.search(ptext)

        seen.add(h.group(1))
        is_seed = any(key in title for key in PRIORITY_SEED_TITLE_KEYS)
        items.append({
            "title_from_list": title,
            "url": detail_url,
            "list_url": list_url,
            "publish_date_from_list": m_date.group(0),
            "category": m_cat.group(1) if m_cat else None,
            "is_priority_seed": is_seed,
        })

    return items


def crawl_index(paths: Dict[str, str], use_cache: bool, max_pages: int) -> List[Dict]:
    """翻页枚举 businessguide，直到某页无新条目或达到 max_pages。"""
    all_items, seen_hash = [], set()

    # 注意：currentPageNo 实为「1 起」，且 0 与 1 都返回第 1 页（从 2 起才真正翻页），
    # 故从 1 开始逐页递增，靠「本页无新条目」判定到底。
    for page in tqdm(range(1, max_pages + 1), desc="抓取上海索引页"):
        list_url = f"{INDEX_URL}?currentPageNo={page}"
        cache_path = os.path.join(paths["html"], f"index_{page}.html")
        hit = use_cache and os.path.exists(cache_path)
        html = fetch_url(list_url, cache_path=cache_path, use_cache=use_cache)
        page_items = extract_index_items(list_url, html)

        new = 0
        for it in page_items:
            h = _INFO_HASH_RE.search(it["url"]).group(1)
            if h not in seen_hash:
                seen_hash.add(h)
                all_items.append(it)
                new += 1
        if not hit:
            polite_sleep()
        if new == 0:  # 本页无新条目 → 翻到底
            print(f"第 {page} 页无新条目，停止翻页。")
            break

    write_jsonl(os.path.join(paths["manifest"], "list_items.jsonl"), all_items)
    seed_found = sum(1 for it in all_items if it["is_priority_seed"])
    print(f"索引枚举完成：{len(all_items)} 条，命中优先种子 {seed_found}/{len(PRIORITY_SEED_TITLE_KEYS)}")
    return all_items


# =========================================================================
# 详情页抓取
# =========================================================================

def crawl_details(paths: Dict[str, str], items: List[Dict], use_cache: bool) -> List[Dict]:
    rows, errors = [], []
    for item in tqdm(items, desc="抓取上海详情页"):
        url = item["url"]
        cache_path = os.path.join(paths["html"], f"detail_{md5_text(url)}.html")
        hit = use_cache and os.path.exists(cache_path)
        try:
            html = fetch_url(url, cache_path=cache_path, use_cache=use_cache)
            row = parse_shanghai_detail(item, html)
            row["crawl_time"] = datetime.now().isoformat(timespec="seconds")
            # 正文极短（「点击查看文件」式页面）→ 下载附件（多为 PDF）抽正文补全
            if (row.get("body_len") or 0) < _MIN_INLINE_BODY:
                for att in row.get("attachments") or []:
                    local = download_file(att["url"], paths["attachments"], referer=url)
                    att["local_path"] = local
                    txt = extract_file_text(local) if local else ""
                    if txt:
                        row["body"], row["body_len"], att["text_extracted"] = txt, len(txt), True
                        break
            rows.append(row)
            save_guide_markdown(row, os.path.join(paths["all_md"], safe_filename(row.get("title"), url)))
        except Exception as e:  # 不静默吞错：逐条记日志
            errors.append({"url": url, "title_from_list": item.get("title_from_list"), "error": str(e)})
        if not hit:
            polite_sleep()

    write_jsonl(os.path.join(paths["manifest"], "all_guides.jsonl"), rows)
    write_jsonl(os.path.join(paths["logs"], "detail_errors.jsonl"), errors)
    print(f"全量详情：all_guides.jsonl（{len(rows)} 篇，失败 {len(errors)}）")
    return rows


# =========================================================================
# 主流程
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="爬取上海律协业务指引并筛选合同相关。")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help="输出目录")
    parser.add_argument("--max-pages", type=int, default=60, help="索引最大翻页数（防御性上限）")
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

    items = crawl_index(paths, use_cache=use_cache, max_pages=args.max_pages)
    rows = crawl_details(paths, items, use_cache=use_cache)

    if args.crawl_only:
        print("已完成全量爬取，未执行合同相关筛选。")
        return

    filter_contract_related_guides(paths, rows)


if __name__ == "__main__":
    main()
