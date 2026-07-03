"""
爬取「人民法院案例库」（https://rmfyalk.court.gov.cn）的指导性案例与参考案例，
全量抓列表后在本地按类型 + 标题筛出合同相关案例，再抓正文并终筛。

⚠️ 该站点是 SPA 且**需要登录**：接口未登录返回 `{"code":401,"msg":"未登录"}`。
   请在浏览器登录 rmfyalk 后，从开发者工具任一 /cpws_al_api 请求复制整串 Cookie 传给 --cookie。

接口（POST，application/json）实测要点：
  - 列表 /cpws_al_api/api/cpwsAl/search：服务端**只支持「全部」库**（searchParams.lib=cpwsAl_qb，
    共约 5400 条），cpwsAl_zdx/cpwsAl_ck 等过滤无效（返回空）。故**类型在本地按 cpws_al_type 切分**：
    01=指导性案例、02=参考案例、04=特色案事例。返回 data.datas[] + data.totalCount。
  - 正文 /cpws_al_api/api/cpwsAl/content：body {"gid": <item.id 原值，已 URL 编码，勿再编码>}；
    返回 data.data（dict），字段：cpws_al_cpyz=裁判要旨、cpws_al_jbaq=基本案情、cpws_al_cpjg=裁判结果、
    cpws_al_cply=裁判理由、cpws_al_glsy=相关法条/索引、cpws_al_keyword=关键词(list)、
    cpws_al_ajzh=案号、cpws_al_slfy_sf_name/cpws_al_sf=审理法院（均为 HTML，需去标签）。

设计：5400 篇里绝大多数是参考案例，全部抓正文既慢又浪费。案例分类器本就是**标题驱动**（在 court
指导案例上已验证准确），故先用标题筛出合同相关（约 960 篇），只对这些抓正文，再用正文终筛。
传 --fetch-all 可改为抓取所选类型的全部正文（不预筛）。

用法（仓库根目录执行）：
  python scripts/crawl/crawl_rmfyalk_cases.py --cookie "<整串>" --probe   # 验证 cookie + 看类型分布
  python scripts/crawl/crawl_rmfyalk_cases.py --cookie "<整串>"           # 指导+参考，标题预筛后抓正文+终筛
  python scripts/crawl/crawl_rmfyalk_cases.py --cookie "<整串>" --libs zdx   # 只要指导性案例
  python scripts/crawl/crawl_rmfyalk_cases.py --filter-only               # 仅基于已抓全量重新筛选
"""

import argparse
import json
import math
import os
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests
from tqdm import tqdm

from legal_crawl_common import (
    clean_text,
    md5_text,
    polite_sleep,
    read_jsonl,
    safe_filename,
    safe_filename_stem,
    sha256_text,
    write_jsonl,
)
from case_crawl_common import (
    classify_case_contract_relevance,
    ensure_case_dirs,
    existing_full_case_stems,
    extract_case_type,
    extract_cause_of_action,
    filter_contract_related_cases,
    save_case_markdown,
)


BASE = "https://rmfyalk.court.gov.cn"
SEARCH_API = f"{BASE}/cpws_al_api/api/cpwsAl/search"
CONTENT_API = f"{BASE}/cpws_al_api/api/cpwsAl/content"
# 目录结构调整后：抓取工作目录在 _crawl/caselib，统一 markdown 归档在 cases/markdown/all
DEFAULT_OUT_DIR = "data/legal_sources/layer2_judicial/cases/_crawl/caselib"
DEFAULT_ALL_MD_DIR = "data/legal_sources/layer2_judicial/cases/markdown/all"

# 库代号 -> (cpws_al_type, 案例类型中文名)
LIB_MAP = {"zdx": ("01", "指导性案例"), "ck": ("02", "参考案例")}

PAGE_SIZE = 50
REQUEST_TIMEOUT = 30
RETRY_TIMES = 3
LIST_SLEEP = 0.4  # 列表是轻量 JSON，稍快即可


# =========================================================================
# 带 Cookie 的会话与请求
# =========================================================================

