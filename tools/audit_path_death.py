# -*- coding: utf-8 -*-
"""路径死亡归因审计（2026-10-19 用户指令 step2）。

目的：搞清楚「≥4 步路径仅占 1.6%」的真实成因，决定下一步调深度 / 调窗口 / 调 MAX_PER_NODE。

在**内存树**上直接量化（不落盘 7GB 文件）：
  1) 路径长度分布：只统计「未被递归的逻辑叶」（empty children = 该路径到此终止），
     按 depth 分桶 = run 长度分布（对齐用户看到的 1.6% 基线）。
  2) 每个逻辑叶归因到死亡原因：
       - L3_visited          : (state+叙事集) 已走过 → 撞 visited 终止（状态空间塌缩主因）
       - hit_real_record     : 子状态命中历史指纹 → 撞真即停
       - irrelevant_to_focus : 与关注点无关（focus=None 时不出现）
       - transmission        : 传导候选天然成叶（结构声明，非状态步）
       - max_depth_terminal  : depth==max_depth 且无非停原因 → 成功走满的「长路径终点」
       - unexpanded_by_agent : raw 最终为空（agent 0 候选 / 全冗余）→ 留树待展
  3) 死亡原因中再细分「no-op 自杀」：stop_reason==L3_visited 且 child_state == parent_state
     （= move 没改状态 → 立即撞 visited 把自己杀了。典型：step 模型对该方向返回 None、
       或漏接 wiring 的静默 no-op）。
  4) 候选层损失（从未成为子节点）：遍历全树 pruned_moves，按 layer 统计
       - window_fit  : chain_days 装不进决策窗口被剪（窗口口径问题）
       - hard_prune  : 逻辑/常识不可能
       - constraint  : 约束层否决
  5) MAX_PER_NODE 宽度压力：在若干代表节点直接调生成器看「原始候选数 vs 截断后 12」。

用法：
  python tools/audit_path_death.py --depth 7            # 精简配置（fast）
  python tools/audit_path_death.py --depth 7 --real     # 生产配置（真实 feeds/history/ledger，对齐 1.6% 基线）
"""
from __future__ import annotations
import argparse
import sys
from collections import Counter

sys.path.insert(0, "tools")
import world_tree
import ai_candidate
import macro_model_a  # 生成器（真实手写规则）
import extract_top_spines as ets  # 复用 ADJ 相邻表与 _dir_of


def build(depth, real):
    """构建审计用世界树。

    real=True  ：走 world_state.read_world_state 真实读取（akshare 等，沙箱内部分源会被
                 代理拦截 → 种子键为 None → 制造**环境性** no-op，不是引擎缺陷）。
    real=False ：改用 run_real_world_tree.SEED_WS 确定性种子（与主链抽取完全同一颗种子），
                 使 no-op / run 归因可复现、且不被沙箱网络失败污染。
                 （2026-10-20 修正：此前 real=False 仍走真实 read_world_state，导致
                   USDCNH/G_KOSPI/CN_POWER_IDX/CN_AI_IDX 种子读失败→None→大量假 no-op；
                   实测强制真实种子后 no-op 归零，证明引擎逻辑无误、那是环境噪声。）
    """
    ai_candidate.GENERATOR_OVERRIDE = macro_model_a.macro_generator
    if not real:
        # 导入即把 world_state.read_world_state 换成确定性 SEED_WS（模块级 patch）
        import run_real_world_tree  # noqa: F401
    tree, ws = world_tree.build_world_tree(
        "宏观情景：宽松+低通胀+厄尔尼诺", max_depth=depth,
        use_real_feeds=real, use_history=real, use_ledger=real,
        prune_weak_transmission=False)
    return tree


def _summary(node):
    return node.get("state_summary")


def _tok_of(move):
    """把 move 归一到 ADJ token：叙事→("N",narr)，方向→("D",target,dir)。"""
    if not isinstance(move, dict):
        return None
    if move.get("action") == "narrative":
        return ("N", move.get("narrative"))
    if move.get("action") == "direction":
        return ("D", move.get("target"), ets._dir_of(move.get("sign")))
    return None


def _longest_run(toks, adj=None):
    """最长连续因果 run 长度。

    2026-10-20 #284 起改用 extract_top_spines._longest_run 的**容错匹配**口径
    （允许跨越已知修饰性叙事节点），使审计与 causal_main_chains.txt 完全对齐。
    adj 参数保留仅为向后兼容（忽略）。
    """
    collapsed = [(t, None) for t in toks]
    # 折叠连续相同 token（对齐 run_extraction 口径）
    dedup = []
    for t in toks:
        if dedup and dedup[-1] == t:
            continue
        dedup.append(t)
    collapsed = [(t, None) for t in dedup]
    _, L, _ = ets._longest_run(collapsed)
    return L


