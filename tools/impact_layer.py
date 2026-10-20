# -*- coding: utf-8 -*-
"""影响度层（impact_layer，2026-10-12 接入；2026-10-13 挂 focus 重定性）。

设计纪律（承接用户「粒度 = 对最终结果的影响度」纠正 #210 + 2026-10-13 影响度挂关注点裁定）：
  - 剪枝的判据是「逻辑/物理不可能」（硬剪枝）；影响度的判据是「对最终结果有没有影响、影响多大」。
    二者**正交**：一个方向即使逻辑可能，若对最终结果影响≈0，也不该因为它「荒谬」被剪。两个不同语义层。
  - **影响度是程度，不是停（2026-10-13 用户裁定）**：low/none 只打 `impact_priority` 标记、继续展开，
    输出时不优先展开；唯一因影响度而停的情形 = `level=="zero"` **且**调用方给了 focus——
    「对某关注点影响为零」才算（外部锚定）。同一个候选对股市为零、对债市非零，停不停取决于问的是哪个。
  - 影响度来源（诚实、可审计）：
      ① 状态驱动：指标读数越偏离常态，由其驱动的方向/叙事影响度越高（极值 = 高影响）。
      ② 真实事件命中：feeds 事件日历若在途某指标相关事件，则该方向影响升级。
    （历史经验传导频次可作为③后续接入；本轮先做①+②，避免无来源的影响度。）
  - 输出 level ∈ {high, mid, low, none, zero} + source + reason（可审计，不黑箱）：
      · zero 只在给定 focus 时出现 = 与关注点无共同驱动指标 → 对该关注点影响为零；
      · none = 有驱动但读数全常态（无 focus 时的旧语义之一）。
  - world_tree 接入：zero+focus → 停止递归展开（stop_reason=irrelevant_to_focus，节点保留，停≠砍）；
    low/none → 打 impact_priority 标记继续展开。

接口：
  assess_impact(candidate, state, events=None, focus=None) -> {"level", "source", "reason"}
    focus：关注点，方向目标名（CN10Y/Equity/LPR_1Y/...）或中文别名（债市/股市/汇率/...）。
           focus=None 或认不出 → 无法判定相对影响度 → 退回全局行为（旧语义），**不停**。
"""
from __future__ import annotations

# 关注点别名（用户口头关注点 → 方向目标名）。认不出的 focus 视为无法判定（同 None，不停）。
FOCUS_ALIASES = {
    "股市": "Equity", "A股": "Equity", "股指": "Equity", "股票": "Equity", "equity": "Equity",
    "债市": "CN10Y", "利率": "CN10Y", "国债": "CN10Y", "收益率": "CN10Y", "cn10y": "CN10Y",
    "汇率": "USDCNH", "人民币": "USDCNH", "usdcnh": "USDCNH",
    "商品": "Commodity", "大宗": "Commodity", "commodity": "Commodity",
    "lpr": "LPR_1Y", "lpr_1y": "LPR_1Y", "lpr_5y": "LPR_5Y",
}


def _normalize_focus(focus):
    """focus → 方向目标名；None/空/认不出 → None（无法判定相对影响度，退回全局行为）。"""
    if not focus or not isinstance(focus, str):
        return None
    f = focus.strip()
    if f in DIRECTION_DRIVERS:
        return f
    return FOCUS_ALIASES.get(f) or FOCUS_ALIASES.get(f.lower())

# 每个方向目标由哪些世界指标驱动（决定「谁的影响度由谁读数决定」）
DIRECTION_DRIVERS = {
    "CN10Y": ["CPI", "PPI", "M2", "policy_stance", "USDCNH"],
    "LPR_1Y": ["CPI", "policy_stance"],
    "LPR_5Y": ["CPI", "policy_stance"],
    "Equity": ["CPI", "M2", "policy_stance"],
    "Commodity": ["PPI"],
    "USDCNH": ["USDCNH"],
}

