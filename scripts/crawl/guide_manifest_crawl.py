"""
通用「策划清单(manifest) → 指引全文」抓取器（第三层语料·律协业务操作指引）。

与 crawl_acla_guides.py（按 acla 站结构抓栏目+种子）正交：本模块**不认识任何具体站点**，
只认 manifest schema —— 每行一篇指引，给定首选/备选全文 URL 与源类型(html|pdf)。
流程：逐条抓取 → 抽正文 → 质量门槛把关（不达标按 fallback 回退）→ 归一到 guide schema →
库内按归一化标题去重 → 复用 guide_crawl_common.filter_contract_related_guides 落盘。

供 crawl_acla_guidebook.py（全国律协《业务操作指引》丛书）调用；未来其它策划清单可复用本模块。
"""

import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from legal_crawl_common import (
    clean_line,
    clean_text,
    fetch_url,
    md5_text,
    polite_sleep,
    read_jsonl,
    safe_filename,
    write_jsonl,
)
from guide_crawl_common import (
    SOURCE_LAYER,
    download_file,
    ensure_guide_dirs,
    extract_file_text,
    extract_passed_and_trial,
    filter_contract_related_guides,
    save_guide_markdown,
)

# 质量门槛：正文须 ≥ MIN_BODY_LEN 字且含结构信号，否则判为脏抓/截断，触发 fallback
MIN_BODY_LEN = 2000
_STRUCTURE_RE = re.compile(r"(目\s*录|第[一二三四五六七八九十百]+章|第\s*\d+\s*条|指引|操作规程)")
_VALID_SOURCE_TYPES = {"html", "pdf"}

# 归一化标题尾注（修订/试行等版本词），用于库内去重
_VERSION_SUFFIX_RE = re.compile(r"[（(](修订版|修订|试行版|试行|征求意见稿)[)）]")


def normalize_title(title: str) -> str:
    """标题归一化（库内去重用）：去版本尾注 (修订版)/（试行）、去书名号《》、去所有空白。"""
    t = title or ""
    t = _VERSION_SUFFIX_RE.sub("", t)
    t = re.sub(r"[《》]", "", t)
    t = re.sub(r"\s+", "", t)
    return t.strip()


