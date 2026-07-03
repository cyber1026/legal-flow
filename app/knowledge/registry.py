"""五个审查支撑知识库的注册表（单一事实来源）。

每个知识库一条 ``KBSpec``：定义 Milvus collection 名、源 chunk 文件、暴露给审查 agent 的
检索工具名/描述，以及给「总结」快模型的库定位提示。入库管线、检索器、检索总结工具都从这里
读规格——新增一个库只改这里 + 在 chunks 目录放一个对应 jsonl，其余代码零改动。

对应的 chunk 文件在 ``settings.kb_chunks_dir``（默认 data/legal_sources/_normalized/chunks）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KBSpec:
    """一个审查支撑知识库的完整规格。"""

    key: str            # 唯一键，CLI / 日志 / artifact 用（如 "standard_clause"）
    collection: str     # Milvus collection 名
    chunk_file: str     # chunks 目录下的源文件名
    display_name: str   # 中文展示名（摘要标题 / 空召回提示用）
    layer: str          # 所属层级（仅描述用）
    tool_name: str      # 暴露给审查 agent 的工具名（LLM 据此选调）
    tool_desc: str      # 工具 docstring —— 指导审查 LLM「何时调、query 写什么」
    summarize_hint: str # 给总结快模型的提示 —— 说明「这个库是什么、该挑什么」
    # 该库「审查参照正文」所在的 metadata 字段（按序拼接，供检索展示/总结）。
    # 默认用 display_text（即 chunk 的 text）；标准条款的有用正文在 clause_text + risk_tips。
    content_fields: tuple[str, ...] = ("display_text",)


# ---------------------------------------------------------------------------
# 五个知识库规格
# ---------------------------------------------------------------------------

KB_REGISTRY: list[KBSpec] = [
    KBSpec(
        key="standard_clause",
        collection="standard_clause_chunks",
        chunk_file="standard_clauses.jsonl",
        display_name="标准合同示范条款库",
        layer="第四层·标准合同（条款级）",
        tool_name="search_standard_clauses",
        tool_desc=(
            "检索官方标准合同示范条款库（市场监管总局等发布的示范文本拆到条款级，含官方风险提示 "
            "risk_tips）。当你想知道「这类条款的规范/示范写法长什么样、官方对它有何风险提示、待审"
            "条款相比示范缺了或偏离了什么」时调用。query 写本条款的主题（如「违约金」「争议解决」"
            "「付款条件」「保密」）。返回的是结合待审条款萃取后的参照要点——它是**审查参照而非法条**，"
            "不要写进 submit_review 的 citations。"
        ),
        summarize_hint=(
            "这是官方标准合同示范条款库（含官方风险提示 risk_tips）。请对照待审条款，挑出：示范条款的"
            "规范写法、官方风险提示，以及待审条款相比示范缺失或偏离之处。"
        ),
        content_fields=("clause_text", "risk_tips"),  # 示范条款正文 + 官方风险提示
    ),
    KBSpec(
        key="standard_contract",
        collection="standard_contract_chunks",
        chunk_file="standard_contracts_full.jsonl",
        display_name="标准合同整份示范文本库",
        layer="第四层·标准合同（整份）",
        tool_name="search_standard_contracts",
        tool_desc=(
            "检索官方标准合同「整份示范文本」库（整体结构 / 条款配置层面）。当你想了解「同类合同通常"
            "应包含哪些条款、整体结构上待审合同是否缺漏关键条款」时调用。query 写合同类型或场景"
            "（如「房屋租赁合同」「建设工程施工合同」）。返回参照性摘要，非法条。"
        ),
        summarize_hint=(
            "这是官方标准合同整份示范文本库。材料里「合同结构」是该示范合同的完整条款标题清单（可据此"
            "判断同类合同应有哪些条款），其后是正文摘录（可能因过长而截断）。请挑出：同类合同应有的条款"
            "配置 / 整体结构，并提示待审合同可能缺漏的关键条款。如需逐字全文，可用 get_standard_contract_fulltext。"
        ),
        # 整份合同正文可达 270KB（超 Milvus 单行上限会被截断），故展示正文优先用「完整条款结构清单」
        # contract_outline（小、不截断）+ 正文摘录 display_text；逐字全文走 fulltext 工具回盘取。
        content_fields=("contract_outline", "display_text"),
    ),
    KBSpec(
        key="judicial",
        collection="judicial_chunks",
        chunk_file="judicial_interpretations.jsonl",
        display_name="司法解释条文库",
        layer="第二层·司法解释",
        tool_name="search_judicial_interpretations",
        tool_desc=(
            "检索最高法 / 最高检司法解释条文库。当法律在本条款情形下含义模糊、需要法院的解释口径时"
            "调用（如合同效力、违约金调整、担保、买卖 / 借贷 / 建设工程纠纷等）。query 写争点。返回"
            "司法解释要点摘要；若要把其中某条作为**正式法律引用**，仍须用 verify_law_article 核验。"
        ),
        summarize_hint=(
            "这是司法解释条文库。请挑出：与本条款争点直接相关的司法解释口径——是哪条解释、说了什么、"
            "对本条款的风险判断意味着什么。"
        ),
    ),
    KBSpec(
        key="case",
        collection="case_chunks",
        chunk_file="cases.jsonl",
        display_name="案例裁判规则库",
        layer="第二层·指导/参考案例",
        tool_name="search_cases",
        tool_desc=(
            "检索指导性案例 / 人民法院参考案例的裁判规则（裁判要旨）库。当你想知道「类似争议法院实际"
            "怎么判、裁判规则是什么」时调用。query 写争点或事实情形。返回裁判规则摘要，作为说理参考，"
            "**不是法条引用**。"
        ),
        summarize_hint=(
            "这是案例裁判规则（裁判要旨）库。请挑出：与检索争点类似的案例，并总结其相关法条、裁判要旨、"
            "基本案情、裁判理由 / 裁判结果，以及该案例对当前问题的参考意义。"
        ),
    ),
    KBSpec(
        key="playbook",
        collection="playbook_chunks",
        chunk_file="playbook_review_points.jsonl",
        display_name="律师实务审查指引库",
        layer="第三层·实务 playbook",
        tool_name="search_playbook",
        tool_desc=(
            "检索律协实务审查指引库（律师实务立场 / 风险等级 / 退让空间 / 谈判建议）。当你想要「实务"
            "中对这类条款的标准立场、能退让到什么程度、谈判怎么提」时调用。query 写条款主题。返回实务"
            "要点摘要。"
        ),
        summarize_hint=(
            "这是律师实务审查指引库。请挑出：对这类条款的实务立场、风险等级、退让空间与谈判建议。"
        ),
    ),
]


# 便捷索引：按 key 取规格（CLI / 工具构建用）。
KB_BY_KEY: dict[str, KBSpec] = {spec.key: spec for spec in KB_REGISTRY}


__all__ = ["KBSpec", "KB_REGISTRY", "KB_BY_KEY"]
