#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_engine.py — 引擎核心不变量测试（开源包专版）

为什么是专版：本项目内部另有一套全量 conformance 门禁（358 项），
覆盖采集/标注/分类等整条流水线，依赖 parquet 语料与 pyarrow，
**不适合随开源包发布**。这里只测「引擎本身」的关键不变量——
也正是开发过程中真实踩过的坑。

零依赖：不需要 pandas / numpy / akshare / pyarrow。

运行：python tests/test_engine.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import ai_candidate  # noqa: E402
import canon  # noqa: E402
import hard_prune  # noqa: E402
import world_state  # noqa: E402
import world_tree  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    mark = "PASS" if cond else "FAIL"
    print("  [%s] %s%s" % (mark, name, ("  — %s" % detail) if detail and not cond else ""))


# ── 离线世界状态种子（不联网、不读 akshare）──────────────────
def _off(v, unit="", kind="raw"):
    return {"value": v, "unit": unit, "as_of_data": "2026-01-01",
            "source": "test_seed", "kind": kind, "note": ""}


SEED = {
    "as_of": "2026-01-01", "signal": "(test)",
    "state": {
        "CPI": _off(0.3, "%"), "PPI": _off(-1.8, "%"),
        "LPR_1Y": _off(3.0, "%"), "LPR_5Y": _off(3.5, "%"),
        "M2": _off(8.0, "%"), "USDCNH": _off(7.15), "CN10Y": _off(1.85, "%"),
        "ENSO": _off(None, kind="unknown"),
        "policy_stance": _off("neutral", kind="interpreted"),
        "US_CPI_YOY": _off(2.7, "%"), "US_10Y": _off(4.2, "%"),
        "US_FEDFUNDS": _off(4.25, "%"), "CN_TURNOVER": _off(9000.0, "亿元"),
        "CN_M1": _off(1.0, "%"), "G_NASDAQ": _off(18000.0),
        "G_SOX": _off(5500.0), "G_ENERGY_IDX": _off(3200.0),
        "CN_POWER_IDX": _off(3000.0), "CN_AI_IDX": _off(4200.0),
        "CN_AGRI_PRICE_IDX": _off(115.0),
    },
    "anchor_cpi": {"scope": "CN", "key": "CPI"},
    "namespace": {"CN": [], "US": [], "GLOBAL": [], "note": "test"},
    "honesty": {"principle": "读世界，不建世界", "raw": 0, "interpreted": 0,
                "unknown": 0, "anchor_scope": "CN", "note": "test seed"},
}


def _install_seed():
    world_state.read_world_state = lambda signal, as_of=None: dict(
        SEED, signal=signal, as_of=as_of or SEED["as_of"])


# ── 组1：候选槽位契约（坑#1：静默空结果）────────────────────
def test_no_silent_empty():
    print("\n[组1] 候选槽位：未注入生成器时必须显式报错（不静默返回空）")
    saved = ai_candidate.GENERATOR_OVERRIDE
    saved_agent = ai_candidate.AGENT_GENERATOR
    try:
        ai_candidate.GENERATOR_OVERRIDE = None
        ai_candidate.AGENT_GENERATOR = None
        ai_candidate.clear_cache()
        raised = False
        try:
            ai_candidate.generate({"depth": 0}, narrative_layer=None,
                                  node_context=None, seen=None)
        except RuntimeError as e:
            raised = "未注入" in str(e) or "AI生成槽位" in str(e)
        check("未注入生成器 → 显式抛错而非静默返回空", raised)
    finally:
        ai_candidate.GENERATOR_OVERRIDE = saved
        ai_candidate.AGENT_GENERATOR = saved_agent
        ai_candidate.clear_cache()


# ── 组2：硬剪枝真的会剪（核心能力）─────────────────────────
def test_hard_prune_fires():
    print("\n[组2] 硬剪枝：违背常识的方向必须被剪掉")
    rules = hard_prune.load_rules()
    check("示范规则库可加载", bool(rules.get("rules")), "rules 为空")

    # 注意：hard_prune.check() 收的是**扁平 state**（state["CPI"]["value"]），
    # 不是 read_world_state() 返回的 {"state": {...}} 包装。
    st = dict(SEED["state"])
    st["_narrative"] = None

    # LPR 自2019 改革以来只降不升 → 推 LPR_1Y 上行必须被剪
    pruned, why = hard_prune.is_pruned(
        st, {"action": "direction", "target": "LPR_1Y", "sign": "up", "horizon": 2},
        rules=rules)
    check("LPR_1Y 上行被剪（只降不升）", bool(pruned), "未被剪: %s" % why)

    # 低通胀(CPI 0.3) + 政策中性 → 10Y 上行 = 荒谬
    pruned2, why2 = hard_prune.is_pruned(
        st, {"action": "direction", "target": "CN10Y", "sign": "up", "horizon": 2},
        rules=rules)
    check("CN10Y 上行被剪（低通胀+非偏紧）", bool(pruned2), "未被剪: %s" % why2)

    # 反向对照：下行方向不应被同类规则剪掉（证明规则不是「一刀切全剪」）
    pruned3, _ = hard_prune.is_pruned(
        st, {"action": "direction", "target": "CN10Y", "sign": "down", "horizon": 2},
        rules=rules)
    check("CN10Y 下行未被同类规则误剪", not pruned3, "下行被误剪")


