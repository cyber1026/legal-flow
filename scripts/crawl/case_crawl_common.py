"""
案例（指导性案例 / 参考案例）爬取与筛选的公共模块。

与 legal_crawl_common.py（面向「司法解释」这类抽象规则文本）相比，本模块面向**裁判案例**，
两者结构与筛选信号完全不同：

- 司法解释：标题是「最高人民法院关于……的解释」，靠条文关键词判断。
- 裁判案例：标题是「当事人 A 诉当事人 B ……XX 纠纷案」，带结构化字段
  （关键词 / 裁判要点(要旨) / 基本案情 / 裁判结果 / 裁判理由 / 相关法条），
  且标题里直接含**案由**——这是中国民事案件最权威的分类信号。

因此本模块的筛选**以案由为核心**，辅以「案件类型（民事/刑事/行政）」「相关法条是否引用
合同编/合同法/担保制度」「关键词字段」多信号判定，避免老规则那种对正文做粗粒度关键词
匹配导致的严重过度命中（正文里几乎都有「合同/履行/赔偿」字样）。

通用 IO（请求、缓存、落盘、文件名安全化等）直接复用 legal_crawl_common，避免重复。
"""

import os
import re
from typing import Dict, List, Optional, Tuple

# 复用司法解释爬虫里的通用工具，保持一套底层实现
from legal_crawl_common import (  # noqa: F401  （部分仅供下游脚本转引）
    HEADERS,
    REQUEST_TIMEOUT,
    RETRY_TIMES,
    clean_line,
    clean_text,
    ensure_dirs,
    fetch_url,
    md5_text,
    polite_sleep,
    read_jsonl,
    safe_filename,
    sha256_text,
    write_jsonl,
)


# =========================================================================
# 一、案例正文的结构化字段
# =========================================================================

# 指导性案例 / 参考案例 正文里各段落的标题行。注意「裁判要点」（指导案例）与
# 「裁判要旨」（参考案例）含义相同，统一归一到 holding 字段。
CASE_SECTION_MARKERS = [
    "关键词",
    "裁判要点",
    "裁判要旨",
    "基本案情",
    "裁判结果",
    "裁判理由",
    "相关法条",
    "相关索引",  # 部分参考案例用「相关索引」收尾
]

# 段落标题 -> 归一化后的字段名
SECTION_FIELD_MAP = {
    "关键词": "keywords_text",
    "裁判要点": "holding",
    "裁判要旨": "holding",
    "基本案情": "facts",
    "裁判结果": "judgment",
    "裁判理由": "reasoning",
    "相关法条": "relevant_statutes",
    "相关索引": "related_index",
}


# 段落标记后允许紧跟的分隔符（用于识别「行内标记」，如「关键词 刑事/...」）。
_MARKER_SEP = (" ", "　", "：", ":", "/", "／", "、")


def split_case_sections(lines: List[str]) -> Dict[str, str]:
    """
    把案例正文的行序列按段落标题切成结构化字段。

    兼容两种页面写法：
      - 新版：标记单独成行（「关键词」一行，内容在后续行）。
      - 老版：标记与内容同行（「关键词 刑事/生产、销售……罪/……」）——老案例普遍如此，
        若不处理会导致关键词/案件类型大面积缺失。
    判定「标记行」：整行等于标记，或以「标记 + 分隔符」开头（分隔符见 _MARKER_SEP），
    后者把同行剩余文本作为该段首段内容。
    """
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None

    for line in lines:
        stripped = line.strip()

        marker, rest = None, ""
        for m in CASE_SECTION_MARKERS:
            if stripped == m or stripped == m + "：" or stripped == m + ":":
                marker, rest = m, ""
                break
            if stripped.startswith(m) and len(stripped) > len(m) and stripped[len(m)] in _MARKER_SEP:
                marker = m
                rest = stripped[len(m):].lstrip(" 　：:").strip()
                break

        if marker:
            current = SECTION_FIELD_MAP[marker]
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
            continue

        if current:
            sections[current].append(stripped)

    return {k: clean_text("\n".join(v)) for k, v in sections.items()}


