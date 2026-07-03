#!/usr/bin/env python3
"""Generate normalized legal-source inputs for contract review knowledge bases.

This script is intentionally conservative:

- High-confidence contract-review materials are written to `_normalized/docs`
  and `_normalized/chunks`.
- Lower-confidence or adjacent materials are written to `_normalized/review`
  for manual approval before ingestion.
- Raw crawler outputs are never modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_ROOT = Path("data/legal_sources")

JUDICIAL_RELATED = [
    "layer2_judicial/interpretations/spc_court/manifest/contract_related_judicial_interpretations.jsonl",
    "layer2_judicial/interpretations/spc_gazette/manifest/contract_related_judicial_interpretations.jsonl",
    "layer2_judicial/interpretations/spp/manifest/contract_related_judicial_interpretations.jsonl",
]
JUDICIAL_CLASSIFIED = [
    "layer2_judicial/interpretations/spc_court/manifest/classified_all.jsonl",
    "layer2_judicial/interpretations/spc_gazette/manifest/classified_all.jsonl",
    "layer2_judicial/interpretations/spp/manifest/classified_all.jsonl",
]

CASE_RELATED = [
    "layer2_judicial/cases/guiding/manifest/contract_related_cases.jsonl",
    "layer2_judicial/cases/caselib/manifest/contract_related_cases.jsonl",
]
CASE_CLASSIFIED = [
    "layer2_judicial/cases/guiding/manifest/classified_all.jsonl",
    "layer2_judicial/cases/caselib/manifest/classified_all.jsonl",
]

PLAYBOOK_RELATED = [
    "layer3_playbooks/acla/manifest/contract_related_guides.jsonl",
    "layer3_playbooks/acla_guidebook/manifest/contract_related_guides.jsonl",
    "layer3_playbooks/shanghai_bar/manifest/contract_related_guides.jsonl",
]
PLAYBOOK_CLASSIFIED = [
    "layer3_playbooks/acla/manifest/classified_all.jsonl",
    "layer3_playbooks/acla_guidebook/manifest/classified_all.jsonl",
    "layer3_playbooks/shanghai_bar/manifest/classified_all.jsonl",
]

STANDARD_CONTRACT_RELATED = [
    "layer4_standard_contracts/samr_national/manifest/contract_related_standard_contracts.jsonl",
    "layer4_standard_contracts/samr_local/manifest/contract_related_standard_contracts.jsonl",
]
STANDARD_CONTRACT_CLASSIFIED = [
    "layer4_standard_contracts/samr_national/manifest/classified_all.jsonl",
    "layer4_standard_contracts/samr_local/manifest/classified_all.jsonl",
]
STANDARD_CLAUSES = [
    "layer4_standard_contracts/samr_national/manifest/all_standard_clauses.jsonl",
    "layer4_standard_contracts/samr_local/manifest/all_standard_clauses.jsonl",
]

DIRECT_PLAYBOOK_TITLE_KEYWORDS = [
    "合同审查",
    "合同起草",
    "合同法律事务",
    "买卖合同",
    "房屋租赁合同",
    "商品房买卖合同",
    "二手房买卖合同",
    "建设工程",
    "施工合同",
    "工程合同",
    "工程争议",
    "劳务派遣合同",
    "劳动合同",
    "技术合同",
    "特许经营",
    "股权转让",
    "股权代持",
    "并购",
    "重组",
    "借款",
    "融资租赁合同",
    "融资性贸易",
    "保理",
    "担保",
    "保函",
    "信用证",
    "票据",
    "保险合同",
    "货物运输",
    "海上货物",
    "租赁合同",
    "承包合同",
    "承揽合同",
    "委托合同",
    "采购合同",
    "供货合同",
    "经销合同",
    "加盟合同",
    "保密协议",
    "竞业限制",
    "对赌",
]

BROAD_PLAYBOOK_KEYWORDS = [
    "企业法律顾问",
    "公司治理",
    "税法",
    "税务",
    "劳动",
    "知识产权",
    "商业秘密",
    "数据",
    "个人信息",
    "破产",
    "债权",
    "合规",
    "证券",
    "资本市场",
    "商事仲裁",
    "律师函",
]

PLAYBOOK_REVIEW_TITLE_KEYWORDS = [
    "尽职调查",
    "评估指引",
    "合规",
    "涉税",
    "税务",
    "法律顾问",
    "公司治理",
    "知识产权",
    "数据",
    "个人信息",
    "破产",
    "清算",
    "重整",
    "劳动人事",
    "劳动与社会保障",
    "证券投资信托",
    "私募投资基金",
    "家族信托",
    "REITs",
    "法律意见书",
    "民事调解协议",
    "生态环境损害赔偿协议",
    "程序法",
    "诉之利益",
    "聘请律师合同",
    "IPO",
    "新政",
    "重磅",
]

PURE_LITIGATION_KEYWORDS = [
    "诉讼",
    "民事赔偿",
    "执行",
    "侵权",
    "案件",
    "仲裁案件",
    "证据保全",
    "强制令",
    "损害赔偿",
    "虚假陈述",
]

STANDARD_CONTRACT_FORM_TITLE_KEYWORDS = [
    "合同",
    "协议",
    "合同书",
    "运单",
    "凭证",
    "委托单",
    "确认书",
    "订单",
    "承诺书",
    "保单",
    "收据",
]

STANDARD_CONTRACT_REVIEW_TITLE_KEYWORDS = [
    "信息卡",
    "参考式样",
    "告知书",
    "行为指引",
    "须知",
    "说明",
]

NOISE_TITLE_KEYWORDS = [
    "会议",
    "论坛",
    "培训",
    "研讨",
    "讲座",
    "活动",
    "年会",
    "通知",
    "公告",
    "倡议",
    "党建",
    "团建",
    "表彰",
    "招聘",
    "征文",
    "报名",
]

CONTRACT_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "买卖/供货": ["买卖", "采购", "销售", "供货", "订购", "经销"],
    "建设工程": ["建设工程", "施工", "工程", "监理", "装饰装修", "预拌", "招投标"],
    "房屋/不动产": ["商品房", "房屋", "租赁", "房地产", "物业", "土地", "不动产"],
    "公司/股权": ["股权", "公司治理", "关联交易", "并购", "重组", "增资", "投资"],
    "劳动用工": ["劳动", "劳务派遣", "用工", "竞业限制", "劳动合同"],
    "知识产权/数据": ["知识产权", "专利", "商标", "著作权", "商业秘密", "数据", "个人信息"],
    "金融担保": ["担保", "保理", "融资租赁", "借款", "票据", "信用证", "保函", "保险"],
    "运输物流": ["运输", "物流", "货运", "海商", "海事", "仓储", "快递"],
    "服务/委托": ["服务", "委托", "承揽", "加工", "家政", "旅游", "培训"],
}

CLAUSE_TYPE_KEYWORDS: dict[str, list[str]] = {
    "主体": ["主体", "当事人", "资质", "资格", "授权", "法定代表人", "代理"],
    "标的": ["标的", "货物", "服务内容", "工程范围", "成果", "数量"],
    "价款": ["价款", "价格", "费用", "报酬", "租金", "服务费"],
    "付款": ["付款", "支付", "结算", "发票", "收款", "保证金", "定金"],
    "质量": ["质量", "质量标准", "质量要求", "验收标准", "维修", "保修", "瑕疵"],
    "交付验收": ["交付", "交货", "验收", "运输", "风险转移"],
    "违约": ["违约", "赔偿", "损失", "违约金", "责任"],
    "解除终止": ["解除", "终止", "撤销", "无效"],
    "争议解决": ["管辖", "仲裁", "诉讼", "争议解决", "法律适用"],
    "保密": ["保密", "商业秘密"],
    "知识产权": ["知识产权", "专利", "商标", "著作权", "许可"],
}

PLAYBOOK_SERVICE_STAGE_KEYWORDS: dict[str, list[str]] = {
    "签约前调查": ["签订前", "订立前", "资信调查", "尽职调查", "背景调查", "主体资格", "履约能力"],
    "合同起草/审查": ["合同审查", "合同起草", "起草", "审查", "修改", "条款设计", "合同主文", "合同文本"],
    "谈判签署": ["谈判", "磋商", "签署", "签订", "用印", "授权委托", "见证"],
    "履行管理": ["履行", "交付", "验收", "付款", "结算", "变更", "通知", "归档"],
    "争议处理": ["纠纷", "争议", "诉讼", "仲裁", "调解", "起诉", "应诉", "证据", "执行"],
}

PLAYBOOK_REVIEW_TASK_KEYWORDS: dict[str, list[str]] = {
    "条款完备性审查": ["主文应包括", "应包括以下内容", "合同应明确", "应明确", "约定以下内容", "条款"],
    "主体/资质审查": ["主体资格", "资质", "身份", "营业执照", "授权", "法定代表人", "代理"],
    "材料/证照清单": ["提交下列资料", "证明文件", "许可证", "证书", "材料", "单证"],
    "风险识别": ["风险提示", "风险", "不利后果", "法律后果", "无效", "解除", "赔偿"],
    "修改建议": ["建议", "修改", "修正", "补充", "替代方案", "提示"],
    "流程操作": ["流程", "程序", "步骤", "办理", "工作要点", "操作"],
    "证据留痕": ["证据", "留存", "留痕", "底稿", "记录", "送达", "归档"],
}

PLAYBOOK_PARTY_PERSPECTIVE_KEYWORDS: dict[str, list[str]] = {
    "买方/买受人": ["买方", "买受人", "购房者"],
    "卖方/出卖人": ["卖方", "出卖人", "销售方", "供货方"],
    "开发商": ["开发商", "房地产开发企业"],
    "发包人/业主": ["发包人", "业主", "建设单位"],
    "承包人/施工方": ["承包人", "施工单位", "总承包单位", "分包单位"],
    "债权人": ["债权人", "贷款人", "担保权人"],
    "债务人": ["债务人", "借款人", "保证人"],
    "公司": ["公司", "企业", "股东", "董事", "高管"],
    "劳动者": ["劳动者", "员工", "职工"],
    "用人单位": ["用人单位", "雇主"],
}

PLAYBOOK_WEB_NOISE_EXACT = {
    "|",
    ")",
    "目录",
    "点赞",
    "精彩评论",
    "作者其他文章",
    "名律网官方账号",
}
PLAYBOOK_WEB_NOISE_PREFIXES = (
    "作者:",
    "作者：",
    "浏览次数",
    "评论(",
    "评论（",
    "点赞(",
    "点赞（",
    "名律网新功能上线",
    "相关推荐",
    "推荐阅读",
    "相关阅读",
)
PLAYBOOK_FOOTER_MARKERS = (
    "作者其他文章",
    "名律网新功能上线",
    "精彩评论",
    "相关推荐",
    "推荐阅读",
    "相关阅读",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="legal_sources root directory")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="normalized output directory, defaults to <root>/_normalized",
    )
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}") from exc
            if isinstance(row, dict):
                row["_source_manifest"] = str(path)
                rows.append(row)
    return rows


# JSONL 字段排序：重要字段（标识/标题/分类/路由）放前面，便于人工 head 检查；
# 大段正文与簿记字段（body/sha/manifest）放最后，避免巨长正文挤在行首看不到关键信息。
FIELD_ORDER_FRONT = [
    "doc_id", "chunk_id", "review_id", "kb_type", "status",
    "chunk_kind",
    "title", "contract_title",
    "citation", "cites", "doc_no", "contract_doc_no",
    "case_no", "court_case_no", "case_category", "cause_of_action", "court",
    "article_no", "section",
    "clause_no", "clause_title", "normalized_clause_type", "clause_role",
    "contract_domain", "contract_domains", "clause_types",
    "scope", "region", "publish_year", "publish_agencies",
    "contract_priority", "filter_decision", "filter_reason",
    "suggested_decision", "review_category", "review_reason",
    "content_depth", "retrieval_weight", "source_quality",
]
FIELD_ORDER_BACK = [
    # 大段正文放最后
    "holding", "facts", "reasoning", "judgment", "legal_text", "body",
    "contract_outline", "clause_text", "risk_tips", "excerpt", "source_excerpt", "text",
    "embed_text", "embedding_text",
    # 长度/标记类
    "body_len", "risk_tips_len", "clause_len", "text_len", "is_short_clause",
    "clause_count", "legacy_law_refs", "matched_keywords", "review_flags", "merged_from", "neighbors", "neighbor_ids",
    # 时间/溯源/簿记放最后
    "publish_time", "publish_date", "effective_date", "crawl_time",
    "source", "source_site", "source_url", "url", "source_title", "source_doc_no", "source_manifest", "source_layer",
    "content_sha256",
]


def order_fields(row: dict[str, Any]) -> dict[str, Any]:
    """按 FRONT/BACK 重排字段，未列出的保持原序居中。"""
    front = [k for k in FIELD_ORDER_FRONT if k in row]
    back = [k for k in FIELD_ORDER_BACK if k in row]
    pinned = set(front) | set(back)
    middle = [k for k in row if k not in pinned]
    return {k: row[k] for k in (*front, *middle, *back)}


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(order_fields(row), ensure_ascii=False) + "\n")
            count += 1
    return count


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        ordered: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    ordered.append(key)
        fields = ordered
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: scalar_for_csv(row.get(key)) for key in fields})
    return len(rows)


def scalar_for_csv(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return value


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(as_text(item) for item in value if item is not None)
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def clean_line(text: Any) -> str:
    return re.sub(r"\s+", " ", as_text(text)).strip()


def clean_text(text: Any, title: str = "", aggressive: bool = False) -> str:
    raw = as_text(text)
    if not raw:
        return ""
    raw = raw.replace("\u3000", " ").replace("_ueditor_page_break_tag_", "\n")
    raw = rejoin_split_numbers(raw)
    if aggressive and title and title in raw:
        first_title = raw.find(title)
        if first_title > 200:
            raw = raw[first_title:]
    for marker in [
        "\n分享到", "\n免责声明：本网", "\n版权所有：", "\nCopyright",
        "\n中国共产党新闻网", "\n地址：北京市东城区东交民巷",  # 政府站页脚导航/版权块
    ]:
        marker_idx = raw.find(marker)
        if aggressive and marker_idx > 0:
            raw = raw[:marker_idx]
    lines = []
    skip_exact = {
        "返回列表",
        "上一篇",
        "下一篇",
        "分享到：",
        "QQ空间",
        "新浪微博",
        "微信",
        "查看更多",
        "网友留言",
        "文章分类",
        "全部文章",
    }
    skip_prefixes = (
        "【责任编辑",
        "责任编辑",
        "Copyright",
        "版权所有",
        "技术咨询",
        "联系电话",
        "电子邮箱",
    )
    for raw_line in raw.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", raw_line).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if aggressive and (line in skip_exact or line.startswith(skip_prefixes)):
            continue
        lines.extend(expand_playbook_concatenated_headings(line))
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# 文号正则：法释〔2012〕14号 / 高检发释字〔…〕…号 / 法发…号 等
_DOC_NO_RE = re.compile(r"(法释|高检发释字|法发|法函|高检发)〔?\s*\d{4}\s*〕?\s*第?\s*\d+\s*号")


def rejoin_split_numbers(text: str) -> str:
    """修复爬虫把数字/日期/文号逐字符断行的产物。

    例：'2011\\n年\\n11\\n月\\n21\\n日' -> '2011年11月21日'，
        '法释〔\\n2012\\n〕\\n14\\n号' -> '法释〔2012〕14号'。
    只在数字与「数字 / 中文计量单位 / 括号」之间去换行，避免误并正文。
    """
    if not text:
        return text
    patterns = [
        (r"(?<=\d)[ \t]*\n+[ \t]*(?=\d)", ""),                                    # 数字↔数字
        (r"(?<=\d)[ \t]*\n+[ \t]*(?=[年月日时分秒次条项款届号期％%、〕）)])", ""),    # 数字→单位/右括号
        (r"(?<=[年月日〔（(第])[ \t]*\n+[ \t]*(?=\d)", ""),                        # 单位/左括号→数字
        (r"(?<=〕)[ \t]*\n+[ \t]*(?=[\d号])", ""),                                # 〕→数字/号
    ]
    prev = None
    while prev != text:  # 迭代到不动点，处理 年→数字→月 这类链式断行
        prev = text
        for pat, repl in patterns:
            text = re.sub(pat, repl, text)
    return text


def extract_doc_no(text: str) -> str:
    """从正文兜底抽取文号（元数据 doc_no 缺失时用）。"""
    match = _DOC_NO_RE.search(text or "")
    return clean_line(match.group(0)).replace(" ", "") if match else ""


# —— P1-5 引用解析：把 relevant_statutes / 正文里的法律引用结构化为 [{law, article}] ——
# 用于建「法条 → 文档」倒排，是律师式检索（先定法条再找解释/案例）的第二条路径。
_CITE_LAW_RE = re.compile(r"《([^》]{2,40})》\s*([^《\n]*)")
_CITE_ART_RE = re.compile(
    r"(?:第)?([0-9一二三四五六七八九十百零两]+)条"
    r"(?:第([0-9一二三四五六七八九十百零两]+)款)?"
    r"(?:第([0-9一二三四五六七八九十百零两]+)项)?"
)


def normalize_law_name(name: str) -> str:
    """规范法律名：去《》与"中华人民共和国"前缀，便于倒排聚合。"""
    name = clean_line(name).strip("《》 ")
    return re.sub(r"^中华人民共和国", "", name)


def _format_citation_article(match: re.Match[str]) -> str:
    article, paragraph, item = match.groups()
    text = f"第{article}条"
    if paragraph:
        text += f"第{paragraph}款"
    if item:
        text += f"第{item}项"
    return text


def parse_citations(*texts: str) -> list[dict[str, Any]]:
    """抽取结构化引用 [{law, article}]；同一法多条会展开成多项，去重。"""
    text = "\n".join(clean_line(t) for t in texts if t)
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in _CITE_LAW_RE.finditer(text):
        law = normalize_law_name(match.group(1))
        if not law:
            continue
        articles = [_format_citation_article(m) for m in _CITE_ART_RE.finditer(match.group(2) or "")]
        for article in articles or [""]:
            key = (law, article)
            if key in seen:
                continue
            seen.add(key)
            results.append({"law": law, "article": article or None})
    return results


def clean_relevant_statutes(value: Any) -> str:
    """清理案例「相关法条」字段，避免把后一节裁判经过标题一并带入。"""
    lines: list[str] = []
    for line in clean_text(value).splitlines():
        line = clean_line(line)
        if not line:
            continue
        if line.startswith("#") or re.match(r"^[一二三四五六七八九十]+审[:：]", line):
            break
        if "《" not in line and "条" not in line:
            break
        lines.append(line)
    return "\n".join(lines)


def detect_status(title: str, body: str) -> str:
    """P1-7 时效状态（保守）：自述文本里多数"废止"是本篇废止旧篇、不代表自身失效，
    无法可靠判定，故默认 active；仅标题显式"已废止"才标 superseded。
    真·废止追踪需外部废止清单，留待后续。"""
    return "superseded" if "已废止" in (title or "") else "active"


def dedupe_by_key(docs: list[dict[str, Any]], key_fields: list[str]) -> tuple[list[dict[str, Any]], int]:
    """P1-4 去重：按自然键（首个非空 key_fields，兜底 content_sha256）保留首条。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    dropped = 0
    for doc in docs:
        key = ""
        for field in key_fields:
            if doc.get(field):
                key = clean_line(doc[field])
                break
        key = key or doc.get("content_sha256") or doc.get("doc_id") or ""
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(doc)
    return out, dropped


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    payload = "\n".join(clean_line(part) for part in parts if part is not None)
    digest = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def best_title(row: dict[str, Any], keys: list[str]) -> str:
    values = [clean_line(row.get(key)) for key in keys]
    values = [value for value in values if value]
    if not values:
        return "未命名"
    values.sort(key=lambda value: (("已于" in value) or ("现予公布" in value), len(value)))
    return values[0]


