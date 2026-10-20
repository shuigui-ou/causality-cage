"""rule_ledger.py — 让规则互相看得见：跨规则资源账本 + 前提互斥检查。

机制（外置、可审计、无 AI 裁量）：
- 每条规则显式声明 consumes{resource,amount} / requires[前提] / negates[前提]。
- **账本只认一种资源类型——path_consumption（共享池求和）**：同一资源上各规则
  consumes 求和，超总量 → 冲突。这是账本唯一该管的（多个动作抢同一份预算，如 100 米跑时间预算）。
- **前提互斥（与资源类型正交）**：任一规则的 negates 命中另一规则的 requires → 冲突。

其余三类曾被错误塞进账本的约束，已各归其位（2026-10-12 深夜用户裁定「账本只该有一种类型」）：
  - policy_space（原 state_derivation）→ world_state 派生字段 + 动作前置条件
    （macro_resources.policy_space_of；candidate_source 生成宽松候选前查它；world_tree._apply_move 让
     宽松叙事下调 LPR，使 policy_space 沿路径真实收缩）
  - attention（原 mutual_exclusion）→ 候选生成层互斥（candidate_source 按 polarity 守卫）
  - decisive_window/fit（原 fit）→ 候选链结构：每条候选自带 chain_days，world_tree._grow 展开时判
    chain_days<=窗口（逐链并行不求和，不累加）

因此本引擎回到单类型：path_consumption 求和 + 前提互斥。系统只做「共享池求和」与「前提比对」，不自由判断。

与硬剪枝的关系：
- 硬剪枝管「单点荒谬」（一条规则 when→forbid 自身成立）。
- 本账本管「联动冲突」（多条规则共享资源 / 前提互否，单看每条都对，合起来矛盾）。
- 两者正交、叠加使用；本文件不改动既有 prune_rules.json / hard_prune.py。
"""
import json
import argparse
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "..", "config", "rule_ledger.json")


def load(path=DEFAULT_CONFIG):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------- when 匹配（与 prune_rules 同构，复用式小实现） ----------
def _cmp(op, a, b):
    try:
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op in ("<=", ">=", "<", ">"):
            # 数值比较（任一侧可转 float 则转）
            try:
                a2, b2 = float(a), float(b)
            except (TypeError, ValueError):
                return False
            if op == "<=":
                return a2 <= b2
            if op == ">=":
                return a2 >= b2
            if op == "<":
                return a2 < b2
            if op == ">":
                return a2 > b2
        if op == "in":
            return a in b
        if op == "not_in":
            return a not in b
    except (TypeError, ValueError):
        return False
    return False


def match_when(when, state):
    if when is None:
        return True
    if "all_of" in when:
        return all(match_when(c, state) for c in when["all_of"])
    if "any_of" in when:
        return any(match_when(c, state) for c in when["any_of"])
    m = when.get("metric")
    op = when.get("op")
    v = when.get("value")
    if m not in state:
        return False
    return _cmp(op, state[m], v)


def select_active(rules, state=None, scenario_ids=None):
    """激活规则集：优先显式 scenario（路径级场景），否则按 when 匹配状态。"""
    if scenario_ids:
        idset = set(scenario_ids)
        return [r for r in rules if r["id"] in idset]
    if state is not None:
        return [r for r in rules if match_when(r.get("when"), state)]
    return []