def extract_html_body(html: str) -> str:
    """从任意站点 HTML 抽正文：先删 script/style/nav/header/footer/aside/form，
    再在 article/div/section 中取「文本最长」的容器块；过短则兜底用整页文本。"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    best = ""
    for c in soup.find_all(["article", "div", "section"]):
        txt = clean_text(c.get_text("\n", strip=True))
        if len(txt) > len(best):
            best = txt
    if len(best) < 200:  # 容器切分失败兜底
        best = clean_text(soup.get_text("\n", strip=True))
    return best


def passes_quality(body: str, min_len: int = MIN_BODY_LEN) -> bool:
    """质量门槛：正文足够长（≥min_len 字）且含结构信号（目录/第N章/第N条/指引/操作规程）。"""
    if not body or len(body) < min_len:
        return False
    return bool(_STRUCTURE_RE.search(body))


def fetch_entry_body(
    entry: Dict, paths: Dict[str, str], use_cache: bool
) -> Tuple[str, Optional[str], List[Dict], List[Dict], bool]:
    """按 source_url + fallback_urls 顺序抓全文，第一个过质量门槛者即返回。

    返回 (body, 实际成功的 url, attachments, errors, network_used)；
    全部失败/不达标 → ('', None, [], errors, network_used)。错误逐条收集（不静默）。
    """
    urls = [entry.get("source_url"), *(entry.get("fallback_urls") or [])]
    urls = [u for u in urls if u]
    stype = entry.get("source_type")
    errors: List[Dict] = []
    network_used = False

    for url in urls:
        try:
            if stype == "pdf":
                key = md5_text(url)
                existed = any(
                    os.path.exists(os.path.join(paths["attachments"], key + ext))
                    for ext in (".pdf", ".doc", ".docx", ".bin")
                )
                local = download_file(url, paths["attachments"])
                network_used = network_used or not existed
                body = extract_file_text(local) if local else ""
                atts = ([{"name": os.path.basename(url), "url": url,
                          "local_path": local, "text_extracted": bool(body)}] if local else [])
            else:  # html
                cache_path = os.path.join(paths["html"], f"manifest_{md5_text(url)}.html")
                hit = use_cache and os.path.exists(cache_path)
                html = fetch_url(url, cache_path=cache_path, use_cache=use_cache)
                network_used = network_used or not hit
                body = extract_html_body(html)
                atts = []
        except Exception as e:  # 试下一个 fallback，错误收集后由 run_manifest 落盘
            errors.append({"title": entry.get("title"), "url": url, "error": str(e)})
            continue
        if passes_quality(body):
            return body, url, atts, errors, network_used
    return "", None, [], errors, network_used


def validate_entry(entry: Dict) -> Optional[str]:
    """校验 manifest 单行最小字段；返回中文错误原因，合法返回 None。

    source_url 允许为空：表示尚未找到全文源，由 run_manifest 分流到 unresolved 交人工补。
    """
    if not clean_line(entry.get("title") or ""):
        return "缺少 title"
    stype = entry.get("source_type")
    if stype not in _VALID_SOURCE_TYPES:
        return f"source_type 非法（须 html|pdf）：{stype!r}"
    return None


def normalize_row(
    entry: Dict, body: str, url_used: Optional[str], attachments: List[Dict]
) -> Dict:
    """把抓取结果归一为 guide schema（与 guide_crawl_common.parse_acla_detail 输出同构），
    供 classify_guide_contract_relevance / filter_contract_related_guides / save_guide_markdown 复用。
    body 为空时仍产出完整元信息行（不静默丢，便于人工追查缺口）。"""
    title = clean_line(entry.get("title") or "")
    passed_date, trial = extract_passed_and_trial(body)
    return {
        "source_layer": SOURCE_LAYER,
        "source_site": entry.get("source_site") or "",
        "association": "全国律协",
        "url": url_used or entry.get("source_url") or "",
        "list_url": "manifest",
        "category": None,
        "is_priority_seed": False,
        "from_guidebook": True,          # 标记来自丛书定向爬虫
        "book": entry.get("book"),       # 出处书册（指引①②③④）
        "title": title,
        "title_from_list": title,
        "committee": None,
        "author": "全国律协",
        "source": "全国律协",
        "publish_date": None,
        "publish_date_from_list": None,
        "passed_date": passed_date,
        "trial_period": trial,
        "attachments": attachments,
        "body": body,
        "body_len": len(body),
        "html_sha256": None,
        "crawl_time": datetime.now().isoformat(timespec="seconds"),
    }


def dedupe_by_title(rows: List[Dict]) -> List[Dict]:
    """库内按归一化标题去重。先把「有正文」的排前，确保同名时保留有正文的那条。"""
    ordered = sorted(rows, key=lambda r: not bool(r.get("body")))  # 有正文(False)排前
    seen, out = set(), []
    for r in ordered:
        key = normalize_title(r.get("title") or "")
        if key and key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def run_manifest(
    manifest_path: str,
    out_dir: str,
    *,
    use_cache: bool = True,
    crawl_only: bool = False,
    filter_only: bool = False,
) -> List[Dict]:
    """通用 manifest 抓取编排入口。

    filter_only：跳过抓取，对已有 all_guides.jsonl 重跑合同相关筛选。
    否则：逐条 validate → 抓全文 → 归一 → 库内去重 → 写 all_guides + 三类缺口日志 → 落盘筛选。
    """
    paths = ensure_guide_dirs(out_dir)
    all_path = os.path.join(paths["manifest"], "all_guides.jsonl")

    if filter_only:
        rows = read_jsonl(all_path)
        if not rows:
            raise RuntimeError(f"未找到全量文件：{all_path}")
        return filter_contract_related_guides(paths, rows)

    entries = read_jsonl(manifest_path)
    if not entries:
        raise RuntimeError(f"manifest 为空或不存在：{manifest_path}")

    rows: List[Dict] = []
    unresolved: List[Dict] = []
    fetch_errors: List[Dict] = []
    extract_failed: List[Dict] = []

    for entry in entries:
        err = validate_entry(entry)
        if err:
            unresolved.append({**entry, "reason": err})
            continue
        if not entry.get("source_url"):
            unresolved.append({**entry, "reason": "source_url 为空，待人工补源"})
            continue

        body, url_used, atts, errors, network_used = fetch_entry_body(entry, paths, use_cache)
        fetch_errors.extend(errors)
        if not body:
            extract_failed.append({
                "title": entry.get("title"), "book": entry.get("book"),
                "tried": [entry.get("source_url"), *(entry.get("fallback_urls") or [])],
            })
            rows.append(normalize_row(entry, "", None, []))  # 保留元信息行，不静默丢
        else:
            row = normalize_row(entry, body, url_used, atts)
            rows.append(row)
            save_guide_markdown(row, os.path.join(paths["all_md"], safe_filename(row["title"], row["url"])))
        if network_used:
            polite_sleep()

    rows = dedupe_by_title(rows)
    write_jsonl(all_path, rows)
    write_jsonl(os.path.join(paths["logs"], "unresolved.jsonl"), unresolved)
    write_jsonl(os.path.join(paths["logs"], "fetch_errors.jsonl"), fetch_errors)
    write_jsonl(os.path.join(paths["logs"], "extract_failed.jsonl"), extract_failed)

    got = sum(1 for r in rows if r.get("body"))
    print(f"manifest 条目 {len(entries)}｜全文成功 {got}｜未解析 {len(unresolved)}｜"
          f"抽取失败 {len(extract_failed)}｜抓取报错 {len(fetch_errors)}")

    if crawl_only:
        print("已完成抓取，未执行合同相关筛选。")
        return rows
    return filter_contract_related_guides(paths, rows)
