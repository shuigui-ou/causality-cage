# -*- coding: utf-8 -*-
"""约束层（constraint layer，2026-10-12 新增）—— 当下真实记录的「路况」筛选。

与硬剪枝(hard_prune.py)的边界（用户 2026-10-12 定性纠正）：
  - 硬剪枝：剪「逻辑 / 物理 / 时间不可能」—— 普适规则，无论何时何地成立
            （如低通胀+偏松下推 CN10Y up=荒谬；CPI 发布不会让公司在 3 天内退市）。
  - 约束层：剪「逻辑上可能、但当下不成立」—— 情境规则，依赖某条具体真实记录
            （央行表态 / 政策路径指引 / 在途事件）。历史上有过、别的环境下成立，但今天这条路封了。
  - 二者都是候选筛选器（都不参与推演本身，只筛候选），但**来源与语义不同**：
      硬剪枝来源 = 领域常识 / 物理 / 逻辑（外置、可审计、普适）
      约束来源   = 当下真实记录（须来自数据源；无真实源不得伪装成约束）

关键纪律（防「脑内规则污染」真实筛选）：
  - 每条约束必须带 provenance：source 指向一条可核查的真实记录。
  - 无真实源的约束一律标 source="pending_feed"，本模块默认 allow_pending=False → 仅记录、不 veto。
    即：系统没有约束源时，约束层**诚实放行**（暴露「无路况数据」这个缺口），绝不拿 agent 推断当路况。
  - 状态(CPI=0.8% 等读数)由 world_state 提供；约束由独立约束源提供；约束绝不改写状态。

数据流：
  load_constraints(path)
  filter_candidates(candidates, constraints, world_state, allow_pending=False)
      -> (kept, vetoed)   # vetoed 每项带 constraint_id + rationale + source
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(BASE, "config")
DEFAULT_CONSTRAINTS = os.path.join(CONFIG, "constraints_cpi_ppi.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hard_prune import _eval_pred, is_pruned  # noqa: E402


def load_constraints(path=None):
    path = path or DEFAULT_CONSTRAINTS
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _veto_match(constraint, candidate):
    """候选是否落在某条约束的否决清单内。"""
    v = constraint.get("vetoes", {})
    action = candidate.get("action", "direction")
    if action == "narrative":
        narrs = v.get("narratives", [])
        return candidate.get("narrative") in narrs
    # direction
    for d in v.get("directions", []):
        if d.get("target") != candidate.get("target"):
            continue
        sign = d.get("sign")
        if sign is None or str(sign).lower() == str(candidate.get("sign", "")).lower():
            return True
    return False


def filter_candidates(candidates, constraints, world_state, allow_pending=False):
    """对候选集施加当下约束筛选。

    返回 (kept, vetoed, pending_noted)：
      kept         = 未触发任何有效约束的候选
      vetoed       = 被某条（已激活的）真实/显式约束否决的候选，带 constraint_id/rationale/source
      pending_noted= 仅因 pending_feed 且 allow_pending=False 而未生效的约束（诚实记录缺口，不否决）

    纪律：pending_feed 约束在 allow_pending=False 时**不否决**，只进 pending_noted，
          确保「没有真实约束源」时系统暴露缺口而非用脑内规则填空。
    """
    kept, vetoed, pending_noted = [], [], []
    # 先收集「未生效的 pending_feed 约束」（与候选无关，去重记录一次，暴露源缺口）
    for cons in constraints.get("constraints", []):
        if cons.get("source") == "pending_feed" and not allow_pending:
            pending_noted.append(cons["id"])
    pending_set = set(pending_noted)
    for cand in candidates:
        active_vetoes = []
        for cons in constraints.get("constraints", []):
            if cons["id"] in pending_set:
                continue
            when = cons.get("applies_when")
            if when is not None and not _eval_pred(world_state, when):
                continue
            if _veto_match(cons, cand):
                active_vetoes.append(cons)
        if active_vetoes:
            vetoed.append({
                "candidate": cand,
                "vetoed_by": [
                    {"constraint_id": c["id"], "type": c.get("type"),
                     "statement": c.get("statement"), "rationale": c.get("rationale"),
                     "source": c.get("source")}
                    for c in active_vetoes
                ],
            })
        else:
            kept.append(cand)
    return kept, vetoed, pending_noted


def enumerate_demo_candidates():
    """演示用候选源（已与旧 expand_rules.json 解耦）。

    真实管线改用 candidate_source.enumerate_candidates(state)（状态驱动枚举）。
    此处仅用于约束层独立演示：构造一组 CPI/政策相关的叙事+方向候选。
    """
    return [
        {"action": "narrative", "narrative": "demand_weak", "desc": "需求偏弱"},
        {"action": "narrative", "narrative": "inflation_concern", "desc": "通胀担忧"},
        {"action": "direction", "target": "CN10Y", "sign": "up"},
        {"action": "direction", "target": "CN10Y", "sign": "down"},
        {"action": "direction", "target": "LPR_1Y", "sign": "down"},
        {"action": "direction", "target": "Equity", "sign": "up"},
    ]


def main():
    ap = argparse.ArgumentParser(
        description="约束层：给定世界状态+候选，按当下真实约束(路况)筛选；无真实源时诚实放行。")
    ap.add_argument("--state", required=True, help="WorldState JSON（含 state 字段）")
    ap.add_argument("--constraints", default=DEFAULT_CONSTRAINTS)
    ap.add_argument("--allow-pending", action="store_true",
                    help="允许 pending_feed 约束生效（仅用于演示树形，不可用于真实筛选）")
    a = ap.parse_args()

    with open(a.state, encoding="utf-8") as f:
        ws = json.load(f)
    state = ws.get("state", ws)
    cons = load_constraints(a.constraints)
    cands = enumerate_demo_candidates()

    # 第 1 层：硬剪枝（普适逻辑不可能）
    after_hard = []
    hard_veto = []
    for c in cands:
        pruned, matched = is_pruned(state, c)
        if pruned:
            hard_veto.append((c, [r["id"] for r in matched]))
        else:
            after_hard.append(c)

    # 第 2 层：约束层（当下路况）
    kept, vetoed, pending = filter_candidates(
        after_hard, cons, state, allow_pending=a.allow_pending)

    print("=== 约束层演示：history-candidates ∩ 当下约束 ===")
    print("候选总数(演示样例):", len(cands))
    print("硬剪枝拦下:", len(hard_veto))
    for c, rs in hard_veto:
        lab = c.get("narrative") if c.get("action") == "narrative" else "%s %s" % (c.get("target"), c.get("sign"))
        print("   HARD  ", lab, "<-", ",".join(rs))
    print("硬剪枝后剩余:", len(after_hard))
    if pending:
        print("pending_feed 约束(未生效, 暴露源缺口):", pending)
    print("约束层否决:", len(vetoed))
    for v in vetoed:
        c = v["candidate"]
        lab = c.get("narrative") if c.get("action") == "narrative" else "%s %s" % (c.get("target"), c.get("sign"))
        by = ",".join(b["constraint_id"] for b in v["vetoed_by"])
        print("   VETO  ", lab, "<-", by)
    print("最终可走(walkable):", len(kept))
    for c in kept:
        lab = c.get("narrative") if c.get("action") == "narrative" else "%s %s" % (c.get("target"), c.get("sign"))
        print("   walk  ", lab)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
