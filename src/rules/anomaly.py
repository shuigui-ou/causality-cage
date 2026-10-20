# -*- coding: utf-8 -*-
"""阶段 1 异常判定 —— 稳健标准化偏差（§2.2 `[H10 已裁定]`）。

为什么不用「当前值 > 前 N 期最大值」：该判据只使用「是不是新高」的秩序信息，
完全丢弃标度信息，其纯噪声误报率恒为 1/(N+1)，**不随来源波动率变化**，
因此无法通过加一个 margin 校准到指定的误报率（`ε = f(cv)`，不是常数）。

为什么「中位数 × k」只能当语义标注：其误报率为 `P(Z > (k-1)/cv)`，
只有 `cv` 很大时才可能触发（`k=3` 需 `cv ≳ 0.86`），见 §14.6 `[H14]`。
"""
from __future__ import annotations

from statistics import NormalDist, median

from .thresholds import T

OK = "ok"
INSUFFICIENT = "insufficient_data"


def z_critical() -> float:
    """单侧临界值。用 stdlib 求逆，零新依赖（§2.2 实现列）。"""
    return NormalDist().inv_cdf(1 - T.ANOMALY_Z_ALPHA)


def robust_scale(window) -> float:
    """MAD 换算为 sigma 的一致估计（正态下 MAD = 0.6745*sigma）。"""
    med = median(window)
    mad = median([abs(v - med) for v in window])
    return T.ANOMALY_MAD_K * mad


def robust_z(x, window):
    """稳健标准化偏差。窗口不合格或尺度退化为 0 时返回 None。"""
    if len(window) < T.ANOMALY_MEDIAN_WINDOW:
        return None
    scale = robust_scale(window)
    if scale == 0:
        # 退化守卫：窗口内过半取值相同 → 该窗口无判别力。
        # **禁止回退到「> 前 N 期最大值」** —— 那正是被删除的分支（§2.2）。
        return None
    return (x - median(window)) / scale


def judge(x, window) -> dict:
    """返回判定结果 + 该结论必须携带的 assumed_params（§14.5 第二个示例）。"""
    z = robust_z(x, window)
    if z is None:
        return {
            "judgement": None,
            "status": INSUFFICIENT,
            "robust_z": None,
            "z_critical": z_critical(),
            "rule_applied": "§2.2 数值增量型 / 阶段 1",
            "derived_params_used": T.derived_params("§2.2.anomaly.numeric_incremental.stage1"),
            "assumed_params": T.assumed_params("§2.2.anomaly.numeric_incremental.stage1"),
            "note": "窗口不足或 MAD=0 → 不判定（禁止回退到 max 分支）",
        }
    crit = z_critical()
    return {
        "judgement": z > crit,
        "status": OK,
        "robust_z": z,
        "z_critical": crit,
        "rule_applied": "§2.2 数值增量型 / 阶段 1",
        "derived_params_used": T.derived_params("§2.2.anomaly.numeric_incremental.stage1"),
        "assumed_params": T.assumed_params("§2.2.anomaly.numeric_incremental.stage1"),
        "note": "误报率为单次检验口径（α=%s），年均约 365×α 次/来源" % T.ANOMALY_Z_ALPHA,
    }


def multiple_label(x, window) -> dict:
    """「中位数 × k」只作**语义标注**用，不参与判定（§14.6 [H14]）。"""
    med = median(window) if window else None
    ratio = (x / med) if med else None
    return {
        "median": med,
        "ratio_to_median": ratio,
        "semantic_hit": (ratio is not None and ratio > T.ANOMALY_MEDIAN_MULT),
        "role": "semantic_annotation_only",
    }