def normalize_title(title: str) -> str:
    title = re.sub(r"\s+", "", title or "")
    title = re.sub(r"[《》“”\"'（）()【】\[\]：:，,。；;、|｜_-]", "", title)
    title = re.sub(r"\d{4}年|\d{4}版|（试行）|试行|修订版", "", title)
    return title.lower()


def collect_domains(title: str, body: str = "") -> list[str]:
    match_text = f"{title}\n{body[:2000]}"
    domains = [domain for domain, keywords in CONTRACT_DOMAIN_KEYWORDS.items() if any(kw in match_text for kw in keywords)]
    return domains or ["通用"]


def collect_clause_types(text: str) -> list[str]:
    return [name for name, keywords in CLAUSE_TYPE_KEYWORDS.items() if any(kw in text for kw in keywords)]


STANDARD_DOMAIN_ALIASES = {
    "买卖": "买卖/供货",
    "供货": "买卖/供货",
    "采购": "买卖/供货",
    "建设工程": "建设工程",
    "工程": "建设工程",
    "房地产": "房屋/不动产",
    "房屋": "房屋/不动产",
    "租赁": "房屋/不动产",
    "服务": "服务/委托",
    "委托": "服务/委托",
    "知识产权": "知识产权/数据",
    "运输物流": "运输物流",
    "通用": "通用",
}

CLAUSE_TYPE_ALIASES = {
    "其他": "",
    "主体": "主体",
    "标的": "标的",
    "价款": "价款",
    "付款": "付款",
    "质量": "质量",
    "期限": "期限",
    "交付验收": "交付验收",
    "违约": "违约",
    "解除终止": "解除终止",
    "争议解决": "争议解决",
    "通知": "通知",
    "保密": "保密",
    "知识产权": "知识产权",
}

CLAUSE_FUNCTION_KEYWORDS: dict[str, list[str]] = {
    "约定当事人身份、资质、授权和签约权限": ["甲方", "乙方", "当事人", "主体", "资质", "资格", "授权", "法定代表人", "代理人"],
    "约定标的、数量、范围、服务内容或工程范围": ["标的", "数量", "规格", "型号", "服务内容", "工程范围", "工作内容", "成果"],
    "约定价款、费用、租金、报酬和计价方式": ["价款", "价格", "费用", "租金", "报酬", "服务费", "计价", "总价", "单价"],
    "约定付款节点、付款条件、付款期限和结算资料": ["付款", "支付", "结算", "发票", "收款", "预付款", "进度款", "尾款", "保证金"],
    "约定质量标准、验收方式、异议期限和不合格处理": ["质量标准", "质量要求", "验收", "异议", "不合格", "维修", "保修", "整改"],
    "约定交付、交货、运输、风险转移和签收": ["交付", "交货", "运输", "签收", "风险转移", "交接", "到货"],
    "约定履行期限、工期、顺延、延期和逾期后果": ["期限", "工期", "顺延", "延期", "逾期", "交付时间", "完成时间"],
    "约定违约责任、违约金、赔偿、损失和责任限制": ["违约", "违约金", "赔偿", "损失", "责任限制", "逾期付款", "逾期交付"],
    "约定解除、终止、单方解除、通知解除和合同退出": ["解除", "终止", "单方解除", "通知解除", "提前终止"],
    "约定争议解决方式、管辖、仲裁、诉讼和适用法律": ["争议解决", "管辖", "仲裁", "诉讼", "仲裁委员会", "适用法律"],
    "约定通知送达、联系人、地址、电子送达和送达生效": ["通知", "送达", "联系人", "通讯地址", "电子邮件", "视为送达"],
    "约定保密义务、商业秘密、披露限制和保密期限": ["保密", "商业秘密", "披露", "泄露", "保密期限"],
    "约定知识产权归属、许可、成果权利和侵权责任": ["知识产权", "专利", "商标", "著作权", "许可", "成果归属", "侵权"],
}

RISK_SIGNAL_KEYWORDS: dict[str, list[str]] = {
    "主体资质或授权不明": ["无权代理", "未经授权", "资质", "资格", "主体不适格", "营业执照", "授权委托"],
    "标的范围、数量、规格或成果标准不清": ["标的不明", "范围不明", "数量不明", "规格不明", "成果不明", "另行确定"],
    "付款条件、期限或结算依据不明": ["付款条件", "付款期限", "结算依据", "发票", "验收合格后付款", "审计结果", "以甲方确认为准"],
    "质量标准、验收流程或异议期限不清": ["质量标准", "质量要求", "验收", "异议期", "视为验收", "不合格", "整改", "保修"],
    "交付、签收、运输或风险转移约定不清": ["交付", "交货", "签收", "运输", "风险转移", "毁损灭失"],
    "履行期限、工期顺延或延期责任不清": ["期限", "工期", "顺延", "延期", "逾期", "不可抗力"],
    "违约责任缺失、过重、过轻或不对等": ["违约责任", "违约金", "赔偿", "损失", "不承担责任", "全部责任", "单方承担"],
    "解除条件、通知程序或退出后果不清": ["解除", "终止", "单方解除", "提前解除", "通知", "清算", "返还"],
    "争议解决条款无效、冲突或管辖不清": ["管辖", "仲裁", "诉讼", "争议解决", "仲裁委员会", "适用法律"],
    "通知送达地址、方式或生效规则不清": ["通知", "送达", "地址", "电子邮件", "短信", "视为送达"],
    "保密范围、期限、例外或违约后果不清": ["保密", "商业秘密", "泄露", "披露", "保密期限"],
    "知识产权归属、许可范围或侵权责任不清": ["知识产权", "著作权", "专利", "商标", "许可", "成果归属", "侵权"],
    "单方决定权、最终解释权或权利义务明显失衡": ["单方决定", "最终解释权", "以甲方为准", "无条件", "不得提出异议", "自行承担"],
}

TRIGGER_KEYWORDS: dict[str, list[str]] = {
    "验收合格": ["验收合格", "验收通过", "签收", "确认"],
    "收到发票或付款申请": ["发票", "付款申请", "收款账户", "结算单"],
    "逾期履行": ["逾期", "迟延", "未按期", "延期"],
    "质量不合格": ["质量不合格", "不符合标准", "瑕疵", "整改"],
    "违约或违反约定": ["违约", "违反本合同", "未履行", "不履行"],
    "解除或终止事件": ["解除", "终止", "提前终止", "单方解除"],
    "不可抗力或政策变化": ["不可抗力", "政策变化", "政府原因"],
}

LEGAL_CONSEQUENCE_KEYWORDS: dict[str, list[str]] = {
    "支付价款、费用、利息或违约金": ["支付", "付款", "利息", "违约金", "滞纳金"],
    "赔偿损失或承担违约责任": ["赔偿", "损失", "违约责任", "承担责任"],
    "返修、更换、退货、整改或重作": ["返修", "更换", "退货", "整改", "重作"],
    "暂停履行、解除合同或终止合同": ["暂停履行", "解除合同", "终止合同", "解除本合同"],
    "没收、扣除或返还保证金": ["保证金", "定金", "扣除", "没收", "返还"],
    "提交争议解决机构处理": ["仲裁", "诉讼", "仲裁委员会"],
}


def normalized_contract_domains(*values: Any) -> list[str]:
    domains: list[str] = []
    for value in values:
        if isinstance(value, list):
            parts = value
        else:
            parts = re.split(r"[/／、,，\s]+", clean_line(value))
        for part in parts:
            item = clean_line(part)
            if not item:
                continue
            domains.append(STANDARD_DOMAIN_ALIASES.get(item, item))
    return dedupe_keep_order([domain for domain in domains if domain]) or ["通用"]


