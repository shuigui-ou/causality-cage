# -*- coding: utf-8 -*-
"""候选源（已退役为常量库 / 实用枚举工具，2026-10-13 架构纠正）。

历史：本模块曾是世界树的**生产候选生成器**（状态驱动枚举，取代手写 expand_rules.json）。
但「生成候选」是 AI 的活，系统里的确定性代码做生成 = 把剪枝侧的"不生成什么"偷偷挪到生成端。
架构纠正后（AI 穷举 → 系统剪枝）：生产生成槽位改由 `ai_candidate.generate` 承担，而该槽位的
候选由 **agent 自身**在系统外部穷举后注入（本系统不调用任何模型服务，不调用 Ollama）。
本模块仅保留为：
  - **常量库**（NARRATIVE_CHAIN_DAYS / NARRATIVE_POLARITY / DIRECTION_CHAIN_DAYS 等，供
    ai_candidate 与 world_tree 的 _system_candidate_guards 复用）；
  - impact_layer / run_full_loop 的实用枚举工具。
  - 注：本模块**不再作为生产生成器，也不再作为降级源**——ai_candidate 未注入 agent 生成器时
    会显式报错（强制"AI 在系统外面"），不会静默回退到确定性枚举。

注意：本模块内 policy_space 前置与相反解读互斥两处逻辑已被 world_tree._system_candidate_guards
搬至系统侧（它们本就是剪枝，不是生成）——本模块不再承担这些，仅作降级时一并复现。

接口（与 ai_candidate.generate 同签名/同返回格式，便于无缝降级）：
  enumerate_candidates(state, narrative_layer=None) -> [candidate, ...]
    candidate = {"action": "narrative", "narrative": "<id>", "desc": "..."}
              | {"action": "direction", "target": "<id>", "sign": "up"|"down"|"flat"}
  state 形如 world_state.read_world_state 的 state：{指标: {"value":..., "kind":...}, ...}
"""
from __future__ import annotations

# 宏观域派生字段 / 链结构：账本重分配后，policy_space 是 world_state 派生字段 + 动作前置条件，
# 时间窗口 fit 落在候选链结构（chain_days），注意力互斥在候选生成层。详见 macro_resources.py 头部注释。
try:
    import macro_resources
except ImportError:  # 以 tools 包导入时
    from . import macro_resources

# 每条叙事候选的「传导时滞」（天）= 它从发生到市场完成重定价的链长。
# 这是 fit 谓词落在候选结构上的依据：每条候选自带 chain_days，world_tree._grow 展开时判
# chain_days <= 决策窗口（逐链并行不求和，不累加）。慢通道(宽松预期等定价日)=10，其余即时=2。
NARRATIVE_CHAIN_DAYS = {
    "policy_easing_expectation": 10,   # 宽松预期：需等 LPR 定价日（慢通道）
    "fiscal_expansion": 2,
    "credit_expansion": 2,
    "external_rate_pressure": 2,
    "inflation_concern": 2,
    "demand_weak": 2,
    "cost_push": 2,
    "fiscal_stress": 2,
    "fiscal_supply_pressure": 2,
}
DIRECTION_CHAIN_DAYS = 2           # 利率/股市即时重定价（快通道）；flat=0
DIRECTION_CHAIN_DAYS_FLAT = 0

# 叙事的宏观立场（候选生成层互斥用：一条信号不能两解）。
# dovish/hawkish 是「同一信号相反解读」；其余（财政/信用/成本/外部）非相反解读，标 neutral 不参与互斥。
NARRATIVE_POLARITY = {
    "policy_easing_expectation": "dovish",
    "demand_weak": "dovish",
    "inflation_concern": "hawkish",
}

# 市场在此框架下会关注的「方向目标」全集（这些是「市场看什么」的维度，不是「一定怎么走」的结论）
# 与旧 expand_rules 不同：这里只是候选目标的宇宙，具体 sign 由状态推导、由硬剪枝/约束过滤。
DIRECTION_TARGETS = ["CN10Y", "LPR_1Y", "LPR_5Y", "Commodity", "Equity", "USDCNH"]