# =========================================================================
# 二、案由（cause of action）—— 合同相关判定的核心信号
# =========================================================================

def strip_title_prefix(title: str) -> str:
    """去掉标题前缀编号（「指导性案例279号：」「参考案例 入库编号xxx：」等），
    返回「当事人 + 案由」主体，供分类匹配与案由提取共用。"""
    if not title:
        return ""
    t = clean_line(title)
    t = re.sub(r"^.*?(?:案例|入库编号|案号)[^：:]*[：:]", "", t).strip()
    return t or clean_line(title)


# 当事人机构后缀（用于把被告名从「被告+案由」里裁掉）。只含**多字机构类**后缀；
# 刻意不含单字后缀（行/社/部/处/所/场/店/厂…），它们常出现在普通词里（行政、合作、处理…），
# 会把案由切坏。漏掉极少数「XX厂/XX店」被告只是案由前残留几个字，属可接受的显示瑕疵。
_PARTY_SUFFIX_PATTERN = (
    r"(?:股份有限公司|有限责任公司|有限公司|分公司|子公司|公司|"
    r"信用合作社|信用社|合作社|联社|银行|"
    r"事务所|研究所|设计院|医院|学校|学院|大学|"
    r"管理委员会|村民委员会|居民委员会|委员会|管委会|政府|"
    r"管理局|分局|支局|邮局|税务局|"
    r"集团|协会|学会|基金会|商会|工厂|商行|商店|超市|宾馆|酒店|中心)"
)
# 一个「当事人块」：以机构后缀或自然人名（X某 / X某某）结尾。
# 字符集含空格/连字符/点号，以兼容外资或被 HTML 拆开的公司名（如「股份 有限公司」「A.P.穆勒-马士基」）。
_NATURAL_PERSON_PATTERN = r"(?:[一-龥]{1,3}某(?:某|[甲乙丙丁戊]|[一-龥])?)"
_PARTY_TOKEN_RE = re.compile(
    r"^[一-龥A-Za-z0-9()（）·.\-　 ]{1,40}?(?:" + _PARTY_SUFFIX_PATTERN + r"|" + _NATURAL_PERSON_PATTERN + r")"
)
# 案由起始动词：当事人块不会以这些开头，命中即认为已到案由。
_CAUSE_VERB_PREFIX_RE = re.compile(r"^(?:损害|侵害|侵犯|妨害|请求|确认|返还|排除|撤销|追偿|追索)")


def extract_cause_of_action(title: str) -> str:
    """
    从案例标题里提取案由，并裁掉当事人名。

    步骤：去编号前缀与结尾「案」→ 从第一个「诉/与」切掉原告 → 逐个剥离开头的被告当事人
    （机构名以「公司/厂/银行…」结尾，自然人以「某」结尾，多被告以「、」「等」分隔），
    遇到案由动词（损害/侵害/确认…）即停止，避免切进含「公司/银行」字样的案由
    （如「损害公司利益责任纠纷」）。

    例：
      「……荣某诉某汽车销售公司买卖合同纠纷案」          -> 「买卖合同纠纷」
      「……诉广州沃某模具有限公司侵害计算机软件著作权纠纷案」 -> 「侵害计算机软件著作权纠纷」
      「……诉张某某等损害公司利益责任纠纷案」              -> 「损害公司利益责任纠纷」
    """
    t = strip_title_prefix(title)
    if not t:
        return ""

    t = re.sub(r"案$", "", t).strip()  # 去结尾「案」
    # 去原告：从第一个「诉」切（A诉B）；无「诉」时退而用「与」。
    # 用第一个而非最后一个，避免案由里「恶意提起……诉讼」中的「诉」被误切。
    if "诉" in t:
        t = t.split("诉", 1)[1]
    elif "与" in t:
        t = t.split("与", 1)[1]

    t = _strip_leading_parties(t)
    # 残留的自然人共同被告「张某某等」「习文有等」
    t = re.sub(r"^[一-龥]{2,4}等(?=[、，]?[一-龥])", "", t).strip()
    t = re.sub(r"^(?:等人|等|因|及|、|，|和)+", "", t).strip()
    t = _trim_party_residue_before_known_cause(t)
    return clean_line(t)


