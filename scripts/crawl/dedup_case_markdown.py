#!/usr/bin/env python3
"""案例 Markdown 去重工具（默认 dry-run，只报告不删除）。

背景：all/ 下历史文件名带 md5(url) 后缀，而 url 的 id token 每次抓取都变，导致同一案例
反复落成新文件（约 10184 文件 / 仅 1225 唯一标题）。本工具按**案例标题**归组，每组保留
最完整（全文优先 > 体积大 > 修改新）的一份，其余视为重复。

用法（项目根目录执行）：
    python scripts/crawl/dedup_case_markdown.py                 # dry-run：只打印计划 + 写计划文件
    python scripts/crawl/dedup_case_markdown.py --apply         # 实际删除重复
    python scripts/crawl/dedup_case_markdown.py --dir <目录>    # 指定目录（默认 cases/markdown/all）

安全约定：
- **默认 dry-run**，不动任何文件；只有显式 --apply 才删除。
- 以「H1 标题」精确归组（而非截断后的文件名主干），避免把不同案例误并、误删。
- 标题无法解析的文件一律保留。
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

# 让 `import case_crawl_common` 可用
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from case_crawl_common import case_md_is_full  # noqa: E402

DEFAULT_DIR = "data/legal_sources/layer2_judicial/cases/markdown/all"


def read_title(path: str) -> str:
    """读取案例 Markdown 的 H1 标题（首个 `# ` 行）作为精确归组键。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        return ""
    return ""


def scan(md_dir: str) -> dict:
    """扫描目录，按标题归组，返回 {title: [文件信息...]}。"""
    groups: dict = defaultdict(list)
    for fname in sorted(os.listdir(md_dir)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(md_dir, fname)
        if not os.path.isfile(path):
            continue
        title = read_title(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        groups[title].append({
            "name": fname,
            "path": path,
            "is_full": case_md_is_full(path),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    return groups


def pick_keeper(files: list) -> dict:
    """从同标题的多份里挑保留项：全文优先 > 体积大 > 修改时间新。"""
    return sorted(files, key=lambda x: (x["is_full"], x["size"], x["mtime"]), reverse=True)[0]


def main() -> None:
    """执行扫描与去重（dry-run 或 --apply）。"""
    parser = argparse.ArgumentParser(description="案例 Markdown 去重（默认 dry-run）")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="待去重的 markdown 目录")
    parser.add_argument("--apply", action="store_true", help="实际删除重复（默认仅 dry-run 报告）")
    args = parser.parse_args()

    md_dir = args.dir
    if not os.path.isdir(md_dir):
        raise SystemExit(f"目录不存在：{md_dir}")

    groups = scan(md_dir)
    total_files = sum(len(v) for v in groups.values())
    unique_titles = len(groups)

    delete_plan = []          # 待删除文件信息
    no_title = groups.get("", [])
    dup_groups = 0
    partial_only_titles = 0   # 只有部分行、无全文的标题（待补全）

    for title, files in groups.items():
        if not title:
            continue  # 无标题：一律保留，不参与去重
        if all(not f["is_full"] for f in files):
            partial_only_titles += 1
        if len(files) <= 1:
            continue
        dup_groups += 1
        keeper = pick_keeper(files)
        for f in files:
            if f["name"] != keeper["name"]:
                delete_plan.append({"title": title, "delete": f["name"],
                                    "keep": keeper["name"], "keep_full": keeper["is_full"]})

    # ---- 报告 ----
    print(f"目录：{md_dir}")
    print(f"总文件：{total_files}，唯一标题：{unique_titles}，无标题文件(保留)：{len(no_title)}")
    print(f"存在重复的标题组：{dup_groups}，计划删除：{len(delete_plan)} 个重复文件")
    print(f"去重后预计保留：{total_files - len(delete_plan)} 个文件")
    print(f"仅有部分行(无全文、待补全)的标题：{partial_only_titles}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 计划/日志写到目标目录的**上一级**，避免污染 markdown 数据目录
    report_dir = os.path.dirname(os.path.abspath(md_dir.rstrip("/")))
    plan_path = os.path.join(report_dir, f"_dedup_plan_{ts}.jsonl")
    with open(plan_path, "w", encoding="utf-8") as f:
        for rec in delete_plan:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"删除计划已写入：{plan_path}")

    if delete_plan[:5]:
        print("示例（前 5 条）：")
        for rec in delete_plan[:5]:
            print(f"  - 删 {rec['delete']}  ← 保留 {rec['keep']}（全文={rec['keep_full']}）")

    if not args.apply:
        print("\n[dry-run] 未删除任何文件。确认无误后加 --apply 实际执行。")
        return

    # ---- 实际删除 ----
    removed = 0
    errors = []
    for rec in delete_plan:
        p = os.path.join(md_dir, rec["delete"])
        try:
            os.remove(p)
            removed += 1
        except OSError as e:
            errors.append({"file": rec["delete"], "error": str(e)})
    print(f"\n[apply] 已删除 {removed} 个重复文件，失败 {len(errors)} 个。")
    if errors:
        err_path = os.path.join(report_dir, f"_dedup_errors_{ts}.jsonl")
        with open(err_path, "w", encoding="utf-8") as f:
            for e in errors:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"失败详情：{err_path}")


if __name__ == "__main__":
    main()
