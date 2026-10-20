# -*- coding: utf-8 -*-
"""规范 id 登记表（canonical registry）—— 2026-10-17 落地（用户选「组合：强制+别名表」）。

职责：把 AI 在候选里写的「原始文本」归一为系统内部规范 id。

收敛锚（2026-10-17 裁定）：direction/transmission 的 (target/from/to) 是 state 变量、有界宇宙，
归一后由 _semantic_key 锚定 added==0（修复「AI 换说法就算新类别 → 追问循环不收敛」）。
narrative 不参与收敛（见 world_tree._grow 追问循环：仅按 id 去重、不计入 added），
故本表的叙事别名只用于「避免重复子节点」，不再是收敛机制——收敛锚在 state，不在叙事 id。

两条铁律（用户裁定）：
  1. **归一化只做确定性规则映射，绝不引入 embedding / LLM**（系统不内置模型，AI 在系统外）。
  2. **别名表是「人选的语义切分」，不是世界事实**——本文件显式标注每一条别名是人写的，
     不假装成客观映射。新增别名 = 人审后追加，不自动推断。

为什么需要它（背景）：
  ai_candidate._normalize 原先对 target/from/to/narrative 原样透传，只校验非空 + sign。
  若 agent（外部 LLM）把同一概念写成不同字符串（"CPI同比" vs "CPI"、"需求弱" vs "demand_weak"），
  direction/transmission 的 _semantic_key 会生成两条不同 key（叙事已不在此层收敛，见上）→
  added 永远 >0 → 追问循环不收敛（cap 默认 None 不截断）。
  本登记表在 _normalize 入站处对方向/传输做「别名归一 + 范围外拒收」，把同义写法并成同一规范 id；
  对叙事仅做别名归一（开放透传），且只用于去重避免重复子节点，不参与 added==0 收敛。

域分级（关键设计，避免误伤现有测试）：
  - direction.target / transmission.from / transmission.to = **有界宇宙**（市场/宏观指标 + 传输节点），
    不在 (规范 ∪ 别名) 内的 → **拒收**（return None），避免污染树、保证收敛。
  - narrative / narrative_retire = **开放语义标签**（系统叙事分类 + 测试占位 X/W/A/B/C 等），
    **不做拒收**（仅别名归一），未知叙事透传，保证开放性与现有测试不退化。

开关：
  - REJECT_BOUNDED = True  → 有界域（direction/target/transmission）范围外拒收。
  - REJECT_NARRATIVE = False → 叙事域开放，不拒收。
  若将来要全局严格，把 REJECT_NARRATIVE 也置 True（需先扩充 NARRATIVE_IDS 覆盖测试占位）。
"""
from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────────────
# 1. 方向目标规范集（有界宇宙）：宏观指标 + 市场维度 + 传输图用到的可观测节点
#    派生自全仓实际用法（candidate_source / feeds / constraints / 各 verify / transmission 图）。
# ─────────────────────────────────────────────────────────────────────────────
DIRECTION_TARGETS = {
    # 宏观指标（state key，agent 可对其断言方向）
    "CPI", "PPI", "M2", "LPR_1Y", "LPR_5Y", "CN10Y", "policy_stance",
    "US_CPI_YOY", "US_10Y",
    # 市场维度（candidate_source.DIRECTION_TARGETS）
    "Commodity", "Equity", "USDCNH",
    # 汇率 / 跨境资本链（2026-10-19 Request B）：北向资金净流向代理指数 + 沪深300 核心资产
    "NORTHBOUND", "CSI300",
    # 信用维度（2026-10-19 用户指令）：社融(信用脉冲) + M1-M2 剪刀差
    "TSF", "M1M2_GAP",
    # 财政维度（2026-10-19 用户指令）：财政立场(赤字率/积极财政，离散政策状态) + 专项债(机制节点，不作可步进状态)
    "FISCAL_STANCE", "SPECIAL_BOND",
    # 传输图 / ENSO-电力-AI 演示用到的可观测节点
    "G_NASDAQ", "G_SOX", "CN_POWER_IDX", "CN_AGRI_PRICE_IDX", "CN_AI_IDX",
    "CN_HS300", "CN_CYB", "CN_KCB50", "ENSO", "CLIMATE", "AG_PRICE",
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. 叙事规范集（开放语义标签）：系统叙事分类
#    派生自 candidate_source.NARRATIVE_* + feeds / demo 实际用到的叙事 id。
#    注意：测试占位 X/W/A/B/C 等自由形式不在此集合（叙事域开放、不拒收）。
# ─────────────────────────────────────────────────────────────────────────────
NARRATIVE_IDS = {
    "policy_easing_expectation", "fiscal_expansion", "credit_expansion",
    "external_rate_pressure", "inflation_concern", "demand_weak", "cost_push",
    "fiscal_stress", "fiscal_supply_pressure", "policy_liquidity_easing",
    "credit_contraction",   # 2026-10-19 用户指令：信用收缩（社融/剪刀差走弱）
    "fiscal_contraction",   # 2026-10-19 用户指令：财政收缩（对应扩张）
    "supply_chain_risk",   # demo / example_agent_loop 追问补全的叙事
}

# 传输节点 = 叙事 ∪ 方向目标 ∪ 环境/市场节点（传输链可跨叙事/指标/环境）
TRANSMISSION_NODES = set(NARRATIVE_IDS) | set(DIRECTION_TARGETS)

# ─────────────────────────────────────────────────────────────────────────────
# 3. 别名表（人选语义切分，非事实）：原始文本 → 规范 id
#    每一条都是人写的同义归一。新增需人审。AI 写别名 → 并成规范 id → 同一 key（收敛）。
#    只列「无歧义」的同义；有歧义的（如单独的 "LPR" 既可能 1Y 也可能 5Y）不入表，避免误并。
# ─────────────────────────────────────────────────────────────────────────────
ALIASES = {
    # ---- 宏观指标同义 ----
    "CPI同比": "CPI", "CPI 同比": "CPI", "CPI增长率": "CPI",
    "居民消费价格指数": "CPI", "通胀同比": "CPI",
    "PPI同比": "PPI", "PPI 同比": "PPI", "工业品出厂价格": "PPI",
    "M2同比": "M2", "M2 同比": "M2", "广义货币": "M2", "广义货币供应": "M2",
    "LPR1年": "LPR_1Y", "LPR一年": "LPR_1Y", "1年期LPR": "LPR_1Y", "1年LPR": "LPR_1Y",
    "LPR5年": "LPR_5Y", "LPR五年": "LPR_5Y", "5年期LPR": "LPR_5Y", "5年LPR": "LPR_5Y",
    "10年期国债": "CN10Y", "十年期国债": "CN10Y", "十年期国债收益率": "CN10Y",
    "国债收益率": "CN10Y", "中债10年": "CN10Y", "10Y国债": "CN10Y",
    "股市": "Equity", "股票": "Equity", "权益": "Equity", "股指": "Equity",
    "大宗商品": "Commodity", "商品": "Commodity",
    "人民币汇率": "USDCNH", "美元兑人民币": "USDCNH", "人民币": "USDCNH",
    "美国CPI同比": "US_CPI_YOY", "美CPI": "US_CPI_YOY",
    "美国10年": "US_10Y", "美十债": "US_10Y", "美债10年": "US_10Y",
    "纳指": "G_NASDAQ", "纳斯达克": "G_NASDAQ", "费城半导体": "G_SOX", "SOX": "G_SOX",
    # ---- 叙事同义（中文/口语 → 规范 snake_case）----
    "需求弱": "demand_weak", "需求偏弱": "demand_weak", "需求不足": "demand_weak",
    "需求疲软": "demand_weak", "终端需求弱": "demand_weak",
    "宽松预期": "policy_easing_expectation", "货币宽松预期": "policy_easing_expectation",
    "宽松": "policy_easing_expectation", "流动性宽松": "policy_liquidity_easing",
    "通胀担忧": "inflation_concern", "通胀": "inflation_concern", "物价压力": "inflation_concern",
    "成本推动": "cost_push", "成本上行": "cost_push", "上游涨价": "cost_push",
    "信用扩张": "credit_expansion", "信用派发": "credit_expansion", "宽信用": "credit_expansion",
    "财政扩张": "fiscal_expansion", "宽财政": "fiscal_expansion",
    "财政压力": "fiscal_stress", "财政张力": "fiscal_stress",
    "财政供给压力": "fiscal_supply_pressure", "供给压力": "fiscal_supply_pressure",
    # ---- 信用维度同义（2026-10-19 用户指令）----
    "社融": "TSF", "新增社融": "TSF", "社会信用": "TSF", "信用脉冲": "TSF",
    "剪刀差": "M1M2_GAP", "M1M2": "M1M2_GAP", "M1-M2": "M1M2_GAP", "M1减M2": "M1M2_GAP",
    # ---- 财政维度同义（2026-10-19 用户指令）----
    "财政立场": "FISCAL_STANCE", "财政状态": "FISCAL_STANCE", "赤字率立场": "FISCAL_STANCE",
    "专项债": "SPECIAL_BOND", "专项债券": "SPECIAL_BOND", "地方政府专项债": "SPECIAL_BOND",
    "外部利率压力": "external_rate_pressure", "海外利率压力": "external_rate_pressure",
    "供应链风险": "supply_chain_risk", "供应链中断": "supply_chain_risk",
    "信用收缩": "credit_contraction", "信用走弱": "credit_contraction", "信用脉冲走弱": "credit_contraction",
    "财政收缩": "fiscal_contraction", "财政收紧": "fiscal_contraction",
}

# 开关
REJECT_BOUNDED = True     # 有界域（direction.target / transmission.from,to）范围外拒收
REJECT_NARRATIVE = False  # 叙事域开放，不拒收（保留测试占位 X/W/A/B/C 等）


def _lookup_alias(raw: str):
    """别名查表：精确 + 去空格 + ASCII 小写兜底。返回规范 id 或 None。"""
    if raw in ALIASES:
        return ALIASES[raw]
    s = raw.strip()
    if s != raw and s in ALIASES:
        return ALIASES[s]
    low = raw.lower()
    if low != raw and low in ALIASES:
        return ALIASES[low]
    return None


def normalize_id(raw, kind, reject=None):
    """把候选里的原始 id 文本归一到规范 id。

    kind: "direction" | "narrative" | "transmission_node"
    reject: 覆盖默认开关（True=范围外拒收返回 None；False=范围外透传）。
    返回：规范 id 字符串；若拒收则返回 None。
    """
    if raw is None:
        return None
    r = str(raw).strip()
    if r == "":
        return None
    # 1) 别名归一（核心：同义写法 → 同一规范 id → 收敛）
    alias = _lookup_alias(r)
    if alias is not None:
        return alias
    # 2) 已是规范 id → 原样
    if kind == "direction" and r in DIRECTION_TARGETS:
        return r
    if kind == "narrative" and r in NARRATIVE_IDS:
        return r
    if kind == "transmission_node" and r in TRANSMISSION_NODES:
        return r
    # 3) 范围外
    if kind == "narrative":
        do_reject = REJECT_NARRATIVE if reject is None else reject
    else:
        do_reject = REJECT_BOUNDED if reject is None else reject
    if do_reject:
        return None
    return r  # 有界域默认也透传（REJECT_BOUNDED=False 时），叙事域恒透传


# 便捷：把候选归一并返回是否合法（用于测试/调试）
def is_canonical(raw, kind):
    return normalize_id(raw, kind) is not None