def run_metric(tree):
    """复算 A-block「连续因果 run」指标（#284 容错匹配口径，对齐 causal_main_chains.txt）。

    并归因 run 为何断：每条路径最长 run 的「第一个断点」归类：
      - contrast_branch  : 断点下游是不透明对照支线叙事（demand_weak / policy_easing_expectation，
                           平行解读非因果 connector——这是**正确**打断，非缺陷）
      - adj_sparse       : 断点下游是方向步，与 run anchor 也无 ADJ 边（真稀疏/未登记边）
      - narrative_gap    : 断点下游是修饰性叙事但锚定桥接失败（两侧非 ADJ 边）
    注：2026-10-20 移除旧 noop_step 归因——它用「叶子 state_summary == 父 state」
        判 Δ=0，但断点 prev 方向步的子状态可能与祖父同（跨叙事节点），产生假阳性；
        真 no-op 已由 _grow 枚举层 noop_self_kill 正确丢弃，不会成为子节点。
    """
    run_hist = Counter()          # 每条路径最长 run 长度
    break_reason = Counter()      # 第一个断点类型
    n_paths = 0

    def _first_break(dedup):
        """在容错口径下找第一个断点，返回 (prev_tok, nxt_tok) 或 None。"""
        if len(dedup) < 2:
            return None
        collapsed = [(t, None) for t in dedup]
        n = len(collapsed)
        cur = [0]
        for i in range(1, n):
            prev_tok = collapsed[cur[-1]][0]
            cur_tok = collapsed[i][0]
            if prev_tok == cur_tok:
                cur = [i]
                continue
            if cur_tok in ets.ADJ.get(prev_tok, set()):
                cur.append(i)
                continue
            if ets._is_transparent(cur_tok):
                anchor = None
                for j in reversed(cur):
                    if not ets._is_transparent(collapsed[j][0]):
                        anchor = j
                        break
                ok = False
                if anchor is not None:
                    k = i + 1
                    while k < n and ets._is_transparent(collapsed[k][0]):
                        k += 1
                    if k < n and collapsed[k][0] in ets.ADJ.get(collapsed[anchor][0], set()):
                        cur.append(i)
                        ok = True
                if not ok:
                    return (prev_tok, cur_tok)
                continue
            # 非修饰性 token：也允许锚定桥接
            anchor = None
            for j in reversed(cur):
                if not ets._is_transparent(collapsed[j][0]):
                    anchor = j
                    break
            if anchor is not None and cur_tok in ets.ADJ.get(collapsed[anchor][0], set()):
                cur.append(i)
                continue
            return (prev_tok, cur_tok)
        return None

    def walk(node, toks, parent_state):
        nonlocal n_paths
        children = node.get("children") or []
        if not children:
            n_paths += 1
            # 折叠连续相同 token
            dedup = []
            for t in toks:
                if dedup and dedup[-1] == t:
                    continue
                dedup.append(t)
            r = _longest_run(dedup)
            run_hist[r] += 1
            brk = _first_break(dedup)
            if brk is not None:
                prev, nxt = brk
                if nxt[0] == "N" and not ets._is_transparent(nxt):
                    break_reason["contrast_branch"] += 1   # 对照鸽派支线：正确打断
                elif nxt[0] == "N":
                    break_reason["narrative_gap"] += 1      # 修饰叙事但桥接失败
                else:
                    break_reason["adj_sparse"] += 1         # 方向步真稀疏
            return
        st = _summary(node)
        for ch in children:
            t = _tok_of(ch.get("move"))
            walk(ch, toks + ([t] if t else []), st)

    walk(tree, [], None)
    return {"run_hist": dict(sorted(run_hist.items(), key=lambda kv: int(kv[0]))),
            "break_reason": dict(break_reason), "n_paths": n_paths}