def _strip_leading_parties(t: str) -> str:
    """反复剥离开头的被告当事人块，直到遇到案由。"""
    prev = None
    while t and t != prev:
        prev = t
        if _CAUSE_VERB_PREFIX_RE.match(t):  # 已到案由（以动词开头）
            break
        m = _PARTY_TOKEN_RE.match(t)
        if not m or m.end() >= len(t):
            break
        nxt = re.sub(r"^(?:、|，|及|和|与|等)+", "", t[m.end():])
        if nxt == t:
            break
        t = nxt
    return t


def _trim_party_residue_before_known_cause(t: str) -> str:
    """裁掉自然人/机构名残片。

    人民法院案例库标题常见「A诉B、C某民间借贷纠纷案」。如果被告自然人名不是
    「某某」而是「某琴/某军/某华」，早期规则会留下「琴民间借贷纠纷」这类残片。
    这里只在残片后紧跟高信号案由关键词时截断，避免误伤真正以动词开头的案由。
    """
    text = (t or "").strip()
    if not text:
        return ""

    cause_heads = sorted(
        set(CONTRACT_CAUSE_EXTRA + CONTRACT_KEYWORDS + NONCONTRACT_CAUSE_KEYWORDS),
        key=len,
        reverse=True,
    )
    for head in cause_heads:
        idx = text.find(head)
        if idx <= 0 or idx > 24:
            continue
        residue = text[:idx]
        if re.search(r"(?:某|、|，|等|公司|银行|学校|学院|医院|中心|甲|乙|丙|丁|戊)", residue):
            return text[idx:]
        # 「琴民间借贷纠纷」「军民间借贷纠纷」这类单字自然人尾字残留。
        if len(residue) <= 2:
            return text[idx:]
    return text


def extract_case_type(keywords_text: str) -> str:
    """
    从「关键词」字段提取案件类型。指导性案例/参考案例的关键词字段首段恒为
    案件大类：民事 / 商事 / 刑事 / 行政 / 国家赔偿 / 执行（以「/」分隔）。
    例：「民事/侵害计算机软件著作权/实质性相似/...」-> 「民事」
    """
    if not keywords_text:
        return ""
    first = re.split(r"[/／、,，\s]", keywords_text.strip(), maxsplit=1)[0]
    return first.strip() if first.strip() in CASE_TYPE_SET else ""


CASE_TYPE_SET = {"民事", "商事", "刑事", "行政", "国家赔偿", "执行", "审判监督"}


# =========================================================================
# 三、合同相关词表（针对案例，全部为高信号短语）
# =========================================================================

# 1) 合同类「案由」白名单——这些案由本身就属于合同/准合同/债之保全范畴。
#    注意：凡案由里含「合同」「协议」二字的，统一视为合同纠纷（见 _cause_is_contract），
#    这里只补充**不含「合同/协议」字样、但仍属合同审查范畴**的案由。
CONTRACT_CAUSE_EXTRA = [
    # 借贷 / 金融（案由不含「合同」）
    "民间借贷", "金融借款", "企业借贷", "小额借款", "储蓄存款", "银行卡",
    # 准合同
    "不当得利", "无因管理",
    # 债之保全 / 债的移转（与合同强相关）
    "债权人代位权", "债权人撤销权", "债权转让", "债务转移", "债权债务概括转移",
    "追偿权", "代位求偿", "保证金", "定金",
    # 担保物权（多由担保合同产生）
    "抵押权", "质权", "留置权", "担保物权", "实现担保物权",
    # 票据 / 信用证 / 保函
    "票据", "本票", "汇票", "支票", "信用证", "独立保函", "保理",
    # 居间 / 行纪 / 中介
    "居间", "行纪", "中介",
    # 期房 / 不动产交易（部分案由不含「合同」）
    "商品房销售", "商品房预售", "房屋买卖", "土地使用权", "建设用地使用权",
    # 农村土地经营（流转类属合同）
    "土地经营权", "土地承包经营权",
    # 缔约阶段
    "缔约过失",
]