def collect_standard_contract_domains(title: str, text: str, raw_domain: Any = "") -> list[str]:
    domains = normalized_contract_domains(raw_domain)
    inferred = collect_domains(title, text)
    if domains == ["通用"]:
        domains = []
    domains.extend(inferred)
    return dedupe_keep_order([domain for domain in domains if domain]) or ["通用"]


def collect_standard_clause_types(*texts: Any) -> list[str]:
    types: list[str] = []
    for text in texts:
        line = clean_line(text)
        if not line:
            continue
        alias = CLAUSE_TYPE_ALIASES.get(line)
        if alias:
            types.append(alias)
        types.extend(collect_clause_types(line))
    return dedupe_keep_order([item for item in types if item])


def keyword_hits(text: str, mapping: dict[str, list[str]], limit: int = 8) -> list[str]:
    hits = [label for label, keywords in mapping.items() if any(keyword in text for keyword in keywords)]
    return hits[:limit]


def clause_semantic_fingerprint(text: str, clause_types: list[str] | None = None) -> dict[str, list[str]]:
    text = clean_text(text)
    functions = keyword_hits(text, CLAUSE_FUNCTION_KEYWORDS, limit=5)
    risks = keyword_hits(text, RISK_SIGNAL_KEYWORDS, limit=8)
    triggers = keyword_hits(text, TRIGGER_KEYWORDS, limit=6)
    consequences = keyword_hits(text, LEGAL_CONSEQUENCE_KEYWORDS, limit=6)
    if clause_types:
        for clause_type in clause_types:
            for function, keywords in CLAUSE_FUNCTION_KEYWORDS.items():
                if clause_type and any(clause_type in keyword or keyword in clause_type for keyword in keywords):
                    functions.append(function)
    return {
        "functions": dedupe_keep_order(functions),
        "risks": dedupe_keep_order(risks),
        "triggers": dedupe_keep_order(triggers),
        "consequences": dedupe_keep_order(consequences),
    }


def compact_original_text(text: str, limit: int = 900) -> str:
    lines = [clean_line(line) for line in clean_text(text).splitlines() if clean_line(line)]
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) <= limit:
        return body
    signal_terms = [
        "甲方", "乙方", "买方", "卖方", "发包人", "承包人", "委托人", "受托人",
        "应", "应当", "不得", "有权", "负责", "承担", "支付", "验收", "交付",
        "违约", "解除", "终止", "赔偿", "管辖", "仲裁",
    ]
    signal_lines = [line for line in lines if any(term in line for term in signal_terms)]
    signal_body = "\n".join(signal_lines)
    if 120 <= len(signal_body) <= limit:
        return signal_body
    if len(signal_body) > limit:
        return signal_body[:limit].rstrip()
    return body[:limit].rstrip()


def join_label(label: str, values: Any) -> str:
    if isinstance(values, list):
        value = "、".join(clean_line(item) for item in values if clean_line(item))
    else:
        value = clean_line(values)
    return f"{label}：{value}" if value else ""


def make_llm_context_header(row: dict[str, Any]) -> str:
    """把检索后 LLM 判断来源/适用性的关键信息压到 text 头部。

    embedding_text 仍负责召回；这里不服务召回，只保证工具如果只传 text，
    LLM 也能看到材料类型、领域、条款类型、来源和时效风险。
    """
    case_no = clean_line(row.get("case_no")) or clean_line(row.get("court_case_no"))
    lines = [
        join_label("资料类型", row.get("kb_type")),
        join_label("材料性质", row.get("chunk_kind")),
        join_label("标题", row.get("title")),
        join_label("合同", row.get("contract_title")),
        join_label("引用", row.get("citation")),
        join_label("案由", row.get("cause_of_action")),
        join_label("案号", case_no),
        join_label("法院", row.get("court")),
        join_label("效力状态", row.get("status")),
        join_label("合同领域", row.get("contract_domains")),
        join_label("条款类型", row.get("clause_types") or row.get("normalized_clause_type")),
        join_label("审查任务", row.get("review_task")),
        join_label("适用阶段", row.get("service_stage")),
        join_label("适用方", row.get("party_perspective")),
        join_label("地区", row.get("region")),
        join_label("年份", row.get("publish_year")),
        join_label("发布机关", row.get("publish_agencies")),
        join_label("旧法引用", row.get("legacy_law_refs")),
        join_label("检索权重", row.get("retrieval_weight")),
        join_label("来源链接", row.get("source_url")),
    ]
    return "\n".join(line for line in lines if line)


def prepend_llm_context(row: dict[str, Any], text: str) -> str:
    header = make_llm_context_header(row)
    body = clean_text(text)
    return f"{header}\n\n{body}".strip() if header else body


def source_quality(row: dict[str, Any]) -> int:
    site = clean_line(row.get("source_site"))
    if row.get("from_guidebook"):
        return 4
    if "lawyers.org.cn" in site:
        return 3
    if "acla.org.cn" in site:
        return 3
    if "court.gov.cn" in site or "人民法院" in site:
        return 3
    if "samr.gov.cn" in site:
        return 3
    return 1


def review_item(
    *,
    kb_type: str,
    source_layer: str,
    title: str,
    reason: str,
    suggested_decision: str,
    row: dict[str, Any],
    body: str = "",
    priority: str = "",
    review_category: str = "relevance",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = {
        "review_id": stable_id("review", kb_type, row.get("url"), title, reason),
        "kb_type": kb_type,
        "source_layer": source_layer,
        "source_site": row.get("source_site") or row.get("source") or "",
        "source_manifest": row.get("_source_manifest", ""),
        "title": title,
        "url": row.get("url") or "",
        "contract_priority": priority or row.get("contract_priority") or "",
        "review_category": review_category,
        "suggested_decision": suggested_decision,
        "review_reason": reason,
        "classify_reason": row.get("classify_reason") or "",
        "matched_keywords": row.get("matched_keywords") or row.get("contract_keyword_hits") or [],
        "body_len": len(body) if body else row.get("body_len") or "",
        "excerpt": make_excerpt(body or row.get("body") or row.get("legal_text") or row.get("holding") or ""),
    }
    if extra:
        item.update(extra)
    return item


def make_excerpt(text: Any, limit: int = 360) -> str:
    cleaned = clean_text(text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."


def normalize_judicial(root: Path, out: Path) -> dict[str, Any]:
    docs: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for rel_path in JUDICIAL_RELATED:
        for row in read_jsonl(root / rel_path):
            title = best_title(row, ["title_from_list", "doc_title", "page_title"])
            body = clean_text(row.get("legal_text") or row.get("body"), title=title, aggressive=True)
            title = recover_title(title, body)  # 标题被截断则从正文补全完整正式标题
            priority = clean_line(row.get("contract_priority"))
            source_layer = "第二层：司法解释"
            if priority not in ("P0_CONTRACT", "P1_CIVIL"):
                review.append(
                    review_item(
                        kb_type="judicial_interpretation",
                        source_layer=source_layer,
                        title=title,
                        reason="低置信合同相关（非 P0/P1），需要确认是否进入合同审查库",
                        suggested_decision="review",
                        row=row,
                        body=body,
                        priority=priority,
                    )
                )
                continue
            if len(body) < 200:
                review.append(
                    review_item(
                        kb_type="judicial_interpretation",
                        source_layer=source_layer,
                        title=title,
                        reason="正文过短或抽取不完整，需要复核",
                        suggested_decision="review",
                        row=row,
                        body=body,
                        priority=priority,
                        review_category="quality",
                    )
                )
                continue
            doc_no = clean_line(row.get("doc_no")) or extract_doc_no(body)
            # 自然键优先（同一法释号跨源稳定、利于去重）；无文号回退 url+title
            doc_id = stable_id("ji", doc_no) if doc_no else stable_id("ji", row.get("url"), title)
            docs.append(
                {
                    "doc_id": doc_id,
                    "kb_type": "judicial_interpretation",
                    "source_layer": source_layer,
                    "source_site": row.get("source_site") or row.get("source") or "",
                    "source_manifest": row.get("_source_manifest", ""),
                    "title": title,
                    "doc_no": doc_no or None,
                    "source": row.get("source"),
                    "publish_time": row.get("publish_time"),
                    "effective_date": row.get("effective_date"),
                    "body": body,
                    "body_len": len(body),
                    "contract_priority": priority,
                    "filter_decision": "include_main",
                    "filter_reason": row.get("classify_reason"),
                    "contract_domains": collect_domains(title, body),
                    "citation": doc_no or title,
                    "cites": parse_citations(body),
                    "status": detect_status(title, body),
                    "source_url": row.get("url"),
                    "content_sha256": text_sha256(body),
                }
            )

    for rel_path in JUDICIAL_CLASSIFIED:
        for row in read_jsonl(root / rel_path):
            if row.get("contract_related"):
                continue
            title = best_title(row, ["title_from_list", "doc_title", "page_title"])
            rejected.append(
                {
                    "kb_type": "judicial_interpretation",
                    "source_layer": "第二层：司法解释",
                    "source_site": row.get("source_site") or row.get("source") or "",
                    "source_manifest": row.get("_source_manifest", ""),
                    "title": title,
                    "url": row.get("url") or "",
                    "drop_reason": row.get("classify_reason") or "合同相关筛选未命中",
                    "contract_priority": row.get("contract_priority") or "DROP",
                }
            )

    docs, judicial_dups = dedupe_by_key(docs, ["doc_no"])
    chunks: list[dict[str, Any]] = []
    for doc in docs:
        chunks.extend(make_judicial_chunks(doc))
    write_jsonl(out / "docs/judicial_interpretations.jsonl", docs)
    write_jsonl(out / "chunks/judicial_interpretations.jsonl", chunks)
    write_jsonl(out / "review/judicial_interpretations.review.jsonl", review)
    write_csv(
        out / "review/judicial_interpretations.review.csv",
        review,
        review_csv_fields(),
    )
    write_jsonl(out / "rejected/judicial_interpretations.rejected.jsonl", rejected)
    return {
        "judicial_docs": len(docs),
        "judicial_chunks": len(chunks),
        "judicial_chunk_kinds": dict(Counter(c["kb_type"] for c in chunks)),
        "judicial_review": len(review),
        "judicial_rejected": len(rejected),
        "judicial_dedup_dropped": judicial_dups,
    }


# =========================================================================
# 司法解释 chunk 切分（分条→逐条 / 批复→整篇 / 修改决定→整篇低权重）
# =========================================================================

_JI_ART_RE = re.compile(r"^第[一二三四五六七八九十百零两\d]+条")
_JI_SEC_RE = re.compile(r"^[一二三四五六七八九十]+、.{0,25}$")
_JI_ENUM_PREFIX_RE = re.compile(r"^[一二三四五六七八九十]+、")
# 纯程序/施行/溯及条（非实体审查规则）
_JI_PROC_RE = re.compile(r"本(解释|规定|批复|意见)[^。]{0,40}(施行|实施|不再适用|废止|尚未终审|已经终审)")


# 文书类型结尾词（判断标题是否完整）+ 正文里的正式标题模式（补全截断标题用）
_DOCTYPE_TAIL_RE = re.compile(r"(解释|规定|批复|决定|意见|答复|纪要|复函|办法)(（[一二三四五六七八九十]+）)?$")
_FORMAL_TITLE_RE = re.compile(
    r"关于[^，。；\n]{4,80}?(?:解释|规定|批复|决定|意见|答复|纪要|复函|办法)(?:（[一二三四五六七八九十]+）)?"
)


def recover_title(title: str, body: str) -> str:
    """标题被截断（不以文书类型词结尾）时，从正文前部找完整正式标题补全。"""
    title = clean_line(title)
    if not title or _DOCTYPE_TAIL_RE.search(title):
        return title
    cands = [m.group(0) for m in _FORMAL_TITLE_RE.finditer(body[:1500] or "")]
    cands = [c for c in cands if len(c) > len(title)]
    if not cands:
        return title
    best = max(cands, key=len)
    return best if best.startswith("最高人民法院") else f"最高人民法院{best}"


def judicial_short_name(title: str) -> str:
    """从司法解释标题推导简称（剥框架词），失败回退全标题。

    处理两类标题：① 关于审理<主题>纠纷案件适用法律问题的解释 → <主题>；
    ② 关于适用《<法律>》<范围>的解释 → <法律><范围>（如『民法典担保制度的解释』『合同法的解释（二）』）。
    """
    name = title or ""
    name = re.sub(r"^最高人民法院", "", name)
    name = name.replace("中华人民共和国", "")            # 去国名前缀
    name = re.sub(r"[《》〈〉]", "", name)                # 去书名号（含 〈〉）
    name = re.sub(r"(具体应用法律|适用法律)(若干)?问题的", "", name)  # 先去复合框架短语
    name = re.sub(r"(若干)?问题的", "", name)
    name = re.sub(r"(纠纷)?案件", "", name)
    name = re.sub(r"^关于(审理|适用|执行)?", "", name)     # 仅去开头的 关于/审理/适用
    name = name.replace("有关", "")
    name = name.strip(" 、的")
    return name or (title or "未命名")


def split_judicial_articles(body: str) -> list[dict[str, Any]]:
    """分条正文 → [{section, article_no, text, body_only}]。

    剥前言（首个章节/条文之前）→ 按行首『第X条』切、章节标题随条文 → 丢弃纯程序条。
    只在行首条号处切，避免误切正文里对其他法律的引用（如『《合同法》第一百二十四条』）。
    无条文返回 []（→ 上层按整篇处理）。
    """
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if _JI_SEC_RE.match(line) or _JI_ART_RE.match(line)), None)
    if start is None:
        return []
    lines = lines[start:]

    segments: list[dict[str, Any]] = []
    section: str | None = None
    cur: dict[str, Any] | None = None
    for line in lines:
        if _JI_SEC_RE.match(line) and not _JI_ART_RE.match(line):
            if cur:
                segments.append(cur)
                cur = None
            section = _JI_ENUM_PREFIX_RE.sub("", line)  # 去"一、"序号，只留主题
            continue
        if _JI_ART_RE.match(line):
            if cur:
                segments.append(cur)
            cur = {"section": section, "article_no": _JI_ART_RE.match(line).group(0), "lines": [line]}
        elif cur:
            cur["lines"].append(line)
    if cur:
        segments.append(cur)

    out: list[dict[str, Any]] = []
    for seg in segments:
        text = "\n".join(seg["lines"])
        body_only = _JI_ART_RE.sub("", text, count=1).strip()
        if not body_only or _JI_PROC_RE.search(body_only[:60]):
            continue
        out.append({"section": seg["section"], "article_no": seg["article_no"], "text": text, "body_only": body_only})
    return out


