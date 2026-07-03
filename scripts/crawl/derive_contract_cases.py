#!/usr/bin/env python3
"""从 all/ 全量派生合同子集**候选**到暂存目录 contract_pending/（默认 dry-run，只报告）。

架构定位：
- `cases/markdown/all/` 是去重后的**全量案例真源**（各来源爬虫统一归档于此）。
- `cases/markdown/contract/` 是**人工审核通过**的合同子集（正式语料，由人把关，本脚本绝不写它）。
- `cases/markdown/contract_pending/` 是**待审核暂存区**：本脚本对 all/ 每篇按 frontmatter
  （标题/案由/案件类型）调用与爬虫**同一套**分类器，把判为合同相关、且尚未出现过的案例放进来，
  **由人工审核后再移入 contract/**（因纯标题分类对 guiding 案例缺案件类型/法条时会误纳行政案件）。

跳过规则（一个标题只会被提交审核一次）：
- 已在 contract/（审核通过）→ 跳过；
- 已在 contract_pending/（待审核）→ 跳过；
- 命中 contract_pending/_rejected.txt（人工拒绝名单，一行一个标题）→ 跳过，防止反复重提。

安全约定：
- **只写 contract_pending/，绝不动 contract/ 与 all/**；同名不覆盖。
- 默认 **dry-run**，只打印计划 + 写计划文件；只有显式 `--apply` 才实际复制写入。

用法（项目根目录执行）：
    python scripts/crawl/derive_contract_cases.py            # dry-run：报告将提交审核哪些
    python scripts/crawl/derive_contract_cases.py --apply    # 实际复制进 contract_pending/
人工审核：把 contract_pending/ 里确认为合同的 .md 移进 contract/；不要的删掉，并把其标题
（# 后的 H1）追加到 contract_pending/_rejected.txt，避免下次又被提交。
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

# 让 `import case_crawl_common` 可用
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from case_crawl_common import classify_case_contract_relevance  # noqa: E402

ALL_DIR = "data/legal_sources/layer2_judicial/cases/markdown/all"
CONTRACT_DIR = "data/legal_sources/layer2_judicial/cases/markdown/contract"
PENDING_DIR = "data/legal_sources/layer2_judicial/cases/markdown/contract_pending"
REJECTED_FILE = "_rejected.txt"  # 位于 pending 目录下

_META_RE_CACHE: dict = {}


def _meta(text: str, label: str) -> str:
    """从案例 md 的元数据块提取「- {label}：值」的值（全角冒号）。"""
    rx = _META_RE_CACHE.get(label)
    if rx is None:
        rx = _META_RE_CACHE[label] = re.compile(rf"^- {re.escape(label)}：(.*)$", re.M)
    m = rx.search(text)
    val = (m.group(1).strip() if m else "")
    return "" if val in ("None", "") else val


def read_title(text: str) -> str:
    """取首个 `# ` 行作为 H1 标题（归一键，与 dedup 一致）。"""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def stamp_classification(text: str, result: dict) -> str:
    """把分类结果回填到「## 合同相关分类」段的两行（原为 None）。"""
    text = re.sub(r"^(- 是否合同相关：).*$",
                  lambda m: m.group(1) + str(result["contract_related"]),
                  text, count=1, flags=re.M)
    text = re.sub(r"^(- 优先级：).*$",
                  lambda m: m.group(1) + str(result["contract_priority"]),
                  text, count=1, flags=re.M)
    return text


def dir_titles(d: str) -> set:
    """目录里已有 md 的 H1 标题集合（用于跳过、避免重复提交）。"""
    titles = set()
    if not os.path.isdir(d):
        return titles
    for fname in os.listdir(d):
        if not fname.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, fname), encoding="utf-8") as f:
                titles.add(read_title(f.read()))
        except OSError:
            continue
    titles.discard("")
    return titles


