#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""恢复因旧版文件名 bug（Errno 36 文件名过长）未落盘的司法解释 markdown。

背景：早期 `safe_filename` 没有按字节截断，当 `doc_title` 解析失败回退成正文首段时，
拼出的文件名超过 Linux 255 字节上限，导致 `markdown/all/` 缺了这些篇——失败明细记在各源
`logs/detail_errors.jsonl`。当前 `safe_filename` 已修复（剥离「已于…」公告 + 90 字节截断），
故只需用现版函数把这些记录从 `all_judicial_interpretations.jsonl` 重新导出即可，正文无损。

做法（精确、幂等）：只针对 detail_errors.jsonl 里登记的失败 URL 重导出，不触碰已有文件，
重复运行不会产生副本。复用爬虫的 `safe_filename` / `save_markdown` 保证与正常产物格式一致。
"""

import json
import os
import sys

# 复用爬虫公共库里的同一套命名与写盘逻辑，确保恢复出的 md 与正常爬取产物完全一致
from legal_crawl_common import safe_filename, save_markdown

# 三个司法解释子源的目录（相对仓库根运行）
SOURCES = [
    ("court", "data/legal_sources/layer2_judicial/interpretations/spc_court"),
    ("spc", "data/legal_sources/layer2_judicial/interpretations/spc_gazette"),
    ("spp", "data/legal_sources/layer2_judicial/interpretations/spp"),
]


def load_jsonl(path):
    """读 jsonl 为 list[dict]，跳过空行。"""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def recover_source(name, root):
    """恢复单个子源：读失败明细 → 在 all_*.jsonl 中按 url 定位 → 用现版函数重写 md。

    返回 (恢复数, 跳过数, 找不到数)。
    """
    err_path = os.path.join(root, "logs", "detail_errors.jsonl")
    all_path = os.path.join(root, "manifest", "all_judicial_interpretations.jsonl")
    md_dir = os.path.join(root, "markdown", "all")

    if not os.path.exists(err_path):
        print(f"[{name}] 无 detail_errors.jsonl，跳过")
        return 0, 0, 0

    # 失败 URL 集合
    err_urls = {json.loads(l)["url"] for l in open(err_path, encoding="utf-8") if l.strip()}
    # url -> 记录
    rows = {r["url"]: r for r in load_jsonl(all_path)}

    os.makedirs(md_dir, exist_ok=True)
    recovered = skipped = missing = 0

    for url in sorted(err_urls):
        row = rows.get(url)
        if row is None:
            missing += 1
            print(f"  [缺失] jsonl 中找不到 {url}（正文不可恢复）")
            continue

        # 用现版（已修复）的 safe_filename 生成短文件名，与正常产物一致
        fname = safe_filename(row.get("doc_title") or row.get("page_title"), url)
        md_path = os.path.join(md_dir, fname)

        if os.path.exists(md_path):
            skipped += 1  # 已存在（之前已成功或本次重复跑），不覆盖
            continue

        save_markdown(row, md_path)
        recovered += 1
        print(f"  [恢复] {fname}")

    print(f"[{name}] 恢复 {recovered}，已存在跳过 {skipped}，正文缺失 {missing}")
    return recovered, skipped, missing


def main():
    # 切到仓库根，保证相对路径正确
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    os.chdir(repo_root)

    total_recovered = total_missing = 0
    for name, root in SOURCES:
        r, _, m = recover_source(name, root)
        total_recovered += r
        total_missing += m

    print(f"\n合计：恢复 {total_recovered} 篇，正文不可恢复 {total_missing} 篇")
    return 0 if total_missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