def make_judicial_chunks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """产出司法解释 chunk：分条逐条；批复/修改决定整篇。embed 用 简称+章节 前缀（去条号），
    text 保留条号供展示引用。"""
    title = doc.get("title") or ""
    name = judicial_short_name(title)
    is_amendment = "关于修改" in title and "决定" in title
    base = {
        "doc_id": doc["doc_id"],
        "title": name,
        "source_title": title,
        "contract_domains": doc.get("contract_domains") or [],
        "status": doc.get("status") or "active",
        "source_doc_no": doc.get("doc_no"),
        "source_layer": doc.get("source_layer"),
        "source_site": doc.get("source_site"),
        "source_url": doc.get("source_url"),
    }

    articles = [] if is_amendment else split_judicial_articles(doc.get("body") or "")
    chunks: list[dict[str, Any]] = []
    if articles:
        for idx, art in enumerate(articles, start=1):
            section = art["section"]
            citation = f"{name} {art['article_no']}"
            # text 自包含出处（简称+条号+章节）：召回后 agent 直接知道来源、可引用
            header = f"{citation}（{section}）" if section else citation
            display_text = f"{header}\n{art['body_only']}"
            clause_types = collect_clause_types(art["body_only"])
            chunk = {
                "chunk_id": f"{doc['doc_id']}#a{idx:02d}",
                "kb_type": "judicial_article",
                "section": section,
                "article_no": art["article_no"],
                "citation": citation,
                "clause_types": clause_types,
                "cites": parse_citations(art["body_only"]),
                "text": display_text,
                "embedding_text": make_judicial_embedding_text(
                    name=name,
                    section=section,
                    article_no=art["article_no"],
                    domains=base["contract_domains"],
                    clause_types=clause_types,
                    body=art["body_only"],
                    status=base["status"],
                ),
                "retrieval_weight": 1.0,
                **base,
            }
            chunk["text"] = prepend_llm_context(chunk, display_text)
            chunk["content_sha256"] = text_sha256(chunk["text"])
            chunks.append(chunk)
        ids = [c["chunk_id"] for c in chunks]
        for i, chunk in enumerate(chunks):  # 前后条供 hydrate（解"前款/前条"援引）
            chunk["neighbor_ids"] = [x for x in (ids[i - 1] if i else None, ids[i + 1] if i + 1 < len(ids) else None) if x]
    else:
        # 批复/复函/修改决定 → 整篇一块（无可切的实体条文）
        body = clean_text(doc.get("body") or "", title=title, aggressive=True)
        clause_types = collect_clause_types(body)
        chunk = {
            "chunk_id": f"{doc['doc_id']}#whole",
            "kb_type": "judicial_amendment" if is_amendment else "judicial_whole",
            "section": None,
            "article_no": None,
            "citation": name,
            "clause_types": clause_types,
            "cites": parse_citations(body),
            "text": f"{name}\n{body}",
            "embedding_text": make_judicial_embedding_text(
                name=name,
                section="",
                article_no="",
                domains=base["contract_domains"],
                clause_types=clause_types,
                body=body,
                status=base["status"],
                whole=True,
            ),
            "retrieval_weight": 0.5 if is_amendment else 0.9,
            "neighbor_ids": [],
            **base,
        }
        chunk["text"] = prepend_llm_context(chunk, f"{name}\n{body}")
        chunk["content_sha256"] = text_sha256(chunk["text"])
        chunks.append(chunk)
    return chunks


def make_judicial_embedding_text(
    *,
    name: str,
    section: str,
    article_no: str,
    domains: list[str],
    clause_types: list[str],
    body: str,
    status: str,
    whole: bool = False,
) -> str:
    fingerprint = clause_semantic_fingerprint(body, clause_types)
    pieces = [
        "层级：司法解释",
        f"材料类型：{'司法解释整篇' if whole else '司法解释条文'}",
        f"文件：{name}",
        join_label("条号", article_no),
        join_label("章节", section),
        join_label("效力状态", status),
        join_label("合同领域", domains),
        join_label("条款类型", clause_types),
        join_label("规制对象", fingerprint["functions"]),
        join_label("典型条款风险", fingerprint["risks"]),
        join_label("触发条件", fingerprint["triggers"]),
        join_label("法律后果", fingerprint["consequences"]),
        f"条文原文：{compact_original_text(body, limit=900 if not whole else 1400)}",
    ]
    return "\n".join(piece for piece in pieces if piece)


def normalize_cases(root: Path, out: Path) -> dict[str, Any]:
    docs: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for rel_path in CASE_RELATED:
        for row in read_jsonl(root / rel_path):
            title = best_title(row, ["doc_title", "page_title", "title_from_list"])
            body, depth = build_case_body(row)
            priority = clean_line(row.get("contract_priority"))
            source_layer = "第二层：指导性案例和参考案例"
            if priority != "P0_CAUSE":
                review.append(
                    review_item(
                        kb_type="case_rule",
                        source_layer=source_layer,
                        title=title,
                        reason="仅因合同法条或弱关键词命中，需确认是否真的服务合同审查",
                        suggested_decision="review",
                        row=row,
                        body=body,
                        priority=priority,
                        extra={
                            "case_type": row.get("case_type"),
                            "cause_of_action": row.get("cause_of_action"),
                            "content_depth": depth,
                        },
                    )
                )
                continue
            natural = clean_line(row.get("case_no")) or clean_line(row.get("court_case_no"))
            doc_id = stable_id("case", natural) if natural else stable_id("case", row.get("url"), title, row.get("case_id"))
            relevant_statutes = clean_relevant_statutes(row.get("relevant_statutes"))
            docs.append(
                {
                    "doc_id": doc_id,
                    "kb_type": "case_rule",
                    "source_layer": source_layer,
                    "source_site": row.get("source_site") or row.get("source") or "",
                    "source_manifest": row.get("_source_manifest", ""),
                    "title": title,
                    "case_type": row.get("case_type"),
                    "case_category": row.get("case_category"),
                    "case_no": row.get("case_no"),
                    "court_case_no": row.get("court_case_no"),
                    "court": row.get("court"),
                    "publish_time": row.get("publish_time"),
                    "cause_of_action": row.get("cause_of_action"),
                    "keywords_text": row.get("keywords_text"),
                    "holding": clean_text(row.get("holding")),
                    "facts": clean_text(row.get("facts")),
                    "reasoning": clean_text(row.get("reasoning")),
                    "judgment": clean_text(row.get("judgment")),
                    "relevant_statutes": relevant_statutes,
                    "cites": parse_citations(relevant_statutes),
                    "status": "active",
                    "body": body,
                    "body_len": len(body),
                    "content_depth": depth,
                    "retrieval_weight": case_retrieval_weight(row, depth),
                    "contract_priority": priority,
                    "filter_decision": "include_main",
                    "filter_reason": row.get("classify_reason"),
                    "contract_domains": collect_domains(title, body),
                    "source_url": row.get("url"),
                    "content_sha256": text_sha256(body),
                }
            )

    for rel_path in CASE_CLASSIFIED:
        for row in read_jsonl(root / rel_path):
            if row.get("contract_related"):
                continue
            title = best_title(row, ["doc_title", "page_title", "title_from_list"])
            rejected.append(
                {
                    "kb_type": "case_rule",
                    "source_layer": "第二层：指导性案例和参考案例",
                    "source_site": row.get("source_site") or row.get("source") or "",
                    "source_manifest": row.get("_source_manifest", ""),
                    "title": title,
                    "url": row.get("url") or "",
                    "case_type": row.get("case_type"),
                    "cause_of_action": row.get("cause_of_action"),
                    "drop_reason": row.get("classify_reason") or "合同相关筛选未命中",
                    "contract_priority": row.get("contract_priority") or "DROP",
                }
            )

    docs, case_dups = dedupe_by_key(docs, ["court_case_no", "case_no"])
    chunks: list[dict[str, Any]] = []
    for doc in docs:
        chunks.extend(make_case_chunks(doc))
    write_jsonl(out / "docs/cases.jsonl", docs)
    write_jsonl(out / "chunks/cases.jsonl", chunks)
    write_jsonl(out / "review/cases.review.jsonl", review)
    write_csv(out / "review/cases.review.csv", review, review_csv_fields() + ["case_type", "cause_of_action", "content_depth"])
    write_jsonl(out / "rejected/cases.rejected.jsonl", rejected)
    return {
        "case_docs": len(docs),
        "case_review": len(review),
        "case_rejected": len(rejected),
        "case_dedup_dropped": case_dups,
        "case_chunks": len(chunks),
        "case_content_depth": dict(Counter(doc["content_depth"] for doc in docs)),
    }


def build_case_body(row: dict[str, Any]) -> tuple[str, str]:
    parts = [
        ("裁判要旨", row.get("holding")),
        ("基本案情", row.get("facts")),
        ("裁判理由", row.get("reasoning")),
        ("裁判结果", row.get("judgment")),
    ]
    cleaned_parts = [(label, clean_text(value)) for label, value in parts if clean_text(value)]
    if cleaned_parts:
        body = "\n\n".join(f"{label}\n{text}" for label, text in cleaned_parts)
    else:
        body = clean_text(row.get("body") or row.get("content_full"))

    has_holding = bool(clean_text(row.get("holding")))
    has_facts = bool(clean_text(row.get("facts")))
    has_reasoning = bool(clean_text(row.get("reasoning")))
    has_judgment = bool(clean_text(row.get("judgment")))
    if has_facts and has_reasoning and has_judgment:
        depth = "facts_reasoning_judgment"
    elif has_facts and has_reasoning:
        depth = "facts_reasoning"
    elif has_holding:
        depth = "holding_only"
    else:
        depth = "body_only"
    return body, depth


def case_retrieval_weight(row: dict[str, Any], depth: str) -> float:
    if row.get("source_site") == "最高人民法院":
        return 1.0
    if depth == "facts_reasoning_judgment":
        return 0.9
    if depth == "facts_reasoning":
        return 0.75
    if depth == "holding_only":
        return 0.45
    return 0.35


def _clean_keywords(keywords_text: str) -> str:
    """keywords_text 形如 '民事/金融借款合同/诉讼时效/…' → 去案件类型前缀、分隔符换『·』。"""
    parts = [p for p in re.split(r"[/／、]", clean_line(keywords_text)) if p]
    if parts and parts[0] in ("民事", "商事", "刑事", "行政", "执行", "国家赔偿"):
        parts = parts[1:]
    return "·".join(parts)


_CASE_FOCUS_RE = re.compile(r"(?:本案[的]?)?(?:争议)?焦点[为是：:]\s*([^。\n]{6,120}[。]?)")
_FACTS_EXCERPT_LIMIT = 400  # 基本案情节选上限：核心情节通常在开头，截断防止个案细节稀释向量


def extract_case_focus(reasoning: str) -> str:
    """从裁判理由抽『争议焦点』句（问句式法律争点），清理开头标点。"""
    match = _CASE_FOCUS_RE.search(reasoning or "")
    return match.group(1).strip(" ：:，,为是") if match else ""


def make_case_chunks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """一案一 chunk（不拆多要旨）。

    embed_text = 〔案由·领域·关键词〕+ 关联法条 + 争议焦点 + 裁判要旨 + 基本案情(节选≤400)
                 —— 聚焦的法律/场景信号，精准召回；不含法院/案号/全文 facts（避免稀释）。
    text       = 出处行 + 裁判要旨 + 基本案情 + 裁判理由 + 裁判结果（缺则省）—— 全文自包含，agent 直接可用。
    """
    cause = clean_line(doc.get("cause_of_action"))
    holding = clean_text(doc.get("holding"))
    relevant_statutes = clean_relevant_statutes(doc.get("relevant_statutes"))
    focus = extract_case_focus(doc.get("reasoning") or "")
    facts_excerpt = clean_text(doc.get("facts"))[:_FACTS_EXCERPT_LIMIT]
    clause_signal_text = "\n".join(
        part for part in [cause, holding, focus, facts_excerpt, clean_text(doc.get("reasoning"))[:500]] if part
    )
    clause_types = collect_standard_clause_types(clause_signal_text)
    fingerprint = clause_semantic_fingerprint(clause_signal_text, clause_types)

    # —— embed 面向待审条款召回：案由/领域/条款类型 + 争议条款信号 + 裁判规则 ——
    kw_list = [p for p in _clean_keywords(doc.get("keywords_text") or "").split("·") if p]
    emb_lines = [
        "层级：案例",
        "材料类型：裁判规则",
        join_label("案由", cause),
        join_label("合同领域", doc.get("contract_domains") or []),
        join_label("条款类型", clause_types),
        join_label("争议关键词", kw_list),
        join_label("争议条款功能", fingerprint["functions"]),
        join_label("争议条款风险", fingerprint["risks"]),
        join_label("触发条件", fingerprint["triggers"]),
        join_label("裁判后果", fingerprint["consequences"]),
    ]
    cites_str = "、".join(f"{c.get('law', '')}{c.get('article') or ''}" for c in (doc.get("cites") or []))
    if relevant_statutes:
        emb_lines.append(f"相关法条：{compact_original_text(relevant_statutes, limit=500)}")
    elif cites_str:
        emb_lines.append(f"关联法条：{cites_str}")
    if focus:
        emb_lines.append(f"争议焦点：{focus}")
    if holding:
        emb_lines.append(f"裁判规则：{compact_original_text(holding, limit=700)}")
    if facts_excerpt:
        emb_lines.append(f"条款/履行场景节选：{compact_original_text(facts_excerpt, limit=350)}")
    embedding_text = "\n".join(line for line in emb_lines if line)

    case_no = clean_line(doc.get("case_no")) or clean_line(doc.get("court_case_no"))
    src_line = " ｜ ".join(b for b in [cause, clean_line(doc.get("case_category")), case_no, clean_line(doc.get("court"))] if b)
    parts = [src_line]
    if relevant_statutes:
        parts.append(f"相关法条：{relevant_statutes}")
    for label, value in [
        ("裁判要旨", holding),
        ("基本案情", clean_text(doc.get("facts"))),
        ("裁判理由", clean_text(doc.get("reasoning"))),
        ("裁判结果", clean_text(doc.get("judgment"))),
    ]:
        if value:
            parts.append(f"{label}：{value}")
    text = "\n\n".join(parts)

    chunk = {
        "chunk_id": doc["doc_id"],
        "doc_id": doc["doc_id"],
        "kb_type": "case_rule",
        "title": doc.get("title"),
        "case_category": doc.get("case_category"),
        "cause_of_action": cause,
        "contract_domains": doc.get("contract_domains") or [],
        "clause_types": clause_types,
        "status": doc.get("status") or "active",
        "court": doc.get("court"),
        "court_case_no": doc.get("court_case_no"),
        "case_no": doc.get("case_no"),
        "publish_time": doc.get("publish_time"),
        "citation": "·".join(b for b in [case_no, cause] if b),
        "relevant_statutes": relevant_statutes,
        "cites": doc.get("cites") or [],
        "content_depth": doc.get("content_depth"),
        "retrieval_weight": doc.get("retrieval_weight"),
        "text": text,
        "embedding_text": embedding_text,
        "source_url": doc.get("source_url"),
    }
    chunk["text"] = prepend_llm_context(chunk, text)
    chunk["content_sha256"] = text_sha256(chunk["text"])
    return [chunk]


