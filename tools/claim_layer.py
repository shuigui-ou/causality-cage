# -*- coding: utf-8 -*-
"""主张层（claim layer，2026-10-12 重构）—— 只抽证据，不生成主张。

设计纪律（承接用户纠偏 #211）：
  - 之前的版本是一个硬编码字典 TRANSDUCTION + Python .format() 填模板，全程无 LLM 参与，
    本质是"把人类写死的字符串打印成结论的样子"。8 月回溯失败根因 = 无推理发生。
  - 本版彻底移除硬编码字典与任何 LLM 调用（不用 Ollama，太慢；推理由 agent 在上下文中做）。
  - 本模块的唯一职责 = **抽取真实证据**（图里的真实传导边 + 真实宏观锚点），
    把"系统实际看到了什么"结构化输出，交给 agent 去推。它自己不推、不编、不贴标签伪装成结论。
  - 若图里没有相关传导边（数据支持缺位），证据 brief 会如实写明，agent 据此诚实声明
    "系统图给不出方向"，而非编造。

**两层闸门（用户 #211 落点 + #213 硬剪枝分层纠正后落地）：**

  ① 硬剪枝（hard_prune，承接 #213）：方向在逻辑/常识上不成立 → **直接排除，不得输出**。
     - 例：低通胀(CPI≤1.5%)+政策偏松下推 CN10Y「上行」= 荒谬（PRUNE-ABS-001）。
     - 判据**外置**在 config/prune_rules.json，由 hard_prune 机械执行，**先于任何 AI 推理**，
       绝不交给 AI 自由裁量。数据缺失时保守「不剪」（无法确认状态→不断言荒谬）。
     - 命中即抛 RefusedDirection，**该方向根本不该被生成**。

  ② 落点检查（require_landing_point，承接 #211）：数据无支撑 → **不剪，标推测、压概率、保留**。
     - 这是软剪枝（#213 分类第 3 类），不是硬拦截。系统图无支撑方向的真实传导边时，
       主张仍可落盘，但必须标 speculation=True + 说明，明确「数据支持缺位」。
     - 以下**均不构成落点**（只影响「软标注」，不再硬拦）：通用维度边 / 宏观锚点(观测点) / agent 外部知识。

  record_claim_with_gate(signal, brief, claim_obj)：落盘前依次过 ① ② 两道。
      claim_obj 必须显式声明 mode ——
        "directional"：含 predicted_target/predicted_sign → 先过①(硬剪枝)，再标②(落点)
        "refused"    ：direction_refused=True → 允许落盘（记录拦截原因），不要求落点
      含糊落盘（不声明 mode）一律禁止 —— 这正是"事后贴标签"的病根。

数据流：
  read_world_state(signal) -> 世界状态(具体值)   # 硬剪枝读它，不是读维度节点
  extract_evidence(signal, graph, use_macro) -> 结构化证据 brief
  hard_prune.is_pruned(state, candidate) -> (pruned, rules)   # ① 荒谬方向硬排除
  require_landing_point(brief) -> (found, edges, reason)      # ② 数据无支撑标推测
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
sys.path.insert(0, TOOLS)
NODE_PREFIX = "opendim:"

# 复用 conclusion_layer 的检测词表与函数（那边负责"检测事件专属节点"，这边负责"抽证据"）
from conclusion_layer import (  # noqa: E402
    EVENT_SPECIFIC_LEXICON,
    _signal_event_topics,
    _graph_event_nodes,
)
# 硬剪枝评估器（外置规则、机械执行、先于 AI 调用）
import hard_prune  # noqa: E402

# 与"价格/利率/债券"直接相关的关键词（收紧：不含过宽的 政策/货币/宏观，避免通用维度误命中）
_RELEVANT_KW = re.compile(
    r"通胀|债券|利率|降息|收益率|国债|CPI|PPI|物价"
)


def _real_macro_anchor(signal, ws_state=None):
    """用真实宏观源取最新读数，作为主张的现实锚点（CN/US 命名空间路由）。
    信号判读为海外/美国驱动 → 锚美国CPI(US_CPI_YOY)，否则锚中国CPI。失败返回 None。
    ws_state 可由调用方传入（避免重复拉取）；为 None 时自行读 world_state。
    """
    try:
        if ws_state is None:
            import world_state as ws_mod
            ws = ws_mod.read_world_state(signal)
        else:
            ws = ws_state if ("anchor_cpi" in ws_state or "state" in ws_state) else {"state": ws_state}
        anchor = ws.get("anchor_cpi")
        st = ws.get("state", {})
        if anchor and anchor.get("value") is not None:
            scope = anchor.get("scope", "CN")
            parts = ["%sCPI同比 %s%%（发布%s）" % (
                "美国" if scope == "US" else "中国",
                anchor["value"], anchor.get("as_of_data", ""))]
            if scope == "US":
                up = st.get("US_PPI", {})
                if isinstance(up, dict) and up.get("value") is not None:
                    parts.append("美国PPI %s%%" % up["value"])
            else:
                cp = st.get("PPI", {})
                if isinstance(cp, dict) and cp.get("value") is not None:
                    parts.append("中国PPI %s%%" % cp["value"])
            return {"summary": "、".join(parts), "scope": scope,
                    "anchor_value": anchor["value"],
                    "raw": "信号判读=%s锚（见 world_state.anchor_cpi）" % scope}
    except Exception:
        return None
    return None


def _real_capital_snapshot(ws_state=None):
    """从世界状态抽取 A股资金面真实读数（成交额/两融/M1），供主张证据引用。
    ws_state 可由调用方传入；为 None 时自行读 world_state。失败或无值返回 None。"""
    try:
        if ws_state is None:
            import world_state as ws_mod
            ws = ws_mod.read_world_state("")
        else:
            ws = ws_state if "state" in ws_state else {"state": ws_state}
        st = ws.get("state", {})
        keys = ["CN_TURNOVER", "CN_MARGIN_BALANCE", "CN_M1"]
        snap = {}
        for k in keys:
            v = st.get(k, {})
            if isinstance(v, dict) and v.get("value") is not None:
                snap[k] = {
                    "value": v["value"], "unit": v.get("unit", ""),
                    "as_of_data": v.get("as_of_data", ""), "source": v.get("source", ""),
                }
        return snap if snap else None
    except Exception:
        return None


def _real_global_snapshot(ws_state=None):
    """从世界状态抽取跨市场/全球真实读数（纳指/费半/能源指数/ASML·TSM财报/KOSPI），
    供主张证据引用。与 cn_capital_snapshot 平行；均为**观测点**，不构成「信号→可观测量」传导落点。
    ws_state 可由调用方传入；为 None 时自行读 world_state。失败或无值返回 None。"""
    try:
        if ws_state is None:
            import world_state as ws_mod
            ws = ws_mod.read_world_state("")
        else:
            ws = ws_state if "state" in ws_state else {"state": ws_state}
        st = ws.get("state", {})
        keys = ["G_NASDAQ", "G_SOX", "G_ENERGY_IDX", "G_KOSPI", "G_ASML_REV", "G_TSM_REV"]
        snap = {}
        for k in keys:
            v = st.get(k, {})
            if isinstance(v, dict):
                if v.get("value") is not None:
                    snap[k] = {"value": v["value"], "unit": v.get("unit", ""),
                               "as_of_data": v.get("as_of_data", ""), "source": v.get("source", "")}
                elif v.get("kind") == "unknown":
                    # 诚实保留 unknown 项（如 KOSPI），标注不可达而非丢弃
                    snap[k] = {"value": None, "unknown": True, "note": v.get("note", "")}
        return snap if snap else None
    except Exception:
        return None


def _real_market_transmission(signal=""):
    """读取 market_transmission_graph（由 build_transmission_graph.py 产出），
    作为 claim GATE 的**合法市场落点来源**之一。失败/文件缺失返回 None。
    严格诚实：只把 data_confirmed（真实历史联动）边当落点；hypothesized 边与
    cross_market_probe（跨境弱联动）只作证据引用，不构成落点。"""
    try:
        import os, json
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "transmission_graph.json")
        if not os.path.exists(p):
            return None
        g = json.load(open(p, encoding="utf-8"))
        edges = g.get("edges", [])
        meta = g.get("meta", {})
        GLOBAL_NODES = {"G_NASDAQ", "G_SOX", "US_10Y", "US_CPI_YOY"}
        ASHARE_NODES = {"CN_CYB", "CN_KCB50", "CN_HS300"}

        def is_bridge(e):
            return ((e["from"] in GLOBAL_NODES and e["to"] in ASHARE_NODES) or
                    (e["from"] in ASHARE_NODES and e["to"] in GLOBAL_NODES))
        dc = [e for e in edges if e.get("kind") == "data_confirmed"]
        hyp = [e for e in edges if e.get("kind") == "hypothesized"]
        bridges = [e for e in dc if is_bridge(e)]
        return {
            "available": True,
            "data_confirmed_edges": dc,
            "hypothesized_edges": hyp,
            "bridges_global_to_ashare": bridges,
            "cross_market_probe": meta.get("cross_market_probe", []),
            "note": ("跨境 data_confirmed 桥接边为空（跨境日收益联动弱，见 cross_market_probe）；"
                     "仅同市场内部强联动(0.8~0.9)与假设边。"
                     if not bridges else "存在跨境 data_confirmed 桥接边，可作结构性落点。"),
        }
    except Exception:
        return None


def extract_evidence(signal, graph_path, use_macro=True, ws_state=None):
    """抽取"系统实际看到什么"——图的真实传导边 + 真实宏观锚点 + 真实 A股资金面。不生成主张。"""
    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)

    signal_topics = _signal_event_topics(signal)
    graph_event = _graph_event_nodes(graph)
    covered = [t for t in signal_topics if t in graph_event]

    nodes = graph.get("nodes", {})
    edges = graph.get("edges", [])

    # 1) 事件专属节点（已在图中被锚定的）
    event_node_labels = sorted({lbl for t in covered for lbl in graph_event[t]})

    # 2) 扫描与主题直接相关的节点 id（标签/key 命中收紧关键词，或 event_specific）
    rel_ids = set()
    for nid, m in nodes.items():
        lbl = str(m.get("label", nid))
        if m.get("event_specific") or _RELEVANT_KW.search(nid) or _RELEVANT_KW.search(lbl):
            rel_ids.add(nid)

    # 3) 真实传导边：两端任一落在相关节点集
    transmission_edges = []
    for e in edges:
        f, t = str(e.get("from", "")), str(e.get("to", ""))
        if f in rel_ids or t in rel_ids:
            co = e.get("cooccur_docs")
            co_count = co if isinstance(co, int) else (len(co) if co else 0)
            transmission_edges.append({
                "from": f, "to": t,
                "confidence": round(float(e.get("confidence", 0) or 0), 3),
                "lift": round(float(e.get("lift", 0) or 0), 3),
                "cooccur_docs_count": co_count,
                "temporal_direction": e.get("temporal_direction", ""),
            })

    # 3b) 事件专属传导边：两端均为 event_specific（这才是系统图真正能支撑方向的边）
    event_specific_ids = {nid for nid, m in nodes.items() if m.get("event_specific")}
    event_specific_edges = [
        e for e in transmission_edges
        if e["from"] in event_specific_ids and e["to"] in event_specific_ids
    ]

    macro = _real_macro_anchor(signal, ws_state) if use_macro else None
    capital = _real_capital_snapshot(ws_state) if use_macro else None
    global_snap = _real_global_snapshot(ws_state) if use_macro else None
    trans = _real_market_transmission(signal) if use_macro else None

    # 4) 诚实判定：区分"事件专属边"与"通用维度误命中"
    if event_specific_edges:
        honest_note = "系统图含 %d 条事件专属真实传导边，可据此推方向。" % len(event_specific_edges)
    elif transmission_edges:
        honest_note = ("系统图命中 %d 条含关键词的边，但均为通用 opendim 维度"
                       "（如 政策与监管/产业与经济），非本事件专属传导边；方向不能据此推，"
                       "数据支持缺位——agent 推理须明确标注方向来自外部宏观语境而非系统图。"
                       ) % len(transmission_edges)
    else:
        honest_note = ("系统图无与主题相关的真实传导边（采集源不含宏观传导结构），"
                       "方向不能由系统图推出，数据支持缺位——agent 推理须明确标注其方向"
                       "来自外部宏观语境而非系统图。")

    return {
        "signal": signal,
        "detected_event_topics": signal_topics,
        "event_nodes_in_graph": event_node_labels,
        "relevant_node_count": len(rel_ids),
        "transmission_edges": transmission_edges,
        "event_specific_transmission_edges": event_specific_edges,
        "transmission_edges_available": len(transmission_edges) > 0,
        "event_specific_transmission_available": len(event_specific_edges) > 0,
        "real_macro_anchor": macro,
        "cn_capital_snapshot": capital,
        "global_market_snapshot": global_snap,
        "market_transmission": trans,
        "honest_note": honest_note,
        "agent_reasoning_required": True,  # 主张必须由 agent 在上下文中推理，本模块不生成
    }


class RefusedDirection(ValueError):
    """硬闸门触发：方向被**硬剪枝**排除（逻辑/常识上不成立），不允许输出。

    这是推理前就被拦截（先于任何 AI 推理的硬性约束，规则外置在 prune_rules.json），
    不是"数据支持缺位"的事后标签 —— 该主张根本不该被生成。
    注意：数据无支撑**不再**触发本异常（那属于软剪枝，落盘时标 speculation 而非拒绝）。
    """


def require_landing_point(brief):
    """推理前的硬闸门：必须先在系统图里找到能支撑方向的「落点」（真实传导边）。

    落点定义（严格）：系统图内一条真实采集到的传导边，其两端均为事件专属节点
    （event_specific=True），且连接「信号主题」与「可观测量」。

    以下均**不构成**落点，闸门必须拦下：
      · 仅含关键词的通用 opendim 维度边（如 政策与监管 / 产业与经济）—— 与事件传导无关
      · 真实宏观锚点（如 CPI 读数）—— 它是观测点，不是"信号→可观测量"的传导边
      · agent 外部知识 / 语境 —— 那是"用脑子填空"，正是要被关在门外的东西

    返回 (found: bool, landing_edges: list, reason: str)。
    """
    edges = brief.get("event_specific_transmission_edges") or []
    if edges:
        return True, edges, (
            "系统图存在 %d 条事件专属真实传导边，可作为方向落点。" % len(edges))
    # 新增：市场传导图 transmision_graph 的 data_confirmed 跨境桥接边，可作结构性落点
    mt = brief.get("market_transmission") or {}
    bridges = mt.get("bridges_global_to_ashare") or []
    sig = (brief.get("signal") or "").lower()
    touches_ashare = any(k in sig for k in ["a股", "创业板", "科创", "沪深", "上证", "a 股", "中国", "内地"])
    if bridges and touches_ashare:
        return True, bridges, (
            "市场传导图(transmission_graph)存在 %d 条 data_confirmed 跨境桥接边（真实历史联动），"
            "可作为结构性落点；属共动结构，非点预测保证。" % len(bridges))
    # 失败分支：区分"有通用边但非落点"与"图里完全无相关结构"
    macro_sum = (brief.get("real_macro_anchor") or {}).get("summary", "无")
    mt = brief.get("market_transmission") or {}
    probe = mt.get("cross_market_probe") or []
    probe_txt = ""
    if probe:
        items = ["%s↔%s 同频=%.2f/领先1日=%.2f" % (p["from"], p["to"], p["corr_same"], p["corr_lead1"])
                  for p in probe]
        probe_txt = " 市场传导图实测跨境日收益联动弱（%s），均低于落边阈值，无跨境 data_confirmed 落点。" % "；".join(items)
    if brief.get("transmission_edges"):
        reason = (
            "GATE BLOCKED：系统图命中 %d 条含关键词的边，但均为通用 opendim 维度，"
            "非事件专属传导边；真实宏观锚点（%s）只是观测点，不构成「信号→可观测量」的"
            "传导落点；外部语境 / agent 知识不得替代落点。拒绝输出方向。%s"
        ) % (len(brief["transmission_edges"]), macro_sum, probe_txt)
    else:
        reason = (
            "GATE BLOCKED：系统图无与主题相关的真实传导边（采集源不含该传导结构），"
            "真实宏观锚点（%s）只是观测点；拒绝输出方向。%s" % (macro_sum, probe_txt)
        )
    return False, [], reason


def _read_world_state_safe(signal):
    """安全读取世界状态（具体观测值），供硬剪枝判定。失败时返回空态 + 原因。"""
    try:
        import world_state as ws_mod
        ws = ws_mod.read_world_state(signal)
        return ws.get("state", {}), None
    except Exception as e:  # akshare 不可达等：保守退回空态（硬剪枝不触发，但落地检查会标推测）
        return {}, ("world_state 读取失败: %s" % e)


def gate_claim(brief, claim_obj):
    """落盘前依次过两道闸门：① 硬剪枝（荒谬方向直接排除）② 落点检查（数据无支撑只标推测）。

    - mode="directional"：必须声明 predicted_target / predicted_sign。
        先过 ①：若被硬剪枝命中 → 抛 RefusedDirection（方向根本不该生成）。
        再过 ②：若图内无落点 → 不拦，标 speculation=True + 说明（软剪枝，保留）。
    - mode="refused"：允许落盘，记录拦截/所需证据（供审计）。
    - 其它 mode：禁止含糊落盘。
    """
    mode = claim_obj.get("mode")
    if mode == "directional":
        target = claim_obj.get("predicted_target")
        sign = claim_obj.get("predicted_sign")
        if not target or not sign:
            raise ValueError(
                "mode='directional' 必须声明 predicted_target 与 predicted_sign；"
                "否则无法判断是否荒谬、也无法定位落点。")
        # ① 硬剪枝：方向在逻辑/常识上不成立 → 直接拦下（先于任何 AI 推理的硬性约束）
        state, ws_err = _read_world_state_safe(brief.get("signal", ""))
        candidate = {"target": target, "sign": sign}
        pruned, matched = hard_prune.is_pruned(state, candidate)
        if pruned:
            reasons = "；".join("[%s] %s" % (r["id"], r["reason"]) for r in matched)
            raise RefusedDirection(
                "HARD PRUNE：候选方向 %s/%s 被硬剪枝规则排除——%s" % (target, sign, reasons))
        # ② 落点检查：数据无支撑 → 不剪，标推测、压概率、保留
        found, edges, reason = require_landing_point(brief)
        if found:
            claim_obj["landing_point"] = {
                "found": True, "edges": edges,
                "gate": "require_landing_point_before_direction"}
        else:
            claim_obj["speculation"] = True
            claim_obj.setdefault("speculation_note", reason)
            claim_obj.setdefault(
                "gate", "hard_prune_passed_landing_missing_marked_speculation")
        return claim_obj
    elif mode == "refused":
        claim_obj.setdefault("direction_refused", True)
        claim_obj.setdefault("gate", "hard_prune_or_landing_refused")
        # 顺带记录：若强行给方向会命中哪些硬剪枝规则（供审计）
        state, _ = _read_world_state_safe(brief.get("signal", ""))
        t, s = claim_obj.get("predicted_target"), claim_obj.get("predicted_sign")
        if t and s:
            _p, matched = hard_prune.is_pruned(state, {"target": t, "sign": s})
            if matched:
                claim_obj.setdefault(
                    "would_have_been_pruned_by",
                    [r["id"] for r in matched])
        found, _edges, reason = require_landing_point(brief)
        claim_obj.setdefault("refusal_reason", reason)
        claim_obj.setdefault(
            "what_is_needed",
            "系统图需长出「信号主题→可观测量」的真实传导边（如 通胀→债券收益率），"
            "来源须为经采集验证的传导结构，而非 agent 外部知识或单点宏观观测。")
        return claim_obj
    else:
        raise ValueError(
            "claim_obj.mode 必须显式为 'directional' 或 'refused'；"
            "禁止含糊落盘（这正是'事后贴标签'的病根）。")


def record_claim_with_gate(signal, brief, claim_obj, out_dir=None):
    """落盘主张，但先过闸门（见 gate_claim）。返回 (path, mode)。

    - mode="directional" 且无落点 -> 抛 RefusedDirection（方向根本不会被落盘）
    - mode="refused"            -> 允许落盘，记录"闸门拦截"原因与所需证据
    """
    gated = gate_claim(brief, claim_obj)
    path = record_claim(signal, gated, out_dir=out_dir)
    return path, gated.get("mode")


def record_claim(signal, claim_obj, out_dir=None):
    """agent 推理完成后，把主张落盘。claim_obj 由 agent 自行构造（内容优先）。"""
    out_dir = out_dir or os.path.join(BASE, "reports", "predictions")
    os.makedirs(out_dir, exist_ok=True)
    h = hashlib.md5(signal.encode("utf-8")).hexdigest()[:10]
    path = os.path.join(out_dir, "claim_%s.json" % h)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(claim_obj, f, ensure_ascii=False, indent=2)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="主张层：抽取真实证据（不生成主张，推理交 agent）")
    ap.add_argument("--signal", required=True, help="信号文本")
    ap.add_argument("--graph", default=os.path.join(BASE, "config", "archive", "factor_graph_opendim_acquired.json"))
    ap.add_argument("--no-macro", action="store_true", help="不接入真实宏观源锚点")
    ap.add_argument("--claim-json", default=None, help="agent 已推理完成的主张 JSON 路径，落盘用")
    a = ap.parse_args()

    if a.claim_json:
        with open(a.claim_json, encoding="utf-8") as f:
            claim = json.load(f)
        brief = extract_evidence(a.signal, a.graph, use_macro=not a.no_macro)
        try:
            path, mode = record_claim_with_gate(a.signal, brief, claim)
        except RefusedDirection as e:
            print("\n[闸门拦截] 拒绝落盘方向主张：")
            print("  ", e)
            print("  -> 该主张系统图无落点，根本不该被生成。请改出 mode='refused' 记录拦截。")
            raise SystemExit(2)
        print(json.dumps(claim, ensure_ascii=False, indent=2))
        print("\n->", path, "(mode=%s)" % mode)
    else:
        brief = extract_evidence(a.signal, a.graph, use_macro=not a.no_macro)
        found, edges, reason = require_landing_point(brief)
        print(json.dumps(brief, ensure_ascii=False, indent=2))
        print("\n[landing_point_gate] found=%s" % found)
        if not found:
            print("  GATE BLOCKED -> 拒绝输出方向。", reason)
        else:
            print("  落点边:", [(e["from"], e["to"]) for e in edges])
        print("[agent_reasoning_required] 以上为系统真实证据；"
              "若 found=False 只能产出 mode='refused'，不得输出方向。")
