# -*- coding: utf-8 -*-
"""世界分岔的记录器（world_tree，2026-10-12 路线 2 重搭完成 + 路线 4 三件接线）。

设计纪律（承接用户本体论纠正 #214 + 结构洞 #215 + 路线 2/4 裁定）：
  - 系统不**建**世界，只**记录**世界在给定状态下实际分出的岔。
  - 起点 = 一份 WorldState（world_state.read_world_state 读来的具体观测值）。
  - 展开（expand）= **AI 穷举**（ai_candidate.generate 槽位，由 agent 自身替代 AI，不调用任何外部模型服务；
    取代旧手写 expand_rules.json 与 candidate_source 确定性枚举器）。AI 在系统外面只负责穷举候选，
    系统随后用世界规则剪枝（承接 2026-10-13 架构纠正：AI 穷举 → 系统剪枝）。
  - 硬剪枝 / 约束**并入「枚举时的世界规则」**：每个候选在产出瞬间即过 is_pruned + 约束层，
    被世界规则排除的候选根本不进入树（不再是外部事后闸门）。硬剪枝外置可审计、先于 AI 裁量。
  - **真实数据支持判断（第三届外置依据，2026-10-15 架构纠正）**：`prune_rules.json`（世界规则，
    判"荒谬吗"）与 `transmission_graph`（真实市场联动，判"有真实数据支持吗"）是**同类**外置可审计依据。
    主循环枚举出"X → Y"市场传导候选时，`transmission_graph` 在枚举后的剪枝阶段作为"真实数据支持"
    维度的判断来源被消费——此前它只被 claim_layer 的 GATE 消费，导致主循环与传导层像两条平行线；
    接上后，"AI 枚举 → 系统剪枝"才完整（剪枝含"世界规则"+"真实数据支持"两类依据）。
  - 叙事节点只叠加 _narrative 覆盖层，绝不改写原始指标；unless_narrative 例外保留罕见真实分支。
  - **每节点重新枚举的递归**（路线 2 核心动作③）：不再只在根节点枚举一次。每个节点都重跑
    六步循环（读状态→AI 枚举→世界规则过滤→走一步→比对→递归）。活叙事节点在子节点重枚举时
    把 _narrative 作为覆盖层传入，子节点据此重新推导候选。
  - 路线 4 三件接线（用户 2026-10-12 末段"全部做"；2026-10-13 停规则收敛后重定性）：
    ① 账本挂路径：rule_ledger 对"路径累积的激活叙事集 + 当前状态 when 命中"做跨规则加总/前提互斥。
       **冲突降级为标记（2026-10-13 用户裁定）**：`ledger_suspect` 只标「这里出现一次可疑加总」，
       不停路、候选照走（与 off_record 同类）；标记沿路径累积，子节点继承父节点全部标记。
    ② 边标记沿路径累积：每个节点的 mark 不是单步属性，而是从根到此路的 mark 序列累积 + 诚实概要。
    ③ 历史比对即停：143 状态序列载入，每步走一步后比对真实记录——撞上真实记录(hit_real_record)
       即停（不再无凭据外推）；落在历史包络之外(off_record)标推测但继续。
  - 停止条件（2026-10-13 收敛为**四条**，前三条客观事实 + 第四条外部锚定）：
      · 无存活候选（枚举为空/全被世界规则排除，事实）；
      · L3_visited（回到走过了的 (state, 叙事路径) 组合，事实；键含 accumulated path_narratives）；
      · hit_real_record（撞真实记录，事实）；
      · irrelevant_to_focus（impact==zero 且调用方给了 focus——「对某关注点影响为零」才算，外部锚定；
        focus=None 时无法判定，此条不触发）。
    **已移出停集、降为标记**：low_impact（程度不是停 → impact_priority 标记，继续展开、
    输出时不优先）、ledger_conflict（→ ledger_suspect 标记，真假由实际数据裁决，系统不现场判）。
    停≠砍：被停止的分支逐支留痕（pruned_moves / stop_reason），可回头深挖。

输出：世界树（先不带概率；概率为第六层，须带可审计来源，后续接入）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
CONFIG = os.path.join(BASE, "config")
sys.path.insert(0, TOOLS)

import hard_prune  # noqa: E402
import world_state  # noqa: E402
import constraints  # noqa: E402
import candidate_source  # noqa: E402  # 仅作常量库（NARRATIVE_POLARITY / NARRATIVE_CHAIN_DAYS 等），已非生成器
import ai_candidate  # noqa: E402  # 候选生成槽位：agent 在系统外部穷举后注入（不调用任何模型服务）
import feeds  # noqa: E402
import impact_layer  # noqa: E402
import rule_ledger  # noqa: E402
import macro_resources  # noqa: E402  # 宏观域真实预算/消耗字段（账本资源层）

RULE_LEDGER_CONFIG = os.path.join(CONFIG, "rule_ledger.json")
HISTORY_PATH = os.path.join(BASE, "reports", "history_states_cpi_ppi.json")

# 历史指纹用到的指标（LPR5Y 在 143 状态里全 nan，不参与比对）
_HIST_INDICATORS = ["CPI", "PPI", "M2", "LPR_1Y"]
# 方向候选可驱动的、且对应历史指标的状态键。
# CN10Y 已于 #232（2026-10-14）接入：state_step_model.json 含 CN10Y 日度真实转移分布，
# cn10y_feed 经 akshare bond_zh_us_rate 真实可读 → CN10Y 方向候选现能真实改状态（不再叶子）。
_MOVE_TARGET_TO_STATEKEY = {
    "CPI": "CPI", "PPI": "PPI", "M2": "M2",
    "LPR_1Y": "LPR_1Y", "LPR_5Y": "LPR_5Y",
    "CN10Y": "CN10Y",
    # 真实市场资产（2026-10-19 续作 #201）：接 akshare 真实历史序列建 step 模型，方向候选现能真实改状态
    "CN_CYB": "CN_CYB", "G_NASDAQ": "G_NASDAQ", "US_10Y": "US_10Y",
    # 汇率 / 跨境资本链（2026-10-19 Request B）：USDCNH 汇率 + 北向资金代理 + 沪深300 核心资产
    # 同样经 state_step_model 真实改状态；step 幅度为 seed 近似（PROVISIONAL_SEED_APPROX），待 akshare 校准。
    "USDCNH": "USDCNH", "CSI300": "CSI300", "NORTHBOUND": "NORTHBOUND",
    # 信用维度（2026-10-19 用户指令）：社融(信用脉冲) + M1-M2 剪刀差，均经真实月度 step 模型改状态
    "TSF": "TSF", "M1M2_GAP": "M1M2_GAP",
    # 财政维度终端资产（2026-10-19 用户指令）：财政扩张→基建/电力投资需求→电力板块上行；
    # step 模型已用 akshare sh000986 真实日线校准（config/state_step_model.json CN_POWER_IDX 块，mult/return_pct）。
    # 此前漏接映射 → _apply_move 在 skey=None 处静默 no-op → 财政链在 CN_POWER_IDX↑ 断流；现补齐。
    "CN_POWER_IDX": "CN_POWER_IDX",
    # policy_stance 为离散状态（偏松/中性/偏紧），步长事件驱动（非价格序列算法），
    # 逻辑在 _step_policy_stance，见用户 2026-10-19 裁定「不要硬套价格序列算法」。
    "policy_stance": "policy_stance",
    # 财政维度（2026-10-19 用户指令）：FISCAL_STANCE 离散政策状态（赤字率/积极财政，事件驱动），
    # 逻辑在 _step_fiscal_stance；SPECIAL_BOND 为机制节点、不作可步进状态（真实月度序列不可得，见 build_credit_fiscal_step.py）。
    "FISCAL_STANCE": "FISCAL_STANCE",
}
# 已知「非状态变量」方向目标 = 合法的 canon direction target，但刻意不写状态
# （结构性叙事/机制节点、或暂无真实 step 模型可校准的变量）。它们产出 no-op 子节点是
# **预期行为**，与「引擎漏接 wiring」必须区分。凡 canon 合法、既不在 _MOVE_TARGET_TO_STATEKEY
# 也不在此表的 target → _apply_move 抛错（见下方），迫使维护者显式二选一：补 step 模型 或 显式加入此表。
# 集合应随 canon.DIRECTION_TARGETS 演进同步维护；若新加 canon target 忘了归类，构建期会直接抛错暴露。
_STRUCTURAL_NOOP_TARGETS = {
    "US_CPI_YOY",      # 美国 CPI 同比：状态输入变量，非本系统可步进写出的目标
    "Commodity", "Equity",   # 市场维度占位，无独立状态键
    "SPECIAL_BOND",   # 专项债：机制节点，不作可步进状态（真实月度序列不可得）
    "G_SOX", "CN_AGRI_PRICE_IDX", "CN_AI_IDX", "CN_HS300", "CN_KCB50",
    "ENSO", "CLIMATE", "AG_PRICE",   # 环境/市场节点，无 step 模型
}
# 状态转移：从历史读出的「一步」delta（世界给的，非人写系数）。
# 生成见 tools/build_state_step_model.py；数据来自 reports/history_states_cpi_ppi.json。
# 取代原 _LPR_STEP 这个凭空编的对称 stand-in（历史证伪：LPR 只降不升）。
_STEP_MODEL_PATH = os.path.join(BASE, "config", "state_step_model.json")


def _load_step_model():
    try:
        with open(_STEP_MODEL_PATH, encoding="utf-8") as f:
            return json.load(f).get("model", {})
    except Exception:
        return {}


_STATE_STEP_MODEL = _load_step_model()

# ─────────────────────────────────────────────────────────────────────────────
# 类别化决策窗口（2026-10-19，用户裁定）：拆解 10 天硬顶，按传导链类别分配窗口。
# 真实宏观传导多为月度/季度节律（利率定价日、CPI 发布、全球迟滞），10 天硬顶会把跨周链误杀。
#   · 政策/利率/价格链：30 天（月度发布 + 全球传导迟滞）
#   · 权益/情绪/汇率链：15 天（反应快但可跨周）
#   · 气候/环境链：60 天（季节尺度）
# 兜底：未列入此表的指标仍走 _STATE_STEP_MODEL.step_days，再兜底 DECISIVE_WINDOW_DAYS=10。
# ─────────────────────────────────────────────────────────────────────────────
_TARGET_WINDOW_DAYS = {
    # 政策 / 利率链（月度定价 + 全球迟滞）
    "policy_stance": 30, "LPR_1Y": 30, "LPR_5Y": 30, "CN10Y": 30,
    "US_10Y": 30, "US_CPI_YOY": 30, "M2": 30,
    # 价格 / 商品链（月度发布）
    "CPI": 30, "PPI": 30, "Commodity": 30, "AG_PRICE": 30,
    "CN_AGRI_PRICE_IDX": 30, "CN_POWER_IDX": 30, "CN_AI_IDX": 30,
    # 权益 / 情绪 / 汇率链（快但可跨周）
    "Equity": 15,     "CN_CYB": 15, "CN_KCB50": 15, "CN_HS300": 15, "CSI300": 15,
    "G_NASDAQ": 15, "G_SOX": 15,
    # USDCNH 汇率链属「利率利差→汇率」过程，月度节律（与 policy/rate 链同口径 30d）；
    # 原 15d 会把 USDCNH up(30d) 误杀 window_fit，故 Request B 起改为 30d。
    "USDCNH": 30, "NORTHBOUND": 15,
    # 信用 / 财政维度（2026-10-19 用户指令）：月度发布节律，与政策/利率链同口径 30d
    "TSF": 30, "M1M2_GAP": 30, "FISCAL_STANCE": 30,
    # 气候 / 环境链（季节尺度）
    "ENSO": 60, "CLIMATE": 60,
}

# ─────────────────────────────────────────────────────────────────────────────
# 叙事类别化决策窗口（2026-10-19「解禁窗口」手术收尾）：上一轮只给 direction 目标建了
# _TARGET_WINDOW_DAYS，叙事候选无 target，一律落回 10 天硬顶被 window_fit 误杀
# （el_nino 60d / policy_easing 30d / demand_weak 30d 全被砍）。现按叙事类别补窗口，
# 与 direction 同口径：气候/环境叙事走季节尺度 60d，政策/通胀/鸽派叙事走月度 30d。
# 原则不变：未列入此表的叙事仍走兜底 DECISIVE_WINDOW_DAYS=10（不扩大兜底）。
# ─────────────────────────────────────────────────────────────────────────────
_NARRATIVE_WINDOW_DAYS = {
    # 气候 / 环境类叙事（季节尺度，同 ENSO/CLIMATE 链）
    "el_nino": 60, "la_nina": 60, "agri_price_shock": 60, "climate_shock": 60,
    # 政策 / 通胀类叙事（月度节律）
    "inflation_concern": 30, "cost_push": 30, "credit_expansion": 30,
    # 信用收缩叙事（2026-10-19 用户指令：社融/剪刀差走弱；chain_days=30，避免落回 10 天硬顶被误杀）
    "credit_contraction": 30, "fiscal_contraction": 30,
    "fiscal_expansion": 30, "fiscal_stress": 30, "policy_easing_expectation": 30,
    "demand_weak": 30, "policy_liquidity_easing": 30, "fiscal_supply_pressure": 30,
    "external_rate_pressure": 30, "supply_chain_risk": 30,
    # 汇率链叙事（2026-10-19 Request B）：利差倒挂→贬值压力(30d) 已列入；北向流出为快变量(15d)
    "northbound_outflow": 15,
}


# ---------------------------------------------------------------------------
# 传导层（transmission_graph）作为"真实数据支持"剪枝依据（2026-10-15 架构纠正）。
# 与 prune_rules.json 同类：外置、可审计；主循环枚举出市场传导候选时在此查真实联动强度。
# 仅覆盖"市场变量之间"的传导（纳指/A股/美债/创业板…），非市场传导（气候→农产品）超出本图，须其它数据源。
# ---------------------------------------------------------------------------

def _load_transmission_graph(path=None, prune_weak=False):
    """读取传导图（config/transmission_graph.json）作为"真实数据支持"剪枝依据。

    与 prune_rules.json 同类：外置、可审计。失败/文件缺失返回 None（诚实降级：不消费、不影响其它剪枝维度）。
    prune_weak=True 时，真实联动弱的跨境候选在剪枝阶段被直接剪（默认 False：标低支持、保留为审计叶）。
    """
    if path is None:
        path = os.path.join(CONFIG, "transmission_graph.json")
    try:
        with open(path, encoding="utf-8") as f:
            g = json.load(f)
    except Exception:
        return None
    g["_index"] = _build_tgraph_index(g)
    g["_prune_weak"] = bool(prune_weak)
    return g


def _build_tgraph_index(tgraph):
    """建查找索引：data_confirmed / hypothesized 边双向存档；cross_market_probe 双向存档。"""
    dc, hyp, probe = {}, {}, {}
    nodes = set(tgraph.get("nodes", []))
    for e in tgraph.get("edges", []):
        if e.get("kind") == "data_confirmed":
            dc[(e["from"], e["to"])] = e
            dc[(e["to"], e["from"])] = e
        elif e.get("kind") == "hypothesized":
            hyp[(e["from"], e["to"])] = e
            hyp[(e["to"], e["from"])] = e
    for p in tgraph.get("meta", {}).get("cross_market_probe", []):
        probe[(p["from"], p["to"])] = p
        probe[(p["to"], p["from"])] = p
    return {"dc": dc, "hyp": hyp, "probe": probe, "nodes": nodes}


def _transmission_support(cand, tgraph):
    """判一条"X → Y"市场传导候选有没有真实数据支持。返回 {"level","mark","detail","corr"}。

    level ∈ {data_confirmed, hypothesized, weak, none, malformed}：
      - data_confirmed：传导图有真实历史联动边（|corr|≥阈值）→ 有据(真实联动)
      - hypothesized ：有文献机制边但待数据确认 → 推测(机制待数据确认)
      - weak         ：落在 cross_market_probe（跨境日收益联动弱，低于落边阈值）→ 推测(真实联动弱)
      - none         ：传导图未覆盖（如非市场传导：气候→农产品）或两节点均在图内却无联动边 → 无数据支撑
    注意：本判断只覆盖"市场变量之间"的传导；非市场传导超出本图，须其它数据源，系统不在此剪枝。
    """
    idx = tgraph.get("_index", {})
    f, t = cand.get("from"), cand.get("to")
    if not f or not t:
        return {"level": "malformed", "mark": "无效传输候选",
                "detail": "传导候选缺 from/to", "corr": None}
    dc, hyp, probe, nodes = idx.get("dc", {}), idx.get("hyp", {}), idx.get("probe", {}), idx.get("nodes", set())
    if (f, t) in dc:
        e = dc[(f, t)]
        best = max(abs(e.get("corr_same", 0) or 0), abs(e.get("corr_lead1", 0) or 0))
        return {"level": "data_confirmed", "mark": "有据(真实联动)",
                "detail": "传导图 data_confirmed 边 %s→%s 同频|corr|=%.2f/领先1日=%.2f(n=%d)，真实历史联动支持该传导"
                          % (e["from"], e["to"], e.get("corr_same", 0) or 0,
                             e.get("corr_lead1", 0) or 0, e.get("n_obs", 0)),
                "corr": best}
    if (f, t) in hyp:
        e = hyp[(f, t)]
        return {"level": "hypothesized", "mark": "推测(机制待数据确认)",
                "detail": "传导图 hypothesized 边 %s→%s（%s）：机制推演存在但尚无真实历史联动确认(needs_data_confirmation)"
                          % (e["from"], e["to"], e.get("mechanism", "")),
                "corr": None}
    if (f, t) in probe:
        p = probe[(f, t)]
        best = max(abs(p.get("corr_same", 0) or 0), abs(p.get("corr_lead1", 0) or 0))
        return {"level": "weak", "mark": "推测(真实联动弱)",
                "detail": "传导图 cross_market_probe %s↔%s 同频=%.2f/领先1日=%.2f(n=%d)，均低于落边阈值；"
                          "该跨境日收益联动弱，流行叙事不被真实数据支持"
                          % (p["from"], p["to"], p.get("corr_same", 0) or 0,
                             p.get("corr_lead1", 0) or 0, p.get("n_obs", 0)),
                "corr": best}
    miss = [x for x in (f, t) if x not in nodes]
    if miss:
        return {"level": "none", "mark": "无数据支撑(传导图未覆盖)",
                "detail": "传导图节点集不含 %s；该传导超出市场变量联动图覆盖（如非市场传导：气候→农产品），"
                          "传导图无法判定，需其它数据源" % "/".join(miss),
                "corr": None}
    return {"level": "none", "mark": "无数据支撑(无联动边)",
            "detail": "%s 与 %s 均在传导图节点集内但无 data_confirmed 边亦无跨境探针记录，真实联动未被确认" % (f, t),
            "corr": None}


_MAG_TIER = {"weak": "p25", "typical": "p50", "strong": "p75"}


def _state_step_delta(statekey, sign, magnitude="typical"):
    """(statekey, sign, magnitude) → 这一步应加到状态值的 delta；历史无该方向返回 None。

    - 决定一 B：幅度分档。magnitude ∈ {weak(=p25), typical(=p50), strong(=p75)}，缺省 typical；
      引擎按历史分布的三档取值，仍是「读数不是拍数」。
    - 历史从未观测到该方向（且未显式硬剪枝，如某指标无 up 档）→ 返回 None → 不动状态；
      该节点因状态不变被 visited 兜底成叶（no-op + dedup，非规则剪枝）。
      注：LPR 上调已显式由 PRUNE-ABS-LPR1Y/5Y 硬剪枝拒绝，不会走到这里。
    - 其他 sign → 0.0，不改状态。
    """
    m = _STATE_STEP_MODEL.get(statekey)
    if not m:
        return None
    tier = m.get(sign)          # up/down → 三档字典 或 None
    if tier is None:
        return None
    tkey = _MAG_TIER.get(magnitude, "p50")
    return tier.get(tkey)
    return 0.0


def _summarize(state):
    """把节点状态压成可读摘要（用于树输出）。"""
    s = {}
    for k, v in state.items():
        if k in ("_narrative", "_transmission"):
            continue
        elif isinstance(v, dict) and "value" in v:
            s[k] = v.get("value")
        else:
            s[k] = v
    return s


def _state_key(state, path_narratives=None):
    """节点指纹（用于 L3 visited 判重）。

    键 = 状态指标值 + 节点级 _narrative 覆盖层 + **路径累积叙事集**。
    承接 #1 分层假设（用户 2026-10-13 选定方案 A：生成 state-primary，但剪枝/影响已路径感知）：
    同状态不同叙事路径会产生不同存活子树，故 visited 必须按 (state, 叙事集) 判重，
    否则 B 路径 revisit 状态 S 时，会吞掉其独有的路径感知剪枝结果（旧键只认 state+_narrative，
    漏掉 accumulated path_narratives 的差异）。
    path_marks 不进键：它只进诚实概要/输出，不进 hard_prune/constraints，且可能无界。
    """
    parts = []
    for k in sorted(state.keys()):
        if k == "_narrative":
            parts.append("narr=%s" % state[k])
        else:
            v = state[k]
            val = v.get("value") if isinstance(v, dict) else v
            parts.append("%s=%s" % (k, val))
    if path_narratives:
        parts.append("pnarr=%s" % ",".join(sorted(path_narratives)))
    return "|".join(parts)


def _copy_state(state):
    """深拷贝状态（值对象为 dict），避免子节点改动污染父节点。"""
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in state.items()}


# policy_stance 离散状态机（用户 2026-10-19 裁定：不能用价格序列算法硬套）。
# 状态 = 偏松/中性/偏紧，步长由事件驱动：tighten(=up) 逐档收紧，easing/loosen(=down) 逐档放松，钳制在两端。
_POLICY_STANCE_ORDER = {"偏松": 0, "中性": 1, "偏紧": 2}
_POLICY_STANCE_REV = {0: "偏松", 1: "中性", 2: "偏紧"}


def _step_policy_stance(cur_dict, sign):
    """policy_stance 离散事件驱动步长。非连续 delta，故不读 _STATE_STEP_MODEL。

    当前值非三态之一（unknown/invalid，如 LPR 不可得推导失败时）→ 不编造，原样返回（诚实 no-op）。
    """
    if not isinstance(cur_dict, dict):
        return cur_dict
    cur = cur_dict.get("value")
    if cur not in _POLICY_STANCE_ORDER:
        return cur_dict  # unknown / 非三态 → 不编造
    idx = _POLICY_STANCE_ORDER[cur]
    if sign in ("up", "tighten"):
        idx = min(2, idx + 1)
    elif sign in ("down", "easing", "loosen"):
        idx = max(0, idx - 1)
    else:
        return cur_dict
    new = dict(cur_dict)
    new["value"] = _POLICY_STANCE_REV[idx]
    new["kind"] = "inferred"  # 标记为推断值（区别于世界给的 interpreted）
    return new


# FISCAL_STANCE 离散状态机（2026-10-19 用户指令）：与 policy_stance 同法（事件驱动、非价格序列算法）。
# 状态 = 收缩/中性/扩张，步长由事件驱动：expansion(=up) 逐档扩张，contraction(=down) 逐档收缩，钳制在两端。
_FISCAL_STANCE_ORDER = {"收缩": 0, "中性": 1, "扩张": 2}
_FISCAL_STANCE_REV = {0: "收缩", 1: "中性", 2: "扩张"}


def _step_fiscal_stance(cur_dict, sign):
    """FISCAL_STANCE 离散事件驱动步长。非连续 delta，故不读 _STATE_STEP_MODEL。

    当前值非三态之一 → 不编造，原样返回（诚实 no-op）。
    """
    if not isinstance(cur_dict, dict):
        return cur_dict
    cur = cur_dict.get("value")
    if cur not in _FISCAL_STANCE_ORDER:
        return cur_dict  # unknown / 非三态 → 不编造
    idx = _FISCAL_STANCE_ORDER[cur]
    if sign in ("up", "expansion"):
        idx = min(2, idx + 1)
    elif sign in ("down", "contraction"):
        idx = max(0, idx - 1)
    else:
        return cur_dict
    new = dict(cur_dict)
    new["value"] = _FISCAL_STANCE_REV[idx]
    new["kind"] = "inferred"
    return new


def _apply_move(state, cand):
    """走一步：方向候选按历史 delta 微调对应指标（让历史比对有意义）；
    叙事候选叠加覆盖层。返回新状态。

    delta 来源 = 世界给的历史转移分布（config/state_step_model.json），非人写系数。
    历史从未观测到、且未被显式硬剪枝的方向 → 不动状态，由 visited 兜底成叶（no-op + dedup，非规则剪枝）。
    （LPR 上调已由 PRUNE-ABS-LPR1Y/5Y 显式拒绝，不靠此隐式兜底。）
    """
    ns = _copy_state(state)
    if cand.get("action") == "transmission":
        # 传导候选是"市场变量间结构声明"，不改宏观状态；仅打标以便审计与 visited 区分（每对传导一个独立叶节点）
        ns["_transmission"] = {"value": "%s->%s" % (cand.get("from"), cand.get("to")),
                               "kind": "transmission_claim"}
        return ns
    if cand.get("action") == "narrative":
        # 叙事只叠加 _narrative 覆盖层，绝不改写任何指标状态（用户 2026-10-17 裁定：
        # 叙事不改 state，收敛锚在方向层；真正的 state 变化由 direction 候选完成）。
        # 若某叙事隐含方向效应（如「宽松预期 → LPR 下调」），应由 agent 显式产出
        # direction 候选（target=LPR_1Y, sign=down）来表达，而非在叙事层偷偷改状态。
        ns["_narrative"] = cand["narrative"]
        return ns
    if cand.get("action") == "direction":
        tgt = cand.get("target")
        skey = _MOVE_TARGET_TO_STATEKEY.get(tgt)
        if not skey:
            # 诚实性（2026-10-19 用户裁定「未识别抛错而非静默丢弃」）：direction 候选的 target
            # 必须「要么能写状态、要么显式声明为非状态变量」。两者都不是 = 引擎接线缺口
            # —— 上一轮漏接 CN_POWER_IDX 正是此处静默 no-op 导致财政链无声断流。
            # 不得静默丢弃：抛错让缺口在构建期暴露，而非在下游树里无声死亡且不可观测。
            if tgt in _STRUCTURAL_NOOP_TARGETS:
                return ns  # 已知非状态变量：不产生状态变化是预期行为（结构性 no-op）
            raise RuntimeError(
                "direction target %r 未被引擎接线：既不在 _MOVE_TARGET_TO_STATEKEY（无 step/离散模型可写状态），"
                "也不在 _STRUCTURAL_NOOP_TARGETS（已知非状态变量）。这是引擎接线缺口，必须二选一："
                "补 _MOVE_TARGET_TO_STATEKEY 的 step 模型，或显式加入 _STRUCTURAL_NOOP_TARGETS；"
                "不能静默 no-op。" % (tgt,))
        # policy_stance：离散状态（偏松/中性/偏紧），步长事件驱动（tighten/easing），
        # 不读连续转移分布，单独走 _step_policy_stance（用户 2026-10-19 裁定）。
        if skey == "policy_stance":
            ns["policy_stance"] = _step_policy_stance(ns.get("policy_stance"), cand.get("sign"))
            return ns
        # FISCAL_STANCE：离散政策状态（收缩/中性/扩张），与 policy_stance 同法（2026-10-19 用户指令）；
        # 赤字率/积极财政属年度决策，事件驱动跳变，不读连续转移分布。
        if skey == "FISCAL_STANCE":
            ns["FISCAL_STANCE"] = _step_fiscal_stance(ns.get("FISCAL_STANCE"), cand.get("sign"))
            return ns
        if isinstance(ns.get(skey), dict) and ns[skey].get("value") is not None:
            try:
                cur = float(ns[skey]["value"])
            except (TypeError, ValueError):
                return ns
            delta = _state_step_delta(skey, cand.get("sign"), cand.get("magnitude", "typical"))
            if delta is None:
                return ns  # 历史无此方向且未显式硬剪枝 → 不动状态；该候选在 _grow 枚举层经 no-op 检测直接丢弃（不再建 L3_visited 子节点，省算力）；LPR 上调已由 PRUNE-ABS-LPR* 显式拒绝
            # 施加模式：指数类（CN_CYB/G_NASDAQ 等）delta 为日收益率%，需乘性施加（new=cur*(1+delta/100)）；
            # 收益率/价格类（US_10Y/CN10Y 等）delta 为加性单位（pp/百分点），加性施加（new=cur+delta）。
            apply_mode = (_STATE_STEP_MODEL.get(skey, {}) or {}).get("apply", "add")
            if apply_mode == "mult":
                new = cur * (1.0 + delta / 100.0)
            else:
                new = cur + delta
            if skey.startswith("LPR"):
                new = max(macro_resources.LPR_FLOOR, new)
            ns[skey] = dict(ns[skey])
            ns[skey]["value"] = round(new, 4)
            ns[skey]["kind"] = "inferred"  # 标记为推断值（区别于世界给的 raw）
    return ns


def _flat_state(state):
    """扁平状态（metric->value），供 rule_ledger 的 when 匹配与历史指纹使用。"""
    flat = {}
    for k, v in state.items():
        if k in ("_narrative", "_transmission"):
            continue
        flat[k] = v.get("value") if isinstance(v, dict) else v
    return flat


def _history_fp(state):
    """历史比对指纹（仅用真实历史有的指标，2 位精度）。"""
    flat = _flat_state(state)
    return tuple(round(flat.get(k), 2) if flat.get(k) is not None else None
                 for k in _HIST_INDICATORS)


def load_history(path=HISTORY_PATH):
    """载入 143 状态序列，构建指纹集 + 指标包络。失败返回 None（诚实降级）。"""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    states = d.get("states", [])
    fps = set()
    months = {}
    env = {k: [None, None] for k in _HIST_INDICATORS}
    keymap = {"CPI": "CPI_YOY", "PPI": "PPI_YOY", "M2": "M2_YOY", "LPR_1Y": "LPR1Y"}
    for s in states:
        row = {}
        ok = True
        for sk, hk in keymap.items():
            val = s.get(hk)
            if val is None or (isinstance(val, float) and val != val):
                ok = False
                break
            row[sk] = float(val)
            lo, hi = env[sk]
            env[sk][0] = row[sk] if lo is None else min(lo, row[sk])
            env[sk][1] = row[sk] if hi is None else max(hi, row[sk])
        if not ok:
            continue
        fp = tuple(round(row[k], 2) for k in _HIST_INDICATORS)
        fps.add(fp)
        months.setdefault(fp, []).append(s.get("month"))
    return {"fingerprints": fps, "months": months, "envelope": env, "n": len(fps)}


def _off_record(state, history):
    """任一指标落在历史包络之外 = 无据外推（推测）。"""
    if not history:
        return False
    flat = _flat_state(state)
    env = history["envelope"]
    for k in _HIST_INDICATORS:
        v = flat.get(k)
        if v is None:
            continue
        lo, hi = env[k]
        if lo is None or hi is None:
            continue
        if v < lo - 1e-9 or v > hi + 1e-9:
            return True
    return False


def _mark_edge(cand, pending_set, cons):
    """边标记：历经硬剪枝+约束仍存活 = 有据；落在 pending_feed 约束否决清单内(源缺失) = 推测(约束源缺失)。"""
    if not pending_set:
        return "有据"
    for cons_rule in cons.get("constraints", []):
        if cons_rule["id"] not in pending_set:
            continue
        v = cons_rule.get("vetoes", {})
        if cand.get("action") == "narrative":
            if cand.get("narrative") in v.get("narratives", []):
                return "推测(约束源缺失)"
        elif cand.get("action") == "direction":
            for d in v.get("directions", []):
                if d.get("target") == cand.get("target") and (
                        d.get("sign") is None or str(d.get("sign")).lower() == str(cand.get("sign", "")).lower()):
                    return "推测(约束源缺失)"
    return "有据"


def _summarize_path_marks(path_marks):
    """沿路径累积的边标记诚实概要。"""
    if not path_marks:
        return "根(无标记)"
    all_solid = all(m.startswith("有据") for m in path_marks)
    any_spec = any(m.startswith("推测") for m in path_marks)
    if all_solid:
        return "全有据"
    if any_spec:
        return "含推测"
    return "混合"


def _ledger_active_at(state, path_narratives, ledger_rules):
    """计算当前节点路径上的激活联动规则集：when 命中状态 ∪ 触发叙事命中。"""
    flat = _flat_state(state)
    active = []
    seen = set()
    for r in ledger_rules:
        rid = r["id"]
        if rid in seen:
            continue
        hit = rule_ledger.match_when(r.get("when"), flat)
        trig = r.get("trigger_narrative") in (path_narratives or set())
        if hit or trig:
            active.append(r)
            seen.add(rid)
    return active


def _dedupe_opposite_readings_dict(cands, state):
    """系统侧剪枝——已废止（用户裁定 2026-10-19）。

    原逻辑：同节点候选含 dovish+hawkish 叙事时，按 CPI≥2.5 阈值整侧砍掉输方
    （keep = "hawkish" if cpi>=2.5 else "dovish"）。

    裁定：dovish（如降息预期 / demand_weak）与 hawkish（如食品冲击 / agri_price_shock）
    是**不同信号**的解读，并非同一信号的两解，完全可以并存。把它们当"同一信号两解"
    整侧封杀是范畴错误——低 CPI 下厄尔尼诺食品冲击恰是前瞻尾部风险，分析师正要同时
    持有"降息空间"与"食品扰动"这对张力。

    正确做法是让两者同时入树，由**状态推演**自然收敛：食品冲击抬升 CPI 状态 →
    CPI 升高再触发政策收紧。门口整侧砍"不顺眼"的叙事，等于架空了状态推演。

    故本函数改为直通：不做 polarity/CPI 整侧剔除（2.5 阈值也一并废止，因为改成任何
    值都是武断的）。真正需要去重的"同一信号相反解读"（如同一 target 同时 up 又 down）
    由 _grow 的 semantic_key 收敛负责，不在此处按叙事 polarity 误杀。
    """
    return cands


def _system_candidate_guards(raw, state, active_narratives):
    """系统侧候选过滤器（承接架构纠正：AI 全量枚举 → 系统剪枝）。

    原 candidate_source 在生成端做了两处其实是「剪枝」的逻辑，现移至此处（系统侧）：
      (a) 去重已激活叙事：AI 可能重复提出路径上已存在的叙事，系统侧剔除（防自递归/冗余）。
      (b) policy_space 前置：LPR 贴净息差下限（policy_space<=0）时不生成宽松叙事——状态不可能。
      (c) 相反解读互斥：dovish/hawkish 同节点互斥（一条信号不能两解）。
    硬剪枝/约束/停规则/路径累积/账本均不在此处改动。

    原则（2026-10-19 用户裁定，显式钉死）：**方向不因「绝对值已高」被拒**。
      系统必须区分「绝对值高」与「方向仍在持续」——CPI 已是 1.2（高）不等于不能再
      提 CPI up（仍在涨）。故本过滤器**绝不可**按 target 当前数值高低丢弃方向候选；
      方向收敛只由 _grow 追问循环的 semantic_key 去重（同 target/sign/magnitude 重复才 added==0）
      驱动，而非按状态饱和提前掐断。任何「因 CPI>=X 就拒 CPI up」式的饱和逻辑都违反此原则。
    """
    # (a) 去重已激活叙事（用全路径集，比原单节点 _narrative 更严格，杜绝任意位置重激活）
    if active_narratives:
        active = set(active_narratives)
        raw = [c for c in raw
               if not (c.get("action") == "narrative" and c.get("narrative") in active)]
    # (b) policy_space 前置条件
    ps = macro_resources.policy_space_of(state)
    raw = [c for c in raw
           if not (c.get("action") == "narrative"
                   and c.get("narrative") == "policy_easing_expectation"
                   and ps <= 1e-9)]
    # (c) 相反解读互斥——已废止（用户裁定 2026-10-19）：dovish/hawkish 是不同信号的解读，
    #     可并存，不再按 polarity+CPI 整侧封杀。_dedupe_opposite_readings_dict 现直通。
    raw = _dedupe_opposite_readings_dict(raw, state)
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# _semantic_key：把候选归一到「语义类别」，供追问循环判定 added==0（"还有没有新候选"）。
#
# 收敛锚（用户裁定 2026-10-17，务必遵守）：
#   · 方向层才是收敛锚：direction 的 (target, sign, magnitude) 是有界宇宙
#     （指标 × up/down × weak/typical/strong），故 added==0 必然可达、不靠 AI 说"我说完了"。
#   · 叙事层不参与收敛：narrative / narrative_retire 只是路径上的语义标签，
#     永不计入 added——即使 AI 无限发明新叙事（干旱→橡胶减产 / 橡胶供应紧张 / 橡胶供给收缩…），
#     也不让追问循环空转。多个叙事若最终指向同一方向，在收敛判定里被抹平为同一个。
#     （叙事自身的去重只按 id 做「避免重复子节点」，见 _grow 追问循环，不走 _semantic_key。）
#
# 设计边界：
#   1. 只基于生产 contract 的规范字段做确定性归一；绝不引入 embedding / 模型
#      （系统不内置 LLM，AI 在系统外）。
#   2. 输入假设已由 canon.normalize_id 在 _normalize 入站处强制：
#      direction.target / transmission.from,to 走有界宇宙 + 范围外拒收；narrative 仅别名归一、开放透传。
#      AI 不守约的后果被「入站强制 + 别名安全网」兜住（见 canon.py；用户 2026-10-17 落地）。
#   3. granularity（哪些字段算"身份"）是人选的语义切分，非世界规则：
#        - direction.magnitude   算身份（B 方案：weak/typical/strong 各是独立分支，不得归一）
#        - transmission.sign     算身份（up/down 是相反方向，不得归一）  ← 2026-10-17 修 bug
#        - direction.target      是 state 变量（CPI/PPI/M2/LPR/CN10Y…），本就是 state 锚定
#        - narrative            不算身份（不进收敛；只作 path_narratives 标记）  ← 2026-10-17 裁定
#        - chain_days           不算身份（只决定边长度，不决定是否分岔）
#   4. 未知 action 退化为完整字典（_json.dumps），宁可多留、不可误并。
# ─────────────────────────────────────────────────────────────────────────────
def _norm(v):
    return v if v is not None else "__NONE__"


def _semantic_key(c):
    action = c.get("action")
    if action == "direction":
        return ("dir", _norm(c.get("target")), _norm(c.get("sign")), _norm(c.get("magnitude")))
    if action == "narrative":
        return ("nar", _norm(c.get("narrative")), _norm(c.get("polarity")))
    if action == "narrative_retire":
        return ("retire", _norm(c.get("narrative")))
    if action == "transmission":
        return ("trans", _norm(c.get("from")), _norm(c.get("to")), _norm(c.get("sign")))
    import json as _json
    return ("raw", _json.dumps(c, sort_keys=True, ensure_ascii=False))


def _grow(node, depth, max_depth, visited, prune_rules, cons, allow_pending=False,
          stop_on_narratives=None, real_narratives=None, events=None,
          ledger_rules=None, ledger_resources=None, history=None,
          path_narratives=None, parent_path_marks=None, focus=None,
          parent_path_suspects=None, ctx=None, raw_override=None, tgraph=None,
          max_probe_rounds=None):
    """每节点重枚举的递归（含路线 4 三件接线）。

    模型 A 设计统一入口（steppable Model C 实装：session 模式传 ctx，single_node=True 只展开本节点、不递归；
    build_world_tree 模式不传 ctx（全递归）。两者共用同一套世界规则逻辑。

    raw_override：真 Model A 接口专用。若由 agent 每轮显式传入候选列表（expand_node(sid, nid, candidates)），
    则跳过内部 ai_candidate.generate（候选生成搬到 agent 侧），直接用传入候选走系统后处理。
    未传（None）则保持 steppable Model C 行为（内部生成器产候选）。
    """
    # ctx 归一：session 模式直接用传入 ctx；build 模式由参数现场构建。
    if ctx is None:
        ctx = {
            "visited": visited, "prune_rules": prune_rules, "cons": cons,
            "allow_pending": allow_pending, "stop_on_narratives": stop_on_narratives,
            "real_narratives": real_narratives, "events": events,
            "ledger_rules": ledger_rules, "ledger_resources": ledger_resources,
            "history": history, "focus": focus, "max_depth": max_depth,
            "as_of": None, "decision_window_days": None,
            "max_probe_rounds": max_probe_rounds,
            "tgraph": tgraph,
        }
    visited = ctx["visited"]; prune_rules = ctx["prune_rules"]; cons = ctx["cons"]
    tgraph = ctx.get("tgraph")
    allow_pending = ctx["allow_pending"]; stop_on_narratives = ctx["stop_on_narratives"]
    real_narratives = ctx["real_narratives"]; events = ctx["events"]
    ledger_rules = ctx["ledger_rules"]; ledger_resources = ctx["ledger_resources"]
    history = ctx["history"]; focus = ctx["focus"]; max_depth = ctx["max_depth"]
    state = node["state"]
    path_narratives = path_narratives or set()
    parent_path_marks = parent_path_marks or []

    # ---- 路线4①：账本挂路径（激活规则集提前算；冲突判定延后到枚举后，且降级为标记）----
    node["ledger_active"] = []
    node["ledger_suspect"] = list(parent_path_suspects or [])  # 沿路径累积（与 path_marks 并列）
    _ledger_active_set = []
    if ledger_rules:
        _ledger_active_set = _ledger_active_at(state, path_narratives, ledger_rules)
        node["ledger_active"] = [r["id"] for r in _ledger_active_set]

    narrative_layer = state.get("_narrative")
    # ---- 展开（expand）= **AI 穷举**（3a 层1：系统内部追问循环 + seen 回传已枚举候选）----
    # 层1：注册式生成器（模型 C / build）下，系统向 AI 多轮追问——每轮把已枚举候选 seen 回传，
    #   AI 只返回尚未想到的新候选，系统按 **语义类别**(_semantic_key) 去重累积，直到"无新增语义类别"(added==0)。
    #   去重是语义级而非字典级：CPI up weak 与 CPI up strong 是不同类别（magnitude 算身份，B 方案）；
    #   干旱→橡胶 up 与 干旱→橡胶 down 是不同类别（transmission 的 sign 算身份，2026-10-17 修 bug）。
    #   cap（max_probe_rounds）仅作 opt-in 安全阀：default=None → 只靠 added==0 收敛；显式传正整数才生效，
    #   且命中时节点标 truncated_by_cap（诚实暴露"枚举被截断、未收敛"，不假装问完了）。
    # 层2（真 Model A）：agent 每轮显式传 candidates（raw_override），跳过内部追问（agent 自行枚举/追问）。
    max_probe = (ctx or {}).get("max_probe_rounds", max_probe_rounds)
    seen = []
    seen_keys = set()        # 方向/传导：语义类别去重（state 锚定、有界 → 驱动 added==0 收敛）
    seen_narr_ids = set()    # 叙事：仅按 id 去重避免重复子节点；**不参与 added**（收敛锚在方向层）
    raw = []
    probe_rounds = 0
    last_added = 0
    # 收敛锚 = 方向候选的 (target, sign, magnitude)：有界（指标 × up/down × weak/typical/strong），
    # 故 added==0 必然可达。叙事是路径上的语义标签，永不计入 added——叙事即便无限，也不让追问循环空转；
    # 多个叙事若最终指向同一方向，在收敛判定里被抹平为同一个（用户 2026-10-17 裁定）。
    while max_probe is None or probe_rounds < max_probe:
        if raw_override is not None:
            # 真 Model A 接口：agent 显式传入候选。经与 build 路径一致的 _normalize，
            # 使动词语汇(tighten/easing)解禁、范围外 target 拒收在两条路径行为一致
            # （否则 serve 路径会绕过 _normalize，丢失 sign 别名/范围外校验）。
            raw = [c for c in (ai_candidate._normalize(c) for c in (raw_override or [])) if c]
            break
        round_cands = ai_candidate.generate(
            state, narrative_layer=path_narratives,
            node_context=_node_context(node, ctx),
            seen=seen)
        added = 0
        for c in (round_cands or []):
            act = c.get("action")
            if act in ("narrative", "narrative_retire"):
                # 叙事不参与 added==0：仅按 id 去重，避免同一节点挂重复叙事子节点；
                # 其分岔在收敛判定里被抹平（同指向一个方向的多个叙事 = 同一个）。
                nid = c.get("narrative")
                if nid is not None and nid not in seen_narr_ids:
                    seen_narr_ids.add(nid)
                    seen.append(c)
                    raw.append(c)
                continue
            k = _semantic_key(c)   # direction / transmission / 其他：state 锚定语义类别
            if k not in seen_keys:  # 同语义类别不重复入 raw、不重复回传 seen
                seen_keys.add(k)
                seen.append(c)
                raw.append(c)
                added += 1
        last_added = added
        probe_rounds += 1
        if added == 0:             # 无新方向/传导语义类别 → 停止（事实触发，符合「停只由事实」）
            break
    # 安全阀诚实标注：因 cap 命中而退出、且仍有新候选未穷尽 → 标记截断（非静默当收敛）
    if max_probe is not None and probe_rounds >= max_probe and last_added > 0:
        node["truncated_by_cap"] = True
        node["truncated_cap_rounds"] = max_probe
    # ---- ② 叙事过期（模型 A 契约下）：提取 agent 注入函数每轮被调用时发出的「叙事退休」信号（narrative_retire）----
    # agent 注入的函数每轮被调用时看着 state + path_narratives + node_context.narratives（valid_while 世界规则）
    # 判断某路径叙事是否仍成立，不成立则发 narrative_retire；系统机械地把它从「向下传递的
    # path_narratives」中移除。当前节点自身的 path_narratives 不动（X 在 N 创建时仍成立），
    # 仅其子节点不再继承 X。path_narratives 由此从「只增不减」变为「可 shrinking」。
    # 注意：retire 不是「下一步候选」，不进剪枝/约束/走步流程，故在 guards 之前单独提取。
    retired = set()
    _surv = []
    for _c in raw:
        if _c.get("action") == "narrative_retire" and _c.get("narrative"):
            retired.add(_c["narrative"])
        else:
            _surv.append(_c)
    raw = _surv
    _agent_gave = len(raw)   # agent 产出的候选步数（不含 narrative_retire 信号）
    effective_path_narratives = set(path_narratives) - retired
    # ---- 系统侧候选过滤（把原生成端的两处剪枝逻辑搬回系统侧；硬剪枝/约束/停规则不动）----
    raw = _system_candidate_guards(raw, state, path_narratives)
    # 真实源激活的叙事作为种子候选（若当前节点尚未激活该叙事），进入树并标记真实源
    if real_narratives:
        active = set()
        if isinstance(narrative_layer, (list, set, tuple)):
            active.update(narrative_layer)
        elif narrative_layer:
            active.add(narrative_layer)
        for nid in real_narratives:
            if nid not in active and nid not in seen_narr_ids:
                seen_narr_ids.add(nid)
                raw.append({"action": "narrative", "narrative": nid,
                            "desc": "真实源激活", "real": True, "source": "feeds"})

    # ---- 空枚举标记（2026-10-16 修正）----
    # 约定（用户原则「停只由事实/外部锚定触发」）：当本节点经 agent 枚举 + 系统 guards
    # 后 raw 仍为空（无候选入树）时，不得静默当死叶（那等于靠 AI 沉默停，违规），而是
    # **标记留树**（unexpanded_by_agent），待 AI/外部锚定日后重展——是标记、不是停，四停不动。
    # 两种成因都归到「raw 最终为空」：
    #   (a) _agent_gave==0 → agent 真·0 候选（AI 沉默）；
    #   (b) _agent_gave>0 但 guards 全去重（仅冗余叙事）→ agent 没给新东西。
    # 两者结果都是「节点未被有意义地展开」，都应标「留树待展」。
    # 不在此标记的情况（这些已有各自记录，非静默死叶）：
    #   · 候选被世界规则剪掉 → pruned_moves 非空；
    #   · 命中四停之一 → 子节点带 stop_reason（L3_visited / hit_real_record / irrelevant_to_focus）；
    #   · 真实源注入候选 → raw 非空。
    if len(raw) == 0 and not real_narratives:
        node["unexpanded_by_agent"] = True
        if _agent_gave == 0:
            node["unexpanded_reason"] = "agent returned 0 candidates (AI 沉默驱动，非四停)"
        else:
            node["unexpanded_reason"] = "agent 输出全被系统 guards 去重(仅冗余叙事)，无新候选入树"

    # ---- 路线4①（续）：账本冲突判定（枚举候选之后；**降级为标记，不停**）----
    # 语义：冲突真假由实际数据裁决，不由系统当场判。系统只标「这里出现一次可疑加总」（与 off_record
    # 同类），路继续、候选照走。标记沿路径累积：本节点命中追加进 ledger_suspect，子节点继承父节点
    # 全部标记——输出时能看到「这条路经过了哪些可疑加总」，而非只有单节点。
    # 残留（用户 2026-10-13 明示不在本轮）：check_ledger 不区分池，同池判定（AI 判断点 + 同池分组求和）
    # 未落地；宏观域 predict_* consumes 全空故不触发，但那是「没数据」不是「算对了」。
    if ledger_rules and _ledger_active_set:
        try:
            _node_resources = macro_resources.build_macro_resources(state, history)
        except Exception:
            _node_resources = ledger_resources
        _res = rule_ledger.check_ledger(_ledger_active_set, _node_resources)
        if not _res["ok"]:
            node["ledger_suspect"].append({
                "depth": depth,
                "state_summary": _summarize(state),
                "conflict": _res,
            })
            # 不 return、不设 stop_reason、不进 pruned_moves——候选照走

    pending_set = set(cons.get("_pending_ids", []))
    child_index = 0
    _parent_state_key = _state_key(state, path_narratives)  # no-op 自杀检测锚：候选未改状态键则撞此键
    for cand in raw:
        # ---- 世界规则①：硬剪枝（逻辑/常识不可能，外置可审计）----
        # 传入 path_narratives：硬剪枝据此按「路径累积集」判定叙事层约束
        # （PRUNE-COG-* / PRUNE-ABS-001 的 unless_narrative），而非只看当前节点单值
        # —— 否则早期叙事被后至叙事覆盖后漏判（承接用户 #230 路径累积纠正）
        pruned, matched = hard_prune.is_pruned(state, cand, rules=prune_rules,
                                               path_narratives=path_narratives)
        if pruned:
            node["pruned_moves"].append({
                "move": cand,
                "layer": "hard_prune",
                "rules": [r["id"] for r in matched],
                "category": matched[0].get("category") if matched else None,
            })
            continue
        # ---- 世界规则②：约束层（当下真实路况；当前全 pending_feed → 诚实放行）----
        kept, vetoed, pending = constraints.filter_candidates(
            [cand], cons, state, allow_pending=allow_pending)
        if vetoed:
            for v in vetoed:
                node["pruned_moves"].append({
                    "move": cand,
                    "layer": "constraint",
                    "rules": [b["constraint_id"] for b in v["vetoed_by"]],
                })
            continue
        # ---- 重分配③：时间窗口 fit 谓词（落在候选结构层，非账本；逐链并行不求和）----
        win = macro_resources.decisive_window_days(state)
        # 类别化窗口（2026-10-19 解禁）：按传导链类别分配，原 10 天硬顶只作兜底。
        # 利率/政策/价格链 30 天、权益/情绪链 15 天、气候/环境链 60 天；其余走 step 模型 / 兜底 10 天。
        if cand.get("action") == "direction":
            cat_win = _TARGET_WINDOW_DAYS.get(cand.get("target"))
            if cat_win is not None:
                win = max(win, cat_win)
            elif cand.get("target") in _STATE_STEP_MODEL:
                win = max(win, _STATE_STEP_MODEL[cand["target"]].get("step_days") or win)
        elif cand.get("action") == "narrative":
            # 叙事类别化窗口（2026-10-19 收尾）：叙事无 target，曾一律落回 10 天硬顶被误杀；
            # 现按叙事类别放宽（气候 60d / 政策通胀 30d），与 direction 同口径。
            nwin = _NARRATIVE_WINDOW_DAYS.get(cand.get("narrative"))
            if nwin is not None:
                win = max(win, nwin)
        if cand.get("chain_days", 0) > win + 1e-9:
            node["pruned_moves"].append({
                "move": cand,
                "layer": "window_fit",
                "reason": "chain_days=%s > 决策窗口%d天，链装不进窗口" % (cand.get("chain_days"), win),
            })
            continue
        # ---- 真实数据支持判断（传导层 transmission_graph，外置可审计，与硬剪枝/约束同类）----
        # AI 枚举出"X → Y"市场传导候选时，在此读传导图判它有没有真实数据支持；
        # 弱（如跨境日收益联动弱）→ 标"推测(真实联动弱)"并降优先级，--prune-weak-transmission 时直接剪；
        # 传导图未覆盖（如非市场传导：气候→农产品）→ 标"无数据支撑(未覆盖)"，不剪（需其它数据源）。
        tg = ctx.get("tgraph")
        tsupport = None
        if cand.get("action") == "transmission" and tg:
            tsupport = _transmission_support(cand, tg)
            if tg.get("_prune_weak") and tsupport["level"] == "weak":
                node["pruned_moves"].append({
                    "move": cand, "layer": "transmission_support",
                    "rules": ["transmission_graph"],
                    "reason": tsupport["detail"],
                })
                continue
        # ---- 存活 → 走一步，构子节点 ----
        child_state = _apply_move(state, cand)
        mark = tsupport["mark"] if tsupport is not None else _mark_edge(cand, pending_set, cons)
        if cand.get("real"):
            mark = "有据(真实源)"
        impact = impact_layer.assess_impact(cand, state, events=events, focus=focus)

        # ---- 路线4③：历史比对（撞真即停 / 离真标推测）----
        child_fp = _history_fp(child_state)
        parent_fp = _history_fp(state)
        real_anchor = bool(history and child_fp in history["fingerprints"])
        off = _off_record(child_state, history)

        # ---- 路线4②：边标记沿路径累积 ----
        path_marks = parent_path_marks + [mark]
        path_honesty = _summarize_path_marks(path_marks)

        # 子节点路径叙事集（用于 visited 键 + 节点字段 + 向下递归传递）；
        # ②：从 effective_path_narratives（已扣除 agent 本轮回退的叙事）起算，故可 shrinking；
        # 若 cand 自身是 narrative，再并入本步新增的叙事（同 state 不同叙事路径 → 不同键）。
        child_path_narr = set(effective_path_narratives)
        if cand.get("action") == "narrative":
            child_path_narr.add(cand["narrative"])

        # ── no-op 自杀消除（用户 2026-10-19 指令）──
        # 候选经 _apply_move 后状态键不变（Δ=0）→ 子节点必撞 L3_visited 自杀（走了一步废棋）。
        # 枚举层直接丢弃：不建子节点、不浪费 visited 比较；记入 pruned_moves 的 noop_self_kill 层。
        # 与 step1「漏接 wiring 抛 RuntimeError」严格区分：此处 target 合法（在 _MOVE_TARGET_TO_STATEKEY
        # 或 _STRUCTURAL_NOOP_TARGETS 中），只是该方向 Δ=0（历史无此方向 / 结构性非状态变量）。
        if _state_key(child_state, child_path_narr) == _parent_state_key:
            node["pruned_moves"].append({
                "move": cand,
                "layer": "noop_self_kill",
                "reason": "候选未改变状态键(Δ=0)，生成子节点必撞 L3_visited 自杀；枚举层直接丢弃",
            })
            continue

        child = {
            "depth": depth + 1,
            "node_id": "%s.%d" % (node["node_id"], child_index),
            # 真实世界时间轴（2026-10-19 Request B）：子节点 day = 父 day + 本步 chain_days。
            # day 只进节点字段/输出，不进 _state_key（visited 判重仍按 state+叙事集），
            # 故累积时间轴不影响 visited 收敛——这是让系统在「真实世界时间轴」跑、而非静态格子的关键。
            "day": node.get("day", 0) + (cand.get("chain_days") or 0),
            "state": child_state,
            "state_summary": _summarize(child_state),
            # 节点存 path_narratives（③ steppable Model C：agent 生成函数每轮被调用时能看到当前路径累积叙事集）
            "path_narratives": sorted(child_path_narr),
            "move": cand,
            # 决定二 2b：rationale 可选；缺则诚实标「无原因」，绝不系统派生因果。
            # 2a：可在供料时直给，或在 steppable Model C 后续轮次中途追加（③ 循环提供更新入口）。
            "rationale": cand.get("rationale") or "无原因",
            "mark": mark,
            "path_marks": path_marks,
            "path_honesty": path_honesty,
            "impact": impact,
            "transmission_support": tsupport,  # 传导层数据支持判断（None=非传导候选）
            "real_anchor": real_anchor,
            "off_record": off,
            "ledger_suspect": list(node["ledger_suspect"]),
            "pruned_moves": [],
            "children": [],
        }
        child_index += 1
        if real_anchor and history:
            child["real_anchor_months"] = history["months"].get(child_fp, [])
        # L2 历史关注点钩子：若到达某关注叙事，记录命中
        if cand.get("action") == "narrative" and stop_on_narratives \
                and cand["narrative"] in stop_on_narratives:
            node.setdefault("_l2_hits", []).append(cand["narrative"])

        key = _state_key(child_state, child_path_narr)
        if key in visited:
            # (state, 叙事路径) 组合已走过 → 逻辑终止（停≠砍，留痕）
            child["stop_reason"] = "L3_visited"
            node["children"].append(child)
            continue
        visited.add(key)
        # 路线4③：撞上真实记录 → 即停（不再无凭据外推）
        if real_anchor and child_fp != parent_fp:
            child["stop_reason"] = "hit_real_record"
            node["children"].append(child)
            continue
        # 与关注点无关 → 停（外部锚定：对某关注点影响度为零才停；focus=None 无法判定，不停）
        if impact.get("level") == "zero" and focus is not None:
            child["stop_reason"] = "irrelevant_to_focus"
            node["children"].append(child)
            continue
        # 影响度低/无 = 程度标记，不停（继续展开；输出时不优先展开）
        if impact.get("level") in ("low", "none"):
            child["impact_priority"] = impact.get("level")
        # 传导候选：真实联动弱 → 降优先级（标低支持，继续保留为审计叶）
        if tsupport is not None and tsupport["level"] == "weak":
            child["impact_priority"] = "low"
        # ---- 递归（build 模式全递归；session 模式 single_node 不递归）----
        # 传导候选是"结构声明"而非状态步，不向下递归（自然成叶，仅作审计落点）
        if cand.get("action") != "transmission" and not ctx.get("single_node") and depth + 1 <= max_depth:
            _grow(child, depth + 1, max_depth, visited, prune_rules, cons,
                  allow_pending=allow_pending, stop_on_narratives=stop_on_narratives,
                  real_narratives=real_narratives, events=events,
                  ledger_rules=ledger_rules, ledger_resources=ledger_resources,
                  history=history, path_narratives=child_path_narr,
                  parent_path_marks=path_marks, focus=focus,
                  parent_path_suspects=node["ledger_suspect"],
                  tgraph=ctx.get("tgraph"),
                  max_probe_rounds=max_probe_rounds)
        node["children"].append(child)
    return node


def build_world_tree(signal, as_of=None, max_depth=3, prune_rules=None, constraints_path=None,
                     allow_pending=False, stop_on_narratives=None, use_real_feeds=True,
                     use_ledger=True, use_history=True, focus=None,
                     prune_weak_transmission=False, max_probe_rounds=None):
    """从 WorldState 展开世界分岔树（每节点重枚举 + 世界规则 inline + 真实源 + 账本标记 + 历史比对）。

    focus：关注点（方向目标名或中文别名，如 债市/CN10Y/股市）。给定后影响度相对它计算，
    与关注点无关的分支停（irrelevant_to_focus）；None → 无法判定相对影响度，只按三条客观事实停。
    """
    ws = world_state.read_world_state(signal, as_of)
    state = ws["state"]
    ai_candidate.clear_cache()  # 每次建树清缓存，避免跨树串味（非确定性模型结果不一致）
    prune_rules = prune_rules or hard_prune.load_rules()
    cons = constraints.load_constraints(constraints_path)
    # 预算顶层 pending 约束 id（用于边标记；real 约束 source 非 pending_feed，不入此列）
    cons["_pending_ids"] = [c["id"] for c in cons.get("constraints", [])
                            if c.get("source") == "pending_feed" and not allow_pending]
    # ---- 真实源注入（feeds）：约束合并 + 真实叙事集合 ----
    real_narratives = []
    feed_status = {}
    n_real_cons = 0
    events = []
    if use_real_feeds:
        cfeed = feeds.build_constraint_feed()
        nfeed = feeds.build_narrative_feed()
        feed_status = {"constraints": cfeed["source_status"],
                       "narratives": nfeed["source_status"]}
        n_real_cons = len(cfeed["constraints"])
        cons.setdefault("constraints", []).extend(cfeed["constraints"])
        real_narratives = [n["narrative"] for n in nfeed["narratives"]]
        try:
            cal = feeds._collect_event_calendar()
            events = cal.get("events", []) if cal else []
        except Exception:
            events = []
    # ---- 路线4③：历史序列载入（须先于账本块：账本 caps 推导可能随真实 LPR 历史收紧）----
    history = load_history() if use_history else None
    # ---- 传导层接入（2026-10-15 架构纠正）：作为"真实数据支持"剪枝依据，与硬剪枝/约束同类 ----
    tgraph = _load_transmission_graph(prune_weak=prune_weak_transmission)
    # ---- 路线4①：账本规则载入 ----
    ledger_rules, ledger_resources = None, None
    if use_ledger:
        try:
            lc = rule_ledger.load(RULE_LEDGER_CONFIG)
            # 活体推演只跑宏观域规则（domain=="predict"）；run_* 演示规则留给引擎 CLI --scenario。
            # resources 用宏观域真实推导的 caps（tools/macro_resources.build_macro_resources），
            # 取代文件里的静态/演示值——数值从真实竞争关系长出来，非手抄演示 schema。
            ledger_rules = [r for r in lc.get("rules", []) if r.get("domain") == "predict"]
            # 资源 caps 随「当前世界状态」推导（非常数）：每个节点再用自身 state 重算。
            ledger_resources = macro_resources.build_macro_resources(state, history)
        except Exception:
            ledger_rules, ledger_resources = None, None

    root = {
        "depth": 0,
        "node_id": "n0",
        "day": 0,
        "state": state,
        "state_summary": _summarize(state),
        "path_narratives": [],
        "path_marks": [],
        "ledger_active": [],
        "ledger_suspect": [],
        "pruned_moves": [],
        "children": [],
    }
    # 根节点的账本检查（激活规则集 + 可疑加总标记）由下方 _grow 自身完成，此处不重复；
    # 这里只补历史锚定（信息性）
    if history:
        root_fp = _history_fp(state)
        root["real_anchor"] = root_fp in history["fingerprints"]
        root["real_anchor_months"] = history["months"].get(root_fp, []) if root["real_anchor"] else []
        root["off_record"] = _off_record(state, history)

    visited = {_state_key(state)}
    _grow(root, 0, max_depth, visited, prune_rules, cons,
          allow_pending=allow_pending, stop_on_narratives=stop_on_narratives,
          real_narratives=real_narratives, events=events,
          ledger_rules=ledger_rules, ledger_resources=ledger_resources,
          history=history, path_narratives=set(), parent_path_marks=[],
          focus=focus, tgraph=tgraph, max_probe_rounds=max_probe_rounds)
    tree = root
    tree["root_signal"] = signal
    tree["root_as_of"] = ws.get("as_of")
    tree["root_focus"] = focus
    n_pending = len(cons.get("_pending_ids", []))
    ledger_n = _count_suspect_events(tree)
    real_n = _count_flag(tree, "real_anchor")
    off_n = _count_flag(tree, "off_record")
    ts_acc = _count_transmission_support(tree, {})
    tree["honesty"] = {
        "principle": "记录世界分岔，不建造世界；候选由 agent（AI）在系统外部穷举后注入（ai_candidate 槽位），"
                     "硬剪枝/约束并入枚举时的世界规则；每节点重枚举递归；无概率",
        "n_pruned": _count_pruned(tree),
        "n_survived": _count_survived(tree),
        "constraint_source": ("%d 条真实约束(feeds) + %d 条 pending_feed 缺口"
                              % (n_real_cons, n_pending)) if use_real_feeds
                             else "全部 pending_feed（真实源未接入）→ 约束层诚实放行",
        "real_narratives": real_narratives,
        "feed_status": feed_status,
        "cn10y": "unknown/缓存（bond_china_yield 本环境 TLS 失败；cn10y_feed 已就位，环境通后自动接）",
        "ledger": {"enabled": bool(ledger_rules), "suspect_events": ledger_n,
                   "note": "冲突降级为标记（ledger_suspect，不停）；真假由实际数据裁决；"
                           "池标识（同池分组求和）未落地，宏观域 consumes 全空故不触发"},
        "history": {"enabled": bool(history), "loaded_states": history["n"] if history else 0,
                    "real_anchor_nodes": real_n, "off_record_nodes": off_n},
        "transmission_support": {
            "enabled": bool(tgraph),
            "levels": ts_acc,
            "note": "传导层(transmission_graph)作为'真实数据支持'剪枝依据接入主循环(2026-10-15架构纠正)；"
                    "此前只被 claim_layer GATE 消费，主循环与传导层像两条平行线。现："
                    "data_confirmed=有据(真实联动) / hypothesized=机制待数据确认 / "
                    "weak=真实联动弱(标低支持, --prune-weak-transmission 可剪) / "
                    "none=传导图未覆盖(非市场传导, 需其它数据源)",
        },
    }
    return tree, ws


# ---------------------------------------------------------------------------
# ③ 分步展开循环（steppable Model C，可断点续跑）：agent 生成函数每轮被调用，但函数一次性注册、决策不在场。
#    真 Model A 有两条实现路径：(a) 层2 注入式 run_agent_loop(agent_fn=)——外部 agent 每轮真在场、自行枚举/追问（见下方）；
#    (b) serve 外部 CLI 轮询——agent 每轮调 CLI init/next/expand/status 把候选显式喂入 expand_node
#    （本 skill 选定的 Model A 形态，2026-10-17 裁定：serve 只走此路径，不接层2 注入式）。设计见 docs/model_a_interface.md。
# ---------------------------------------------------------------------------
_SESSIONS = {}


# ② 叙事过期（模型 A 契约下）：把 config/narratives.json 的 valid_while 注册表作为**只读世界规则引用**
# 暴露给 agent（node_context.narratives），供 agent「看着 state 判断叙事是否仍成立」。
# 系统**绝不**自动求值 valid_while（那部分在模型 A 契约下 VOIDED）；只提供事实，判断由 agent 负责。
_NARRATIVES_REF = None


def _load_narratives_ref():
    """懒加载叙事 valid_while 引用表（只读，供 agent 判断用；系统不自动应用）。"""
    global _NARRATIVES_REF
    if _NARRATIVES_REF is not None:
        return _NARRATIVES_REF
    ref = {}
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "config", "narratives.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for n in data.get("narratives", []):
            ref[n["id"]] = n.get("valid_while")
    except Exception:
        ref = {}
    _NARRATIVES_REF = ref
    return ref


def _node_context(node, ctx):
    """向 agent 生成函数提供的本节点上下文（分步展开 steppable Model C 时每轮传给 generate；
    真 Model A 每轮重新决策由 run_agent_loop(agent_fn=) 层2 实现）。

    3a 层1 追问循环增强（2026-10-16）：现向 agent 暴露：
      - pruned_moves：本节点已被系统剪掉的候选（硬剪枝/约束/窗口/传导/账本），让 agent 看清边界、避免重复提出被剪项；
      - focus：当前关注点，让 agent 在追问轮次聚焦与关注点相关的候选。
    配合 seen（已枚举候选回传），agent 在追问轮次拥有「已枚举什么 / 已被剪什么 / 现在关注什么」三向上下文。"""
    return {
        "node_id": node.get("node_id"),
        "depth": node.get("depth"),
        "path_narratives": list(node.get("path_narratives") or []),
        "path_marks": list(node.get("path_marks") or []),
        "ledger_suspect": list(node.get("ledger_suspect") or []),
        "pruned_moves": list(node.get("pruned_moves") or []),
        "focus": (ctx or {}).get("focus"),
        "as_of": (ctx or {}).get("as_of"),
        "decision_window_days": (ctx or {}).get("decision_window_days"),
        # 2026-10-19 暴露累计方向态：agent（尤其无状态接入的未来大模型）在 n0.0 这类节点
        # 能一眼看到「CPI 已 up / 大宗已 up / PPI 已 down」等当前走到哪一步，而非全靠上一轮
        # 自己提交的候选当记忆。否则会反复提案已被状态吸收的方向（_system_candidate_guards 静默吞掉）。
        "state_summary": node.get("state_summary"),
        # ②：叙事 valid_while 世界规则（只读引用），让 agent 有「判据」可对照，而非凭空判断。
        "narratives": _load_narratives_ref(),
    }


def _prepare(signal, as_of=None, max_depth=3, prune_rules=None, constraints_path=None,
             allow_pending=False, stop_on_narratives=None, use_real_feeds=True,
             use_ledger=True, use_history=True, focus=None,
             prune_weak_transmission=False, max_probe_rounds=None):
    """准备一次展开：读世界状态 + 载入规则/约束/真实源/历史/账本，搭好 root 与 ctx。
    被 build_world_tree（递归全展开）与 create_session（steppable Model C 增量展开）复用。"""
    ws = world_state.read_world_state(signal, as_of)
    state = ws["state"]
    ai_candidate.clear_cache()
    prune_rules = prune_rules or hard_prune.load_rules()
    cons = constraints.load_constraints(constraints_path)
    cons["_pending_ids"] = [c["id"] for c in cons.get("constraints", [])
                            if c.get("source") == "pending_feed" and not allow_pending]
    real_narratives = []
    feed_status = {}
    n_real_cons = 0
    events = []
    if use_real_feeds:
        cfeed = feeds.build_constraint_feed()
        nfeed = feeds.build_narrative_feed()
        feed_status = {"constraints": cfeed["source_status"], "narratives": nfeed["source_status"]}
        n_real_cons = len(cfeed["constraints"])
        cons.setdefault("constraints", []).extend(cfeed["constraints"])
        real_narratives = [n["narrative"] for n in nfeed["narratives"]]
        try:
            cal = feeds._collect_event_calendar()
            events = cal.get("events", []) if cal else []
        except Exception:
            events = []
    history = load_history() if use_history else None
    tgraph = _load_transmission_graph(prune_weak=prune_weak_transmission)
    ledger_rules, ledger_resources = None, None
    if use_ledger:
        try:
            lc = rule_ledger.load(RULE_LEDGER_CONFIG)
            ledger_rules = [r for r in lc.get("rules", []) if r.get("domain") == "predict"]
            ledger_resources = macro_resources.build_macro_resources(state, history)
        except Exception:
            ledger_rules, ledger_resources = None, None
    root = {
        "depth": 0,
        "node_id": "n0",
        "day": 0,
        "state": state,
        "state_summary": _summarize(state),
        "path_narratives": [],
        "path_marks": [],
        "ledger_active": [],
        "ledger_suspect": [],
        "pruned_moves": [],
        "children": [],
    }
    if history:
        root_fp = _history_fp(state)
        root["real_anchor"] = root_fp in history["fingerprints"]
        root["real_anchor_months"] = history["months"].get(root_fp, []) if root.get("real_anchor") else []
        root["off_record"] = _off_record(state, history)
    visited = {_state_key(state)}
    ctx = {
        "visited": visited,
        "prune_rules": prune_rules,
        "cons": cons,
        "allow_pending": allow_pending,
        "stop_on_narratives": stop_on_narratives,
        "real_narratives": real_narratives,
        "events": events,
        "ledger_rules": ledger_rules,
        "ledger_resources": ledger_resources,
        "history": history,
        "focus": focus,
        "tgraph": tgraph,
        "max_depth": max_depth,
        "max_probe_rounds": max_probe_rounds,
        "as_of": ws.get("as_of"),
        "decision_window_days": macro_resources.decisive_window_days(state),
    }
    meta = {"feed_status": feed_status, "n_real_cons": n_real_cons, "real_narratives": real_narratives}
    return root, ctx, ws, meta


def create_session(signal, as_of=None, max_depth=3, use_real_feeds=True,
                   use_ledger=True, use_history=True, focus=None,
                   prune_weak_transmission=False, max_probe_rounds=None):
    """steppable Model C：创建一个展开会话。只搭好 root 与 frontier（[n0]），不递归。
    注意：run_agent_loop(agent_fn=None) 时机械循环同一注册函数（层1 追问循环在 _grow 内部），
    若传 agent_fn= 则每轮由外部 agent 真在场自行枚举/追问（层2 真 Model A）。
    随后由外层 agent 逐轮调用 expand_node 驱动（或 run_agent_loop 自动跑完）。"""
    root, ctx, ws, meta = _prepare(signal, as_of=as_of, max_depth=max_depth,
                                    use_real_feeds=use_real_feeds, use_ledger=use_ledger,
                                    use_history=use_history, focus=focus,
                                    prune_weak_transmission=prune_weak_transmission,
                                    max_probe_rounds=max_probe_rounds)
    ctx["single_node"] = True
    ctx["nodes_by_id"] = {"n0": root}
    ctx["frontier"] = ["n0"]
    ctx["_expanded"] = set()
    ctx["meta"] = meta
    ctx["ws"] = ws
    sid = "sess_" + uuid.uuid4().hex[:8]
    _SESSIONS[sid] = {"root": root, "ctx": ctx, "signal": signal, "as_of": as_of,
                      "max_depth": max_depth, "status": "created", "rounds": 0}
    return sid


def get_session(sid):
    s = _SESSIONS.get(sid)
    if not s:
        raise KeyError("session %s 不存在" % sid)
    return s["root"]


def expand_node(sid, nid=None, candidates=None):
    """分步展开原子 API。

    steppable Model C（candidates=None）：内部用一次性注册的生成器产候选，系统剪枝后生成子节点。
    真 Model A（candidates 由 agent 每轮显式传入）：跳过内部生成，直接用传入候选走系统后处理；
    候选生成完全搬到 agent 侧，系统只做世界规则剪枝/约束/走步/路径累积。

    不递归（session 模式 single_node=True）；子节点入 frontier 等待后续轮次。
    返回本次创建的子节点列表，幂等（已展开节点返回 [] 且状态空间不变）。"""
    s = _SESSIONS.get(sid)
    if not s:
        raise KeyError("session %s 不存在" % sid)
    ctx = s["ctx"]
    if nid is None:
        if not ctx["frontier"]:
            s["status"] = "done"
            return []
        nid = ctx["frontier"].pop()
    node = ctx["nodes_by_id"].get(nid)
    if node is None:
        raise KeyError("node %s 不在 session %s" % (nid, sid))
    if nid in ctx["_expanded"]:
        return []
    # 真 Model A 接口：agent 显式指定 nid 时，该节点出 frontier（已展开，待展开集合收缩）；
    # 若 nid 来自 get_frontier，等价 pop；若 agent 自选节点，也保证不重复出现在 frontier。
    if nid in ctx["frontier"]:
        ctx["frontier"].remove(nid)
    _grow(node, node.get("depth", 0), ctx["max_depth"],
          ctx["visited"], ctx["prune_rules"], ctx["cons"],
          allow_pending=ctx["allow_pending"], stop_on_narratives=ctx["stop_on_narratives"],
          real_narratives=ctx["real_narratives"], events=ctx["events"],
          ledger_rules=ctx["ledger_rules"], ledger_resources=ctx["ledger_resources"],
          history=ctx["history"],
          path_narratives=set(node.get("path_narratives") or []),
          parent_path_marks=node.get("path_marks") or [],
          parent_path_suspects=node.get("ledger_suspect") or [],
          ctx=ctx, raw_override=candidates)
    ctx["_expanded"].add(nid)
    s["rounds"] += 1
    children = node["children"]
    to_frontier = [c["node_id"] for c in children
                   if not c.get("stop_reason") and node.get("depth", 0) + 1 <= ctx["max_depth"]]
    for c in children:
        ctx["nodes_by_id"][c["node_id"]] = c
    ctx["frontier"].extend(reversed(to_frontier))
    if not ctx["frontier"]:
        s["status"] = "done"
    return children


def get_frontier(sid):
    """真 Model A 接口：返回当前待展开节点 id 列表（frontier）。
    agent 每轮调用它决定下一个要 expand 的节点。空列表表示无待展开节点。"""
    s = _SESSIONS.get(sid)
    if not s:
        raise KeyError("session %s 不存在" % sid)
    return list(s["ctx"]["frontier"])


def is_done(sid):
    """真 Model A 接口：返回该 session 是否已展开完毕（frontier 空）。
    等价于 s['status']=='done'，但作为独立可读谓词暴露给 agent 每轮轮询。"""
    s = _SESSIONS.get(sid)
    if not s:
        raise KeyError("session %s 不存在" % sid)
    return len(s["ctx"]["frontier"]) == 0


def run_agent_loop(sid, max_rounds=None, agent_fn=None):
    """分步展开驱动：通过 get_frontier / expand_node(sid, nid, candidates) / is_done 三件套驱动。

    两种驱动源（共用同一展开接口）：
      - agent_fn=None（层1 模型 C）：系统用已注册生成器（ai_candidate.GENERATOR_OVERRIDE /
        set_agent_generator），由 _grow 内部追问循环（seen 回传）向 AI 多轮追问。agent 决策不在场，
        但 3a 层1 追问机制完整（每轮把已枚举候选回传 AI，AI 只补新候选）。
      - agent_fn=callable（层2 真 Model A）：外部 agent 每轮真在场，自行枚举/追问，把候选显式传给
        expand_node。agent_fn 收到上下文 dict，返回候选列表；系统只做世界规则后处理（_grow 跳过内部追问）。

    agent_fn 上下文 dict 字段：sid, nid, node, state, path_narratives, node_context, frontier, focus。
    系统侧只做世界规则剪枝/约束/走步/路径累积/账本/窗口 fit/_apply_move，不生成候选。"""
    s = _SESSIONS.get(sid)
    if not s:
        raise KeyError("session %s 不存在" % sid)
    rounds = 0
    while (not is_done(sid)) and (max_rounds is None or rounds < max_rounds):
        frontier = get_frontier(sid)
        if not frontier:
            break
        nid = frontier[-1]  # 与 expand_node(sid) 的 pop()（LIFO）一致
        node = s["ctx"]["nodes_by_id"].get(nid)
        if agent_fn is not None:
            # 层2 真 Model A：外部 agent 每轮真在场，自行枚举/追问 → 显式传候选（_grow 跳过内部追问）
            ctx_dict = {
                "sid": sid, "nid": nid, "node": node,
                "state": node.get("state", {}),
                "path_narratives": set(node.get("path_narratives") or []),
                "node_context": _node_context(node, s["ctx"]),
                "frontier": frontier,
                "focus": s["ctx"].get("focus"),
            }
            candidates = agent_fn(ctx_dict)
            expand_node(sid, nid, candidates)
        else:
            # 层1 模型 C：系统用已注册生成器，_grow 内部追问循环（seen 回传）向 AI 多轮追问
            expand_node(sid, nid)
        rounds += 1
    if is_done(sid):
        s["status"] = "done"
    return {"rounds": rounds, "status": s["status"], "nodes": len(s["ctx"]["nodes_by_id"])}


def save_session(sid, path):
    s = _SESSIONS.get(sid)
    if not s:
        raise KeyError("session %s 不存在" % sid)
    payload = {"session_id": sid, "signal": s["signal"], "as_of": s["as_of"],
               "max_depth": s["max_depth"], "status": s["status"], "rounds": s["rounds"],
               "tree": s["root"]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def load_session(path):
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    root, ctx, ws, meta = _prepare(payload["signal"], as_of=payload["as_of"],
                                    max_depth=payload["max_depth"])
    root = payload["tree"]
    nodes_by_id = {}
    expanded = set()

    def _index(n):
        nodes_by_id[n["node_id"]] = n
        if n.get("children") or n.get("stop_reason"):
            expanded.add(n["node_id"])
        for c in n.get("children", []):
            _index(c)
    _index(root)
    frontier = [nid for nid, n in nodes_by_id.items()
               if nid not in expanded and n.get("depth", 0) < payload["max_depth"]]
    ctx["single_node"] = True
    ctx["nodes_by_id"] = nodes_by_id
    ctx["frontier"] = frontier
    ctx["_expanded"] = expanded
    ctx["meta"] = meta
    ctx["ws"] = ws
    sid = payload["session_id"]
    _SESSIONS[sid] = {"root": root, "ctx": ctx, "signal": payload["signal"],
                      "as_of": payload["as_of"], "max_depth": payload["max_depth"],
                      "status": payload["status"], "rounds": payload["rounds"]}
    return sid


def _count_pruned(node):
    n = len(node.get("pruned_moves", []))
    for ch in node.get("children", []):
        n += _count_pruned(ch)
    return n


def _count_survived(node):
    n = len(node.get("children", []))
    for ch in node.get("children", []):
        n += _count_survived(ch)
    return n


def _count_flag(node, key):
    n = 1 if node.get(key) else 0
    if key == "real_anchor" and not node.get(key):
        n = 0
    for ch in node.get("children", []):
        n += _count_flag(ch, key)
    return n


def _count_suspect_events(node, parent_len=0):
    """统计「可疑加总」事件数：每个事件只在它发生的节点计一次（子孙继承的标记不计）。"""
    n = max(0, len(node.get("ledger_suspect", [])) - parent_len)
    for ch in node.get("children", []):
        n += _count_suspect_events(ch, len(node.get("ledger_suspect", [])))
    return n


def _count_transmission_support(node, acc):
    """统计传导层数据支持判断在各层级的出现次数（按 level 分桶）。"""
    for ch in node.get("children", []):
        ts = ch.get("transmission_support")
        if ts:
            acc[ts.get("level", "unknown")] = acc.get(ts.get("level", "unknown"), 0) + 1
        _count_transmission_support(ch, acc)
    return acc


def _print_tree(node, indent):
    pad = "  " * indent
    summ = node.get("state_summary", {})
    narr = summ.get("_narrative")
    tag = (" [narr=%s]" % narr) if narr else ""
    extra = ""
    if node.get("stop_reason"):
        extra += " <%s>" % node["stop_reason"]
    if node.get("real_anchor"):
        extra += " [撞真:%s]" % (node.get("real_anchor_months") or "?")
    if node.get("off_record"):
        extra += " [离真]"
    ph = node.get("path_honesty")
    if ph:
        extra += " {%s}" % ph
    ls = node.get("ledger_suspect")
    if ls:
        extra += " [账本可疑:%d]" % len(ls)
    print("%s[depth=%d]%s state=%s%s" % (pad, node.get("depth", 0), tag, summ, extra))
    for pm in node.get("pruned_moves", []):
        mv = pm["move"]
        if mv.get("action") == "narrative":
            label = "narrative:%s" % mv.get("narrative")
        elif mv.get("action") == "transmission":
            label = "transmission:%s->%s" % (mv.get("from"), mv.get("to"))
        else:
            label = "dir:%s %s" % (mv.get("target"), mv.get("sign"))
        print("%s  X %s %s  by %s" % (pad, pm["layer"].upper(), label, ",".join(pm["rules"])))
    for ch in node.get("children", []):
        mv = ch.get("move", {})
        if mv.get("action") == "narrative":
            label = "narrative:%s" % mv.get("narrative")
        elif mv.get("action") == "transmission":
            label = "transmission:%s->%s" % (mv.get("from"), mv.get("to"))
        else:
            label = "dir:%s %s" % (mv.get("target"), mv.get("sign"))
        mark = ch.get("mark", "")
        stop = (" <%s>" % ch["stop_reason"]) if ch.get("stop_reason") else ""
        ra = " [撞真]" if ch.get("real_anchor") else ""
        of = " [离真]" if ch.get("off_record") else ""
        ts = ch.get("transmission_support")
        tsextra = (" [传导:%s]" % ts["level"]) if ts else ""
        print("%s  -> %s [%s]%s%s%s%s" % (pad, label, mark, stop, ra, of, tsextra))
        _print_tree(ch, indent + 1)


def main():
    ap = argparse.ArgumentParser(
        description="世界分岔记录器：状态驱动枚举 + 每节点重枚举递归 + 世界规则 inline + 账本挂路径 + 历史比对")
    ap.add_argument("--signal", default="9月CPI PPI居民消费价格数据")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(BASE, "reports", "world_tree.json"))
    ap.add_argument("--rules", default=hard_prune.DEFAULT_RULES)
    ap.add_argument("--constraints", default=constraints.DEFAULT_CONSTRAINTS)
    ap.add_argument("--allow-pending", action="store_true",
                    help="允许 pending_feed 约束生效（仅演示，不可用于真实筛选）")
    ap.add_argument("--stop-narratives", default=None,
                    help="L2 历史关注点：到达这些叙事即视为命中（逗号分隔）")
    ap.add_argument("--no-ledger", action="store_true", help="关闭账本挂路径")
    ap.add_argument("--no-history", action="store_true", help="关闭历史比对")
    ap.add_argument("--focus", default=None,
                    help="关注点（方向目标名或中文别名，如 债市/CN10Y/股市）：影响度相对它计算，"
                         "与关注点无关的分支停（irrelevant_to_focus）；不给则只按客观事实停")
    ap.add_argument("--prune-weak-transmission", action="store_true",
                    help="传导候选真实联动弱(跨境日收益 corr<阈值)时直接剪（默认：标低支持保留为审计叶）")
    a = ap.parse_args()
    stop_on = set(s.strip() for s in (a.stop_narratives or "").split(",") if s.strip()) or None
    tree, ws = build_world_tree(
        a.signal, a.as_of, max_depth=a.depth,
        prune_rules=hard_prune.load_rules(a.rules),
        constraints_path=a.constraints,
        allow_pending=a.allow_pending,
        stop_on_narratives=stop_on,
        use_ledger=not a.no_ledger,
        use_history=not a.no_history,
        focus=a.focus,
        prune_weak_transmission=a.prune_weak_transmission)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2)
    print("世界树 @ %s （信号=%s，max_depth=%d）" % (tree["root_as_of"], tree["root_signal"], a.depth))
    print("  根状态:", _summarize(ws["state"]))
    h = tree["honesty"]
    print("  硬剪枝/约束拦截 %d 个候选，存活 %d 个分支" % (h["n_pruned"], h["n_survived"]))
    print("  账本可疑加总 %d 处（标记，不停）；历史撞真节点 %d；离真节点 %d（载入 %d 状态）"
          % (h["ledger"]["suspect_events"], h["history"]["real_anchor_nodes"],
             h["history"]["off_record_nodes"], h["history"]["loaded_states"]))
    if tree.get("root_focus"):
        print("  关注点: %s（与关注点无关的分支已停）" % tree["root_focus"])
    ts = tree.get("honesty", {}).get("transmission_support", {})
    if ts.get("enabled"):
        print("  传导层数据支持(各 level 命中数): %s" % (ts.get("levels") or {}))
    _print_tree(tree, 0)
    print("->", os.path.relpath(a.out, BASE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
