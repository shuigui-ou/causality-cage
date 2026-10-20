# -*- coding: utf-8 -*-
"""候选生成槽位：AI 穷举（由 agent 自身替代 AI，不调用任何外部模型服务）。

架构纠正（2026-10-13，用户裁定）：
  - 正确边界 = **AI 穷举 → 系统剪枝**。AI 在系统外面，只负责把"当前状态下所有可能的下一步"
    往外倒；系统（硬剪枝/约束/停规则/路径累积/账本）拿到候选后再剪。
  - 原 candidate_source 是系统里一段人写代码却在做"生成候选"的活——把剪枝侧的
    "不生成什么"偷偷挪到了生成端。本模块把生成槽位交还给 AI。
  - **AI 由 agent 自身替代**：本系统不内置任何 LLM / 模型服务（不调用 Ollama 等外部进程）。
    候选生成由 agent（本对话中的 LLM）在系统外部完成后注入本槽位。
  - candidate_source 不再作为生产生成器，仅保留为常量库（NARRATIVE_CHAIN_DAYS 等）
    与 impact_layer / run_full_loop 的实用枚举工具。

接口（与旧 enumerate_candidates 同返回格式）：
  generate(state, narrative_layer=None) -> [candidate, ...]
    candidate = {"action": "narrative", "narrative": "<id>", "desc": "...", "chain_days": int, "polarity": ...}
              | {"action": "direction", "target": "<id>", "sign": "up"|"down"|"flat",
                 "chain_days": int, "magnitude": "weak"|"typical"|"strong"|None,
                 "rationale": "<str|None>"}
              | {"action": "narrative_retire", "narrative": "<id>", "rationale": "<str>"}
                # ② 叙事过期（agent 注入函数每轮被调用时判定）：agent 注入的函数每轮被调用时看着 state + path_narratives + node_context.narratives
                # （valid_while 世界规则引用）判断某路径叙事已不成立时发出。它**不是「下一步候选」**，
                # 不生成子节点，只把该叙事从「向下传递的 path_narratives」中移除（path_narratives 由此
                # 可 shrinking）。系统机械执行，不做因果派生。「判不判、何时判」由 agent 负责。

注入方式（agent 在系统外部穷举后填入，系统内部绝不自行生成）：
  1. 单次覆盖：设 ai_candidate.GENERATOR_OVERRIDE = callable(state, narrative_layer) -> list
  2. 注册复用：ai_candidate.set_agent_generator(callable)  （多次建树复用）
  - 两者皆空 → 显式报错，强制"AI 在系统外面"的架构（系统不生产候选）。
"""
from __future__ import annotations

import json
import logging

import candidate_source  # 仅作常量库（NARRATIVE_CHAIN_DAYS / NARRATIVE_POLARITY / DIRECTION_CHAIN_DAYS 等）
import canon  # 规范 id 登记表：别名归一 + 范围外拒收（见 canon.py，2026-10-17 落地）

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger("ai_candidate")

# 测试/离线/agent 注入点：设 ai_candidate.GENERATOR_OVERRIDE = callable(state, narrative_layer) -> list
GENERATOR_OVERRIDE = None
# 注册式注入（多次建树复用，同样由 agent 在外部填充）
AGENT_GENERATOR = None

# 单树内一致性缓存：key=(state_json, narrative_layer)
_CACHE: dict = {}