# 2) 合同类**关键词**（用于标题/关键词字段补充判定，均为多字高信号词）
CONTRACT_KEYWORDS = [
    "买卖合同", "供用电", "赠与合同", "借款合同", "保证合同", "抵押合同",
    "质押合同", "定金合同", "租赁合同", "房屋租赁", "融资租赁", "承揽合同",
    "建设工程", "施工合同", "勘察设计合同", "运输合同", "货物运输", "客运合同",
    "保管合同", "仓储合同", "委托合同", "委托理财", "行纪合同", "中介合同",
    "技术合同", "技术开发", "技术转让", "技术服务", "技术咨询", "技术许可",
    "服务合同", "物业服务", "旅游合同", "保险合同", "合伙协议", "合伙合同",
    "股权转让", "增资", "特许经营", "经销", "供货", "代理合同", "广告合同",
    "网络服务合同", "医疗服务合同", "教育培训合同", "商品房买卖", "预售",
    "农村土地承包", "中外合资", "中外合作", "联营",
]

# 3) 合同法条信号——「相关法条」字段引用以下法律/条文时，判为合同相关（强信号）。
#    覆盖《民法典》合同编与合同编通则解释、担保制度解释、买卖/租赁/建工等专项解释，
#    以及已废止但案例仍引用的《合同法》《担保法》。
CONTRACT_STATUTE_PATTERNS = [
    r"合同法",
    r"民法典[^，。；]{0,12}合同编",
    r"合同编通则",
    r"担保法",
    r"担保制度",
    r"买卖合同[^，。；]{0,6}(?:解释|规定)",
    r"建设工程[^，。；]{0,8}(?:施工合同|解释)",
    r"融资租赁[^，。；]{0,6}(?:合同|解释)",
    r"民间借贷[^，。；]{0,6}(?:规定|解释)",
    r"商品房买卖[^，。；]{0,6}(?:解释|规定)",
    r"城镇房屋租赁",
    r"保理",
]
_CONTRACT_STATUTE_RE = re.compile("|".join(CONTRACT_STATUTE_PATTERNS))

# 《民法典》合同编大致条文区间（第463条—第988条：合同 + 准合同）。
# 若相关法条引用「《中华人民共和国民法典》第N条」且 N 落在该区间，亦视为合同相关。
_MINFADIAN_ARTICLE_RE = re.compile(r"民法典[^第]{0,8}第\s*(\d{2,4})\s*条")
MINFADIAN_CONTRACT_RANGE = (463, 988)

# 刑事 / 行政法条——「相关法条」引用即判为刑事/行政案件（关键词字段缺失时的兜底排除）。
# 合同审查语料不收刑事（含合同诈骗罪）与行政案件。
_CRIMINAL_STATUTE_RE = re.compile(r"《[^》]*刑法[^》]*》|刑事诉讼法|刑法修正案")
_ADMIN_STATUTE_RE = re.compile(r"行政诉讼法|行政处罚法|行政复议法|行政强制法|行政许可法|治安管理处罚")

# 非案例页面（批次发布通知、案例目录等）——这些不是裁判案例，应整体排除。
_NONCASE_TITLE_RE = re.compile(r"(?:发布第.{1,4}批.*指导性案例)|(?:指导性案例.*目录)")


def is_non_case(title: str) -> bool:
    """标题是否为「非案例」页面（批次发布通知 / 目录等）。"""
    t = (title or "").strip()
    if not t:
        return False
    if t.endswith("的通知") or t.endswith("通知"):
        return True
    if _NONCASE_TITLE_RE.search(t):
        return True
    return False