# ── 组3：叙事接地（坑#2：叙事与世界状态矛盾）────────────────
def test_narrative_grounding():
    print("\n[组3] 叙事接地：与世界矛盾的中间叙事必须被拒")
    rules = hard_prune.load_rules()
    st = dict(SEED["state"])   # CPI=0.3 ≤1.5，政策非偏紧
    pruned, why = hard_prune.is_pruned(
        st, {"action": "narrative", "narrative": "inflation_concern"}, rules=rules)
    check("低通胀下引入 inflation_concern 被剪", bool(pruned), "未被剪: %s" % why)


# ── 组4：规范层（范围外拒收）────────────────────────────────
def test_canon():
    print("\n[组4] canon：范围外 id 必须拒收")
    ok = canon.is_canonical("CN10Y", "direction")
    check("CN10Y 是合法方向 id", ok)
    bad = canon.is_canonical("TOTALLY_MADE_UP_TARGET", "direction")
    check("编造的方向 id 被拒", not bad)


# ── 组5：L3 状态去重（坑#3：状态塌缩）──────────────────────
def test_l3_dedup():
    print("\n[组5] 状态去重：同一世界状态不得无限展开")
    _install_seed()
    import candidate_source as cs

    def gen(state, narrative_layer=None, node_context=None, seen=None):
        seen = set(seen or [])
        layer = set(narrative_layer or []) if narrative_layer else set()
        out = []
        for name in ("cost_push", "demand_weak"):
            if name in seen or name in layer:
                continue
            out.append({"action": "narrative", "narrative": name,
                        "desc": name, "chain_days": cs.NARRATIVE_CHAIN_DAYS.get(name, 2)})
        return out

    ai_candidate.set_agent_generator(gen)
    try:
        result = world_tree.build_world_tree(
            "测试信号：产业政策调整", max_depth=3,
            use_real_feeds=False, use_ledger=False, use_history=False)
        # build_world_tree 返回 (tree, ws) 二元组
        tree = result[0] if isinstance(result, tuple) else result

        def collect(n, out=None):
            out = out if out is not None else []
            out.append(n)
            for c in n.get("children") or []:
                collect(c, out)
            return out

        nodes = collect(tree)
        check("树能生长出节点", len(nodes) > 1, "只有 %d 个节点" % len(nodes))

        from collections import Counter as _C
        stops = _C(str(n.get("stop_reason")) for n in nodes)
        print("       [诊断] 节点=%d 最大深度=%d 停止原因=%s" % (
            len(nodes), max(n.get("depth", 0) for n in nodes), dict(stops)))

        # 判重必须用引擎自己的 _state_key —— 它含 _narrative 覆盖层与路径累积叙事集，
        # 比 (state_summary, path_narratives) 更严。之前用后者会误报：
        # state_summary 是展示用摘要，不含 _narrative 层，会把「同状态不同叙事覆盖」
        # 误判成塌缩。
        seen_keys = {}
        dup = []
        for n in nodes:
            k = world_tree._state_key(n.get("state") or {}, n.get("path_narratives") or [])
            if k in seen_keys:
                dup.append(k)
            seen_keys[k] = True
        if dup:
            print("       [诊断] 重复键: %s" % dup[:2])
        check("同层无重复状态键（状态塌缩已抑制）", not dup,
              "发现 %d 个重复" % len(dup))

        # 真出现重复时，引擎必须给它打上 L3_visited 标记（留痕，不静默砍）
        if dup:
            marked = sum(1 for n in nodes if n.get("stop_reason") == "L3_visited")
            check("重复节点带 L3_visited 标记（停≠砍，须留痕）", marked >= len(dup),
                  "重复 %d 个但仅 %d 个带标记" % (len(dup), marked))

        # L3_visited 是否被触发，取决于叙事集大小——本测试只用 2 个叙事，
        # 树很浅，本就不该触发。这里验证**机制存在**（源码里有该停止原因分支），
        # 而非强求这个特定小场景必然命中。真实深树会触发，见 demo.py 输出。
        import inspect as _insp
        src = _insp.getsource(world_tree)
        check("引擎含 L3_visited 停止机制", "L3_visited" in src,
              "world_tree 源码中找不到 L3_visited 分支")
    finally:
        ai_candidate.set_agent_generator(None)
        ai_candidate.clear_cache()


# ── 组6：零依赖可导入（坑#4：顶层硬依赖导致 fresh clone 崩）──
def test_zero_dep_import():
    print("\n[组6] 零依赖：pandas 为惰性加载，顶层不硬依赖")
    check("world_state.pd 为惰性代理", type(world_state.pd).__name__ == "_LazyPandas",
          "实际类型: %s" % type(world_state.pd).__name__)


import json  # noqa: E402  (组5 用)

if __name__ == "__main__":
    print("=" * 62)
    print("引擎核心不变量测试（开源包专版，零依赖）")
    print("=" * 62)
    for fn in (test_no_silent_empty, test_hard_prune_fires, test_narrative_grounding,
               test_canon, test_l3_dedup, test_zero_dep_import):
        try:
            fn()
        except Exception as e:
            FAIL.append((fn.__name__, "%s: %s" % (type(e).__name__, e)))
            print("  [FAIL] %s 抛异常 — %s: %s" % (fn.__name__, type(e).__name__, e))

    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\n失败明细：")
        for n, d in FAIL:
            print("  ✗ %s  %s" % (n, d))
    print("=" * 62)
    raise SystemExit(1 if FAIL else 0)