# ─────────────────────────────────────────────────────────────────────────────
# 动词语汇解禁（2026-10-19，用户裁定）：让系统识别 tighten/easing/loosen 等
# "动作型" sign，并在内部归一到规范 sign（up/down/flat）。原始动词保留在 raw_sign
# 供展示/honesty（"tighten" 一目了然是收紧，而非中性 "up"）。
#   通用动词：tighten/easing/loosen/strengthen/weaken
#   目标专属：policy_stance 用 tighten(=up,收紧) / easing·loosen(=down,放松)
# 原则（用户 2026-10-19）：宏观推演充满动词语汇，系统必须能接纳并转化为状态变化，
#       不得因 sign 不在 {up,down,flat} 就静默丢弃候选。
# 注：policy_stance 用离散事件模型（见 world_tree._step_policy_stance，用户 2026-10-19 裁定：
#     离散状态偏松/中性/偏紧，步长由事件 tighten/easing 驱动，不套价格序列算法）；
#     CN_CYB/G_NASDAQ/US_10Y 已由 build_market_step_model.py 接 akshare 真实历史建好 step 模型，
#     故 tighten / down / up 等动词候选现能真实推进状态（不再 no-op）。
# ─────────────────────────────────────────────────────────────────────────────
_SIGN_CANON = {
    "up": "up", "down": "down", "flat": "flat",
    "tighten": "up", "easing": "down", "loosen": "down",
    "strengthen": "up", "weaken": "down",
    # 财政立场离散状态动词（FISCAL_STANCE）：expansion=扩张(→up)、contraction=收缩(→down)。
    # 与 policy_stance 的 tighten/easing 同法；归一为 up/down 后由 world_tree._step_fiscal_stance
    # 用原始 discrete sign 驱动跳变（用户 2026-10-19 指令：信用/财政是宏观推演两条大腿）。
    "expansion": "up", "contraction": "down",
}
_TARGET_SIGN_ALIASES = {
    "policy_stance": {"tighten": "up", "easing": "down", "loosen": "down"},
}


def set_agent_generator(fn):
    """注册 agent 候选生成器（系统外部 AI 的注入点，多次建树复用）。"""
    global AGENT_GENERATOR
    AGENT_GENERATOR = fn


def _norm_layer(narrative_layer):
    if narrative_layer is None:
        return None
    if isinstance(narrative_layer, (list, set, tuple)):
        return tuple(sorted(str(x) for x in narrative_layer))
    return str(narrative_layer)


def _cache_key(state, narrative_layer, node_context, seen):
    """缓存键：在 (state, narrative_layer, node_context) 基础上纳入 seen 的稳定签名。

    修复（2026-10-14 复盘）：原 key 仅 (state_json, narrative_layer)，**不含 node_context**——
    同一 (state, 叙事层) 经不同路径抵达、node_context 不同（尤其 path_narratives/path_marks
    影响 narrative_retire 信号）时，第二次会直接返回第一次的候选，**吞掉 agent 的「每轮在场」**。
    这是语义级 bug（非性能），steppable Model C 与真 Model A 下都必须修。
    node_id 唯一标识节点，配合路径上下文字段即可区分不同节点、避免错误复用。

    3a 层1 追问循环（2026-10-16）：seen 是「本节点已枚举候选」列表，每轮累积、各不相同，
    必须进缓存键——否则第 2 轮追问会因命中第 1 轮 (seen=[]) 的缓存而**返回旧结果、追问空转**。"""
    nc = node_context or {}
    ctx_sig = (
        nc.get("node_id"),
        nc.get("depth"),
        tuple(sorted(nc.get("path_narratives") or [])),
        tuple(sorted(nc.get("path_marks") or [])),
        tuple(sorted(json.dumps(x, sort_keys=True, ensure_ascii=False)
                          for x in (nc.get("ledger_suspect") or []))),
        nc.get("as_of"),
        nc.get("decision_window_days"),
    )
    seen_sig = tuple(
        json.dumps(c, sort_keys=True, ensure_ascii=False) for c in (seen or []))
    return (json.dumps(state, sort_keys=True, ensure_ascii=False),
            _norm_layer(narrative_layer), ctx_sig, seen_sig)


