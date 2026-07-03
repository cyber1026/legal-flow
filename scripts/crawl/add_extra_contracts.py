"""把 samr_national / samr_local 中精选的「买卖」合同，用与 samr 完全一致的 pipeline 补录进
samr/sales_contracts/，凑齐 100 篇。

samr（89 篇）按关键词「买卖」抓取，已含 16 个国家级买卖合同；这里再从：
- samr_national（部委级，5 个不重复的买卖）
- samr_local（地方级，挑类型/地区多样的 6 个买卖）
共 11 个，**全部走 crawl_samr_sale_contracts.process_one**（doc→/View HTML、docx→自研转换器、
标题/元信息表/风险提示/使用说明/正文裁剪），保证格式与 samr 现有 89 篇逐一致。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawl_samr_sale_contracts as C  # noqa: E402

BASE = "data/legal_sources/layer4_standard_contracts"

# 精选 11 个买卖合同（标题, 来源目录）。优先国家部委，地方取类型/地区多样者。
SELECTED = [
    ("商品房买卖合同（预售）（住房城乡建设部、国家工商总局2014版）", "samr_national"),
    ("钢材买卖（订货）合同（国家工商总局2008版）", "samr_national"),
    ("粮食买卖合同（国家工商总局2000版）", "samr_national"),
    ("煤矿机电产品买卖合同（国家工商总局2000版）", "samr_national"),
    ("棉花买卖合同（国家工商总局2000版 ）", "samr_national"),
    ("京津冀地区工业品买卖合同（京津冀2023版）", "samr_local"),
    ("上海市汽车买卖合同", "samr_local"),
    ("内蒙古自治区肉牛买卖合同", "samr_local"),
    ("海南省预拌混凝土买卖合同", "samr_local"),
    ("安徽省酒买卖合同（安徽省2014版）", "samr_local"),
    ("江西省农机具买卖合同（江西省2013版）", "samr_local"),
]


def _load_source(src: str) -> dict:
    """读取 national/local 的合同 manifest（title → 记录）。"""
    path = f"{BASE}/{src}/manifest/all_standard_contracts.jsonl"
    return {json.loads(l)["title"]: json.loads(l) for l in open(path, encoding="utf-8")}


def _to_meta(rec: dict) -> dict:
    """把 national/local 记录映射为 process_one 需要的 meta（字段名与搜索 meta 对齐）。"""
    return {
        "id": rec["doc_id"],
        "title": rec["title"],
        "brief": rec.get("brief", ""),
        "department": rec.get("publish_agencies", ""),
        "publish_year": (rec.get("publish_year") or "").replace("年", ""),
        "region": rec.get("region", ""),
        "category": rec.get("category", ""),
        "scope": rec.get("scope", ""),
        "is_local": rec.get("scope", "").startswith("地方"),
        "url": rec["url"],
    }


def main() -> None:
    paths = C.ensure_dirs()
    manifest_path = os.path.join(paths["manifest"], "sale_contracts.jsonl")
    manifest = [json.loads(l) for l in open(manifest_path, encoding="utf-8")]
    have_ids = {r["id"] for r in manifest}

    sources = {s: _load_source(s) for s in {src for _, src in SELECTED}}
    added = 0
    for title, src in SELECTED:
        rec = sources[src].get(title)
        if rec is None:
            print(f"  [skip] 源中未找到: {title}")
            continue
        if rec["doc_id"] in have_ids:
            print(f"  [skip] 已存在: {title}")
            continue
        meta = _to_meta(rec)
        print(f"[+] {title}")
        try:
            row = C.process_one(meta, paths, skip_pdf=False, use_cache=True)
            manifest.append(row)
            have_ids.add(meta["id"])
            added += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] {exc}")

    C._write_jsonl(manifest_path, manifest)
    C._write_summary(os.path.join(paths["manifest"], "summary.csv"), manifest)
    print(f"\n补录完成：新增 {added} 篇，合计 {len(manifest)} 篇")


if __name__ == "__main__":
    main()