def normalize_playbooks(root: Path, out: Path) -> dict[str, Any]:
    raw_candidates: list[dict[str, Any]] = []
    docs: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for rel_path in PLAYBOOK_RELATED:
        for row in read_jsonl(root / rel_path):
            title = best_title(row, ["title", "title_from_list"])
            body = clean_text(row.get("body"), title=title, aggressive=True)
            decision, reason, category = classify_playbook_second_pass(row, title, body)
            raw_candidates.append(
                {
                    "row": row,
                    "title": title,
                    "body": body,
                    "decision": decision,
                    "reason": reason,
                    "category": category,
                    "dedupe_key": normalize_title(title),
                    "quality": source_quality(row),
                }
            )

    raw_candidates.sort(
        key=lambda item: (
            item["decision"] != "include_main",
            -item["quality"],
            -len(item["body"]),
            item["title"],
        )
    )

    accepted_titles: set[str] = set()
    for item in raw_candidates:
        row = item["row"]
        title = item["title"]
        body = item["body"]
        source_layer = "第三层：操作指引 playbook"
        if item["decision"] == "include_main" and item["dedupe_key"] in accepted_titles:
            review.append(
                review_item(
                    kb_type="playbook",
                    source_layer=source_layer,
                    title=title,
                    reason="同标题 playbook 已有更高质量版本进入主库，当前版本待确认是否保留",
                    suggested_decision="drop_duplicate",
                    row=row,
                    body=body,
                    priority=row.get("contract_priority") or "",
                    review_category="dedupe",
                    extra={"dedupe_key": item["dedupe_key"]},
                )
            )
            continue

        if item["decision"] != "include_main":
            review.append(
                review_item(
                    kb_type="playbook",
                    source_layer=source_layer,
                    title=title,
                    reason=item["reason"],
                    suggested_decision=item["decision"],
                    row=row,
                    body=body,
                    priority=row.get("contract_priority") or "",
                    review_category=item["category"],
                    extra={
                        "dedupe_key": item["dedupe_key"],
                        "contract_domains": collect_domains(title, body),
                        "clause_types": collect_clause_types(f"{title}\n{body[:2500]}"),
                    },
                )
            )
            continue

        accepted_titles.add(item["dedupe_key"])
        doc_id = stable_id("pb", row.get("url"), title)
        domains = collect_domains(title, body)
        clause_types = collect_clause_types(f"{title}\n{body[:2500]}")
        doc = {
            "doc_id": doc_id,
            "kb_type": "playbook",
            "source_layer": source_layer,
            "source_site": row.get("source_site") or row.get("source") or "",
            "source_manifest": row.get("_source_manifest", ""),
            "association": row.get("association"),
            "title": title,
            "url": row.get("url"),
            "category": row.get("category"),
            "committee": row.get("committee"),
            "author": row.get("author"),
            "source": row.get("source"),
            "publish_date": row.get("publish_date") or row.get("publish_date_from_list"),
            "body": body,
            "body_len": len(body),
            "contract_priority": row.get("contract_priority"),
            "filter_decision": "include_main",
            "filter_reason": item["reason"],
            "contract_domains": domains,
            "clause_types": clause_types,
            "source_quality": item["quality"],
            "dedupe_key": item["dedupe_key"],
            "content_sha256": text_sha256(body),
        }
        docs.append(doc)
        chunks.extend(make_playbook_chunks(doc))

    for rel_path in PLAYBOOK_CLASSIFIED:
        for row in read_jsonl(root / rel_path):
            if row.get("contract_related"):
                continue
            title = best_title(row, ["title", "title_from_list"])
            rejected.append(
                {
                    "kb_type": "playbook",
                    "source_layer": "第三层：操作指引 playbook",
                    "source_site": row.get("source_site") or row.get("source") or "",
                    "source_manifest": row.get("_source_manifest", ""),
                    "title": title,
                    "url": row.get("url") or "",
                    "drop_reason": row.get("classify_reason") or "合同相关筛选未命中",
                    "contract_priority": row.get("contract_priority") or "DROP",
                }
            )

    write_jsonl(out / "docs/playbooks.filtered.jsonl", docs)
    write_jsonl(out / "chunks/playbook_review_points.jsonl", chunks)
    write_jsonl(out / "review/playbooks.review.jsonl", review)
    write_csv(
        out / "review/playbooks.review.csv",
        review,
        review_csv_fields() + ["dedupe_key", "contract_domains", "clause_types"],
    )
    write_jsonl(out / "rejected/playbooks.rejected.jsonl", rejected)
    write_csv(
        out / "reports/playbook_filter_report.csv",
        sorted(
            [
                {
                    "title": doc["title"],
                    "source_site": doc["source_site"],
                    "contract_priority": doc["contract_priority"],
                    "filter_decision": doc["filter_decision"],
                    "filter_reason": doc["filter_reason"],
                    "contract_domains": doc["contract_domains"],
                    "clause_types": doc["clause_types"],
                    "url": doc["url"],
                }
                for doc in docs
            ]
            + [
                {
                    "title": item["title"],
                    "source_site": item["source_site"],
                    "contract_priority": item["contract_priority"],
                    "filter_decision": item["suggested_decision"],
                    "filter_reason": item["review_reason"],
                    "contract_domains": item.get("contract_domains", []),
                    "clause_types": item.get("clause_types", []),
                    "url": item["url"],
                }
                for item in review
            ],
            key=lambda item: (item["filter_decision"], item["source_site"], item["title"]),
        ),
        [
            "title",
            "source_site",
            "contract_priority",
            "filter_decision",
            "filter_reason",
            "contract_domains",
            "clause_types",
            "url",
        ],
    )

    return {
        "playbook_input": len(raw_candidates),
        "playbook_docs": len(docs),
        "playbook_chunks": len(chunks),
        "playbook_review": len(review),
        "playbook_rejected": len(rejected),
        "playbook_review_by_decision": dict(Counter(item["suggested_decision"] for item in review)),
    }


def classify_playbook_second_pass(row: dict[str, Any], title: str, body: str) -> tuple[str, str, str]:
    priority = clean_line(row.get("contract_priority"))
    title_text = title
    match_text = f"{title_text}\n{clean_line(row.get('category'))}\n{clean_line(row.get('committee'))}"
    has_explicit_contract = "合同" in title_text or "协议" in title_text
    direct_hits = [kw for kw in DIRECT_PLAYBOOK_TITLE_KEYWORDS if kw in match_text]
    broad_hits = [kw for kw in BROAD_PLAYBOOK_KEYWORDS if kw in match_text]
    review_title_hits = [kw for kw in PLAYBOOK_REVIEW_TITLE_KEYWORDS if kw in title_text]
    noise_hits = [kw for kw in NOISE_TITLE_KEYWORDS if kw in title_text]
    litigation_hits = [kw for kw in PURE_LITIGATION_KEYWORDS if kw in title_text]

    if noise_hits:
        return "review_drop", f"标题像会议/培训/通知/活动内容：{'、'.join(noise_hits)}", "relevance"
    if len(body) < 800:
        return "review", "正文过短或抽取不完整，暂不入主 playbook 库", "quality"
    if priority != "P0_CONTRACT":
        return "review", "P1 相邻主题，仅有邻近域信号，需要人工确认合同审查相关性", "relevance"
    if review_title_hits:
        return "review", f"宽泛专项业务或合规/尽调主题：{'、'.join(review_title_hits)}，需要确认是否服务合同审查", "relevance"
    if has_explicit_contract or direct_hits:
        if litigation_hits and not any(term in title_text for term in ["合同", "协议", "建设工程", "股权", "劳动"]):
            return "review", f"标题偏诉讼/案件处理（{'、'.join(litigation_hits)}），虽有业务信号但需确认是否服务合同审查", "relevance"
        hits = (["合同/协议"] if has_explicit_contract else []) + direct_hits
        return "include_main", f"二次过滤保留：标题命中直接合同审查/交易主题：{'、'.join(hits)}", "relevance"
    if "合同" in body[:5000] and ("审查" in body[:5000] or "起草" in body[:5000]):
        return "review", "正文包含合同起草/审查章节，但标题是宽泛业务主题，需人工确认是否整体入库", "relevance"
    if broad_hits:
        return "review", f"宽泛邻近业务主题：{'、'.join(broad_hits)}，需要人工确认是否仅条件召回", "relevance"
    return "review", "P0 初筛命中但未命中二次直接合同主题，需要人工复核", "relevance"


def make_playbook_chunks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    sections = split_playbook_theme_sections(doc["body"], doc["title"])
    for index, section in enumerate(sections, start=1):
        chunk_text = section["text"]
        section_path = section["section_path"]
        chunk_id = stable_id("pbchunk", doc["doc_id"], index, chunk_text[:80])
        domains = collect_playbook_domains(doc["title"], section_path, chunk_text)
        clause_types = collect_clause_types(f"{doc['title']}\n{' > '.join(section_path)}\n{chunk_text}")
        review_tasks = collect_playbook_review_tasks(chunk_text)
        if section_path and section_path[-1] in {"总则", "附则", "前言", "引言"}:
            review_tasks = ["适用范围/定义"]
        service_stage = infer_playbook_service_stage(f"{' > '.join(section_path)}\n{chunk_text}")
        party_perspective = infer_playbook_party_perspective(f"{doc['title']}\n{' > '.join(section_path)}\n{chunk_text}")
        article_range = extract_playbook_article_range(chunk_text)
        chunk_kind = infer_playbook_chunk_kind(chunk_text, section_path, review_tasks)
        citation = make_playbook_citation(doc["title"], section_path, article_range)
        display_text = make_playbook_display_text(doc, section, citation)
        embedding_text = make_playbook_embedding_text(
            doc=doc,
            section=section,
            chunk_text=chunk_text,
            domains=domains,
            clause_types=clause_types,
            review_tasks=review_tasks,
            service_stage=service_stage,
            party_perspective=party_perspective,
            chunk_kind=chunk_kind,
        )
        chunk = {
                "chunk_id": chunk_id,
                "doc_id": doc["doc_id"],
                "kb_type": "playbook_review_point",
                "chunk_kind": chunk_kind,
                "status": "active",
                "source_layer": doc["source_layer"],
                "source_site": doc["source_site"],
                "association": doc.get("association"),
                "title": doc["title"],
                "citation": citation,
                "contract_domains": domains,
                "clause_types": clause_types,
                "service_stage": service_stage,
                "review_task": review_tasks,
                "party_perspective": party_perspective,
                "section_title": section["section_title"],
                "section_path": section_path,
                "section_part_index": section["section_part_index"],
                "article_range": article_range,
                "text": display_text,
                "text_len": len(display_text),
                "embedding_text": embedding_text,
                "source_title": doc["title"],
                "source_url": doc["url"],
                "publish_date": doc.get("publish_date"),
                "source_excerpt": make_excerpt(chunk_text, limit=220),
                "filter_decision": doc["filter_decision"],
                "chunk_index": index,
                "retrieval_weight": playbook_retrieval_weight(chunk_kind, review_tasks),
            }
        chunk["text"] = prepend_llm_context(chunk, display_text)
        chunk["text_len"] = len(chunk["text"])
        chunk["content_sha256"] = text_sha256(chunk["text"])
        chunks.append(chunk)
    ids = [chunk["chunk_id"] for chunk in chunks]
    for i, chunk in enumerate(chunks):
        chunk["neighbor_ids"] = [x for x in (ids[i - 1] if i else None, ids[i + 1] if i + 1 < len(ids) else None) if x]
    return chunks


def collect_playbook_review_tasks(text: str) -> list[str]:
    tasks = [name for name, keywords in PLAYBOOK_REVIEW_TASK_KEYWORDS.items() if any(kw in text for kw in keywords)]
    return tasks or ["实务要点"]