# 4) 明确**非合同类**案由（硬排除信号）——案由命中且无任何合同信号时直接 DROP。
#    覆盖：侵权责任、知识产权权属/侵害、物权、人格权、婚姻家庭继承、公司组织法、
#    刑事罪名、行政、特别程序等。
NONCONTRACT_CAUSE_KEYWORDS = [
    # 侵权（非违约）
    "机动车交通事故责任", "医疗损害责任", "产品责任", "环境污染责任",
    "饲养动物损害", "物件损害责任", "教育机构责任", "网络侵权责任",
    "生命权", "健康权", "身体权", "名誉权", "荣誉权", "隐私权", "肖像权",
    "姓名权", "人格权", "侵权责任", "财产损害赔偿",
    # 知识产权权属 / 侵害（许可合同除外，许可合同含「合同」会先命中合同）
    "侵害著作权", "侵害计算机软件", "侵害专利", "侵害商标", "侵害实用新型",
    "侵害外观设计", "侵害发明专利", "侵害植物新品种", "侵害集成电路",
    "著作权权属", "专利权权属", "商标权权属", "著作权属", "专利权属", "商标权属",
    "权属纠纷", "确认不侵害", "不正当竞争", "垄断", "商业诋毁", "假冒",
    # 行政（关键词字段缺失时的标题兜底）
    "行政纠纷", "行政处罚", "行政复议", "行政赔偿", "行政登记", "行政确认",
    "行政许可", "行政强制", "行政征收", "行政不作为",
    # 物权（非合同）
    "物权确认", "返还原物", "排除妨害", "消除危险", "相邻关系",
    "共有", "地役权", "所有权确认", "用益物权",
    # 婚姻家庭继承
    "离婚", "婚约财产", "同居", "抚养", "扶养", "赡养", "收养",
    "继承", "遗赠", "遗嘱", "分家析产", "夫妻财产",
    # 人身 / 身份
    "确认亲子关系", "监护", "宣告失踪", "宣告死亡",
    # 公司组织法（非合同）
    "公司决议", "公司解散", "公司清算", "股东资格确认", "股东知情权",
    "请求公司收购股份", "损害公司利益", "损害股东利益", "清算责任",
    # 程序 / 特别程序 / 选举
    "选民资格", "指定遗产管理人", "申请认定财产无主",
]

# 刑事罪名关键词（标题级）。案例库的参考案例里有大量刑事案（如「合同诈骗案」「票据诈骗案」），
# 标题含「合同」会被误判为合同纠纷；而这类「部分行」无案件类型字段可依据，故须按标题里的**罪名**排除。
# 这些词在民商事合同案由里几乎不会出现（民事用「欺诈」而非「诈骗」），匹配可靠。
CRIMINAL_CHARGE_KEYWORDS = [
    "诈骗", "贪污", "受贿", "行贿", "贿赂", "挪用", "职务侵占", "侵占罪",
    "盗窃", "抢劫", "抢夺", "敲诈勒索", "非法吸收", "集资", "洗钱",
    "走私", "贩卖", "运输毒品", "制造毒品", "持有毒品", "危险驾驶", "交通肇事",
    "寻衅滋事", "聚众", "伪造货币", "伪造", "变造", "假冒注册商标", "假药", "劣药",
    "侵犯公民个人信息", "渎职", "玩忽职守", "滥用职权", "强奸", "猥亵", "强制猥亵",
    "绑架", "故意伤害", "故意杀人", "过失致人", "非法经营", "非法占用", "非法采矿",
    "污染环境", "组织卖淫", "容留", "介绍卖淫", "妨害公务", "袭警", "掩饰", "隐瞒",
    "窝藏", "包庇", "虚开", "逃税", "骗取贷款", "骗取", "开设赌场", "赌博",
    "拐卖", "敲诈", "强迫交易", "合同诈骗", "票据诈骗", "信用卡诈骗", "贷款诈骗",
    "金融凭证", "操纵证券", "内幕交易", "强制医疗", "宣告无罪", "无罪案",
]


def _is_criminal_by_title(match_text: str) -> bool:
    """标题是否含刑事罪名（用于无案件类型字段时的刑事排除）。"""
    return any(kw in match_text for kw in CRIMINAL_CHARGE_KEYWORDS)


