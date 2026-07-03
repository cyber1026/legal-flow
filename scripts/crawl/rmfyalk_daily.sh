#!/usr/bin/env bash
# 人民法院案例库「参考案例」每日续抓脚本（受站点每日全文浏览配额 ~100 篇限制）。
#
# 由 cron 每天定时调用，也可手动运行。逻辑：
#   1. 当天已成功跑过 → 直接退出（避免重复抓列表、浪费配额已耗尽的请求）；
#   2. 先 --probe 探活（1 个请求）：cookie 失效 → 记日志并退出（提示更新 cookie）；
#   3. cookie 有效 → 跑 --libs ck（仅参考案例，指导性与 court 重复不抓），缓存按标题续抓，
#      撞每日配额自动停；跑完记当天日期标记。
#
# 用前提：把登录后的整串 Cookie 写入 scripts/crawl/.rmfyalk_cookie（token 约 5 小时有效，需当天刷新）。

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$REPO/.venv/bin/python"
SCRIPT="$REPO/scripts/crawl/crawl_rmfyalk_cases.py"
# 参考案例抓取工作目录（目录结构调整后：cases/_crawl/caselib，存 manifest/raw/logs）。
# 显式作为 --out 传给爬虫，路径在脚本里一目了然。
CASELIB="$REPO/data/legal_sources/layer2_judicial/cases/_crawl/caselib"
# 全部案例 Markdown 统一归档目录；下载前据此按标题去重、跳过已有全文（省每日配额）
MD_ALL="$REPO/data/legal_sources/layer2_judicial/cases/markdown/all"
# 已审核通过的合同子集（正式语料，人工把关；脚本只读不写）
MD_CONTRACT="$REPO/data/legal_sources/layer2_judicial/cases/markdown/contract"
# 待审核暂存区：每日从 all/ 派生的合同候选先进这里，由人工审核后再移入 contract/
MD_PENDING="$REPO/data/legal_sources/layer2_judicial/cases/markdown/contract_pending"
DERIVE="$REPO/scripts/crawl/derive_contract_cases.py"
LOGDIR="$CASELIB/logs"
LOG="$LOGDIR/daily_cron.log"
MARKER="$LOGDIR/.last_success_date"

mkdir -p "$LOGDIR"
cd "$REPO" || exit 1
TODAY="$(date +%F)"
ts() { date '+%F %T'; }

# 1) 当天已成功 → 跳过
if [ "$(cat "$MARKER" 2>/dev/null)" = "$TODAY" ]; then
    echo "[$(ts)] 今日已续抓过，跳过。" >> "$LOG"
    exit 0
fi

# 2) 探活
if ! "$PY" "$SCRIPT" --out "$CASELIB" --probe 2>>"$LOG" | grep -q "有效"; then
    echo "[$(ts)] Cookie 无效/过期，跳过。请更新 scripts/crawl/.rmfyalk_cookie（登录后复制整串 Cookie）。" >> "$LOG"
    exit 0
fi

# 3) 续抓参考案例（新抓到的全文写入 all/）
echo "[$(ts)] Cookie 有效，开始续抓参考案例…" >> "$LOG"
"$PY" "$SCRIPT" --out "$CASELIB" --all-md-dir "$MD_ALL" --libs ck >> "$LOG" 2>&1
echo "$TODAY" > "$MARKER"

# 3b) 从 all/ 派生合同候选到暂存区 contract_pending/（与爬虫同一套分类器；只写暂存、待人工审核）
echo "[$(ts)] 从 all/ 派生合同候选到 contract_pending/（待人工审核）…" >> "$LOG"
"$PY" "$DERIVE" --all-dir "$MD_ALL" --contract-dir "$MD_CONTRACT" --pending-dir "$MD_PENDING" --apply >> "$LOG" 2>&1

# 记录当前全文进度
"$PY" - <<'PYEOF' >> "$LOG" 2>&1
import json
ROOT="data/legal_sources/layer2_judicial/cases/_crawl/caselib"
kept=[json.loads(l) for l in open(f"{ROOT}/manifest/contract_related_cases.jsonl",encoding="utf-8")]
full=sum(1 for r in kept if r.get("content_full"))
print(f"  进度：合同相关参考案例 {len(kept)} 篇，已全文 {full}，待补 {len(kept)-full}")
PYEOF
echo "[$(ts)] 本日续抓结束。" >> "$LOG"