def generate(state, narrative_layer=None, node_context=None, seen=None):
    """从当前状态穷举候选（未经剪枝）。AI（agent）全量生成，系统随后剪枝。

    node_context：分步展开（steppable Model C）时，系统向 agent 生成函数提供的本节点上下文
                  （path_narratives/path_marks/ledger_suspect/as_of/depth 等 + 现已含 pruned_moves/focus，
                  见 world_tree._node_context），供 agent 判断「下一步候选」。
    seen：追问循环（3a 层1，world_tree._grow 内部）回传的「本节点已枚举候选」列表。agent 据此只返回
          **尚未想到/未提及**的新候选，系统侧去重累积，直到无新增(added==0)或达上限。
          - 旧桩（无 seen 形参）：探测不到 seen（或 **kwargs）则不传，等价于单次枚举（追问第 1 轮）。
          - 不传 seen（直接调用 generate）也兼容：默认 None → 第 1 轮枚举。
    未注入任何生成器时显式报错——本系统不在内部生成候选（AI 在系统外面）。
    """
    if GENERATOR_OVERRIDE is not None:
        raw = _call_gen(GENERATOR_OVERRIDE, state, narrative_layer, node_context, seen=seen)
    elif AGENT_GENERATOR is not None:
        raw = _call_gen(AGENT_GENERATOR, state, narrative_layer, node_context, seen=seen)
    else:
        raise RuntimeError(
            "AI 生成槽位未注入：本系统采用「AI 穷举 → 系统剪枝」架构，AI 在系统外部，"
            "候选必须由 agent 注入。请设 ai_candidate.GENERATOR_OVERRIDE = callable(state, narrative_layer)"
            " 或调用 ai_candidate.set_agent_generator(fn)。（本系统不内置任何模型服务，不调用 Ollama。）"
        )

    key = _cache_key(state, narrative_layer, node_context, seen)
    if key in _CACHE:
        return _CACHE[key]

    out = []
    for it in (raw or []):
        c = _normalize(it)
        if c:
            out.append(c)
    _CACHE[key] = out
    return out


def _call_gen(fn, state, narrative_layer, node_context, seen=None):
    """调用 agent 生成器：探测是否接受 seen / node_context 形参（或含 **kwargs），是则传入。
    旧桩 (state, narrative_layer) 自动兼容——既不接受 seen 也不接受 node_context 时按两参调用。"""
    import inspect
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(state, narrative_layer)
    params = sig.parameters
    accepts_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
    if ("seen" in params) or accepts_kw:
        return fn(state, narrative_layer, node_context=node_context, seen=seen)
    if "node_context" in params:
        return fn(state, narrative_layer, node_context=node_context)
    return fn(state, narrative_layer)