def collect_playbook_domains(title: str, section_path: list[str], text: str) -> list[str]:
    """Playbook 的领域优先从标题和章节取，避免正文里泛词（如服务、费用）稀释召回标签。"""
    context = f"{title}\n{' > '.join(section_path)}"
    domains = collect_domains(context)
    if domains != ["通用"]:
        return prune_playbook_domains(domains, f"{context}\n{text[:800]}")
    return prune_playbook_domains(collect_domains(title, text[:1200]), f"{context}\n{text[:1200]}")


def prune_playbook_domains(domains: list[str], text: str) -> list[str]:
    if "服务/委托" in domains and not re.search(r"(服务合同|服务协议|委托合同|委托协议|承揽|加工|家政|旅游|培训|物业服务)", text):
        domains = [domain for domain in domains if domain != "服务/委托"]
    return domains or ["通用"]


def infer_playbook_service_stage(text: str) -> str:
    first_line = clean_line(text.splitlines()[0] if text.splitlines() else "")
    if first_line in {"总则", "附则", "前言", "引言"}:
        return "通用"
    scores = {
        name: sum(1 for kw in keywords if kw in text)
        for name, keywords in PLAYBOOK_SERVICE_STAGE_KEYWORDS.items()
    }
    best, score = max(scores.items(), key=lambda item: item[1])
    return best if score else "通用"


def infer_playbook_party_perspective(text: str) -> str:
    leading = text[:300]
    if "为开发商" in leading:
        return "开发商"
    if "为买受人" in leading or "为购房者" in leading:
        return "买方/买受人"
    for perspective, keywords in PLAYBOOK_PARTY_PERSPECTIVE_KEYWORDS.items():
        if any(kw in leading for kw in keywords):
            return perspective
    scores = {
        name: sum(1 for kw in keywords if kw in text)
        for name, keywords in PLAYBOOK_PARTY_PERSPECTIVE_KEYWORDS.items()
    }
    best, score = max(scores.items(), key=lambda item: item[1])
    return best if score else "通用"


def infer_playbook_chunk_kind(text: str, section_path: list[str], review_tasks: list[str]) -> str:
    match_text = f"{' > '.join(section_path)}\n{text[:1000]}"
    if any(term in match_text for term in ["【律师工作提示", "参考模板", "示范文本", "合同范例", "合同模板", "附件："]):
        return "template_guidance"
    if section_path and section_path[-1] in {"总则", "附则", "前言", "引言"}:
        return "practice_note"
    if "风险提示" in match_text or "风险识别" in review_tasks:
        return "risk_warning"
    if "条款完备性审查" in review_tasks or "修改建议" in review_tasks:
        return "drafting_review_guidance"
    if "材料/证照清单" in review_tasks or "主体/资质审查" in review_tasks:
        return "due_diligence_checklist"
    if "流程操作" in review_tasks:
        return "workflow"
    if any(term in match_text for term in ["裁判", "效力", "认定", "规则"]):
        return "legal_analysis"
    return "practice_note"


def extract_playbook_article_range(text: str) -> list[str]:
    articles = re.findall(r"第[一二三四五六七八九十百千万零〇两0-9]+条", text)
    if articles:
        return [articles[0]] if articles[0] == articles[-1] else [articles[0], articles[-1]]
    decimal_items = re.findall(r"(?m)^(\d+(?:\.\d+)+)", text)
    if decimal_items:
        return [decimal_items[0]] if decimal_items[0] == decimal_items[-1] else [decimal_items[0], decimal_items[-1]]
    return []


def make_playbook_citation(title: str, section_path: list[str], article_range: list[str]) -> str:
    pieces = [title]
    if section_path:
        pieces.append(" > ".join(section_path[-3:]))
    if article_range:
        pieces.append("-".join(article_range))
    return " ｜ ".join(clean_line(piece) for piece in pieces if clean_line(piece))


def make_playbook_display_text(doc: dict[str, Any], section: dict[str, Any], citation: str) -> str:
    source_bits = [
        doc["title"],
        clean_line(doc.get("association")),
        clean_line(doc.get("publish_date")),
    ]
    header = " ｜ ".join(bit for bit in source_bits if bit)
    lines = [header]
    section_path = section.get("section_path") or []
    if section_path:
        lines.append(f"章节：{' > '.join(section_path)}")
    if citation:
        lines.append(f"引用：{citation}")
    lines.append("")
    lines.append(section["text"])
    return "\n".join(lines).strip()


def make_playbook_embedding_text(
    *,
    doc: dict[str, Any],
    section: dict[str, Any],
    chunk_text: str,
    domains: list[str],
    clause_types: list[str],
    review_tasks: list[str],
    service_stage: str,
    party_perspective: str,
    chunk_kind: str,
) -> str:
    fingerprint = clause_semantic_fingerprint(chunk_text, clause_types)
    section_path = section.get("section_path") or []
    context = [
        "层级：实务指引",
        f"材料类型：{chunk_kind}",
        f"主题：{doc['title']}",
        join_label("合同领域", domains),
        join_label("条款类型", clause_types),
        join_label("审查任务", review_tasks),
        join_label("适用阶段", service_stage),
        join_label("适用方", party_perspective),
        join_label("条款功能", fingerprint["functions"]),
        join_label("风险信号", fingerprint["risks"]),
        join_label("触发条件", fingerprint["triggers"]),
        join_label("法律后果", fingerprint["consequences"]),
    ]
    if section_path:
        context.append(f"章节：{' > '.join(section_path[-3:])}")
    context.append(f"审查要点原文：{trim_playbook_embedding_body(chunk_text)}")
    return "\n".join(item for item in context if item)


def trim_playbook_embedding_body(text: str, limit: int = 1200) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    body = "\n".join(lines)
    if len(body) <= limit:
        return body

    signal_terms = [
        "审查", "核对", "明确", "约定", "风险", "提示", "应当", "不得",
        "包括", "提交", "证明", "违约", "解除", "无效", "管辖", "仲裁",
    ]
    signal_lines = [line for line in lines if playbook_heading_level(line) or any(term in line for term in signal_terms)]
    signal_body = "\n".join(signal_lines)
    if 400 <= len(signal_body) <= limit:
        return signal_body
    if len(signal_body) > limit:
        return signal_body[:limit].rstrip()
    return body[:limit].rstrip()


def dedupe_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def playbook_retrieval_weight(chunk_kind: str, review_tasks: list[str]) -> float:
    if chunk_kind == "practice_note":
        return 0.55
    if chunk_kind in {"risk_warning", "drafting_review_guidance", "due_diligence_checklist"}:
        return 1.0
    if "实务要点" in review_tasks:
        return 0.75
    return 0.9


def split_playbook_theme_sections(
    text: str,
    title: str,
    max_chars: int = 3500,
    min_chars: int = 200,
) -> list[dict[str, Any]]:
    lines = clean_playbook_section_lines(text, title)
    sections: list[dict[str, Any]] = []
    outline: dict[int, str] = {}
    current_lines: list[str] = []
    current_path: list[str] = []

    def flush_current() -> None:
        if not current_lines:
            return
        section_text = "\n".join(current_lines).strip()
        content_len = len(clean_line(section_text))
        if content_len < min_chars:
            return
        path = current_path.copy()
        section_title = path[-1] if path else title
        for part_index, part_text in enumerate(split_long_playbook_section(section_text, max_chars=max_chars), start=1):
            if len(clean_line(part_text)) < min_chars:
                continue
            sections.append(
                {
                    "section_title": section_title,
                    "section_path": path,
                    "section_part_index": part_index,
                    "text": part_text,
                }
            )

    for line in lines:
        level = playbook_heading_level(line)
        starts_major_section = level is not None and level <= 3
        starts_article_section = level == 4 and not any(path_level <= 3 for path_level in outline)
        if starts_major_section or starts_article_section:
            flush_current()
            if level is not None:
                for path_level in list(outline):
                    if path_level >= level:
                        del outline[path_level]
                outline[level] = line
            current_path = [outline[path_level] for path_level in sorted(outline)]
            current_lines = [line]
            continue
        if not current_lines:
            current_path = [outline[path_level] for path_level in sorted(outline)]
        current_lines.append(line)

    flush_current()
    return sections


def clean_playbook_section_lines(text: str, title: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", raw_line).strip()
        if not line:
            continue
        if is_playbook_footer_line(line):
            break
        if line == "目录":
            lines.append(line)
            continue
        if line in PLAYBOOK_WEB_NOISE_EXACT:
            continue
        if any(line.startswith(prefix) for prefix in PLAYBOOK_WEB_NOISE_PREFIXES):
            continue
        if is_playbook_generic_disclaimer(line):
            continue
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{1,2}:\d{1,2})?", line):
            continue
        if re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日(?:\s+\d{1,2}:\d{1,2}:\d{1,2})?", line):
            continue
        lines.append(line)

    while lines and lines[0] == title:
        lines.pop(0)

    toc_idx = next((idx for idx, line in enumerate(lines[:160]) if line == "目录"), None)
    if toc_idx is not None:
        content_start = None
        for idx in range(toc_idx + 1, len(lines)):
            if is_playbook_article_heading(lines[idx]):
                content_start = idx
                if idx > 0 and playbook_heading_level(lines[idx - 1]) in {1, 2, 3}:
                    content_start = idx - 1
                break
        if content_start is not None:
            lines = lines[content_start:]
        else:
            lines = [line for idx, line in enumerate(lines) if idx < toc_idx or line != "目录"]

    return [line for line in lines if line != title and line != "目录"]


def is_playbook_generic_disclaimer(line: str) -> bool:
    return any(
        marker in line
        for marker in [
            "仅供律师在提供",
            "不是强制性的",
            "不保证涵盖",
            "不构成任何明示或默示的担保",
            "所产生的一切风险由该律师",
            "免责声明：",
            "分享到：",
        ]
    )


def expand_playbook_concatenated_headings(line: str) -> list[str]:
    chapter_section = re.match(
        r"^(第[一二三四五六七八九十百千万零〇两0-9]+章.+?)(第[一二三四五六七八九十百千万零〇两0-9]+节.+)$",
        line,
    )
    if chapter_section:
        return [chapter_section.group(1), chapter_section.group(2)]
    volume_chapter = re.match(
        r"^(第[一二三四五六七八九十百千万零〇两0-9]+编.+?)(第[一二三四五六七八九十百千万零〇两0-9]+章.+)$",
        line,
    )
    if volume_chapter:
        return [volume_chapter.group(1), volume_chapter.group(2)]
    return [line]


def is_playbook_footer_line(line: str) -> bool:
    return any(marker in line for marker in PLAYBOOK_FOOTER_MARKERS)


def playbook_heading_level(line: str) -> int | None:
    stripped = line.strip()
    if re.match(r"^第[一二三四五六七八九十百千万零〇两0-9]+编", stripped):
        return 1
    if re.match(r"^第[一二三四五六七八九十百千万零〇两0-9]+章", stripped):
        return 2
    if re.match(r"^第[一二三四五六七八九十百千万零〇两0-9]+节", stripped):
        return 3
    if stripped in {"总则", "附则", "前言", "引言"}:
        return 3
    if is_playbook_article_heading(stripped):
        return 4
    if is_short_topic_heading(stripped):
        return 5
    return None


def is_playbook_article_heading(line: str) -> bool:
    return bool(re.match(r"^第[一二三四五六七八九十百千万零〇两0-9]+条", line))


def is_short_topic_heading(line: str) -> bool:
    if len(line) > 45:
        return False
    if re.search(r"[。；;：:]$", line):
        return False
    return bool(re.match(r"^([一二三四五六七八九十]+、|\d+(?:\.\d+)+\s*|\d+(?:\.\d+)?[、.])\S+", line))


def split_long_playbook_section(text: str, max_chars: int = 3500, soft_min_chars: int = 900) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    units = split_playbook_units(text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for unit in units:
        unit_len = len(unit)
        if unit_len > max_chars:
            if current:
                chunks.append("\n".join(current).strip())
                current = []
                current_len = 0
            chunks.extend(split_oversized_playbook_unit(unit, max_chars=max_chars))
            continue
        if current and current_len + unit_len > max_chars and current_len >= soft_min_chars:
            chunks.append("\n".join(current).strip())
            current = []
            current_len = 0
        current.append(unit)
        current_len += unit_len
    if current:
        chunks.append("\n".join(current).strip())

    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < soft_min_chars and len(merged[-1]) + len(chunk) <= max_chars:
            merged[-1] = f"{merged[-1]}\n{chunk}".strip()
        else:
            merged.append(chunk)
    return merged


def split_oversized_playbook_unit(text: str, max_chars: int) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line)
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current).strip())
            current = []
            current_len = 0
        if line_len > max_chars:
            chunks.extend(line[i : i + max_chars].strip() for i in range(0, line_len, max_chars) if line[i : i + max_chars].strip())
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current).strip())
    return chunks


def split_playbook_units(text: str) -> list[str]:
    lines = text.splitlines()
    units: list[str] = []
    current: list[str] = []
    for line in lines:
        starts_new = bool(current) and (is_playbook_article_heading(line) or is_short_topic_heading(line))
        if starts_new:
            units.append("\n".join(current).strip())
            current = []
        current.append(line)
    if current:
        units.append("\n".join(current).strip())
    if len(units) == 1:
        units = [unit.strip() for unit in re.split(r"\n{1,2}", text) if unit.strip()]
    return units


def first_meaningful_line(text: str, limit: int = 160) -> str:
    for line in text.splitlines():
        line = clean_line(line)
        if len(line) >= 8 and not re.fullmatch(r"[第章节编一二三四五六七八九十、.0-9（）()]+", line):
            return line[:limit]
    return clean_line(text)[:limit]