def build_session(cookie: str) -> requests.Session:
    """构造带登录 Cookie 的会话。接口要求 JSON POST。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ContractRiskResearchCrawler/1.0",
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE,
        "Referer": f"{BASE}/view/list.html",
        "X-Requested-With": "XMLHttpRequest",
    })
    if cookie:
        s.headers["Cookie"] = cookie.strip()
    return s


def post_json(session: requests.Session, url: str, payload: Dict) -> Dict:
    """POST JSON 并解析返回；401 未登录直接上抛、不重试。"""
    last_err = None
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            resp = session.post(url, data=json.dumps(payload), timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if str(data.get("code")) == "401":
                raise PermissionError(
                    "接口返回 401 未登录：Cookie 无效或已过期，请重新登录 rmfyalk 复制最新 Cookie。"
                )
            return data
        except PermissionError:
            raise
        except Exception as e:
            last_err = e
            time.sleep(min(20, 2 ** attempt + random.random()))
    raise RuntimeError(f"请求失败：{url}，error={last_err}")


# =========================================================================
# 列表：抓「全部」并本地按类型切分
# =========================================================================

def _search_payload(page: int) -> Dict:
    return {
        "page": page,
        "size": PAGE_SIZE,
        "lib": "qb",
        "searchParams": {
            "userSearchType": 1, "isAdvSearch": "0",
            "selectValue": "qw", "lib": "cpwsAl_qb", "sort_field": "",
        },
    }


def _to_item(it: Dict) -> Optional[Dict]:
    gid = it.get("id") or it.get("cpws_al_id")
    if not gid:
        return None
    type_code = it.get("cpws_al_type") or ""
    return {
        "gid": gid,
        "cpws_al_id": it.get("cpws_al_id"),
        "title_from_list": it.get("cpws_al_title") or "",
        "cpws_al_type": type_code,
        "case_category": {"01": "指导性案例", "02": "参考案例", "04": "特色案事例"}.get(type_code, type_code),
        "holding_from_list": it.get("cpws_al_cpyz") or "",
        "url": f"{BASE}/view/content.html?id={gid}",
    }


def crawl_all_list(session: requests.Session, paths: Dict[str, str], type_codes: set) -> List[Dict]:
    """抓「全部」列表全部分页，本地保留所选 cpws_al_type，返回去重后的列表项。"""
    first = (post_json(session, SEARCH_API, _search_payload(1)).get("data") or {})
    total = int(first.get("totalCount") or 0)
    pages = math.ceil(total / PAGE_SIZE) if total else 0
    print(f"案例库全部命中 {total} 篇，分 {pages} 页")

    seen, items = set(), []

    def collect(datas):
        for it in datas or []:
            item = _to_item(it)
            if item and item["gid"] not in seen and item["cpws_al_type"] in type_codes:
                seen.add(item["gid"])
                items.append(item)

    collect(first.get("datas"))
    for p in tqdm(range(2, pages + 1), desc="抓取列表"):
        collect((post_json(session, SEARCH_API, _search_payload(p)).get("data") or {}).get("datas"))
        time.sleep(LIST_SLEEP)

    write_jsonl(os.path.join(paths["manifest"], "list_items.jsonl"), items)
    print(f"所选类型列表项：{len(items)} 篇")
    return items


# =========================================================================
# 正文：按精确字段解析
# =========================================================================

def _strip_html(s: str) -> str:
    """去 HTML 标签与全角空格，保留段落换行。"""
    if not s:
        return ""
    s = re.sub(r"<\s*br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</\s*p\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("　", " ")
    return clean_text(s)


def _join_keywords(kw) -> str:
    """cpws_al_keyword 是 list（首元素为案件类型 民事/刑事…）。拼成「民事/.../...」。"""
    if isinstance(kw, list):
        return "/".join(str(x) for x in kw if x)
    return str(kw or "")


def _extract_inner(raw: Dict) -> Optional[Dict]:
    """从 content 返回提取有效正文 dict；无效（超配额「获取失败l3」/未授权/空）返回 None。

    站点对账号有**每日全文浏览配额（约 100 篇）**，超限后返回 code=500 / msg=获取失败l3 / data=None。
    必须识别为失败（不可静默当成功），且**不缓存失败**以便日后重试。
    """
    if not isinstance(raw, dict) or str(raw.get("code")) != "0":
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    inner = data.get("data")
    if isinstance(inner, dict) and (inner.get("cpws_al_title") or inner.get("cpws_al_cpyz")):
        return inner
    return None


def parse_content(item: Dict, inner: Dict) -> Dict:
    """有全文时，按 data.data 的精确字段解析为结构化记录（content_full=True）。"""
    title = inner.get("cpws_al_title") or item.get("title_from_list") or ""
    keywords_text = _join_keywords(inner.get("cpws_al_keyword"))
    holding = _strip_html(inner.get("cpws_al_cpyz")) or _strip_html(item.get("holding_from_list"))
    return {
        **_base_row(item, title),
        "case_id": inner.get("cpws_al_no"),
        "court_case_no": inner.get("cpws_al_ajzh"),
        "court": inner.get("cpws_al_slfy_sf_name") or inner.get("cpws_al_sf") or None,
        "case_type": extract_case_type(keywords_text),
        "publish_time": inner.get("cpws_al_zs_date") or inner.get("cpws_al_rk_time"),
        "keywords_text": keywords_text,
        "holding": holding,                                       # 裁判要旨/要点
        "facts": _strip_html(inner.get("cpws_al_jbaq")),          # 基本案情
        "judgment": _strip_html(inner.get("cpws_al_cpjg")),       # 裁判结果
        "reasoning": _strip_html(inner.get("cpws_al_cply")),      # 裁判理由
        "relevant_statutes": _strip_html(inner.get("cpws_al_glsy")),  # 相关法条/索引
        "content_full": True,
        "html_sha256": sha256_text(json.dumps(inner, ensure_ascii=False)),
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
    }


def row_from_list(item: Dict) -> Dict:
    """无全文（超配额/未授权）时，用列表数据构建**部分行**：标题 + 完整裁判要旨。
    裁判要旨是案例最核心的裁判规则，足以入合同语料；content_full=False 标记待补全。"""
    title = item.get("title_from_list") or ""
    return {
        **_base_row(item, title),
        "case_id": None, "court_case_no": None, "court": None,
        "case_type": "",
        "publish_time": None,
        "keywords_text": "",
        "holding": _strip_html(item.get("holding_from_list")),    # 列表里的完整裁判要旨
        "facts": "", "judgment": "", "reasoning": "", "relevant_statutes": "",
        "content_full": False,
        "html_sha256": "",
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
    }


def _base_row(item: Dict, title: str) -> Dict:
    return {
        "source_type": "案例库",
        "source_site": "人民法院案例库",
        "case_category": item.get("case_category"),
        "url": item["url"],
        "title_from_list": item.get("title_from_list"),
        "page_title": title,
        "doc_title": title,
        "case_no": None,
        "case_no_int": None,
        "gid": item.get("gid"),
        "cause_of_action": extract_cause_of_action(title),
        "body": "",
    }


def _is_contract_by_title(item: Dict) -> bool:
    """标题级合同相关预筛（用与最终一致的分类器，仅喂标题）。"""
    return classify_case_contract_relevance({
        "doc_title": item.get("title_from_list") or "",
        "keywords_text": "", "relevant_statutes": "",
    })["contract_related"]


QUOTA_STOP_AFTER = 3  # 连续这么多次「获取失败」即判定超配额，停止联网（避免无谓刷服务器）


def crawl_contents(session: requests.Session, items: List[Dict], paths: Dict[str, str]) -> List[Dict]:
    """抓正文。识别每日配额：超限后停止联网，剩余用列表裁判要旨构建部分行。

    去重 / 增量：以「标题主干」为稳定键，下载前先看 all/ 是否已有该案**全文**——
    命中则跳过联网（省每日约 100 篇配额）且不重复落盘；落盘统一用稳定文件名
    （主干.md），同案重跑覆盖，不再产生 url 哈希重复。
    """
    all_rows, errors = [], []
    raw_dir = os.path.join(paths["html"], "content_json")
    os.makedirs(raw_dir, exist_ok=True)
    all_md_dir = paths["all_md"]
    os.makedirs(all_md_dir, exist_ok=True)

    # 已有「全文」案例的标题主干集合：命中即视为下载过，跳过联网与重复落盘
    full_stems = existing_full_case_stems(all_md_dir)

    quota_hit = False
    consec_fail = 0
    n_full = 0          # 本次新抓到的全文数
    n_skip_existing = 0  # 因 all/ 已有全文而跳过的数

    for item in tqdm(items, desc="抓取正文"):
        gid = item["gid"]
        stem = safe_filename_stem(item.get("title_from_list") or str(gid))
        # 缓存按**标题**（稳定键）命名——gid 是每次列表查询都变的临时 token，按 gid 缓存跨运行会失效。
        cache = os.path.join(raw_dir, f"{md5_text(item.get('title_from_list') or str(gid))}.json")
        inner = None

        # 1) 复用有效缓存；无效缓存（历史失败）删除以便重试
        if os.path.exists(cache):
            try:
                with open(cache, "r", encoding="utf-8") as f:
                    inner = _extract_inner(json.load(f))
            except Exception:
                inner = None
            if inner is None:
                os.remove(cache)

        # 2) all/ 已有全文 → 跳过联网与落盘（缓存若在仍用于补全清单字段，否则用列表部分行）
        if stem in full_stems:
            n_skip_existing += 1
            all_rows.append(parse_content(item, inner) if inner is not None else row_from_list(item))
            continue

        # 3) 未命中缓存且未超配额则联网
        if inner is None and not quota_hit:
            try:
                raw = post_json(session, CONTENT_API, {"gid": gid})
            except PermissionError:
                raise
            inner = _extract_inner(raw)
            if inner is not None:
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False)
                consec_fail = 0
                polite_sleep()
            else:
                consec_fail += 1
                errors.append({"gid": gid, "title": item.get("title_from_list"), "msg": raw.get("msg")})
                if consec_fail >= QUOTA_STOP_AFTER:
                    quota_hit = True
                    tqdm.write(f"⚠️ 连续 {consec_fail} 次获取失败，判定已达每日浏览配额，停止联网，"
                               f"剩余案例改用列表裁判要旨构建部分行（次日重跑可续抓）。")

        # 4) 构建行：有全文→完整，否则→部分（裁判要旨）
        if inner is not None:
            row = parse_content(item, inner)
            n_full += 1
        else:
            row = row_from_list(item)
        all_rows.append(row)

        # 5) 落盘到 all/：稳定文件名（标题主干.md）。新抓到全文则登记主干，避免本轮内同名重复落盘
        save_case_markdown(row, os.path.join(all_md_dir, f"{stem}.md"))
        if inner is not None:
            full_stems.add(stem)

    write_jsonl(os.path.join(paths["manifest"], "all_cases.jsonl"), all_rows)
    write_jsonl(os.path.join(paths["logs"], "content_errors.jsonl"), errors)
    n_partial = len(all_rows) - n_full - n_skip_existing
    print(f"正文：本次新抓全文 {n_full} 篇，已有全文跳过 {n_skip_existing} 篇，"
          f"部分(仅裁判要旨) {n_partial} 篇，失败请求 {len(errors)} 次")
    if n_partial:
        print(f"  注：超每日配额，{n_partial} 篇仅有标题+裁判要旨。次日重跑同命令即从断点续抓（已抓全文跳过不耗配额）。")
    return all_rows


# =========================================================================
# 主流程
# =========================================================================

# cookie 文件：定时任务/重复运行时从此读取（gitignore）。用户每次登录后把整串 Cookie 写入即可。
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rmfyalk_cookie")


def resolve_cookie(arg_cookie: str) -> str:
    """cookie 来源优先级：--cookie > 环境变量 RMFYALK_COOKIE > 文件 .rmfyalk_cookie。"""
    if arg_cookie:
        return arg_cookie
    if os.environ.get("RMFYALK_COOKIE"):
        return os.environ["RMFYALK_COOKIE"]
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def main():
    parser = argparse.ArgumentParser(description="爬取人民法院案例库（参考案例为主）并筛选合同相关。")
    parser.add_argument("--cookie", default="", help="登录后的会话 Cookie（整串）；不传则读环境变量 RMFYALK_COOKIE 或 .rmfyalk_cookie 文件")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help="抓取工作目录（manifest/raw/logs）")
    parser.add_argument("--all-md-dir", default=DEFAULT_ALL_MD_DIR,
                        help="全部案例 Markdown 归档目录（统一存放、下载前据此去重跳过）")
    # 默认只抓参考案例：指导性案例与 court_guiding_cases 重复（已全文），无需重抓。
    parser.add_argument("--libs", default="ck", help="抓取类型：zdx=指导性(与court重复),ck=参考；逗号分隔，默认 ck")
    parser.add_argument("--fetch-all", action="store_true", help="抓所选类型全部正文（默认仅抓标题预筛出的合同相关）")
    parser.add_argument("--probe", action="store_true", help="只验证 Cookie 并打印类型分布")
    parser.add_argument("--crawl-only", action="store_true", help="只抓正文，不终筛")
    parser.add_argument("--filter-only", action="store_true", help="仅基于已抓 all_cases.jsonl 重新筛选")
    args = parser.parse_args()

    paths = ensure_case_dirs(args.out)
    # all/ 归档统一到 cases/markdown/all（与 _crawl 工作目录解耦），便于跨来源去重与切分
    paths["all_md"] = args.all_md_dir
    os.makedirs(paths["all_md"], exist_ok=True)
    all_path = os.path.join(paths["manifest"], "all_cases.jsonl")

    if args.filter_only:
        rows = read_jsonl(all_path)
        if not rows:
            raise RuntimeError(f"未找到全量文件：{all_path}")
        filter_contract_related_cases(paths, rows)
        return

    cookie = resolve_cookie(args.cookie)
    if not cookie:
        raise SystemExit(
            f"缺少 Cookie。请把登录后的整串 Cookie 写入 {COOKIE_FILE}（或用 --cookie / 环境变量 RMFYALK_COOKIE）。"
            "登录 https://rmfyalk.court.gov.cn 后从开发者工具任一 /cpws_al_api 请求复制 Cookie。"
        )

    session = build_session(cookie)
    lib_keys = [k.strip() for k in args.libs.split(",") if k.strip() in LIB_MAP]
    if not lib_keys:
        raise SystemExit(f"--libs 无有效值，可选：{list(LIB_MAP)}")
    type_codes = {LIB_MAP[k][0] for k in lib_keys}

    try:
        if args.probe:
            d = post_json(session, SEARCH_API, _search_payload(1)).get("data") or {}
            print(f"Cookie 有效 ✓ 案例库全部命中 {d.get('totalCount')} 篇")
            return

        # 1. 抓全部列表，本地保留所选类型
        items = crawl_all_list(session, paths, type_codes)

        # 2. 标题预筛合同相关（除非 --fetch-all）
        if args.fetch_all:
            targets = items
            print("（--fetch-all）抓取所选类型全部正文")
        else:
            targets = [it for it in items if _is_contract_by_title(it)]
            print(f"标题预筛合同相关：{len(targets)} / {len(items)} 篇，仅对这些抓正文")

        # 3. 抓正文
        rows = crawl_contents(session, targets, paths)
    except PermissionError as e:
        # Cookie 失效——定时任务场景下友好退出（不刷错误栈），提示更新 cookie
        print(f"⚠️ {e}")
        print(f"请更新 {COOKIE_FILE} 后重跑。")
        raise SystemExit(0)

    if args.crawl_only:
        print("已完成正文抓取，未执行终筛。")
        return

    # 4. 基于正文终筛
    filter_contract_related_cases(paths, rows)


if __name__ == "__main__":
    main()