def _is_contract_signal(match_text: str) -> bool:
    """标题主体是否含合同/准合同信号。

    在「去编号前缀的标题」上匹配——标题里的案由是案件官方分类，当事人名几乎不含
    法律案由词，因此既可靠又不会像在长正文上匹配那样过度命中。
    """
    if "合同" in match_text or "协议" in match_text:
        return True
    return any(kw in match_text for kw in CONTRACT_CAUSE_EXTRA)


def _is_noncontract_signal(match_text: str) -> bool:
    """标题主体是否命中明确的非合同类（硬排除）。"""
    return any(kw in match_text for kw in NONCONTRACT_CAUSE_KEYWORDS)


def _statutes_hit_contract(relevant_statutes: str) -> bool:
    """相关法条是否引用了合同编 / 合同法 / 担保等合同相关规范。"""
    text = relevant_statutes or ""
    if _CONTRACT_STATUTE_RE.search(text):
        return True
    for m in _MINFADIAN_ARTICLE_RE.finditer(text):
        art = int(m.group(1))
        if MINFADIAN_CONTRACT_RANGE[0] <= art <= MINFADIAN_CONTRACT_RANGE[1]:
            return True
    return False


# =========================================================================
# 四、优化版「合同相关」分类器
# =========================================================================

def classify_case_contract_relevance(row: Dict) -> Dict:
    """
    判断单条案例是否与合同（合同审查场景）相关，并给出优先级与理由。

    判定优先级（从高到低，治本式分层）：
      DROP_NONCASE  批次发布通知/目录等非案例页面 —— 整体剔除
      DROP（刑事/行政）案件类型或法条表明是刑事/行政 —— 非合同审查范畴
      P0_CAUSE   案由即合同/准合同类               —— 最可靠
      P1_STATUTE 相关法条引用合同编/合同法/担保等   —— 实质合同争议
      P2_KEYWORD 案由命中合同关键词                 —— 待复核
      DROP       案由属硬排除类 / 无任何合同信号

    关键设计：只在**短而高信号的字段**（案由、标题、关键词字段、相关法条）上做匹配，
    绝不对基本案情/裁判理由等长正文做关键词匹配（那是老规则过度命中的根源）。
    """
    title = row.get("doc_title") or row.get("title_from_list") or row.get("page_title") or ""
    # 标题末尾的案由是案例库最稳定的分类信号；抓取接口返回/旧规则写入的
    # cause_of_action 可能残留被告姓名尾字（如「琴民间借贷纠纷」），因此优先重算。
    cause = extract_cause_of_action(title) or row.get("cause_of_action") or ""
    keywords_text = row.get("keywords_text") or ""
    relevant_statutes = row.get("relevant_statutes") or ""
    case_type = row.get("case_type") or extract_case_type(keywords_text)

    # 匹配文本：去编号前缀的标题主体（含当事人 + 案由）。短、高信号、官方分类。
    match_text = strip_title_prefix(title)

    # 信号汇总
    strong_contract = ("合同" in match_text) or ("协议" in match_text)  # 标题含「合同/协议」——决定性
    extra_contract = any(kw in match_text for kw in CONTRACT_CAUSE_EXTRA)  # 借贷/担保物权/票据等
    cause_is_noncontract = _is_noncontract_signal(match_text)
    statute_hit = _statutes_hit_contract(relevant_statutes)
    # 案由命中合同关键词（P2）。在**已剥离当事人的案由**上匹配，避免命中公司名（如「技术开发有限公司」）
    keyword_hits = sorted({kw for kw in CONTRACT_KEYWORDS if kw in cause})

    # 刑事/行政/国家赔偿——排除。case_type（来自关键词字段）缺失时，用法条引用 + 标题罪名兜底判定。
    criminal_hit = bool(_CRIMINAL_STATUTE_RE.search(relevant_statutes)) or _is_criminal_by_title(match_text)
    admin_hit = bool(_ADMIN_STATUTE_RE.search(relevant_statutes))
    type_excluded = (case_type in {"刑事", "行政", "国家赔偿"}) or criminal_hit or admin_hit

    keep = False
    priority = "DROP"
    reason = ""

    if is_non_case(title):
        # 批次发布通知 / 目录等——不是裁判案例
        priority = "DROP_NONCASE"
        reason = "非案例页面（批次发布通知/目录）"
    elif type_excluded:
        # 刑事/行政/国家赔偿——直接排除（即便偶含「合同」字样，如合同诈骗罪也非合同审查范畴）
        label = case_type or ("刑事" if criminal_hit else "行政")
        reason = f"案件类型为「{label}」，非合同审查范畴"
    elif strong_contract:
        # 标题含「合同/协议」是决定性信号，优先于非合同案由
        keep, priority = True, "P0_CAUSE"
        reason = f"案由属合同类：{cause or '（标题含合同/协议）'}"
    elif cause_is_noncontract:
        # 明确非合同案由（侵权/知产侵害/物权/婚姻/公司组织等）且无「合同/协议」字样
        reason = f"案由属明确非合同类：{cause}"
    elif extra_contract:
        # 不含「合同」字样但属合同范畴的案由（民间借贷/担保物权/票据/不当得利等）
        keep, priority = True, "P0_CAUSE"
        reason = f"案由属合同/准合同类：{cause}"
    elif statute_hit:
        keep, priority = True, "P1_STATUTE"
        reason = "相关法条引用合同编/合同法/担保等合同规范"
    elif keyword_hits:
        keep, priority = True, "P2_KEYWORD_REVIEW"
        reason = f"案由命中合同关键词：{'、'.join(keyword_hits)}，建议人工复核"
    else:
        reason = f"无合同信号（案由：{cause or '未识别'}）"

    return {
        **row,
        "cause_of_action": cause,
        "case_type": case_type,
        "contract_related": keep,
        "contract_priority": priority,
        "classify_reason": reason,
        "contract_keyword_hits": keyword_hits,
        "statute_hit_contract": statute_hit,
    }