def normalize_standard_contracts(root: Path, out: Path) -> dict[str, Any]:
    docs: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    accepted_doc_ids: set[str] = set()
    source_rows_by_doc_id: dict[str, dict[str, Any]] = {}

    for rel_path in STANDARD_CONTRACT_RELATED:
        for row in read_jsonl(root / rel_path):
            title = best_title(row, ["title", "title_from_list"])
            body = clean_text(row.get("body"), title=title)
            priority = clean_line(row.get("contract_priority"))
            source_layer = "第四层：标准合同"
            source_rows_by_doc_id[clean_line(row.get("doc_id"))] = row
            include_standard, standard_reason = classify_standard_second_pass(priority, title, body)
            if not include_standard:
                review.append(
                    review_item(
                        kb_type="standard_contract",
                        source_layer=source_layer,
                        title=title,
                        reason=standard_reason,
                        suggested_decision="review",
                        row=row,
                        body=body,
                        priority=priority,
                        extra={
                            "scope": row.get("scope"),
                            "category": row.get("category"),
                            "region": row.get("region"),
                            "publish_year": row.get("publish_year"),
                        },
                    )
                )
                continue
            doc_id = clean_line(row.get("doc_id")) or stable_id("std", row.get("url"), title)
            accepted_doc_ids.add(doc_id)
            docs.append(
                {
                    "doc_id": doc_id,
                    "kb_type": "standard_contract",
                    "source_layer": source_layer,
                    "source_site": row.get("source_site") or "",
                    "source_manifest": row.get("_source_manifest", ""),
                    "title": title,
                    "scope": row.get("scope"),
                    "category": row.get("category"),
                    "region": row.get("region"),
                    "publish_year": row.get("publish_year"),
                    "publish_agencies": row.get("publish_agencies"),
                    "doc_no": row.get("doc_no"),
                    "body": body,
                    "body_len": len(body),
                    "risk_tips": clean_text(row.get("risk_tips")),
                    "risk_tips_len": row.get("risk_tips_len"),
                    "contract_priority": priority,
                    "filter_decision": "include_main",
                    "filter_reason": standard_reason,
                    "contract_domains": collect_domains(title, body),
                    "source_url": row.get("url"),
                    "content_sha256": text_sha256(body),
                }
            )

    for rel_path in STANDARD_CONTRACT_CLASSIFIED:
        for row in read_jsonl(root / rel_path):
            if row.get("contract_related"):
                continue
            title = best_title(row, ["title", "title_from_list"])
            rejected.append(
                {
                    "kb_type": "standard_contract",
                    "source_layer": "第四层：标准合同",
                    "source_site": row.get("source_site") or "",
                    "source_manifest": row.get("_source_manifest", ""),
                    "title": title,
                    "url": row.get("url") or "",
                    "drop_reason": row.get("classify_reason") or "合同相关筛选未命中",
                    "contract_priority": row.get("contract_priority") or "DROP",
                }
            )

    clause_result = normalize_standard_clauses(root, out, accepted_doc_ids, source_rows_by_doc_id)

    write_jsonl(out / "docs/standard_contracts.jsonl", docs)
    write_jsonl(out / "review/standard_contracts.review.jsonl", review)
    write_csv(
        out / "review/standard_contracts.review.csv",
        review,
        review_csv_fields() + ["scope", "category", "region", "publish_year"],
    )
    write_jsonl(out / "rejected/standard_contracts.rejected.jsonl", rejected)
    return {
        "standard_contract_docs": len(docs),
        "standard_contract_review": len(review),
        "standard_contract_rejected": len(rejected),
        **clause_result,
    }


def classify_standard_second_pass(priority: str, title: str, body: str) -> tuple[bool, str]:
    if any(keyword in title for keyword in STANDARD_CONTRACT_REVIEW_TITLE_KEYWORDS):
        return False, "标题像告知书/信息卡/行为指引等非合同文本，需要人工确认是否纳入标准合同库"
    if priority == "P0_STANDARD":
        return True, "二次过滤保留：命中官方示范文本或强标准合同信号"
    if any(keyword in title for keyword in STANDARD_CONTRACT_FORM_TITLE_KEYWORDS):
        return True, "二次过滤保留：标题是合同/协议/运单/凭证等合同表单，虽为 P1 但可进入标准合同库"
    if any(keyword in body[:800] for keyword in ["合同编号", "甲方", "乙方", "出卖人", "买受人", "委托人", "承租人", "出租人"]):
        return True, "二次过滤保留：正文开头呈现合同表单结构，虽为 P1 但可进入标准合同库"
    return False, "未命中合同表单结构，需要确认是否作为标准合同入库"