# ---------- 账本：唯一类型 path_consumption + 前提互斥 ----------
def check_ledger(active, resources):
    """对给定激活规则集做账本判定。

    账本只认**一种资源类型**——path_consumption（共享池求和）：同一资源上各规则
    consumes 求和，超总量 → 冲突。这是账本唯一该管的（多个动作抢同一份预算）。

    其余三类原被塞进账本的约束，已各归其位（2026-10-12 深夜重分配）：
      - policy_space（原 state_derivation）→ world_state 派生字段 + 动作前置条件
      - attention（原 mutual_exclusion）→ 候选生成层互斥（candidate_source）
      - decisive_window/fit（原 fit）→ 候选链结构（chain_days），world_tree._grow 判

    另：前提互斥（任一 negates 命中另一 requires → 冲突）是逻辑矛盾判定，与资源类型正交，保留。

    返回结构保持 {active, ledger, resource_conflicts, premise_conflicts, ok}，
    兼容 world_tree（只读 ok / resource_conflicts / premise_conflicts 长度）。
    """
    ledger = {}
    for rid, info in resources.items():
        ledger[rid] = {
            "type": info.get("type", "path_consumption"),
            "total": info.get("total", float("inf")),
            "unit": info.get("unit", ""),
            "consumed": 0.0,
            "by": [],
        }

    # 资源账本求和（仅 path_consumption 一类；未知资源默认 path_consumption、总量 inf）
    for r in active:
        for c in r.get("consumes", []) or []:
            res = c["resource"]
            amt = c.get("amount", 0.0)
            if res not in ledger:
                ledger[res] = {
                    "type": "path_consumption",
                    "total": float("inf"),
                    "unit": c.get("unit", ""),
                    "consumed": 0.0,
                    "by": [],
                }
            ledger[res]["consumed"] += amt
            ledger[res]["by"].append({"rule": r["id"], "amount": amt})

    resource_conflicts = []
    for rid, info in ledger.items():
        if info["consumed"] > info["total"] + 1e-9:
            resource_conflicts.append(
                {
                    "resource": rid,
                    "type": info["type"],
                    "total": info["total"],
                    "unit": info["unit"],
                    "consumed": round(info["consumed"], 4),
                    "over_by": round(info["consumed"] - info["total"], 4),
                    "by": info["by"],
                }
            )

    # 前提互斥（不变）
    premise_conflicts = []
    for r in active:
        for neg in r.get("negates", []) or []:
            for other in active:
                if other["id"] == r["id"]:
                    continue
                if neg in (other.get("requires", []) or []):
                    premise_conflicts.append(
                        {
                            "negating_rule": r["id"],
                            "negated_premise": neg,
                            "contradicted_rule": other["id"],
                            "contradicted_requires": neg,
                        }
                    )

    return {
        "active": [r["id"] for r in active],
        "ledger": ledger,
        "resource_conflicts": resource_conflicts,
        "premise_conflicts": premise_conflicts,
        "ok": not (resource_conflicts or premise_conflicts),
    }


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(
        description="跨规则资源账本 + 前提互斥检查（外置、可审计、无 AI 裁量）"
    )
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--state", help="WorldState JSON 文件，按 when 自动选激活规则")
    ap.add_argument(
        "--scenario", nargs="*", help="显式激活的规则 id 列表（路径级场景）"
    )
    ap.add_argument("--out", help="结果写出 JSON")
    a = ap.parse_args()

    cfg = load(a.config)
    resources = cfg.get("resources", {})
    rules = cfg.get("rules", [])

    state = None
    if a.state:
        with open(a.state, encoding="utf-8") as f:
            st = json.load(f)
            state = st.get("state", st)

    active = select_active(rules, state=state, scenario_ids=a.scenario)
    if not active:
        print("[WARN] 无激活规则（检查 --scenario / --state）")
    res = check_ledger(active, resources)

    print("=" * 64)
    print("激活规则:", res["active"])
    print("-" * 64)
    print("资源账本（唯一类型 path_consumption：共享池求和）:")
    for rid, info in res["ledger"].items():
        t = info.get("type", "path_consumption")
        tot = "∞" if info["total"] == float("inf") else "%.2f" % info["total"]
        over = info["consumed"] > info["total"] + 1e-9
        flag = "  ⚠超预算" if over else ""
        print(
            "  [%s] %s: 占用 %.2f / 总量 %s %s%s"
            % (t, rid, info["consumed"], tot, info["unit"], flag)
        )
        for b in info["by"]:
            print("      <- %s 占 %.2f" % (b["rule"], b["amount"]))
    print("注：policy_space(派生字段+前置条件)/attention(候选层互斥)/fit(候选chain_days) 已迁出账本。")
    print("-" * 64)
    if res["resource_conflicts"]:
        print("资源冲突（path_consumption 求和超预算）:")
        for c in res["resource_conflicts"]:
            if c["type"] == "path_consumption":
                print(
                    "  ⚠ [%s] %s 占用 %.2f > 总量 %.2f %s (超 %.2f)；构成: %s"
                    % (
                        c["type"], c["resource"], c["consumed"], c["total"], c["unit"],
                        c["over_by"],
                        ", ".join("%s=%.2f" % (x["rule"], x["amount"]) for x in c["by"]),
                    )
                )
    if res["premise_conflicts"]:
        print("前提互斥冲突（negates 命中 requires）:")
        for c in res["premise_conflicts"]:
            print(
                "  ⚠ %s 否定前提[%s] -> 与 %s 的 requires[%s] 矛盾"
                % (
                    c["negating_rule"],
                    c["negated_premise"],
                    c["contradicted_rule"],
                    c["contradicted_requires"],
                )
            )
    if res["ok"]:
        print("✓ 无跨规则冲突（账本平衡，前提自洽）")
    else:
        print(
            "✗ 检测到 %d 处资源冲突 + %d 处前提互斥冲突"
            % (len(res["resource_conflicts"]), len(res["premise_conflicts"]))
        )
    print("=" * 64)

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print("结果已写:", a.out)


if __name__ == "__main__":
    main()