# 优先级排序权重
_PRIORITY_ORDER = {
    "P0_CAUSE": 0,
    "P1_STATUTE": 1,
    "P2_KEYWORD_REVIEW": 2,
    "DROP": 99,
}


def ensure_case_dirs(out_dir: str) -> Dict[str, str]:
    """案例输出目录结构。复用 legal_crawl_common.ensure_dirs 的命名习惯。"""
    return ensure_dirs(out_dir)


# =========================================================================
# 案例 Markdown 去重 / 增量判定
# 历史问题：all/ 下文件名带 md5(url) 后缀，而 url 的 id token 每次抓取都变，
# 导致同一案例反复落成新文件（10184 文件 / 仅 1225 唯一标题）。
# 下列工具以「标题主干」为稳定键，支撑：下载前跳过已有全文、以及存量去重清理。
# =========================================================================

# 旧命名 `{主干}_{12位十六进制}.md` 的 url 哈希后缀
_URL_HASH_SUFFIX_RE = re.compile(r"_[0-9a-f]{12}$")


def stem_from_md_filename(filename: str) -> str:
    """从 markdown 文件名还原标题主干，兼容旧（带 url 哈希）与新（稳定）两种命名。"""
    name = filename[:-3] if filename.endswith(".md") else filename
    return _URL_HASH_SUFFIX_RE.sub("", name)