def _normalize(item):
    """把候选规整为系统内部格式。

    模型 A 契约（agent 注入函数每轮被调用、树一层层长；注意：此为 steppable Model C 形态，agent 决策不在场）：agent 若显式提供了 chain_days / polarity /
    rationale，系统**必须尊重**，不得用 candidate_source 常量覆盖——否则 agent 的供料被
    「人写的常量库」削平（见 docs/model_a_interface.md §4）。仅在 agent 未提供时，才回退到
    candidate_source 常量作兜底（避免 agent 不供字段时下游 KeyError / 缺省失真）。
    """
    if not isinstance(item, dict):
        return None
    action = item.get("action")
    if action == "narrative":
        nid = canon.normalize_id(item.get("narrative"), "narrative")
        if not nid:
            return None
        return {
            "action": "narrative",
            "narrative": nid,
            "desc": item.get("desc", ""),
            # agent 优先；未给才回退常量
            "chain_days": item.get("chain_days")
                          if item.get("chain_days") is not None
                          else candidate_source.NARRATIVE_CHAIN_DAYS.get(nid, 2),
            "polarity": item.get("polarity")
                        if item.get("polarity") is not None
                        else candidate_source.NARRATIVE_POLARITY.get(nid),
            "rationale": item.get("rationale", ""),
        }
    if action == "direction":
        tgt = canon.normalize_id(item.get("target"), "direction")
        sgn = item.get("sign")
        # 范围外 target 由 canon.normalize_id（REJECT_BOUNDED）拒收返回 None —— 属「有界宇宙收敛保护」，
        # 不在 sign 白名单范围，保持静默拒收（另有 verify_canon 回归断言）。
        if not tgt:
            return None
        # sign 是 direction 必填字段：缺失 = 生成器输出残缺，必须抛错而非静默丢弃。
        if sgn is None:
            raise ValueError(
                "direction 候选缺失 sign（target=%r）。sign 是必填字段，系统不静默丢弃残缺候选——"
                "请修正生成器输出。" % tgt)
        # 动词语汇解禁（2026-10-19）：通用动词 + 目标专属动词 → 规范 sign；
        # 保留 raw_sign 供展示。tighten/easing 等均归一到 up/down，确保 _apply_move / dedup 一致。
        raw_sign = sgn
        canon_sign = _SIGN_CANON.get(sgn)
        if canon_sign is None:
            canon_sign = _TARGET_SIGN_ALIASES.get(tgt, {}).get(sgn)
        if canon_sign not in ("up", "down", "flat"):
            # 系统诚实性（2026-10-19 用户裁定「未识别抛错而非静默丢弃」）：未知/未登记 sign
            # 一律抛错，让生成器（尤其未来接入的 LLM）的输出缺口在构建期暴露，而非被静默吞掉、
            # 在下游树里无声断流且不可观测。要支持新动词，须显式补进 _SIGN_CANON / _TARGET_SIGN_ALIASES。
            raise ValueError(
                "sign 白名单拒收：target=%r 的 sign=%r 不在已识别动词集合 %s，"
                "也未在目标专属别名 %s 中。系统不静默丢弃未知 sign——"
                "请修正生成器输出，或向 ai_candidate._SIGN_CANON / _TARGET_SIGN_ALIASES 显式补登记。"
                % (tgt, sgn, sorted(_SIGN_CANON.keys()),
                   sorted(_TARGET_SIGN_ALIASES.get(tgt, {}).keys())))
        default_cd = (candidate_source.DIRECTION_CHAIN_DAYS_FLAT
                      if canon_sign == "flat" else candidate_source.DIRECTION_CHAIN_DAYS)
        return {
            "action": "direction",
            "target": tgt,
            "sign": canon_sign,
            "raw_sign": raw_sign,  # 原始动词（tighten/easing/loosen），honesty 展示用
            # 决定一 B：幅度分档。agent 显式给 magnitude ∈ {weak,typical,strong} 才分岔，
            # 未给（None）则由 _apply_move 默认取典型档 p50（不系统编造）。
            "magnitude": item.get("magnitude"),
            # agent 优先；未给才回退常量
            "chain_days": item.get("chain_days")
                          if item.get("chain_days") is not None
                          else default_cd,
            "rationale": item.get("rationale", ""),
        }
    if action == "transmission":
        frm = canon.normalize_id(item.get("from"), "transmission_node")
        to = canon.normalize_id(item.get("to"), "transmission_node")
        if not frm or not to:
            return None
        return {
            "action": "transmission",
            "from": frm,
            "to": to,
            # 可选：断言的传导方向（down=前者跌带动后者跌）；仅用于叙事/标签，不作涨跌点预测
            "sign": item.get("sign"),
            "chain_days": item.get("chain_days")
                          if item.get("chain_days") is not None else 2,
            "rationale": item.get("rationale", ""),
        }
    if action == "narrative_retire":
        # ② 叙事过期（模型 A 契约下）：agent 注入的函数每轮被调用时发「退休」信号，系统机械地把它从向下传递的 path_narratives 移除。
        # 非「下一步候选」，不生成子节点，只改路径上下文携带集。系统不对此做因果派生。
        nid = canon.normalize_id(item.get("narrative"), "narrative")
        if not nid:
            return None
        return {
            "action": "narrative_retire",
            "narrative": nid,
            "rationale": item.get("rationale", ""),
        }
    return None


def clear_cache():
    """清空单树缓存（每次 build_world_tree 调用前可清，避免跨树串味）。"""
    _CACHE.clear()
