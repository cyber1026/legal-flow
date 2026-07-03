"""
全国律协《业务操作指引》丛书（①②③④，约 56 篇正式指引）定向抓取脚本。

背景：这些正式指引以**纸质丛书**出版、acla.org.cn 基本不挂全文网页，全文散落地方律协/
汇编/律所/文库站。故用「策划式 manifest」逐篇定位全文 URL，交通用抓取器
guide_manifest_crawl.run_manifest 处理：抓取 → 抽正文 → 质量门槛 → 归一 → 库内去重 →
复用合同相关筛选落盘。manifest 见 <out>/manifest.jsonl（人审过、可断点续补）。

用法（仓库根目录执行）：
  python scripts/crawl/crawl_acla_guidebook.py
  python scripts/crawl/crawl_acla_guidebook.py --no-cache
  python scripts/crawl/crawl_acla_guidebook.py --crawl-only     # 只抓不筛
  python scripts/crawl/crawl_acla_guidebook.py --filter-only    # 基于已有 all_guides.jsonl 重筛
"""

import argparse
import os

from guide_manifest_crawl import run_manifest

# 默认输出目录（data/ 已 gitignore，产物不入库）
DEFAULT_OUT_DIR = "data/legal_sources/layer3_playbooks/acla_guidebook"


def main():
    """CLI 入口：解析参数后调用通用 manifest 抓取器。"""
    parser = argparse.ArgumentParser(description="全国律协业务操作指引丛书定向抓取（manifest 驱动）。")
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help="输出目录")
    parser.add_argument("--manifest", default=None, help="manifest.jsonl 路径（默认 <out>/manifest.jsonl）")
    parser.add_argument("--no-cache", action="store_true", help="不使用本地缓存")
    parser.add_argument("--crawl-only", action="store_true", help="只抓取不筛选")
    parser.add_argument("--filter-only", action="store_true", help="基于已有 all_guides.jsonl 重筛")
    args = parser.parse_args()

    # manifest 路径：显式指定优先，否则默认放在输出目录下
    manifest_path = args.manifest or os.path.join(args.out, "manifest.jsonl")
    run_manifest(
        manifest_path,
        args.out,
        use_cache=not args.no_cache,
        crawl_only=args.crawl_only,
        filter_only=args.filter_only,
    )


if __name__ == "__main__":
    main()