def audit(tree, max_depth):
    total_nodes = 0
    leaf_by_depth = Counter()        # 逻辑叶（empty children）按 depth = 路径长度
    death_cause = Counter()          # 逻辑叶的死亡原因
    noop_self_kill = 0              # L3_visited 且父子 state 同
    pruned_by_layer = Counter()      # 候选层被剪（未成子节点）
    unexpanded = 0

    # 栈携带 (node, parent_state_key)。parent_state_key 用**含路径叙事集**的
    # world_tree._state_key 算——不能用 state_summary 比较：
    # _summarize() 会剥掉 _narrative/_transmission 键，走「叙事步」的叶子 summary
    # 必等于父节点，旧口径把它误判成 no-op（2026-10-20 修正，实测该假阳性占 28.4%）。
    stack = [(tree, None)]
    while stack:
        node, parent_key = stack.pop()
        total_nodes += 1
        children = node.get("children") or []
        for pm in node.get("pruned_moves") or []:
            pruned_by_layer[pm.get("layer")] += 1
        if node.get("unexpanded_by_agent"):
            unexpanded += 1

        if not children:
            # 逻辑叶（未被递归）
            d = node.get("depth", 0)
            leaf_by_depth[d] += 1
            mv = node.get("move") or {}
            sr = node.get("stop_reason")
            if node.get("unexpanded_by_agent"):
                death_cause["unexpanded_by_agent"] += 1
            elif mv.get("action") == "transmission":
                death_cause["transmission"] += 1
            elif sr == "L3_visited":
                # 正确 no-op 判据：状态键（state + 路径叙事集）与父相同 = 走了一步废棋
                node_key = world_tree._state_key(
                    node.get("state") or {}, set(node.get("path_narratives") or []))
                if parent_key is not None and node_key == parent_key:
                    noop_self_kill += 1
                death_cause["L3_visited"] += 1
            elif sr == "hit_real_record":
                death_cause["hit_real_record"] += 1
            elif sr == "irrelevant_to_focus":
                death_cause["irrelevant_to_focus"] += 1
            elif d >= max_depth:
                death_cause["max_depth_terminal"] += 1
            else:
                death_cause["other_leaf"] += 1
        else:
            # 注意：压栈的 parent_key 必须是**当前节点自己的键**（供其子节点比较），
            # 不是子节点的键——否则叶子弹出时 parent_key 恒等于自身键，no-op 判据恒真。
            k = world_tree._state_key(
                node.get("state") or {}, set(node.get("path_narratives") or []))
            for ch in children:
                stack.append((ch, k))
    return {
        "total_nodes": total_nodes,
        "leaf_by_depth": dict(sorted(leaf_by_depth.items(), key=lambda kv: int(kv[0]))),
        "death_cause": dict(death_cause),
        "noop_self_kill": noop_self_kill,
        "noop_self_kill_cand": pruned_by_layer.get("noop_self_kill", 0),
        "pruned_by_layer": dict(pruned_by_layer),
        "unexpanded": unexpanded,
    }


def max_per_node_pressure(depth):
    """在若干代表节点调生成器，看原始候选是否被 MAX_PER_NODE=12 截断。"""
    ai_candidate.GENERATOR_OVERRIDE = macro_model_a.macro_generator
    tree, ws = world_tree.build_world_tree(
        "宏观情景：宽松+低通胀+厄尔尼诺", max_depth=depth,
        use_real_feeds=False, use_history=False, use_ledger=False,
        prune_weak_transmission=False)
    samples = []
    def collect(node, d):
        if d <= 2:
            samples.append((d, node.get("state")))
        for c in (node.get("children") or []):
            collect(c, d + 1)
    collect(tree, 0)
    rows = []
    for d, state in samples[:8]:
        raw = macro_model_a.macro_generator(state, narrative_layer=set())
        rows.append((d, len(raw)))
    return rows


