#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo.py — 一行跑出因果推演树，并现场展示系统如何剪掉「瞎话」。

    python demo.py [信号] [深度]

零依赖、零联网、零 API Key。不需要 pandas / numpy / akshare / requests。

本 demo 用一个**确定性离线生成器**代替大模型：它故意同时产出「合理候选」与
「违背常识的候选」，好让 hard_prune 当场把它们剪掉——观众看到的不是
「系统很聪明」，而是**系统拒绝让胡说八道进入推演**。

这个架构（AI 穷举 → 系统剪枝）本身就是本项目的核心主张：
**候选生成可以交给 AI，但否决权必须留在系统手里。**
"""
from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "tools"))

import ai_candidate  # noqa: E402
import world_state  # noqa: E402
import world_tree  # noqa: E402


def _off(value, unit="", as_of="2026-01-01", kind="raw", source="demo_seed", note=""):
    """构造一个世界状态观测项，字段与 world_state 的输出保持一致。"""
    return {"value": value, "unit": unit, "as_of_data": as_of,
            "source": source, "kind": kind, "note": note}


def _off_unknown(note="离线 demo：不读真实源，诚实留空"):
    return {"value": None, "unit": "", "as_of_data": "",
            "source": "demo_seed", "kind": "unknown", "note": note}


#: 离线世界状态种子（确定性，不联网、不读 akshare）。
#: 诚实声明：这些是**占位观测**，不是真实行情。demo 的目的是演示剪枝机制，
#: 不是演示数据质量——真实数据支路需要 pandas + akshare，见 README 说明。
SEED_WS = {
    "as_of": "2026-01-01",
    "signal": "(demo)",
    "state": {
        "CPI": _off(0.3, "%", as_of="2025-12"),
        "PPI": _off(-1.8, "%", as_of="2025-12"),
        "LPR_1Y": _off(3.0, "%"),
        "LPR_5Y": _off(3.5, "%"),
        "M2": _off(8.0, "%"),
        "USDCNH": _off(7.15, ""),
        "CN10Y": _off(1.85, "%"),
        "ENSO": _off(None, "", kind="unknown",
                     note="离线 demo 不读 ENSO 真实源；真实支路见 akshare"),
        "policy_stance": _off("neutral", "", kind="interpreted"),
        "US_CPI_YOY": _off(2.7, "%"),
        "US_10Y": _off(4.2, "%"),
        "US_FEDFUNDS": _off(4.25, "%"),
        "CN_TURNOVER": _off(9000.0, "亿元"),
        "CN_M1": _off(1.0, "%"),
        "G_NASDAQ": _off(18000.0, ""),
        "G_SOX": _off(5500.0, ""),
        "G_ENERGY_IDX": _off(3200.0, ""),
        "CN_POWER_IDX": _off(3000.0, ""),
        "CN_AI_IDX": _off(4200.0, ""),
        "CN_AGRI_PRICE_IDX": _off(115.0, ""),
    },
    "anchor_cpi": {"scope": "CN", "key": "CPI"},
    "namespace": {"CN": [], "US": [], "GLOBAL": [],
                  "note": "离线 demo：无命名空间划分"},
    "honesty": {"principle": "读世界，不建世界",
                "raw": 0, "interpreted": 0, "unknown": 0,
                "anchor_scope": "CN",
                "note": "离线 demo 种子：占位观测，非真实行情"},
}


def _install_offline_world():
    """把 world_state.read_world_state 换成离线种子。

    为什么必须换：read_world_state 会无条件读取 30+ 项真实行情（akshare/网络），
    这是引擎的**在线**支路。demo 要做到「clone 下来一行跑通、零依赖零联网」，
    就必须显式把世界状态切到确定性种子。这一步是 demo 自己的选择，
    不修改引擎行为——在线跑法完全不变。
    """
    world_state.read_world_state = lambda signal, as_of=None: dict(
        SEED_WS, signal=signal, as_of=as_of or SEED_WS["as_of"])


# ─────────────────────────────────────────────────────────────
# 1. 离线确定性生成器（扮演「AI 穷举」的角色）
# ─────────────────────────────────────────────────────────────
# 真跑时这一层换成你的大模型（见 tools/llm_generator.py，OpenAI 兼容适配器）。
# **生成器可以胡说，但系统必须能剪掉它。**

GOOD_NARRATIVES = [
    ("cost_push", 2),                 # 成本推升：快通道(2 天)
    ("demand_weak", 2),               # 需求走弱：快通道
    ("policy_easing_expectation", 10),  # 宽松预期：慢通道(10 天，等 LPR 定价)
    ("inflation_concern", 2),
]
# 违背常识的候选：系统应当剪掉它们
BAD_NARRATIVES = [
    ("fiscal_expansion", 2),          # 与「需求走弱」同时成立 → 互斥，应被剪
    ("policy_easing_expectation", 10),  # 与 hawkish 立场冲突 → 应被剪
]


def offline_generator(state, narrative_layer=None, node_context=None, seen=None):
    """确定性穷举：同一状态永远返回同一批候选（可复现，无随机）。

    seen：系统回传的「本节点已枚举候选」，追问循环靠它收敛。

    chain_days 用引擎真实常量（candidate_source.NARRATIVE_CHAIN_DAYS）：
    快通道 2 天、慢通道 10 天。传真实天数而不是拍脑袋的数，是 demo 能长出
    真树的前提——随意给天数会被 window_fit 层全部剪掉（那是窗口口径问题，
    不是剪枝逻辑问题，两者必须分清）。
    """
    import candidate_source as cs

    seen = set(seen or [])
    out = []
    depth = 0
    if isinstance(state, dict):
        depth = int(state.get("depth") or 0)

    layer = set(narrative_layer or []) if narrative_layer else set()

    for i, (name, days) in enumerate(GOOD_NARRATIVES + BAD_NARRATIVES):
        if name in seen:
            continue
        # 已在路径上的叙事不再重复提议（真实 agent 也该这么干）
        if name in layer:
            continue
        out.append({
            "action": "narrative",
            "narrative": name,
            "desc": "候选：%s（第 %d 层枚举）" % (name, depth + 1),
            "chain_days": cs.NARRATIVE_CHAIN_DAYS.get(name, days),
            "polarity": cs.NARRATIVE_POLARITY.get(name, "neutral"),
            "magnitude": ["weak", "typical", "strong"][i % 3],
        })
    return out


# ─────────────────────────────────────────────────────────────
# 2. 树打印（复用引擎自带的节点语义标记）
# ─────────────────────────────────────────────────────────────
def _mv_label(mv):
    mv = mv or {}
    a = mv.get("action")
    if a == "narrative":
        return "narrative:%s" % mv.get("narrative")
    if a == "transmission":
        return "transmission:%s->%s" % (mv.get("from"), mv.get("to"))
    return "dir:%s %s" % (mv.get("target"), mv.get("sign"))


def _render(node, indent=0, out=None):
    """按引擎原生语义渲染一棵树；顺带把剪枝记录收集到 out。"""
    out = out if out is not None else []
    pad = "  " * indent
    node_id = node.get("id") or node.get("path") or ""
    print("%s● %s  [depth=%s]%s%s" % (
        pad, node_id, node.get("depth", indent),
        ("  <%s>" % node["stop_reason"]) if node.get("stop_reason") else "",
        ("  [撞真]%s" % (node.get("real_anchor_months") or "")) if node.get("real_anchor") else "",
    ))
    for pm in node.get("pruned_moves", []) or []:
        rules = ",".join(pm.get("rules") or [])
        line = "%s   ✂ [%s] %s  ← %s" % (pad, (pm.get("layer") or "?").upper(),
                                        _mv_label(pm.get("move")), rules)
        print(line)
        out.append({"layer": pm.get("layer"), "move": _mv_label(pm.get("move")), "rules": rules})
    for ch in node.get("children", []) or []:
        print("%s   ↳ %s [%s]%s" % (
            pad, _mv_label(ch.get("move")), ch.get("mark", ""),
            ("  <%s>" % ch["stop_reason"]) if ch.get("stop_reason") else ""))
        _render(ch, indent + 1, out)
    return out


def _count(node, acc=None):
    acc = acc if acc is not None else {"nodes": 0, "pruned": 0}
    acc["nodes"] += 1
    acc["pruned"] += len(node.get("pruned_moves", []) or [])
    for ch in node.get("children", []) or []:
        _count(ch, acc)
    return acc


# ─────────────────────────────────────────────────────────────
# 3. 主流程
# ─────────────────────────────────────────────────────────────
def main():
    signal = sys.argv[1] if len(sys.argv) > 1 else "测试信号：产业政策调整"
    depth = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    # 注入离线生成器 —— 真跑时这里是你的 LLM
    ai_candidate.set_agent_generator(offline_generator)
    # 切到离线世界状态（占位观测），保证零联网、零依赖
    _install_offline_world()

    # 静默 tqdm：部分数据源库会打进度条，demo 要干净输出
    try:
        import tqdm as _tqdm
        _tqdm.tqdm.__init__.__defaults__ = (None,) * len(
            _tqdm.tqdm.__init__.__defaults__ or ())
    except Exception:
        pass

    print("=" * 70)
    print("因果推演树 · 离线 Demo   (AI 穷举 → 系统剪枝)")
    print("=" * 70)
    print("信号    : %s" % signal)
    print("深度    : %d" % depth)
    print("生成器  : offline_generator（确定性离线桩，不联网、不调模型服务）")
    print("世界状态: 离线种子（占位观测，非真实行情）")
    print("剪枝器  : hard_prune（系统侧，拥有最终否决权）")
    print()

    result = world_tree.build_world_tree(
        signal,
        max_depth=depth,
        use_real_feeds=False,   # demo 离线：不开真实数据源
        use_ledger=False,
        use_history=False,
    )
    # build_world_tree 返回 (tree, ws) 二元组
    tree = result[0] if isinstance(result, tuple) else result

    print("-" * 70)
    print("推演树（●=存活节点，✂=被剪掉的候选）")
    print("-" * 70)
    pruned_log = _render(tree)
    stats = _count(tree)

    print()
    print("=" * 70)
    print("剪枝汇总")
    print("=" * 70)
    print("存活节点: %d    被剪候选: %d" % (stats["nodes"], stats["pruned"]))
    if pruned_log:
        print()
        print("被剪掉的候选及其依据：")
        seen = set()
        for p in pruned_log:
            key = (p["layer"], p["move"], p["rules"])
            if key in seen:
                continue
            seen.add(key)
            print("  ✂ %-10s %-34s 依据规则: %s" % (
                (p["layer"] or "?").upper(), p["move"][:34], p["rules"][:46]))
    print()
    print("要点：被剪掉的是「不合常识的路径」，而不是「AI 没生成的东西」。")
    print("     系统不替 AI 编答案，但系统有权拒绝明显错误的答案。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())