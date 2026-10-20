# -*- coding: utf-8 -*-
"""宏观域状态派生字段 + 账本资源（2026-10-12 深夜重分配后）。

用户裁定（推翻「资源=被消耗量」单一模型，进而推翻「引擎四类型」过渡态）：
  账本引擎只该有**一种类型**——path_consumption（共享池求和）。其余三类约束**根本不属于账本**，
  各自回到正确的层（详见 system_definition.md「账本只管 path_consumption」段）：

    - policy_space（原 state_derivation）→ world_state 派生字段 + 动作前置条件
        （policy_space_of 见下；candidate_source 生成宽松候选前查它；
         world_tree._apply_move 让宽松叙事下调 LPR，使 policy_space 沿路径真实收缩）
    - attention（原 mutual_exclusion）→ 候选生成层互斥
        （candidate_source 按 polarity 守卫：一条信号不能两解）
    - decisive_window / fit（原 fit）→ 候选链结构
        （每条候选自带 chain_days；world_tree._grow 展开时判 chain_days<=窗口，逐链并行不求和）

本模块拆两半：
  ① 账本资源 build_macro_resources()：只返回 path_consumption 类（run_time_budget_100m）；
     宏观三资源已迁出，不再以「账本资源」身份出现。
  ② 状态派生 helper：policy_space_of / decisive_window_days / attention_bandwidth /
     active_theme_count —— 供 world_state / candidate_source / world_tree 的「正确层」调用。
"""
from __future__ import annotations

import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(BASE, "reports", "history_states_cpi_ppi.json")

# ===========================================================================
# 真实锚点（全部带来源；系统读真实世界，不手工编数字）
# ===========================================================================

# ANCHOR: 政策利率真实下限 ≈ 2.0%（银行净息差约束；2024 年商业银行 NIM 约 1.5%，
#   再降挤压盈利 → PBoC 实操下限约 2.0%）。
LPR_FLOOR = 2.00
# 基线兜底：state 无 LPR 读数时用真实当前 3.0%（诚实标注，非编造）
LPR_FALLBACK = 3.00

# ANCHOR/结构常数：中国 CPI 约每月 9-10 日发布；LPR 每月 20 日定价 → 真实决策窗口 ≈ 10 天。
# 日历刚性，不随世界状态变；未来若 world_state 带真实发布/定价日期，可改此处派生。
DECISIVE_WINDOW_DAYS = 10
WIN_LPR_CHANNEL = 10    # 降息/宽松预期链需等定价日（慢通道）
WIN_FAST_CHANNEL = 2    # 利率/股市/财政消息即时重定价（快通道）

# attention 派生口径（HEURISTIC，info only；真正的互斥逻辑已在 candidate_source）
ATTN_INTRINSIC = 1.0
ATTN_HEADROOM_QUIET = 0.5
ATTN_DECAY_PER_THEME = 0.1
ATTN_FLOOR = 1.0
ATTN_CEIL = 1.5


# ===========================================================================
# 状态派生（派生字段，不是被消耗的资源）
# ===========================================================================
def _lpr_value(state):
    if not state:
        return None
    return (state.get("LPR_1Y") or {}).get("value")


def policy_space_of(state):
    """policy_space = (LPR1Y − 净息差下限)，100bp=1.0，夹 >=0。

    这是 **world_state 派生字段**，不是被消耗的资源。宽松动作不「扣减」它，
    而是经 world_tree._apply_move 改变 LPR，进而改变它。
    返回当前 state 下的余量（归一化：100bp 余量 = 1.0）。
    """
    lpr = _lpr_value(state)
    if lpr is None:
        lpr = LPR_FALLBACK
    room_bp = max(0.0, (lpr - LPR_FLOOR) * 100.0)
    return room_bp / 100.0


def decisive_window_days(state=None):
    """决策窗口（天）= 结构常数 10（CPI 发布 → LPR 定价日）。

    属物理/制度常数，不随世界状态变；state 参数保留为未来按真实日期派生的接口。
    """
    return DECISIVE_WINDOW_DAYS


def attention_bandwidth(state):
    """注意力带宽（info only，HEURISTIC 派生；互斥判定已搬至 candidate_source）。

    0.5 安静余量 + 0.1/主题衰减为判断值，方向(信息越密→带宽越窄)可靠，斜率属校准。
    """
    n = active_theme_count(state)
    cap = ATTN_INTRINSIC + ATTN_HEADROOM_QUIET - ATTN_DECAY_PER_THEME * max(0, n - 1)
    return min(ATTN_CEIL, max(ATTN_FLOOR, cap))


def active_theme_count(state):
    """当前真实世界里「同时活跃的宏观主题数」= 信息流密度代理（从 world_state 真值算）。

    判定全部来自真实读数，非脑内：
      CPI tail(≤1.5 或 ≥2.5)、PPI tail(≤−1.5 或 ≥1.0) → 通胀/通缩主题在台面
      M2 ≥ 11 → 信用/流动性脉冲主题
      policy_stance ∈ {偏松,偏紧} → 政策非中性主题
      CN10Y 有读数 → 利率主题在台面（unknown 不计入）
    """
    if not state:
        return 0
    n = 0
    cpi = (state.get("CPI") or {}).get("value")
    ppi = (state.get("PPI") or {}).get("value")
    m2 = (state.get("M2") or {}).get("value")
    stance = (state.get("policy_stance") or {}).get("value")
    cn10y = (state.get("CN10Y") or {}).get("value")
    if cpi is not None and (cpi <= 1.5 or cpi >= 2.5):
        n += 1
    if ppi is not None and (ppi <= -1.5 or ppi >= 1.0):
        n += 1
    if m2 is not None and m2 >= 11.0:
        n += 1
    if stance in ("偏松", "偏紧"):
        n += 1
    if cn10y is not None:
        n += 1
    return n


# ===========================================================================
# 对外接口
# ===========================================================================
def build_macro_resources(state=None, history=None):
    """账本吃的 resources 字典——重分配后**只含 path_consumption 类**（宏观三资源已迁出）。

    state/history 保留签名兼容 world_tree，但宏观派生字段不再以账本资源身份出现。
    """
    return {
        "run_time_budget_100m": {"type": "path_consumption", "unit": "秒", "total": 13},
    }


def resource_summary(state=None):
    """人类可读摘要（用于报告/CLI 自检）。"""
    return {
        "policy_space": {
            "cap": round(policy_space_of(state), 4),
            "floor": LPR_FLOOR,
            "note": "world_state 派生字段 + 动作前置条件（已迁出账本，非消耗资源）",
        },
        "attention_bandwidth": {
            "cap": round(attention_bandwidth(state), 4),
            "note": "HEURISTIC 派生，info only；互斥判定在 candidate_source",
        },
        "decisive_window_days": {
            "cap": DECISIVE_WINDOW_DAYS,
            "note": "结构常数；fit 谓词在 world_tree._grow（chain_days<=窗口）",
        },
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(resource_summary())
