#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人民法院案例库「合同纠纷参考案例」结构感知切分脚本。

输入：data/legal_sources/layer2_judicial/cases/caselib/markdown/contract/*.md
输出：带 payload 的 jsonl（每行一个 chunk，可直接喂向量入库）。

切分策略（结构感知优先 → 超长结构块再做长度受控的二级切分）：
  1. 按 Markdown 的 `## ` 标题把全文拆成结构块，再按语义合并为三个「结构组」：
       - 裁判要点  ← 「裁判要点 / 裁判要旨」
       - 基本案情  ← 「基本案情」+「裁判结果」（裁判结果约 92% 为空，多数即基本案情本身）
       - 裁判理由  ← 「裁判理由」+「相关法条」
  2. 每个非空结构组先尝试整块成 chunk；若 token 超上限，则在「组内」做句子感知二级切分，
     overlap 按整句回退（约 10%），绝不跨结构、跨案例切分。
  3. 每个 chunk 注入统一的上下文头（标题 + 案由 + 关键词），上下文头计入 token 预算。
  4. 空结构块不生成 chunk。

token 计量使用 BGE-M3（XLM-RoBERTa）真 tokenizer，离线读本地 HF 缓存。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

# 三个「结构组」：组标签 -> 该组按顺序合并的原始结构块标题
SECTION_GROUPS: list[tuple[str, list[str]]] = [
    ("裁判要点", ["裁判要点 / 裁判要旨"]),
    ("基本案情", ["基本案情", "裁判结果"]),
    ("裁判理由", ["裁判理由", "相关法条"]),
]

# 内联小标签的显示名覆盖：默认用结构块原标题，个别过长的在此简写
INLINE_LABEL_OVERRIDES: dict[str, str] = {"裁判要点 / 裁判要旨": "裁判要旨"}

# 中文句子切分的分隔层级（从粗到细）：先按句末标点，再按分句标点，最后兜底
SENTENCE_DELIMS: list[str] = ["。！？；\n", "，、", ""]

# 特殊 token / 取整的安全余量，避免入库时 CLS/SEP 把长度顶破上限
TOKEN_SAFETY_MARGIN = 8


# ---------------------------------------------------------------------------
# Tokenizer 封装
# ---------------------------------------------------------------------------

class TokenCounter:
    """封装 BGE-M3 tokenizer，提供「不含特殊 token 的纯文本 token 数」计量。"""

    def __init__(self, model_name: str) -> None:
        """加载 tokenizer，优先离线读本地缓存，失败再尝试联网。"""
        from transformers import AutoTokenizer  # 延迟导入，避免无依赖时也报错

        try:
            self._tk = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
        except Exception:  # 本地缓存缺失时退回联网下载（仅 tokenizer，体积很小）
            self._tk = AutoTokenizer.from_pretrained(model_name)

    def count(self, text: str) -> int:
        """统计一段文本的 token 数（不计特殊 token）。"""
        if not text:
            return 0
        return len(self._tk.encode(text, add_special_tokens=False))


# ---------------------------------------------------------------------------
# Markdown 解析
# ---------------------------------------------------------------------------

@dataclass
class CaseDoc:
    """一篇案例文书解析后的结构化结果。"""

    source_file: str                       # 文件名（含 hash 后缀）
    title: str                             # 案例标题（来自首行 `# `）
    source: str = ""                       # 来源子库：caselib（参考案例）/ guiding（指导性案例）
    meta: dict[str, str] = field(default_factory=dict)  # 元数据块键值
    keywords: list[str] = field(default_factory=list)   # 关键词列表
    statutes: list[str] = field(default_factory=list)   # 相关法条列表
    trial_levels: list[str] = field(default_factory=list)  # 审级行（###### 一审/二审…）
    sections: dict[str, str] = field(default_factory=dict)  # 结构块标题 -> 正文


def _strip_or_none(value: str) -> Optional[str]:
    """把 'None' / 空白统一归一为 None，其余去首尾空白返回。"""
    value = (value or "").strip()
    if not value or value == "None":
        return None
    return value


def parse_markdown(path: Path) -> CaseDoc:
    """把一篇案例 Markdown 解析为 CaseDoc。

    解析规则：
      - 首个 `# ` 行为标题；
      - `## xxx` 切分结构块；
      - 「元数据」块按 `- key：value` 解析为字典；
      - 「关键词」块按 `/` 切成列表；
      - 「相关法条」块剥离 `###### 审级行` 后按行切成法条列表，审级行单独收集。
    """
    text = path.read_text(encoding="utf-8")
    doc = CaseDoc(source_file=path.name, title="")

    # 逐行扫描，遇到 `## ` 切换当前结构块
    cur: Optional[str] = None
    buf: list[str] = []

    def flush() -> None:
        """把当前缓冲写入对应结构块。"""
        if cur is not None:
            doc.sections[cur] = "\n".join(buf).strip()

    for line in text.splitlines():
        if doc.title == "" and line.startswith("# ") and not line.startswith("## "):
            doc.title = line[2:].strip()
            continue
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            flush()
            cur = m.group(1).strip()
            buf = []
        elif cur is not None:
            buf.append(line)
    flush()

    # 元数据块：`- 键：值`
    for raw in doc.sections.get("元数据", "").splitlines():
        mm = re.match(r"^-\s*([^：]+)：\s*(.*)$", raw.strip())
        if mm:
            doc.meta[mm.group(1).strip()] = mm.group(2).strip()

    # 关键词块：斜杠分隔
    kw_raw = doc.sections.get("关键词", "").strip()
    if kw_raw:
        doc.keywords = [k.strip() for k in re.split(r"[/、，,\s]+", kw_raw) if k.strip()]

    # 相关法条块：剥离 ###### 审级行
    statute_lines: list[str] = []
    for raw in doc.sections.get("相关法条", "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("######"):
            doc.trial_levels.append(s.lstrip("# ").strip())
        else:
            statute_lines.append(s)
    doc.statutes = statute_lines
    # 把剥离审级行后的纯法条文本回写，供组装正文使用
    doc.sections["相关法条"] = "\n".join(statute_lines).strip()

    return doc


# ---------------------------------------------------------------------------
# 句子感知切分
# ---------------------------------------------------------------------------

def split_sentences(text: str, delim_level: int = 0) -> list[str]:
    """按分隔层级把文本切成「句子」单元，保留分隔符。

    delim_level 指向 SENTENCE_DELIMS：0=句末标点，1=分句标点，2=兜底（逐字符）。
    """
    if delim_level >= len(SENTENCE_DELIMS):
        return [text] if text else []
    delims = SENTENCE_DELIMS[delim_level]
    if delims == "":
        # 兜底层：按字符返回（仅在单句仍超长时配合硬切使用）
        return list(text)

    sents: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in delims:
            piece = "".join(buf).strip()
            if piece:
                sents.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        sents.append(tail)
    return sents


def enforce_max_sentence(
    sentences: list[str], counter: TokenCounter, budget: int, delim_level: int = 0
) -> list[tuple[str, int]]:
    """确保每个句子的 token 数不超过 budget；超长句递归用更细的分隔层级再切。

    返回 (句子文本, token 数) 列表。
    """
    out: list[tuple[str, int]] = []
    for s in sentences:
        n = counter.count(s)
        if n <= budget:
            out.append((s, n))
            continue
        # 当前粒度仍超长：换更细的分隔层级递归
        finer = split_sentences(s, delim_level + 1)
        if len(finer) <= 1:
            # 已无法再细分（例如纯字符层）：按 token 硬窗口切
            out.extend(_hard_window_split(s, counter, budget))
        else:
            out.extend(enforce_max_sentence(finer, counter, budget, delim_level + 1))
    return out


def _hard_window_split(text: str, counter: TokenCounter, budget: int) -> list[tuple[str, int]]:
    """最后兜底：按字符近似硬切，保证每片 token 不超 budget。"""
    pieces: list[tuple[str, int]] = []
    buf = ""
    for ch in text:
        trial = buf + ch
        if counter.count(trial) > budget and buf:
            pieces.append((buf, counter.count(buf)))
            buf = ch
        else:
            buf = trial
    if buf:
        pieces.append((buf, counter.count(buf)))
    return pieces


def _render_labeled(indices: list[int], labeled: list[tuple[str, str, int]]) -> str:
    """把一组（带标签的）句子渲染成 chunk 正文：每段连续同标签的句子前加一次内联标签。

    这样无论该句子段落是某结构组的首块还是续块、是否跨子块，每块开头都带正确标签。
    """
    parts: list[str] = []
    prev_label: Optional[str] = None
    for idx in indices:
        label, sent, _ = labeled[idx]
        if label != prev_label:
            parts.append(("\n" if parts else "") + f"【{label}】")
            prev_label = label
        parts.append(sent)
    return "".join(parts)


def pack_labeled_with_overlap(
    labeled: list[tuple[str, str, int]],
    counter: "TokenCounter",
    budget: int,
    overlap_tokens: int,
) -> list[str]:
    """把（标签, 句子, token 数）贪心装箱为多个 chunk，箱间按整句回退做 overlap。

    装箱时把「每个出现的标签」的 token 开销计入预算（一个 chunk 可能跨 1~2 个子块标签）；
    保证每个 chunk ≤ budget、箱间至少前进一句、overlap 取上一箱末尾若干完整句。
    """
    n = len(labeled)
    if n == 0:
        return []

    # 缓存各标签文本的 token 开销
    label_cost: dict[str, int] = {}

    def cost(label: str) -> int:
        if label not in label_cost:
            label_cost[label] = counter.count(f"【{label}】")
        return label_cost[label]

    chunks: list[str] = []
    i = 0
    while i < n:
        cur: list[int] = []
        cur_labels: set[str] = set()
        cur_tok = 0
        j = i
        while j < n:
            label, _, tok = labeled[j]
            add = tok + (cost(label) if label not in cur_labels else 0)
            if cur and cur_tok + add > budget:  # 至少装一句
                break
            cur.append(j)
            cur_labels.add(label)
            cur_tok += add
            j += 1
        chunks.append(_render_labeled(cur, labeled))
        if j >= n:
            break
        # overlap 起点：从 j-1 往回取整句，累计不超过 overlap_tokens
        ov_tok = 0
        k = j - 1
        while k > i and ov_tok + labeled[k][2] <= overlap_tokens:
            ov_tok += labeled[k][2]
            k -= 1
        next_i = k + 1
        if next_i <= i:  # 保证前进，避免死循环
            next_i = i + 1
        i = next_i
    return chunks


# ---------------------------------------------------------------------------
# 上下文头 + 组装 chunk
# ---------------------------------------------------------------------------

def build_context_header(doc: CaseDoc) -> str:
    """构造注入每个 chunk 的上下文头：标题 + 案由 + 关键词。"""
    cause = _strip_or_none(doc.meta.get("案由", "")) or ""
    parts = [f"案例：{doc.title}"]
    if cause:
        parts.append(f"案由：{cause}")
    if doc.keywords:
        parts.append("关键词：" + "、".join(doc.keywords))
    return "【" + "｜".join(parts) + "】"


def chunk_group_body(
    doc: CaseDoc,
    sub_sections: list[str],
    counter: TokenCounter,
    body_budget: int,
    overlap_tokens: int,
) -> list[str]:
    """把一个结构组切成若干 chunk 正文，每块都带内联小标签。

    内联标签默认用子块原标题，个别过长标题按 INLINE_LABEL_OVERRIDES 简写
    （如「裁判要点 / 裁判要旨」→「裁判要旨」）。
    - 整组（含标签）能装进预算 → 单块返回，子块标签内联保留；
    - 超长 → 在「组内」按子块切句、标签感知装箱，**每个续块也带正确标签**，绝不跨组。
    """
    present = [
        (INLINE_LABEL_OVERRIDES.get(name, name), doc.sections.get(name, "").strip())
        for name in sub_sections
    ]
    present = [(label, body) for label, body in present if body]
    if not present:  # 该结构组为空，不产出 chunk
        return []

    # 先尝试整组单块（含标签）
    combined = "\n".join(f"【{label}】{body}" for label, body in present)
    if counter.count(combined) <= body_budget:
        return [combined]

    # 超长：构造带标签的句子序列（标签不混进句子，渲染时再补）
    labeled: list[tuple[str, str, int]] = []
    for label, body in present:
        label_tok = counter.count(f"【{label}】")
        sents = split_sentences(body, delim_level=0)
        # 单句上限要扣掉自身标签开销，保证「标签+单句」也不超预算
        sized = enforce_max_sentence(sents, counter, body_budget - label_tok, delim_level=0)
        labeled.extend((label, s, n) for s, n in sized)

    return pack_labeled_with_overlap(labeled, counter, body_budget, overlap_tokens)


def build_payload(doc: CaseDoc, section_type: str) -> dict:
    """构造 chunk 的 case 级 payload（键名用英文，chunk_index/total 后续补）。

    字段值保留中文（如案由、法条文本），仅键名英文化，便于程序化检索/过滤。
    """
    return {
        "source_file": doc.source_file,            # 来源文件名（含 hash 后缀）
        "source": doc.source,                      # 子库：caselib（参考案例）/ guiding（指导性案例）
        "section_type": section_type,              # 结构组：裁判要点 / 基本案情 / 裁判理由
        "cause_of_action": _strip_or_none(doc.meta.get("案由", "")),   # 案由
        "keywords": doc.keywords,                  # 关键词列表
        "statutes": doc.statutes,                  # 相关法条列表
        "court": _strip_or_none(doc.meta.get("审理法院", "")),         # 审理法院
        # 以下为便于引用/去重的附加 case 级元信息（成本极低，工程化有用）
        "case_number": _strip_or_none(doc.meta.get("案号", "")),       # 案号
        "case_type": _strip_or_none(doc.meta.get("案例类型", "")),     # 案例类型：参考案例 / 指导性案例
        "guiding_case_number": _strip_or_none(doc.meta.get("指导案例编号", "")),  # 指导案例编号（参考案例为 None）
        "caselib_id": _strip_or_none(doc.meta.get("案例库编号", "")),  # 案例库编号
        "trial_levels": doc.trial_levels,          # 审级行（一审/二审…）
    }


def chunk_case(
    doc: CaseDoc, counter: TokenCounter, max_tokens: int, overlap_ratio: float
) -> list[dict]:
    """把一篇案例切成若干 chunk 记录（含上下文头、payload、chunk_index/total）。"""
    header = build_context_header(doc)
    header_tokens = counter.count(header)
    # 正文净预算 = 上限 − 上下文头 − 安全余量
    body_budget = max_tokens - header_tokens - TOKEN_SAFETY_MARGIN
    if body_budget < 64:  # 极端兜底，正常案例不会触发
        body_budget = 64
    overlap_tokens = int(body_budget * overlap_ratio)

    records: list[dict] = []
    for section_type, sub_sections in SECTION_GROUPS:
        # 结构感知优先 + 超长二级切分（每块带内联标签、句子感知 overlap、绝不跨组）
        bodies = chunk_group_body(doc, sub_sections, counter, body_budget, overlap_tokens)
        for body_text in bodies:
            payload = build_payload(doc, section_type)
            records.append(
                {
                    "text": f"{header}\n{body_text}",  # 入向量的最终文本（含上下文）
                    "context_header": header,           # 单独保留，便于调试/重组
                    "body": body_text,                  # 不含上下文头的纯正文
                    "metadata": payload,
                }
            )

    # 补 chunk_index / chunk_total（按案例全局编号）并生成稳定 id（带来源前缀防撞名）
    stem = Path(doc.source_file).stem
    prefix = f"{doc.source}__" if doc.source else ""
    total = len(records)
    for idx, rec in enumerate(records):
        rec["id"] = f"{prefix}{stem}__{idx:02d}"
        rec["metadata"]["chunk_index"] = idx
        rec["metadata"]["chunk_total"] = total
    return records


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def source_from_case_type(meta: dict[str, str]) -> str:
    """从案例「案例类型」字段推断子库来源（合并后文件夹不再区分来源）。

    指导性案例 → guiding；参考案例 → caselib；其余 → case（兜底）。
    """
    case_type = (meta.get("案例类型") or "").strip()
    if "指导" in case_type:
        return "guiding"
    if "参考" in case_type:
        return "caselib"
    return "case"


def main() -> int:
    """CLI 入口：遍历合并后的案例目录、切分、写统一 jsonl、打印统计。"""
    parser = argparse.ArgumentParser(description="案例（参考案例+指导性案例）合同纠纷结构感知切分")
    parser.add_argument(
        "--input-dir",
        nargs="+",
        default=["data/legal_sources/layer2_judicial/cases/markdown/contract"],
        help="一个或多个输入 Markdown 目录（默认合并后的 cases/markdown/contract）",
    )
    parser.add_argument(
        "--output",
        default="data/legal_sources/layer2_judicial/cases/chunks/contract_cases_chunks.jsonl",
        help="输出 jsonl 路径（默认两库合并到 cases/chunks 下）",
    )
    parser.add_argument("--max-tokens", type=int, default=1500, help="单 chunk token 上限")
    parser.add_argument("--overlap-ratio", type=float, default=0.10, help="二级切分整句 overlap 比例")
    parser.add_argument("--model", default="BAAI/bge-m3", help="计量 token 用的 tokenizer")
    parser.add_argument("--limit", type=int, default=0, help="每个目录只处理前 N 个文件（0=全部，调试用）")
    args = parser.parse_args()

    # 收集待处理文件（来源 source 改由每篇的「案例类型」字段推断，见下）
    files: list[Path] = []
    for raw in args.input_dir:
        d = Path(raw)
        if not d.is_dir():
            print(f"[错误] 输入目录不存在：{d}", file=sys.stderr)
            return 1
        mds = sorted(d.glob("*.md"))
        if args.limit:
            mds = mds[: args.limit]
        files.extend(mds)
    if not files:
        print("[错误] 输入目录下没有 .md 文件", file=sys.stderr)
        return 1

    print(f"[信息] 加载 tokenizer：{args.model} …")
    counter = TokenCounter(args.model)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 统计计数器
    n_chunks = 0
    by_section: dict[str, int] = {}
    by_source: dict[str, int] = {}  # 各子库产出的 chunk 数
    n_secondary = 0          # 触发二级切分的「结构组」数
    token_lens: list[int] = []

    with out_path.open("w", encoding="utf-8") as fout:
        for fp in files:
            try:
                doc = parse_markdown(fp)
                doc.source = source_from_case_type(doc.meta)  # 由案例类型推断子库，写入 payload 与 id 前缀
                records = chunk_case(doc, counter, args.max_tokens, args.overlap_ratio)
            except Exception as exc:  # 单篇失败不应中断整批，记录到日志
                print(f"[警告] 解析失败，跳过 {fp.name}：{exc}", file=sys.stderr)
                continue

            # 统计：同一 section_type 出现多个 chunk 即发生过二级切分
            seen_section: dict[str, int] = {}
            for rec in records:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                st = rec["metadata"]["section_type"]
                by_section[st] = by_section.get(st, 0) + 1
                by_source[doc.source] = by_source.get(doc.source, 0) + 1
                seen_section[st] = seen_section.get(st, 0) + 1
                token_lens.append(counter.count(rec["text"]))
                n_chunks += 1
            n_secondary += sum(1 for c in seen_section.values() if c > 1)

    # 打印汇总
    token_lens.sort()

    def pct(p: float) -> int:
        return token_lens[min(len(token_lens) - 1, int(len(token_lens) * p))] if token_lens else 0

    print("\n================ 切分完成 ================")
    print(f"输入文件      : {len(files)}")
    print(f"输出 chunk    : {n_chunks}  ->  {out_path}")
    print(f"按子库分布    : {by_source}")
    print(f"按结构组分布  : {by_section}")
    print(f"发生二级切分的结构组数: {n_secondary}")
    if token_lens:
        print(
            f"chunk token   : min={token_lens[0]} 中位={pct(0.5)} p95={pct(0.95)} max={token_lens[-1]}"
        )
        over = sum(1 for t in token_lens if t > args.max_tokens)
        print(f"超过上限的 chunk: {over}（应为 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
