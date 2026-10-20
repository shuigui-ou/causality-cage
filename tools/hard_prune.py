# -*- coding: utf-8 -*-
"""硬剪枝评估器（hard_prune，2026-10-12 新增）。

设计纪律（承接用户 #213 分层纠正）：
  - 硬剪枝 = 排除「在世界处于何种状态时，哪些方向在逻辑/常识上不成立」。
  - 它**不需要数据确认**（领域常识/物理约束/时间窗口即可判定），且**外置、可审计、
    先于任何 AI 调用执行**——绝不交给 AI 自由裁量（AI 可能用脑内规则覆盖世界约束）。
  - 与「数据无支撑」严格区分：数据无支撑属于软剪枝（标推测、压概率、不删），
    本模块**不处理**；数据缺失时本模块保守地「不剪」（无法确认状态 → 不断言荒谬）。
  - 判据格式全部写在 config/prune_rules.json，本模块只是机械执行器。

规则分层（2026-10-13 用户裁定补两层内容缺口，非新机制）：
  - 既有层（经济/物理/时间/逻辑）：domain_absurd / logical_contradiction /
    physical_constraint / time_window / narrative_grounding——规则都是**状态条件化**的
    （when 必填才有意义），且遵守保守性：when 因缺数据不成立 → 不剪。
  - 新增层 cognitive_constraint（精神层）：市场参与者作为人的认知硬边界——
    预期是心理量，不越级驱动物价/信用等实体读数（PRUNE-COG-*，按 _narrative 覆盖层条件化）。
  - 新增层 social_constraint（人伦理层）：社会伦理与制度红线（PRUNE-SOC-*）。
    与既有层的一个结构差异：伦理红线是**范畴排除**而非状态判定，可以无 when 条件
    （PRUNE-SOC-003 无条件生效——数据缺失同样不放行，这不是保守性原则的例外）。

输入：
  state     = 节点状态：{指标: {value, kind, ...}, 可选 "_narrative": "<叙事标识>"}
              （中间叙事节点只叠加 _narrative 覆盖层，绝不改写底层原始指标）
  candidate = {action:"direction", target, sign, horizon_days?}   拟输出的方向
            | {action:"narrative", narrative:"<叙事标识>"}         拟引入的中间叙事解读
输出：
  is_pruned(state, candidate, path_narratives=None) -> (pruned: bool, matched_rules: list)
  path_narratives：到当前节点为止沿路径累积的叙事集（来自 _grow 递归）；
                  给上时按「路径上出现过」语义判定叙事层约束，不给则退化到 state["_narrative"] 单值。

关键纪律（承接用户 #215 结构洞纠正）：
  - 硬剪枝是**每个节点**的通用闸门——不仅拦「方向」，也拦「引入荒谬叙事」。
  - 引入中间叙事本身是一道候选（action:"narrative"），须经 forbid_narrative 规则检验；
    通过后才叠加 _narrative 覆盖层，再对其展开方向候选。
  - 这样「先引入荒谬叙事再推方向」的绕行被堵：叙事引入这一步就被 PRUNE-NARR-* 拦下，
    根本到不了方向层。且叙事节点不改写原始指标，原方向剪枝（PRUNE-ABS-001）在每个节点照样生效。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(BASE, "config")
DEFAULT_RULES = os.path.join(CONFIG, "prune_rules.json")

_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}


def load_rules(path=None):
    path = path or DEFAULT_RULES
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _metric(state, metric, path_narratives=None):
    if metric == "_narrative":
        # 叙事层读「路径累积集」而非单节点覆盖值（承接用户 #230 路径累积纠正）：
        # _apply_move 对 _narrative 是**覆盖**而非累积（新叙事会盖掉旧叙事），但 _grow 已
        # 沿递归累积 path_narratives 集合并传给本模块。硬剪枝必须读这个集合，使「路径上出现过的
        # 叙事」都生效——否则早期叙事被后至叙事覆盖后，依赖该早期叙事的 PRUNE-COG-* 漏判、
        # PRUNE-ABS-001 的 unless_narrative 失豁免（原实现只认 state["_narrative"] 单值）。
        # 返回的 kind="narrative_set" 让 _eval_leaf 走「成员判别」而非标量比较。
        return path_narratives, "narrative_set"
    m = state.get(metric)
    if isinstance(m, dict):
        return m.get("value"), m.get("kind")
    # 非 dict 覆盖层（裸值）直接取值
    return m, None


def _eval_leaf(state, leaf, path_narratives=None):
    metric = leaf["metric"]
    op = leaf["op"]
    if op == "exists":
        val, kind = _metric(state, metric, path_narratives)
        if kind == "narrative_set":
            return bool(val)  # 空集合 = 无叙事
        return val is not None
    if op == "unknown":
        val, kind = _metric(state, metric, path_narratives)
        if kind == "narrative_set":
            return not bool(val)
        return val is None or kind in ("unknown",)
    val, kind = _metric(state, metric, path_narratives)
    if val is None:
        # 缺数据 → 无法判定 → 不匹配（保守：不剪）
        return False
    target = leaf["value"]
    if metric == "_narrative":
        # 叙事层谓词语义 = 「该叙事是否出现在路径累积集里」（而非当前节点单值相等）。
        # 与系统既有的 path_marks / path_narratives / ledger_suspect 路径累积口径一致：
        # 只要在路径上出现过，约束就该生效。
        if op in ("==", "in"):
            return target in val if kind == "narrative_set" else val == target
        if op in ("!=", "not_in"):
            return target not in val if kind == "narrative_set" else val != target
        return False  # 其他 op 对叙事层无意义，保守不剪
    if op not in _OPS:
        raise ValueError("未知算子: %s" % op)
    try:
        return _OPS[op](val, target)
    except TypeError:
        return _OPS[op](str(val), str(target))


def _eval_pred(state, pred, path_narratives=None):
    if "all_of" in pred:
        return all(_eval_pred(state, p, path_narratives) for p in pred["all_of"])
    if "any_of" in pred:
        return any(_eval_pred(state, p, path_narratives) for p in pred["any_of"])
    if "not" in pred:
        return not _eval_pred(state, pred["not"], path_narratives)
    return _eval_leaf(state, pred, path_narratives)


def _direction_matches(candidate, fd):
    if candidate.get("target") != fd.get("target"):
        return False
    signs = fd.get("sign")
    if isinstance(signs, str):
        signs = [signs]
    csign = str(candidate.get("sign", "")).lower()
    return csign in [str(s).lower() for s in signs]


def check(state, candidate, rules=None, path_narratives=None):
    """返回匹配（即应被剪除）的规则列表；空表=不剪。

    保守性：数据缺失使 `when` 无法成立时，规则不触发 → 不剪。
    即「数据无支撑」永远不会通过本模块变成「硬剪枝」。

    path_narratives（2026-10-13 #230 路径累积纠正）：调用方可传入「到当前节点为止、
    沿路径累积的叙事集」。硬剪枝据此判定叙事层约束（PRUNE-COG-* 的 when 命中、
    PRUNE-ABS-001 的 unless_narrative 豁免），而非只看 state["_narrative"] 单值——
    否则早期叙事被后至叙事覆盖后会漏判/失豁免。未传时退化到当前节点单值（CLI/根节点）。

    叙事层（承接用户 #215 结构洞纠正）：
      - candidate.action=="narrative" 时，只匹配含 forbid_narrative 的规则；
      - candidate.action=="direction"（默认）时，只匹配含 forbid_direction 的规则；
      - unless_narrative：某有根据的叙事（如 fiscal_stress）可豁免对应方向剪枝，
        用于修正「太宽」风险——罕见但真实的例外得以保留为低概率分支。
    """
    rules = rules or load_rules()
    action = candidate.get("action", "direction")
    # 活跃叙事 = 路径累积集 ∪ 当前节点单值（节点单值恒在路径集内；CLI/根节点无路径集时退化到单值）
    active = set(path_narratives) if path_narratives else set()
    node_narr = state.get("_narrative") if isinstance(state, dict) else None
    if node_narr:
        active.add(node_narr)
    matched = []
    for rule in rules.get("rules", []):
        when = rule.get("when")
        if when is not None and not _eval_pred(state, when, path_narratives=active):
            continue
        # 叙事层剪枝：规则禁止引入某叙事
        fn = rule.get("forbid_narrative")
        if fn is not None:
            if action != "narrative":
                continue
            if candidate.get("narrative") != fn:
                continue
        else:
            # 方向层剪枝：要求 candidate 是方向
            fd = rule.get("forbid_direction")
            if fd is not None:
                if action != "direction":
                    continue
                if not _direction_matches(candidate, fd):
                    continue
        mh = rule.get("forbid_if_horizon_over")
        if mh is not None:
            h = candidate.get("horizon_days")
            if h is None or h <= mh:
                continue
        # 叙事例外（修正「太宽」）：路径上出现过的、有根据的叙事可豁免该方向剪枝
        # （承接用户 #230 路径累积纠正：用 active 集而非 state["_narrative"] 单值，
        #   避免「早期叙事被后至叙事覆盖后失豁免」）
        un = rule.get("unless_narrative")
        if un is not None and un in active:
            continue
        matched.append(rule)
    return matched


def is_pruned(state, candidate, rules=None, path_narratives=None):
    matched = check(state, candidate, rules=rules, path_narratives=path_narratives)
    return (len(matched) > 0, matched)


def main():
    ap = argparse.ArgumentParser(
        description="硬剪枝检查：给定世界状态+候选方向，判断是否应被排除（展开时即拦下）")
    ap.add_argument("--state", required=True, help="WorldState JSON（含 state 字段）")
    ap.add_argument("--target", default=None, help="候选方向作用的指标，如 CN10Y")
    ap.add_argument("--sign", default=None, help="候选方向，如 up/down/flat")
    ap.add_argument("--horizon", type=int, default=None, help="候选方向的时间窗口（天）")
    ap.add_argument("--action", default="direction",
                    help="候选类型：direction（默认）或 narrative")
    ap.add_argument("--narrative", default=None,
                    help="叙事标识（action=narrative 时使用）；或为方向候选设置所在节点的 _narrative 覆盖层")
    ap.add_argument("--path-narratives", default=None,
                    help="逗号分隔的路径累积叙事集（模拟 _grow 传入的 path_narratives，复现「路径上出现过」语义）；"
                         "不传则退化到 --narrative 单值")
    ap.add_argument("--rules", default=DEFAULT_RULES)
    a = ap.parse_args()
    with open(a.state, encoding="utf-8") as f:
        ws = json.load(f)
    state = ws.get("state", ws)
    if a.narrative is not None and a.action == "direction":
        # 让方向候选落在某个已存活的叙事节点上（测试 unless_narrative 例外）
        state = dict(state)
        state["_narrative"] = a.narrative
    if a.action == "narrative":
        cand = {"action": "narrative", "narrative": a.narrative or a.target}
    else:
        cand = {"action": "direction", "target": a.target, "sign": a.sign}
    if a.horizon is not None:
        cand["horizon_days"] = a.horizon
    path_narratives = set(s.strip() for s in a.path_narratives.split(",") if s.strip()) \
        if a.path_narratives else None
    pruned, matched = is_pruned(state, cand, rules=load_rules(a.rules),
                                path_narratives=path_narratives)
    if a.action == "narrative":
        print("候选叙事引入: narrative=%s" % cand.get("narrative"))
    else:
        print("候选方向: target=%s sign=%s" % (a.target, a.sign))
    if pruned:
        print(">>> HARD PRUNED（硬剪枝命中，该候选在逻辑/常识上不成立，展开时即拦下）")
        for r in matched:
            print("  [%s] %s" % (r["id"], r["category"]))
            print("      说明: %s" % r["description"])
            print("      理由: %s" % r["reason"])
    else:
        print(">>> NOT PRUNED（该候选未被任何硬剪枝规则排除；若数据无支撑须标推测而非剪除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