def load_rejected(pending_dir: str) -> set:
    """读取人工拒绝名单（pending/_rejected.txt，一行一个标题，# 开头为注释）。"""
    path = os.path.join(pending_dir, REJECTED_FILE)
    titles = set()
    if not os.path.isfile(path):
        return titles
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                titles.add(line)
    return titles


def main() -> None:
    """扫描 all/、分类、把新候选提交到 contract_pending/（dry-run 或 --apply）。"""
    ap = argparse.ArgumentParser(description="从 all/ 派生合同候选到暂存区待人工审核（默认 dry-run）")
    ap.add_argument("--all-dir", default=ALL_DIR, help="全量案例 markdown 目录（真源）")
    ap.add_argument("--contract-dir", default=CONTRACT_DIR, help="已审核通过的合同目录（只读，用于跳过）")
    ap.add_argument("--pending-dir", default=PENDING_DIR, help="待审核暂存目录（写入目标）")
    ap.add_argument("--apply", action="store_true", help="实际复制写入（默认仅 dry-run 报告）")
    args = ap.parse_args()

    if not os.path.isdir(args.all_dir):
        raise SystemExit(f"目录不存在：{args.all_dir}")
    os.makedirs(args.pending_dir, exist_ok=True)

    approved = dir_titles(args.contract_dir)
    pending = dir_titles(args.pending_dir)
    rejected = load_rejected(args.pending_dir)
    seen = approved | pending | rejected

    total = no_title = related = 0
    add_plan = []
    prio_dist = Counter()

    for fname in sorted(os.listdir(args.all_dir)):
        if not fname.endswith(".md"):
            continue
        src = os.path.join(args.all_dir, fname)
        if not os.path.isfile(src):
            continue
        total += 1
        try:
            with open(src, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        title = read_title(text)
        if not title:
            no_title += 1
            continue
        result = classify_case_contract_relevance({
            "doc_title": title,
            "cause_of_action": _meta(text, "案由"),
            "case_type": _meta(text, "案件类型"),
        })
        if not result["contract_related"]:
            continue
        related += 1
        prio_dist[result["contract_priority"]] += 1
        if title in seen:
            continue
        add_plan.append({"title": title, "src": fname, "priority": result["contract_priority"],
                         "_text": stamp_classification(text, result)})

    # ---- 报告 ----
    print(f"all/ 目录：{args.all_dir}（共 {total} 篇，无标题跳过 {no_title}）")
    print(f"已审核通过 contract/：{len(approved)} | 待审核 pending/：{len(pending)} | 拒绝名单：{len(rejected)}")
    print(f"判为合同相关：{related} 篇；其中已见过 {related - len(add_plan)}，**本次新提交审核 {len(add_plan)}**")
    print(f"合同相关优先级分布：{dict(prio_dist)}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.dirname(os.path.abspath(args.pending_dir.rstrip("/")))
    plan_path = os.path.join(report_dir, f"_contract_pending_plan_{ts}.jsonl")
    with open(plan_path, "w", encoding="utf-8") as f:
        for rec in add_plan:
            f.write(json.dumps({k: v for k, v in rec.items() if k != "_text"},
                               ensure_ascii=False) + "\n")
    print(f"提交计划已写入：{plan_path}")
    for rec in add_plan[:5]:
        print(f"  + [{rec['priority']}] {rec['title']}")

    if not args.apply:
        print("\n[dry-run] 未写入任何文件。确认无误后加 --apply 实际复制到 contract_pending/。")
        return

    written, skipped = 0, 0
    for rec in add_plan:
        dst = os.path.join(args.pending_dir, rec["src"])
        if os.path.exists(dst):
            skipped += 1
            continue
        with open(dst, "w", encoding="utf-8") as f:
            f.write(rec["_text"])
        written += 1
    print(f"\n[apply] 提交 {written} 篇到 contract_pending/ 待人工审核，跳过同名 {skipped}。"
          f"contract/ 与 all/ 未改动。")


if __name__ == "__main__":
    main()