# 叙事 → 典型方向推导映射（承接真实叙事源 feeds 接入：真实叙事须能驱动方向，而非只当标记）。
# 这是「市场在该叙事下真实会考虑的方向」维度集合，具体 sign 仍过世界规则(硬剪枝/约束)过滤。
# 注意：fiscal_supply_pressure / external_rate_pressure 等由 feeds 真实源激活的叙事也走此映射。
NARRATIVE_DIRECTION_MAP = {
    "fiscal_supply_pressure": [("CN10Y", "up"), ("Equity", "down")],
    "external_rate_pressure": [("CN10Y", "up"), ("USDCNH", "up"), ("Equity", "down")],
    "policy_liquidity_easing": [("LPR_1Y", "down"), ("CN10Y", "down"), ("Equity", "up")],
    "fiscal_stress": [("CN10Y", "up")],
    "inflation_concern": [("CN10Y", "up")],
    "demand_weak": [("LPR_1Y", "down"), ("Equity", "down")],
    "cost_push": [("Commodity", "up")],
    "policy_easing_expectation": [("LPR_1Y", "down"), ("CN10Y", "down")],
    "fiscal_expansion": [("CN10Y", "up"), ("Equity", "up")],
    "credit_expansion": [("CN10Y", "up"), ("Equity", "up")],
}


def _v(state, key):
    """取某指标的 (value, kind)。不存在/非 dict → (None, None)。"""
    m = state.get(key)
    if not isinstance(m, dict):
        return None, None
    return m.get("value"), m.get("kind")


def _valid(val):
    """真实有值（非 None、非 NaN）才参与枚举；unknown 标缺的除外由 kind 判读。"""
    if val is None:
        return False
    if isinstance(val, float) and val != val:  # NaN
        return False
    return True