# 叙事 → 关联指标（用于叙事影响度：指标越极端，该叙事越「真有影响」）
NARRATIVE_DRIVERS = {
    "demand_weak": ["CPI"],
    "inflation_concern": ["CPI"],
    "cost_push": ["PPI"],
    "policy_easing_expectation": ["policy_stance", "CPI"],
    "credit_expansion": ["M2"],
    "fiscal_stress": ["M2"],
    "fiscal_supply_pressure": ["M2"],
    "policy_liquidity_easing": ["policy_stance"],
    "external_rate_pressure": [],  # 外部压力全局高影响，不由单一读数决定
}


def _extremity(state, key):
    """返回某指标相对常态的极端度（0~1）。0=常态，1=极端。"""
    m = state.get(key)
    if not isinstance(m, dict):
        return 0.0
    v = m.get("value")
    if v is None or (isinstance(v, float) and v != v):
        return 0.0
    if key == "CPI":
        # 常态约 1.5~2.5；偏离越远越极端
        return min(1.0, abs(v - 2.0) / 2.5)
    if key == "PPI":
        return min(1.0, abs(v) / 6.0)
    if key == "M2":
        return min(1.0, abs(v - 8.0) / 6.0)
    if key == "policy_stance":
        s = m.get("value")
        return 1.0 if s in ("偏松", "偏紧") else 0.3  # 中性=弱信号
    if key == "USDCNH":
        return 0.5  # 有读数即中等影响（汇率影响面宽）
    return 0.0


def _level_from_extremity(e):
    if e >= 0.6:
        return "high"
    if e >= 0.3:
        return "mid"
    if e > 0.0:
        return "low"
    return "none"


def assess_impact(candidate, state, events=None, focus=None):
    """评估一条候选的影响度。events = feeds 事件日历（可选）；focus = 关注点（可选）。

    focus 语义（2026-10-13 用户裁定「影响度挂在关注点上」）：
      - focus=None（或认不出）→ 全局影响度（旧行为），level ∈ {high, mid, low, none}；
      - focus=方向目标名/中文别名 → 影响度**相对该关注点**计算：
          · 与关注点无共同驱动指标的候选 → level="zero"（对该关注点影响为零，由 world_tree 判停）；
          · 有共同驱动 → 用**共同驱动**的极端度计算（影响经共享读数传导到关注点）。
    """
    focus_t = _normalize_focus(focus)
    if candidate.get("action") == "narrative":
        nid = candidate.get("narrative")
        drivers = NARRATIVE_DRIVERS.get(nid, [])
        if nid == "external_rate_pressure":
            return {"level": "high", "source": "state-driven",
                    "reason": "外部利率压力为全球传导，固有高影响（不随关注点判零）"}
        if not drivers:
            return {"level": "mid", "source": "state-driven",
                    "reason": "叙事无明确驱动指标，默认中等影响"}
        if focus_t is not None:
            focus_drivers = DIRECTION_DRIVERS.get(focus_t, [])
            drivers = [d for d in drivers if d in focus_drivers]
            if not drivers:
                return {"level": "zero", "source": "focus-relative",
                        "reason": "叙事 %s 与关注点 %s 无共同驱动指标 → 对该关注点影响为零" % (nid, focus_t)}
        ext = max(_extremity(state, d) for d in drivers)
        lvl = _level_from_extremity(ext)
        # 真实事件命中升级
        if events:
            hit = any(_narrative_event_match(nid, e) for e in events)
            if hit and lvl in ("low", "none"):
                lvl = "mid"
        reason = "驱动指标 %s 极端度 %.2f → %s" % (drivers, ext, lvl)
        if focus_t is not None:
            reason += "（相对关注点 %s）" % focus_t
        return {"level": lvl, "source": "state-driven" if focus_t is None else "focus-relative",
                "reason": reason}

    # transmission（市场变量间传导候选）：影响度由传导层真实联动判断承担，本层只做中性占位
    if candidate.get("action") == "transmission":
        return {"level": "mid", "source": "transmission-claim",
                "reason": "传导候选(%s→%s)：影响度由传导层真实联动判断承担，本层不编造宏观驱动"
                          % (candidate.get("from"), candidate.get("to"))}

    # direction
    tgt = candidate.get("target")
    drivers = DIRECTION_DRIVERS.get(tgt, [])
    if focus_t is not None and tgt != focus_t:
        focus_drivers = DIRECTION_DRIVERS.get(focus_t, [])
        shared = [d for d in drivers if d in focus_drivers]
        if not shared:
            return {"level": "zero", "source": "focus-relative",
                    "reason": "方向 %s 与关注点 %s 无共同驱动指标 → 对该关注点影响为零" % (tgt, focus_t)}
        drivers = shared
    if not drivers:
        return {"level": "mid", "source": "state-driven",
                "reason": "方向目标 %s 无明确驱动映射，默认中等" % tgt}
    ext = max(_extremity(state, d) for d in drivers)
    lvl = _level_from_extremity(ext)
    # 真实事件命中升级
    if events:
        hit = any(_direction_event_match(tgt, e) for e in events)
        if hit and lvl in ("low", "none"):
            lvl = "mid"
    reason = "驱动指标 %s 极端度 %.2f → %s" % (drivers, ext, lvl)
    if focus_t is not None:
        reason += "（相对关注点 %s）" % focus_t
    return {"level": lvl, "source": "state-driven" if focus_t is None else "focus-relative",
            "reason": reason}


