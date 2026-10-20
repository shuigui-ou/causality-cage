# -*- coding: utf-8 -*-
"""§9.2 计数恒等式校验 —— 不成立则报告作废并告警。

三条恒等式（原文照录）：
    signals_new      = signals_scored + signals_baseline
    signals_scored   = chains_building + signals_observing + signals_rejected
    chains_verifying = chains_verified + chains_rejected + chains_pending

口径（§9.1 已裁定，2026-09-24）：`signals_new` 指**本周期新采集入队的信号总数**，
**不是** `status == "new"` 的信号数。二者同名不同义，是 §14.7「历史」那类歧义的复发。
"""
from __future__ import annotations

# (左式字段, 右式字段元组)
IDENTITIES = (
    ("signals_new", ("signals_scored", "signals_baseline")),
    ("signals_scored", ("chains_building", "signals_observing", "signals_rejected")),
    ("chains_verifying", ("chains_verified", "chains_rejected", "chains_pending")),
)


class IdentityViolation(Exception):
    pass


def check(report: dict) -> list:
    """返回校验明细；不抛异常，便于把明细写进报告。"""
    rows = []
    for lhs, rhs in IDENTITIES:
        lv = report.get(lhs, 0)
        rv = sum(report.get(k, 0) for k in rhs)
        rows.append({
            "identity": "%s = %s" % (lhs, " + ".join(rhs)),
            "lhs": lv,
            "rhs": rv,
            "ok": lv == rv,
        })
    return rows


def assert_identities(report: dict) -> None:
    """发布前强制校验（§9.2 规则：每次输出前必须校验，不成立则报告作废并告警）。"""
    bad = [r for r in check(report) if not r["ok"]]
    if bad:
        raise IdentityViolation(
            "§9.2 恒等式不成立，报告作废："
            + "; ".join("%s（左=%s 右=%s）" % (b["identity"], b["lhs"], b["rhs"]) for b in bad)
        )