def enumerate_candidates(state, narrative_layer=None):
    """从节点状态生成候选（未经剪枝）：叙事 + 方向，全部由真值推导。

    narrative_layer：当前节点已叠加的 _narrative 覆盖层，避免自递归重复提出同一叙事。
    """
    narratives, directions = [], []

    cpi, _ = _v(state, "CPI")
    ppi, _ = _v(state, "PPI")
    m2, _ = _v(state, "M2")
    stance, _ = _v(state, "policy_stance")
    usdcnh, _ = _v(state, "USDCNH")

    # ---------- 叙事候选（状态驱动）----------
    if _valid(cpi):
        if cpi <= 1.5:
            narratives.append(("demand_weak", "需求偏弱：低通胀反映终端需求不足"))
            narratives.append(("policy_easing_expectation", "宽松预期：低通胀打开货币政策空间"))
        if cpi >= 2.5:
            narratives.append(("inflation_concern", "通胀担忧：物价压力抬头"))
    if _valid(ppi) and ppi >= 3.0:
        narratives.append(("cost_push", "成本推动：上游价格上行向终端传导"))
    # M2 盲点修复：旧手册无 M2 段，这里凡是真读到 M2 高就产出信用/财政叙事
    if _valid(m2) and m2 >= 11.0:
        narratives.append(("credit_expansion", "信用扩张：M2 同比高增，货币派生活跃"))
        narratives.append(("fiscal_expansion", "财政扩张：M2 高增反映宽货币+宽信用下的财政/信贷扩张脉冲"))
        narratives.append(("fiscal_stress", "财政/供给压力：宽货币+宽信用下的供给与财政张力"))

    # 已激活叙事集合（支持 list / str / set；用于防自递归 + 叙事→方向推导）
    active = set()
    if isinstance(narrative_layer, (list, set, tuple)):
        active.update(narrative_layer)
    elif narrative_layer:
        active.add(narrative_layer)
    if active:
        narratives = [n for n in narratives if n[0] not in active]

    # ---- 重分配①：policy_space 前置条件（world_state 派生字段，非账本资源）----
    # 货币宽松叙事需要「当前 LPR 离净息差下限还有空间」。无空间则不生成该候选（动作前置条件）。
    ps = macro_resources.policy_space_of(state)
    narratives = [
        (nid, ndesc) for (nid, ndesc) in narratives
        if not (nid == "policy_easing_expectation" and ps <= 1e-9)
    ]

    # ---- 重分配②：注意力互斥守卫（候选生成层；一条信号不能两解）----
    narratives = _dedupe_opposite_readings(narratives, state)

    # ---------- 方向候选（状态驱动）----------
    easing = (stance == "偏松") or (_valid(cpi) and cpi <= 1.5)
    tightening_pressure = (_valid(cpi) and cpi >= 2.5) or (_valid(ppi) and ppi >= 3.0)
    credit_hot = _valid(m2) and m2 >= 11.0

    if easing:
        if ps > 1e-9:
            directions += [("LPR_1Y", "down"), ("LPR_5Y", "down"),
                           ("CN10Y", "down"), ("Equity", "up")]
        else:
            # 无货币空间（LPR 贴净息差下限）：不再生成 1Y 降息方向，仅保留其它方向
            directions += [("LPR_5Y", "down"), ("CN10Y", "down"), ("Equity", "up")]
    if tightening_pressure:
        directions += [("CN10Y", "up"), ("Commodity", "up")]
    if credit_hot:
        directions += [("CN10Y", "up"), ("Equity", "up")]
    if _valid(usdcnh):
        directions += [("USDCNH", "up"), ("USDCNH", "down"), ("USDCNH", "flat")]
    # 已激活叙事 → 典型方向推导（真实源叙事或状态推导叙事驱动的方向候选）
    for nid in active:
        for tgt, sgn in NARRATIVE_DIRECTION_MAP.get(nid, []):
            directions.append((tgt, sgn))
    # 兜底：无任何条件触发时给中性候选，避免递归空死（由硬剪枝/约束后续判定）
    if not directions:
        directions += [("CN10Y", "flat"), ("Equity", "flat")]

    # ---------- 去重组装（每条候选附 chain_days / polarity，供 fit 谓词与互斥层使用）----------
    cands = []
    seen_n = set()
    for nid, ndesc in narratives:
        if nid in seen_n:
            continue
        seen_n.add(nid)
        cands.append({
            "action": "narrative", "narrative": nid, "desc": ndesc,
            "chain_days": NARRATIVE_CHAIN_DAYS.get(nid, 0),
            "polarity": NARRATIVE_POLARITY.get(nid),
        })
    seen_d = set()
    for tgt, sgn in directions:
        if (tgt, sgn) in seen_d:
            continue
        seen_d.add((tgt, sgn))
        cd = DIRECTION_CHAIN_DAYS_FLAT if sgn == "flat" else DIRECTION_CHAIN_DAYS
        cands.append({"action": "direction", "target": tgt, "sign": sgn, "chain_days": cd})
    return cands


def _dedupe_opposite_readings(narratives, state):
    """同一节点的候选里，dovish 与 hawkish 相反解读互斥（一条新闻不能两解）。

    正常情况下由状态驱动已互斥（低CPI→dovish / 高CPI→hawkish 不会同现）；
    本守卫是**防御性落点**：若同现，按 state 实际支持度保留其一
    （低CPI留 dovish、高CPI留 hawkish，否则去 hawkish）。互斥逻辑归此层，不再进账本。
    """
    pols = {nid: NARRATIVE_POLARITY.get(nid) for nid, _ in narratives}
    has_dovish = any(p == "dovish" for p in pols.values())
    has_hawkish = any(p == "hawkish" for p in pols.values())
    if not (has_dovish and has_hawkish):
        return narratives
    cpi, _ = _v(state, "CPI")
    if cpi is not None and cpi >= 2.5:
        keep, drop = "hawkish", "dovish"
    else:  # 低CPI或未知 → 保留 dovish
        keep, drop = "dovish", "hawkish"
    return [(nid, d) for (nid, d) in narratives if pols[nid] != drop]


def enumerate_direction_targets():
    """返回方向目标全集（供 run_full_loop._hard_prune_probe 之类遍历用，取代旧 expand_rules）。"""
    return list(DIRECTION_TARGETS)