def _narrative_event_match(nid, ev):
    txt = (ev.get("event", "") + ev.get("region", ""))
    table = {
        "inflation_concern": ("CPI", "通胀"),
        "demand_weak": ("零售", "消费", "GDP"),
        "cost_push": ("PPI", "大宗", "原材料"),
        "policy_easing_expectation": ("降准", "降息", "LPR", "MLF"),
        "external_rate_pressure": ("美联储", "美债", "FOMC"),
        "fiscal_supply_pressure": ("国债", "地方债", "发行"),
    }
    for k in table.get(nid, ()):
        if k in txt:
            return True
    return False


def _direction_event_match(tgt, ev):
    txt = ev.get("event", "")
    table = {
        "CN10Y": ("国债", "收益率", "美债", "利率"),
        "LPR_1Y": ("LPR", "降息", "降准", "MLF"),
        "LPR_5Y": ("LPR", "房贷", "降息"),
        "Equity": ("股市", "A股", "股指"),
        "Commodity": ("大宗", "商品", "PPI"),
        "USDCNH": ("汇率", "人民币", "美元"),
    }
    for k in table.get(tgt, ()):
        if k in txt:
            return True
    return False


def main():
    import json
    import argparse
    import sys
    sys.path.insert(0, ".")
    from world_state import read_world_state
    ap = argparse.ArgumentParser(description="影响度评估（状态驱动+事件命中）")
    ap.add_argument("--signal", default="9月CPI PPI居民消费价格数据")
    ap.add_argument("--focus", default=None,
                    help="关注点（方向目标名或中文别名，如 债市/CN10Y/股市）：影响度相对它计算")
    a = ap.parse_args()
    ws = read_world_state(a.signal)
    state = ws["state"]
    import candidate_source as cs
    cands = cs.enumerate_candidates(state)
    print("状态:", {k: v.get("value") for k, v in state.items() if isinstance(v, dict)})
    if a.focus:
        print("关注点:", a.focus, "→", _normalize_focus(a.focus) or "（认不出，按全局评估）")
    for c in cands:
        lab = c.get("narrative") if c.get("action") == "narrative" else "%s %s" % (c.get("target"), c.get("sign"))
        print("  %-28s ->" % lab, assess_impact(c, state, focus=a.focus))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