def case_md_is_full(path: str) -> bool:
    """判断一篇案例 Markdown 是否为「全文」。

    超每日配额时只落「标题 + 裁判要旨」的部分行，其「基本案情 / 裁判理由」为空；
    据此区分全文与部分行，避免把部分行误判为已下载而永不补全。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    for header in ("## 基本案情", "## 裁判理由"):
        m = re.search(re.escape(header) + r"\n(.*?)(?=\n## |\Z)", text, re.S)
        if m and m.group(1).strip():
            return True
    return False


def existing_full_case_stems(all_md_dir: str) -> set:
    """扫描 all/ 目录，返回已落「全文」案例的标题主干集合（下载前去重用）。"""
    stems = set()
    if not os.path.isdir(all_md_dir):
        return stems
    for fname in os.listdir(all_md_dir):
        if fname.endswith(".md") and case_md_is_full(os.path.join(all_md_dir, fname)):
            stems.add(stem_from_md_filename(fname))
    return stems


def filter_contract_related_cases(paths: Dict[str, str], rows: List[Dict]) -> List[Dict]:
    """对全量案例做合同相关筛选，落盘分类全集 / 合同相关子集 / 摘要 CSV / Markdown。"""
    classified = [classify_case_contract_relevance(r) for r in rows]
    related = [r for r in classified if r["contract_related"]]

    related.sort(
        key=lambda x: (
            _PRIORITY_ORDER.get(x.get("contract_priority"), 99),
            -(x.get("case_no_int") or 0),  # 指导案例按编号倒序（新→旧）
            x.get("doc_title") or "",
        )
    )

    classified_path = os.path.join(paths["manifest"], "classified_all.jsonl")
    related_path = os.path.join(paths["manifest"], "contract_related_cases.jsonl")
    summary_csv_path = os.path.join(paths["manifest"], "contract_related_summary.csv")

    write_jsonl(classified_path, classified)
    write_jsonl(related_path, related)
    _write_case_csv(summary_csv_path, related)

    # 清空合同相关 Markdown 目录后重写，避免历次运行（规则收紧后）的陈旧文件残留
    import glob
    for old in glob.glob(os.path.join(paths["contract_md"], "*.md")):
        os.remove(old)

    md_errors = []
    for row in related:
        try:
            md_path = os.path.join(
                paths["contract_md"],
                safe_filename(row.get("doc_title") or row.get("page_title"), row["url"]),
            )
            save_case_markdown(row, md_path)
        except Exception as e:  # 单篇 Markdown 失败不应中断整体
            md_errors.append({"url": row.get("url"), "error": str(e)})

    if md_errors:
        write_jsonl(os.path.join(paths["logs"], "contract_markdown_errors.jsonl"), md_errors)

    # 各优先级数量统计，便于评估筛选效果
    from collections import Counter
    dist = Counter(r["contract_priority"] for r in classified)

    print(f"全部分类结果：{classified_path}")
    print(f"合同相关结果：{related_path}（共 {len(related)} / {len(classified)} 篇）")
    print(f"合同相关摘要 CSV：{summary_csv_path}")
    print(f"合同相关 Markdown：{paths['contract_md']}")
    print(f"优先级分布：{dict(dist)}")

    return related


def _write_case_csv(path: str, rows: List[Dict]):
    """合同相关案例摘要 CSV（便于人工抽查筛选效果）。"""
    import csv

    fields = [
        "contract_priority", "case_no", "case_type", "cause_of_action",
        "doc_title", "court", "case_id", "publish_time", "url", "classify_reason",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


# =========================================================================
# 五、Markdown 输出
# =========================================================================

def save_case_markdown(row: Dict, path: str):
    """把单条案例渲染为 Markdown，保留结构化字段，便于后续入库切分。"""
    content = f"""# {row.get("doc_title") or row.get("page_title")}

## 元数据

- 来源类型：{row.get("source_type")}
- 来源网站：{row.get("source_site")}
- 案例类型：{row.get("case_category")}
- 指导案例编号：{row.get("case_no")}
- 案例库编号：{row.get("case_id")}
- 案号：{row.get("court_case_no")}
- 审理法院：{row.get("court")}
- 案由：{row.get("cause_of_action")}
- 案件类型：{row.get("case_type")}
- 发布时间：{row.get("publish_time")}
- 原文链接：{row.get("url")}

## 合同相关分类

- 是否合同相关：{row.get("contract_related")}
- 优先级：{row.get("contract_priority")}
- 分类原因：{row.get("classify_reason")}
- 命中合同关键词：{"、".join(row.get("contract_keyword_hits") or [])}

---

## 关键词

{row.get("keywords_text") or ""}

## 裁判要点 / 裁判要旨

{row.get("holding") or ""}

## 基本案情

{row.get("facts") or ""}

## 裁判结果

{row.get("judgment") or ""}

## 裁判理由

{row.get("reasoning") or ""}

## 相关法条

{row.get("relevant_statutes") or ""}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