def normalize_standard_clauses(
    root: Path,
    out: Path,
    accepted_doc_ids: set[str],
    source_rows_by_doc_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rel_path in STANDARD_CLAUSES:
        for row in read_jsonl(root / rel_path):
            doc_id = clean_line(row.get("doc_id"))
            if doc_id in accepted_doc_ids:
                by_doc[doc_id].append(row)

    chunks: list[dict[str, Any]] = []
    quality_review: list[dict[str, Any]] = []
    merged_short_count = 0
    skipped_empty_count = 0
    full_chunks = make_standard_contract_full_chunks(accepted_doc_ids, source_rows_by_doc_id, by_doc)

    for doc_id, rows in by_doc.items():
        rows.sort(key=lambda row: int(row.get("clause_index") or 0))
        contract_row = source_rows_by_doc_id.get(doc_id, {})
        for row in rows:
            title = clean_line(row.get("contract_title")) or best_title(contract_row, ["title", "title_from_list"])
            text = clean_text(row.get("clause_text"))
            flags = standard_clause_review_flags(row, text)
            if flags:
                quality_review.append(
                    review_item(
                        kb_type="standard_clause",
                        source_layer="第四层：标准合同条款",
                        title=title,
                        reason=";".join(flags),
                        suggested_decision="review",
                        row={**contract_row, **row},
                        body=text,
                        priority=contract_row.get("contract_priority") or "",
                        review_category="quality",
                        extra={
                            "doc_id": doc_id,
                            "clause_id": row.get("clause_id"),
                            "clause_index": row.get("clause_index"),
                            "clause_no": row.get("clause_no"),
                            "clause_title": row.get("clause_title"),
                            "normalized_clause_type": row.get("normalized_clause_type"),
                            "contract_domain": row.get("contract_domain"),
                            "review_flags": flags,
                        },
                    )
                )
            if len(text) < 10:
                skipped_empty_count += 1
                continue
            if should_merge_with_previous(row, text) and chunks and chunks[-1]["doc_id"] == doc_id:
                chunks[-1]["clause_text"] = f"{chunks[-1]['clause_text']}\n{text}".strip()
                chunks[-1]["clause_len"] = len(chunks[-1]["clause_text"])
                chunks[-1]["clause_types"] = collect_standard_clause_types(
                    chunks[-1].get("normalized_clause_type"),
                    chunks[-1].get("clause_title"),
                    chunks[-1].get("clause_text"),
                ) or [clean_line(chunks[-1].get("normalized_clause_type")) or "其他"]
                chunks[-1]["text"] = prepend_llm_context(chunks[-1], make_standard_clause_display_text(chunks[-1]))
                chunks[-1]["embedding_text"] = make_standard_clause_embedding_text(chunks[-1])
                chunks[-1]["merged_from"].append(row.get("clause_id"))
                chunks[-1]["review_flags"] = sorted(set(chunks[-1]["review_flags"] + flags + ["merged_short_clause"]))
                chunks[-1]["content_sha256"] = text_sha256(chunks[-1]["text"])
                merged_short_count += 1
                continue
            clause_type = clean_line(row.get("normalized_clause_type")) or "其他"
            domains = collect_standard_contract_domains(title, text, row.get("contract_domain"))
            clause_types = collect_standard_clause_types(clause_type, row.get("clause_title"), text) or [clause_type]
            chunk = {
                "chunk_id": clean_line(row.get("clause_id")) or stable_id("stdclause", doc_id, row.get("clause_index"), text[:80]),
                "doc_id": doc_id,
                "kb_type": "standard_clause",
                "source_layer": "第四层：标准合同条款",
                "source_site": contract_row.get("source_site") or row.get("source_site") or "",
                "source_manifest": row.get("_source_manifest", ""),
                "contract_title": title,
                "contract_doc_no": row.get("doc_no") or contract_row.get("doc_no"),
                "scope": row.get("scope") or contract_row.get("scope"),
                "publish_year": row.get("publish_year") or contract_row.get("publish_year"),
                "publish_agencies": row.get("publish_agencies") or contract_row.get("publish_agencies"),
                "region": row.get("region") or contract_row.get("region"),
                "source_url": row.get("url") or contract_row.get("url"),
                "clause_no": row.get("clause_no"),
                "clause_index": row.get("clause_index"),
                "clause_title": row.get("clause_title"),
                "section_path": row.get("section_path"),
                "clause_role": row.get("clause_role"),
                "clause_text": text,
                "clause_len": len(text),
                "text": "",
                "embedding_text": "",
                "normalized_clause_type": clause_type,
                "contract_domain": row.get("contract_domain") or "通用",
                "contract_domains": domains,
                "clause_types": clause_types,
                "retrieval_weight": standard_clause_retrieval_weight(text, clause_type),
                "risk_tips": clean_text(contract_row.get("risk_tips")),
                "is_short_clause": len(text) < 20,
                "review_flags": flags,
                "merged_from": [row.get("clause_id")],
                "content_sha256": text_sha256(text),
            }
            chunk["text"] = prepend_llm_context(chunk, make_standard_clause_display_text(chunk))
            chunk["embedding_text"] = make_standard_clause_embedding_text(chunk)
            chunk["content_sha256"] = text_sha256(chunk["text"])
            chunks.append(chunk)

    ids_by_doc: dict[str, list[str]] = defaultdict(list)
    for chunk in chunks:
        ids_by_doc[chunk["doc_id"]].append(chunk["chunk_id"])
    positions: dict[str, int] = {}
    for doc_id, ids in ids_by_doc.items():
        for index, chunk_id in enumerate(ids):
            positions[chunk_id] = index
    for chunk in chunks:
        ids = ids_by_doc[chunk["doc_id"]]
        index = positions[chunk["chunk_id"]]
        chunk["neighbor_ids"] = [x for x in (ids[index - 1] if index else None, ids[index + 1] if index + 1 < len(ids) else None) if x]

    write_jsonl(out / "chunks/standard_contracts_full.jsonl", full_chunks)
    write_jsonl(out / "chunks/standard_clauses.jsonl", chunks)
    write_jsonl(out / "review/standard_clauses_quality.review.jsonl", quality_review)
    write_csv(
        out / "review/standard_clauses_quality.review.csv",
        quality_review,
        review_csv_fields()
        + [
            "doc_id",
            "clause_id",
            "clause_index",
            "clause_no",
            "clause_title",
            "normalized_clause_type",
            "contract_domain",
            "review_flags",
        ],
    )

    return {
        "standard_contract_full_chunks": len(full_chunks),
        "standard_clause_chunks": len(chunks),
        "standard_clause_quality_review": len(quality_review),
        "standard_clause_merged_short": merged_short_count,
        "standard_clause_skipped_empty": skipped_empty_count,
    }


def make_standard_contract_full_chunks(
    accepted_doc_ids: set[str],
    source_rows_by_doc_id: dict[str, dict[str, Any]],
    clause_rows_by_doc_id: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for doc_id in sorted(accepted_doc_ids):
        row = source_rows_by_doc_id.get(doc_id, {})
        title = best_title(row, ["title", "title_from_list"])
        body = clean_text(row.get("body"), title=title)
        risk_tips = clean_text(row.get("risk_tips"))
        clauses = clause_rows_by_doc_id.get(doc_id, [])
        outline = make_standard_contract_outline(clauses)
        text = make_standard_contract_full_text(row, title, body, risk_tips)
        legacy_law_refs = detect_legacy_law_refs(body)
        domains = collect_domains(title, body)
        clause_types = dedupe_keep_order(
            item
            for clause in clauses
            for item in collect_standard_clause_types(
                clause.get("normalized_clause_type"),
                clause.get("clause_title"),
                clause.get("clause_text"),
            )
        )
        chunk = {
                "chunk_id": f"{doc_id}#full",
                "doc_id": doc_id,
                "kb_type": "standard_contract_full",
                "chunk_kind": "contract_full_text",
                "status": "active",
                "source_layer": "第四层：标准合同全文",
                "source_site": row.get("source_site") or "",
                "source_manifest": row.get("_source_manifest", ""),
                "contract_title": title,
                "contract_doc_no": row.get("doc_no"),
                "scope": row.get("scope"),
                "region": row.get("region"),
                "publish_year": row.get("publish_year"),
                "publish_agencies": row.get("publish_agencies") or [],
                "source_url": row.get("url"),
                "contract_domain": clean_line(row.get("category")) or (domains[0] if domains else "通用"),
                "contract_domains": domains,
                "clause_types": clause_types,
                "contract_outline": outline,
                "clause_count": len(clauses),
                "body_len": len(body),
                "risk_tips_len": len(risk_tips),
                "text": text,
                "text_len": len(text),
                "embedding_text": make_standard_contract_full_embedding_text(
                    row=row,
                    title=title,
                    domains=domains,
                    clause_types=clause_types,
                    outline=outline,
                    body=body,
                    legacy_law_refs=legacy_law_refs,
                ),
                "retrieval_weight": standard_contract_full_retrieval_weight(len(body), legacy_law_refs),
                "legacy_law_refs": legacy_law_refs,
            }
        chunk["text"] = prepend_llm_context(chunk, text)
        chunk["text_len"] = len(chunk["text"])
        chunk["content_sha256"] = text_sha256(chunk["text"])
        chunks.append(chunk)
    return chunks


def make_standard_contract_full_text(row: dict[str, Any], title: str, body: str, risk_tips: str) -> str:
    header = [
        f"合同：{title}",
        f"层级：{clean_line(row.get('scope'))}" if clean_line(row.get("scope")) else "",
        f"地区：{clean_line(row.get('region'))}" if clean_line(row.get("region")) else "",
        f"年份：{clean_line(row.get('publish_year'))}" if clean_line(row.get("publish_year")) else "",
    ]
    agencies = row.get("publish_agencies") or []
    if agencies:
        header.append(f"发布机关：{'、'.join(str(item) for item in agencies if item)}")
    parts = [line for line in header if line]
    parts.append("\n【合同正文】")
    parts.append(body)
    if risk_tips:
        parts.append("\n【风险提示】")
        parts.append("以下为审查提示，不属于示范合同正文。")
        parts.append(risk_tips)
    return "\n".join(parts).strip()


def make_standard_contract_outline(rows: list[dict[str, Any]], limit: int = 120) -> list[str]:
    outline: list[str] = []
    for row in sorted(rows, key=lambda item: int(item.get("clause_index") or 0)):
        clause_no = clean_line(row.get("clause_no"))
        clause_title = clean_line(row.get("clause_title"))
        clause_type = clean_line(row.get("normalized_clause_type"))
        if not clause_no and not clause_title:
            continue
        label = " ".join(part for part in [clause_no, clause_title] if part)
        if clause_type and clause_type != "其他":
            label = f"{label}（{clause_type}）"
        if label and label not in outline:
            outline.append(label)
        if len(outline) >= limit:
            outline.append(f"...（共 {len(rows)} 个条款/字段块）")
            break
    return outline


def make_standard_contract_full_embedding_text(
    *,
    row: dict[str, Any],
    title: str,
    domains: list[str],
    clause_types: list[str],
    outline: list[str],
    body: str,
    legacy_law_refs: list[str],
) -> str:
    fingerprint = clause_semantic_fingerprint("\n".join([title, body[:8000], "、".join(outline)]), clause_types)
    representative_signals = representative_contract_signals(outline, body, limit=45)
    pieces = [
        "层级：标准合同全文",
        "材料类型：标准合同全文",
        f"合同：{title}",
        join_label("合同领域", domains),
        join_label("覆盖条款类型", clause_types),
        join_label("适用层级", row.get("scope")),
        join_label("地区", row.get("region")),
        join_label("年份", row.get("publish_year")),
        join_label("条款功能覆盖", fingerprint["functions"]),
        join_label("常见风险主题", fingerprint["risks"]),
        join_label("代表性条款信号", representative_signals),
    ]
    agencies = row.get("publish_agencies") or []
    if agencies:
        pieces.append(f"发布机关：{'、'.join(str(item) for item in agencies if item)}")
    doc_no = clean_line(row.get("doc_no"))
    if doc_no:
        pieces.append(f"示范文本编号：{doc_no}")
    if outline:
        pieces.append(f"条款结构：{'、'.join(outline)}")
    if legacy_law_refs:
        pieces.append(f"旧法引用：{'、'.join(legacy_law_refs)}")
    return "\n".join(piece for piece in pieces if piece)


def representative_contract_signals(outline: list[str], body: str, limit: int = 45) -> list[str]:
    candidates: list[str] = []
    for item in outline:
        candidates.extend(re.split(r"[、，,（）()\s]+", clean_line(item)))
    for keyword_group in (*CLAUSE_TYPE_KEYWORDS.values(), *TRIGGER_KEYWORDS.values(), *LEGAL_CONSEQUENCE_KEYWORDS.values()):
        for keyword in keyword_group:
            if keyword in body:
                candidates.append(keyword)
    candidates = [
        item
        for item in candidates
        if len(item) >= 2 and item not in {"其他", "通用", "甲方", "乙方", "合同", "条款", "字段块"}
    ]
    return dedupe_keep_order(candidates)[:limit]


def detect_legacy_law_refs(text: str) -> list[str]:
    refs = []
    for law in ["合同法", "担保法", "物权法", "民法通则", "民法总则", "婚姻法", "继承法", "收养法", "侵权责任法"]:
        if f"中华人民共和国{law}" in text or f"《{law}》" in text or law in text:
            refs.append(law)
    return refs


def standard_contract_full_retrieval_weight(body_len: int, legacy_law_refs: list[str]) -> float:
    weight = 1.0
    if body_len > 32000:
        weight = 0.82
    elif body_len > 16000:
        weight = 0.9
    if legacy_law_refs:
        weight -= 0.08
    return round(max(weight, 0.65), 2)


def standard_clause_retrieval_weight(text: str, clause_type: str) -> float:
    weight = 1.0
    if clean_line(clause_type) == "其他":
        weight -= 0.08
    if len(text) < 40:
        weight -= 0.12
    elif len(text) > 2500:
        weight -= 0.08
    return round(max(weight, 0.72), 2)


def standard_clause_review_flags(row: dict[str, Any], text: str) -> list[str]:
    # 只保留「真质量问题」（空/过短正文、缺标题）→ 进人工复核；
    # 系统性标注缺口（clause_type=="其他"、标题仅编号）不再当复核项：那是分类器覆盖问题，
    # 量大且非逐条人工可解，照常入库（类型信号已在 normalized_clause_type 字段保留）。
    flags: list[str] = []
    if not text:
        flags.append("empty_clause_text")
    if 0 < len(text) < 20:
        flags.append("short_clause_lt_20")
    return flags


def should_merge_with_previous(row: dict[str, Any], text: str) -> bool:
    clause_type = clean_line(row.get("normalized_clause_type"))
    return len(text) < 20 or (len(text) < 45 and clause_type == "其他")


def make_standard_clause_display_text(chunk: dict[str, Any]) -> str:
    header = [
        f"合同：{clean_line(chunk.get('contract_title'))}",
        join_label("位置", clean_line(chunk.get("section_path"))),
        join_label("条款", " ".join(part for part in [clean_line(chunk.get("clause_no")), clean_line(chunk.get("clause_title"))] if part)),
        join_label("条款类型", chunk.get("clause_types") or [chunk.get("normalized_clause_type")]),
    ]
    body = clean_text(chunk.get("clause_text"))
    return "\n".join([line for line in header if line] + ["", body]).strip()


def make_standard_clause_embedding_text(chunk: dict[str, Any]) -> str:
    clause_text = clean_text(chunk.get("clause_text"))
    clause_types = chunk.get("clause_types") or collect_standard_clause_types(
        chunk.get("normalized_clause_type"),
        chunk.get("clause_title"),
        clause_text,
    )
    domains = chunk.get("contract_domains") or normalized_contract_domains(chunk.get("contract_domain"))
    fingerprint = clause_semantic_fingerprint(clause_text, clause_types)
    clause_label = " ".join(
        part
        for part in [clean_line(chunk.get("clause_no")), clean_line(chunk.get("clause_title"))]
        if part
    )
    pieces = [
        "层级：标准合同条款",
        "材料类型：标准条款",
        f"所属合同：{chunk.get('contract_title')}",
        join_label("合同领域", domains),
        join_label("条款类型", clause_types),
        join_label("条款", clause_label),
        join_label("条款功能", fingerprint["functions"]),
        join_label("适用/触发条件", fingerprint["triggers"]),
        join_label("法律后果", fingerprint["consequences"]),
        join_label("风险信号", fingerprint["risks"]),
    ]
    section = clean_line(chunk.get("section_path"))
    if section:
        pieces.append(f"条款路径：{section}")
    role = clean_line(chunk.get("clause_role"))
    if role:
        pieces.append(f"条款角色：{role}")
    pieces.append(f"标准条款原文：{compact_original_text(clause_text, limit=1200)}")
    return "\n".join(piece for piece in pieces if piece)


def review_csv_fields() -> list[str]:
    return [
        "review_id",
        "kb_type",
        "source_layer",
        "source_site",
        "title",
        "contract_priority",
        "review_category",
        "suggested_decision",
        "review_reason",
        "classify_reason",
        "matched_keywords",
        "body_len",
        "url",
        "excerpt",
        "source_manifest",
    ]


def write_unified_review_report(out: Path) -> int:
    review_files = [
        out / "review/judicial_interpretations.review.jsonl",
        out / "review/cases.review.jsonl",
        out / "review/playbooks.review.jsonl",
        out / "review/standard_contracts.review.jsonl",
        out / "review/standard_clauses_quality.review.jsonl",
    ]
    rows: list[dict[str, Any]] = []
    for path in review_files:
        rows.extend(read_jsonl(path))
    rows.sort(key=lambda row: (row.get("kb_type") or "", row.get("source_site") or "", row.get("title") or ""))
    return write_csv(out / "reports/review_items.csv", rows, fields=None)


def write_source_manifest(out: Path, root: Path) -> None:
    payload = {
        "root": str(root),
        "sources": {
            "judicial_related": JUDICIAL_RELATED,
            "judicial_classified": JUDICIAL_CLASSIFIED,
            "case_related": CASE_RELATED,
            "case_classified": CASE_CLASSIFIED,
            "playbook_related": PLAYBOOK_RELATED,
            "playbook_classified": PLAYBOOK_CLASSIFIED,
            "standard_contract_related": STANDARD_CONTRACT_RELATED,
            "standard_contract_classified": STANDARD_CONTRACT_CLASSIFIED,
            "standard_clauses": STANDARD_CLAUSES,
        },
    }
    write_json(out / "manifests/source_files.json", payload)


def write_readme(out: Path) -> None:
    readme = """# `_normalized` 输出说明

本目录由 `scripts/normalize_legal_sources.py` 生成，原始爬取数据不在这里修改。

- `docs/*.jsonl`：高置信、可作为主知识库输入的文档级数据（每行一条记录，字段按重要度排序，
  大段正文在行尾）。带 `cites`（结构化法律引用）、司法解释带 `status`（现行/废止，保守默认 active）。
- `chunks/*.jsonl`：高置信、可直接向量化的切片级数据。
  - `chunks/playbook_review_points.jsonl` 保存 playbook 的原文实务要点切片。
  - `chunks/standard_contracts_full.jsonl` 保存标准合同全文切片，`text` 包含合同正文和风险提示，`embedding_text` 使用合同识别信息和条款结构摘要。
  - `chunks/standard_clauses.jsonl` 保存标准合同条款级切片。
- `preview/*.preview.json`：docs/ 与 chunks/ 的**缩进多行可读副本**（给人检查用，大文件只取前若干条）。
  canonical 数据仍以 `.jsonl` 为准（一行一条，供管线流式读取 / grep / 增量追加）。
- `review/*.review.jsonl|csv`：低置信、正文质量可疑的数据，等待人工审核。注意：系统性标注缺口
  （如条款类型「其他」、标题仅编号）不再当复核项，照常入库。
- `rejected/*.rejected.jsonl`：爬取阶段已判为明显非合同审查主题的数据摘要。
- `reports/review_items.csv`：汇总所有待审核项，便于人工筛选。
- `reports/citation_index.json`：「法条 → 文档」倒排索引（律师式检索的第二条路径）。
- `reports/normalization_summary.json`：本次生成统计。
- `manifests/source_files.json`：本次读取的源 manifest 列表。
"""
    (out / "README.md").write_text(readme, encoding="utf-8")


PREVIEW_LIMIT = 80


def build_citation_index(out: Path) -> int:
    """P1-5：汇总各 docs 的结构化 cites，建「法条 → 文档」倒排，写 reports/citation_index.json。"""
    index: dict[str, list[str]] = defaultdict(list)
    for name in ("judicial_interpretations", "cases"):
        path = out / "docs" / f"{name}.jsonl"
        if not path.exists():
            continue
        for row in read_jsonl(path):
            doc_id = row.get("doc_id")
            for cite in row.get("cites") or []:
                law = cite.get("law")
                if not law:
                    continue
                key = f"{law} {cite.get('article') or ''}".strip()
                if doc_id and doc_id not in index[key]:
                    index[key].append(doc_id)
    payload = {key: index[key] for key in sorted(index)}
    write_json(out / "reports/citation_index.json", payload)
    return len(payload)


def write_preview(out: Path) -> int:
    """为 docs/ 与 chunks/ 的每个 jsonl 生成缩进多行的 preview/*.json（给人读）。

    canonical .jsonl 仍是一行一条（流式读取/grep/增量追加，管线用）；
    preview 是其缩进可读副本，大文件只取前 PREVIEW_LIMIT 条 + 计数提示。
    """
    count = 0
    for sub in ("docs", "chunks"):
        for path in sorted((out / sub).glob("*.jsonl")):
            rows = read_jsonl(path)
            sample = [order_fields(row) for row in rows[:PREVIEW_LIMIT]]
            payload: dict[str, Any] = {
                "_file": f"{sub}/{path.name}",
                "_total_records": len(rows),
                "_preview_records": len(sample),
                "records": sample,
            }
            if len(rows) > PREVIEW_LIMIT:
                payload["_note"] = f"仅预览前 {PREVIEW_LIMIT} 条，完整数据见 {sub}/{path.name}"
            target = out / "preview" / f"{path.stem}.preview.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            count += 1
    return count


def main() -> None:
    args = parse_args()
    root = args.root
    out = args.out or root / "_normalized"
    for subdir in ["docs", "chunks", "review", "rejected", "reports", "manifests", "preview"]:
        (out / subdir).mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    write_source_manifest(out, root)
    summary: dict[str, Any] = {
        "generated_at": started_at,
        "root": str(root),
        "out": str(out),
    }
    summary.update(normalize_judicial(root, out))
    summary.update(normalize_cases(root, out))
    summary.update(normalize_playbooks(root, out))
    summary.update(normalize_standard_contracts(root, out))
    summary["review_items_csv"] = write_unified_review_report(out)
    summary["citation_index_keys"] = build_citation_index(out)
    summary["preview_files"] = write_preview(out)
    summary["generated_files"] = sorted(
        str(path.relative_to(out))
        for path in out.rglob("*")
        if path.is_file()
    )
    write_json(out / "reports/normalization_summary.json", summary)
    write_readme(out)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