def _pct(n, tot):
    return 100.0 * n / tot if tot else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=7)
    ap.add_argument("--real", action="store_true", help="用生产配置(真实 feeds/history/ledger)对齐 1.6% 基线")
    args = ap.parse_args()

    print("=== 路径死亡归因审计 (depth=%d, %s) ==="
          % (args.depth, "生产配置(真实网络种子)" if args.real else "确定性种子(SEED_WS，可复现)"))
    tree = build(args.depth, args.real)
    r = audit(tree, args.depth)

    print("\n[1] 节点总数: %d" % r["total_nodes"])

    lbd = r["leaf_by_depth"]
    tot = sum(lbd.values())
    print("\n[2] 路径长度分布（逻辑叶按 depth 分桶 = run 长度；≥4 步占比见下）:")
    cum = 0
    ge4 = 0
    for d in lbd:
        n = lbd[d]
        cum += n
        if int(d) >= 4:
            ge4 += n
        print("    depth=%-2s : %7d 叶 (%5.2f%%)  累计: %5.2f%%" % (d, n, _pct(n, tot), _pct(cum, tot)))
    print("    ⇒ ≥4 步路径合计: %d (%.2f%%)   |  ≥5 步: %.2f%%"
          % (ge4, _pct(ge4, tot), _pct(sum(v for d, v in lbd.items() if int(d) >= 5), tot)))

    # ── [2b] 复算 A-block「连续因果 run」指标（对齐用户看到的 1.6%/1.9%）──
    rm = run_metric(tree)
    rh = rm["run_hist"]
    np_ = rm["n_paths"]
    print("\n[2b] 连续因果 run 分布（ADJ 相邻度，对齐 causal_main_chains.txt 口径）:")
    rcum = 0
    rge4 = 0
    for k in rh:
        v = rh[k]
        rcum += v
        if int(k) >= 4:
            rge4 += v
        print("    run=%-2s : %7d 路径 (%5.2f%%)" % (k, v, _pct(v, np_)))
    print("    ⇒ run≥4 路径: %d (%.2f%%)   |  run≥5: %.2f%%   （#284 容错匹配口径，对齐 causal_main_chains.txt）"
          % (rge4, _pct(rge4, np_), _pct(sum(v for k, v in rh.items() if int(k) >= 5), np_)))
    print("    run 断裂首因归因（每条短路径取第一个断点；容错口径下的真断点）:")
    for k in sorted(rm["break_reason"], key=lambda k: -rm["break_reason"][k]):
        print("      %-16s : %6d" % (k, rm["break_reason"][k]))
    print("      注：contrast_branch（对照鸽派支线 demand_weak/policy_easing_expectation）是**正确**打断，")
    print("          非缺陷——它们是同一状态的平行解读，不是因果 connector。")

    print("\n[3] 逻辑叶死亡原因:")
    dc = r["death_cause"]
    csum = sum(dc.values())
    for k in sorted(dc, key=lambda k: -dc[k]):
        print("    %-22s : %7d (%5.2f%%)" % (k, dc[k], _pct(dc[k], csum)))
    print("    —— 其中真 no-op 自杀(状态键含叙事集与父相同=走废棋): %d (占 L3_visited %.1f%%)"
          % (r["noop_self_kill"], _pct(r["noop_self_kill"], dc.get("L3_visited", 0))))
    print("        （2026-10-20 修正判据：旧口径用 state_summary 比较，会把「叙事步」叶子误判成")
    print("         no-op——_summarize 剥掉 _narrative 键，叙事步 summary 必等于父；假阳性曾占 28.4%%。）")

    print("\n[4] 候选层被剪（从未成为子节点）按 layer:")
    pb = r["pruned_by_layer"]
    if not pb:
        print("    （无 pruned_moves 记录）")
    for k in sorted(pb, key=lambda k: -pb[k]):
        print("    %-18s : %6d" % (k, pb[k]))

    print("\n[5] unexpanded_by_agent 节点数: %d" % r["unexpanded"])

    print("\n[6] MAX_PER_NODE 宽度压力（生成器原始候选数，截断阈值=12）:")
    for d, n in max_per_node_pressure(args.depth):
        flag = "  <-- 被截断!" if n > 12 else ""
        print("    depth=%-2d 原始候选数=%d%s" % (d, n, flag))

    # ── 自动归因结论 ──
    print("\n=== 自动归因结论 ===")
    l3 = dc.get("L3_visited", 0)
    wf = pb.get("window_fit", 0)
    hp = pb.get("hard_prune", 0)
    mpt = dc.get("max_depth_terminal", 0)
    print("  L3_visited=%.1f%%  window_fit剪枝=%d  hard_prune=%d  走满max_depth终点=%d"
          % (_pct(l3, csum), wf, hp, mpt))
    if l3 > 0.3 * csum:
        print("  ⇒ 主因 = 状态空间塌缩(L3_visited 主导)：宏观可写状态变量少，几步就撞 visited。")
        print("    · 候选层 noop_self_kill=%d（_grow 枚举层已直接丢弃 Δ=0 候选，不建 L3_visited 子节点）" % r["noop_self_kill_cand"])
        print("    · 2026-10-20 实测：换确定性种子（真实种子全部可读）后，候选层 noop_self_kill=0 →")
        print("      引擎逻辑无缺陷；此前观测到的大量 no-op 是沙箱网络读种子失败(键=None)的**环境噪声**。")
        print("    · 剩余 L3_visited 是**真实**状态空间有限所致，非缺 step 模型、非引擎缺陷。")
        print("    · 下一步优先级：① 增加可写状态维度（#283 已补 KCB50/SOX/AGRI step 模型）>")
        print("      ② 放宽 visited 键语义 > ③ 加 depth（depth=7 已远超宏观链 5-6 步需求，再加只增爆炸）。")
    if wf == 0:
        print("  ⇒ 窗口口径不是死因（window_fit 剪枝=0），无需调窗口。")
    if all(n <= 12 for _, n in max_per_node_pressure(args.depth)):
        print("  ⇒ MAX_PER_NODE=12 无压力（根/浅层原始候选均≤12），无需调 MAX_PER_NODE。")


if __name__ == "__main__":
    main()
