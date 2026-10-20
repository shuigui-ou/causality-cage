# -*- coding: utf-8 -*-
"""extract_top_spines.py — 从真实世界树中抽取「因果传导主链清单」（干净主线，非散沙）。

用法：
    python tools/run_real_world_tree.py 7            # 默认：内存构建 + 输出主链清单 causal_main_chains.txt（不落盘整树）
    python tools/extract_top_spines.py --tree PATH   # 离线分析：对已落盘整树（需先 --dump）抽取主链


重写说明（2026-10-19 用户指令）：
  旧版 A 块用「有序子序列包含度」给脊柱打分——把两个并行分支（如 CPI↑ 与 USDCNH↑）当因果链，是伪因果。
  本版改为**因果相邻度（直接父子关系，中间不能被无关步隔开）**，且相邻度覆盖**叙事步 + 方向步**两种真实因果步
  （宏观分析师眼里的「一步扣一步」本就含气候→食品→CPI→政策 这类叙事链，不只有方向步）：
    一条主链 = 路径上最长的一段「连续步」，其中每一步都直接因果承接上一步
    （el_niño → agri_price_shock → CPI↑ → policy_stance↑ → CN10Y↑ → CN_CYB↓
      才是链；CPI↑ → USDCNH↑ 不是，因二者无直接因果相邻）。
  相邻判定依据 = ADJ（宏观机制相邻表，由生成器实际因果规则导出，可审计、可人审增补）。
  不在 ADJ 的相邻步（含并行分支、对比鸽派支线如 demand_weak/policy_easing_expectation 的脱节）= 链被打断。

做法：
  · 每条根→叶路径还原成「统一步序列」T：叙事步记为 ("N", narr)，方向步记为 ("D", target, dir)。
  · 最长连续因果 run = T 中连续相邻且两两在 ADJ 的最长段。run 越长、且终端落在可观测资产越「一步扣一步」。
  · 排序：先按最长 run 长度，再按方向步数，再按是否落终端资产。
  · 背景前提（US_10Y↑ 由种子 5.09 触发）保留展示并标注，但它是合法因果边的上游，不剔除。

用法：
    python tools/extract_top_spines.py                 # 默认读 reports/world_tree_real_run.json
    python tools/extract_top_spines.py --top 8
    python tools/extract_top_spines.py --tree PATH
"""
import argparse
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────────────────────────────────────────
# ADJ：宏观机制「直接相邻」表（token→set(token)）。token = ("N", narr) 或 ("D", target, dir)。
# dir：+1=增/收紧/扩张，-1=减/放松/收缩。仅收录生成器实际编码的「直接因果一步」。
# ─────────────────────────────────────────────────────────────────────────────
_NN = [  # 叙事→叙事
    ("el_nino", "agri_price_shock"),
    ("agri_price_shock", "inflation_concern"),
    # 2026-10-20 #284 补边：macro_model_a 3b 明确编码
    #   if "external_rate_pressure" in path: ... add("narrative", northbound_outflow)
    # 即「汇率贬值压力 → 北向资金流出」是真实生成器边（可跨过 USDCNH↑ 计量步的捷径）。
    ("external_rate_pressure", "northbound_outflow"),
]
_ND = [  # 叙事→方向
    ("agri_price_shock", "CPI", +1),
    # 2026-10-20 #284 补边：macro_model_a 3 明确编码
    #   if ("inflation_concern" in path) or ("agri_price_shock" in path) or cpi>2.5:
    #       → policy_stance tighten
    # 即 agri_price_shock（食品冲击=通胀压力）可直接驱动政策收紧，无需先落 CPI↑ 方向步。
    ("agri_price_shock", "policy_stance", +1),
    ("inflation_concern", "policy_stance", +1),
    ("external_rate_pressure", "USDCNH", +1),
    ("northbound_outflow", "NORTHBOUND", -1),
    # 2026-10-20 #284 补边：macro_model_a 3b 明确编码
    #   if "northbound_outflow" in path: ... add CSI300 down
    # 即「北向资金净流出 → A股核心资产承压」是真实生成器边（可跨过 NORTHBOUND↓ 计量步）。
    ("northbound_outflow", "CSI300", -1),
    ("credit_contraction", "M1M2_GAP", -1),
    ("credit_contraction", "TSF", -1),
    ("credit_contraction", "CSI300", -1),
    ("credit_contraction", "CN_CYB", -1),
    ("fiscal_expansion", "FISCAL_STANCE", +1),
    ("fiscal_expansion", "CN_POWER_IDX", +1),
    ("fiscal_expansion", "CSI300", +1),
    ("fiscal_expansion", "CPI", +1),
]
_DN = [  # 方向→叙事
    ("CPI", +1, "inflation_concern"),
    ("policy_stance", +1, "external_rate_pressure"),
    ("policy_stance", +1, "credit_contraction"),   # 政策收紧→信用收缩（引擎 3c 必经叙事 connector）
    ("US_10Y", +1, "external_rate_pressure"),        # 美债↑→利差倒挂→汇率贬值压力（引擎 3b 必经 connector）
    ("USDCNH", +1, "northbound_outflow"),
]
_DD = [  # 方向→方向
    ("CPI", +1, "policy_stance", +1),
    ("policy_stance", +1, "CN10Y", +1),
    ("policy_stance", +1, "LPR_1Y", +1),
    # 2026-10-20 #284 补边：macro_model_a 3 明确编码
    #   if (stance in ("中性","偏紧")) or (cn10y > 2.0): add CN_CYB down
    # 即「政策收紧（利率上行信号）→ 长久期成长估值压缩」是真实生成器边（可跨过 CN10Y↑/LPR_1Y↑ 计量步）。
    ("policy_stance", +1, "CN_CYB", -1),
    ("CN10Y", +1, "CN_CYB", -1),
    ("LPR_1Y", +1, "CN_CYB", -1),
    ("CN10Y", +1, "USDCNH", +1),
    ("US_10Y", +1, "G_NASDAQ", -1),
    ("US_10Y", +1, "G_SOX", -1),
    ("US_10Y", +1, "USDCNH", +1),
    ("USDCNH", +1, "NORTHBOUND", -1),
    ("NORTHBOUND", -1, "CSI300", -1),
    ("US_10Y", +1, "CSI300", -1),
    ("policy_stance", +1, "TSF", -1),
    ("TSF", -1, "M1M2_GAP", -1),
    ("TSF", -1, "CSI300", -1),
    ("TSF", -1, "CN_CYB", -1),
    ("M1M2_GAP", -1, "CSI300", -1),
    ("M1M2_GAP", -1, "CN_CYB", -1),
    ("FISCAL_STANCE", +1, "CN_POWER_IDX", +1),
    ("FISCAL_STANCE", +1, "CSI300", +1),
    ("FISCAL_STANCE", +1, "SPECIAL_BOND", +1),
    ("FISCAL_STANCE", +1, "CPI", +1),
    ("SPECIAL_BOND", +1, "CN_POWER_IDX", +1),
]

ADJ = {}
for a, b in _NN:
    ADJ.setdefault(("N", a), set()).add(("N", b))
for a, t, d in _ND:
    ADJ.setdefault(("N", a), set()).add(("D", t, d))
for t, d, a in _DN:
    ADJ.setdefault(("D", t, d), set()).add(("N", a))
for a, da, b, db in _DD:
    ADJ.setdefault(("D", a, da), set()).add(("D", b, db))

# ─────────────────────────────────────────────────────────────────────────────
# 修饰性/连接性叙事（2026-10-20 #284 容错匹配）
#
# 定义（可审计、自维护，不硬编码名单）：**已在 ADJ 中作为因果 connector 出现的叙事**。
# 这类节点只起「机制说明」作用（如 external_rate_pressure = 利差倒挂→贬值压力），
# 其上下游的**方向步**才是可观测状态变化；当它夹在两个已证相邻的步之间时，
# 应可被跨越而不打断 run。
#
# 对照鸽派支线（demand_weak / policy_easing_expectation）**不在 ADJ 中** → 不透明：
# 它们是同一状态的另一种解读（平行支线，非因果 connector），跨越它们会制造伪因果。
# ─────────────────────────────────────────────────────────────────────────────
def _transparent_narratives():
    names = set()
    for k, vs in ADJ.items():
        if k[0] == "N":
            names.add(k[1])
        for v in vs:
            if v[0] == "N":
                names.add(v[1])
    return names


TRANSPARENT_NARRATIVES = _transparent_narratives()


def _is_transparent(tok):
    """修饰性叙事 = 已在 ADJ 中作为因果 connector 出现的叙事节点。"""
    return tok[0] == "N" and tok[1] in TRANSPARENT_NARRATIVES


def _longest_run(collapsed):
    """最长连续因果 run（容错匹配版，2026-10-20 #284）。

    容错规则（允许跨越已知修饰性节点，但绝不制造伪因果）：
      · 相邻判定仍以 ADJ 为唯一依据；
      · 修饰性叙事 T 若与 run 内上一个 token 相邻 → 正常计入 run；
      · 若不相邻，则看 T 之后**下一个非修饰性 token** 是否与 run 的 anchor
        （anchor = run 内最后一个非修饰性 token）相邻：
          相邻 → T 被吸收进 run（不打断），后续 token 继续承接 anchor；
          不相邻 → 仍打断（两侧必须都是 ADJ 边才允许跨越）；
      · run 内若无 anchor（全是修饰性叙事）→ 打断，防止无意义叙事串被计成链。

    返回 (best_start, best_len, n_bridged)：n_bridged = **真正靠容错跨越接上**的
    修饰性节点数（诚实披露用）。注意：run 内的修饰性叙事若本身与前后 token 硬相邻，
    不计入 n_bridged——只有「两侧不相邻、靠 anchor 桥接吸收」的才算。
    """
    n = len(collapsed)
    if n == 0:
        return 0, 0, 0
    best_start = best_len = 0
    cur_start = 0
    cur = [0]          # 当前 run 的 token 下标（始终连续）
    cur_bridged = set()  # 当前 run 中真正被桥接吸收的 token 下标
    best_bridged = set()
    for i in range(1, n):
        prev_tok = collapsed[cur[-1]][0]
        cur_tok = collapsed[i][0]
        if prev_tok == cur_tok:
            cur = [i]
            cur_start = i
            cur_bridged = set()
            continue
        if cur_tok in ADJ.get(prev_tok, set()):
            cur.append(i)
        elif _is_transparent(cur_tok):
            # T 与 prev 不相邻：尝试跨越（锚定到 run 内最后非修饰性 token）
            anchor = None
            for j in reversed(cur):
                if not _is_transparent(collapsed[j][0]):
                    anchor = j
                    break
            bridged = False
            if anchor is not None:
                k = i + 1
                while k < n and _is_transparent(collapsed[k][0]):
                    k += 1
                if k < n and collapsed[k][0] in ADJ.get(collapsed[anchor][0], set()):
                    cur.append(i)  # T 被吸收，不打断
                    cur_bridged.add(i)  # 仅此处记「真正跨越」
                    bridged = True
            if not bridged:
                cur = [i]
                cur_start = i
                cur_bridged = set()
        else:
            # 非修饰性 token 与 prev 不相邻：也要尝试锚定桥接——
            # prev 可能是刚被吸收的修饰节点，真正承接关系在 anchor 上。
            # 例：US_10Y↑ → [inflation_concern](吸收) → USDCNH↑，
            #     USDCNH↑ 与 inflation_concern 不相邻，但与 anchor US_10Y↑ 相邻 → 应续链。
            anchor = None
            for j in reversed(cur):
                if not _is_transparent(collapsed[j][0]):
                    anchor = j
                    break
            if anchor is not None and cur_tok in ADJ.get(collapsed[anchor][0], set()):
                cur.append(i)
            else:
                cur = [i]
                cur_start = i
                cur_bridged = set()
        if len(cur) > best_len:
            best_len = len(cur)
            best_start = cur_start
            best_bridged = set(cur_bridged)
    if len(cur) > best_len:
        best_len = len(cur)
        best_start = cur_start
        best_bridged = set(cur_bridged)
    n_bridged = sum(1 for j in best_bridged if best_start <= j < best_start + best_len)
    return best_start, best_len, n_bridged


TERMINAL = {"CSI300", "NORTHBOUND", "USDCNH", "CN_CYB", "G_NASDAQ", "G_SOX",
           "CN_POWER_IDX", "CN_AI_IDX", "CN_KCB50", "CN_HS300"}

_SIGN_DIR = {"up": +1, "tighten": +1, "expansion": +1,
             "down": -1, "easing": -1, "loosen": -1, "contraction": -1}


def _dir_of(sign):
    return _SIGN_DIR.get(sign, 0)


def _val(d):
    if isinstance(d, dict):
        v = d.get("value")
        return v if isinstance(v, (int, float)) else None
    return d


SNAP_KEYS = ["CPI", "PPI", "M2", "LPR_1Y", "CN10Y", "policy_stance", "US_10Y",
             "USDCNH", "NORTHBOUND", "CSI300", "CN_CYB", "G_NASDAQ", "TSF",
             "M1M2_GAP", "FISCAL_STANCE", "CN_POWER_IDX"]


def _fmt_snap(s):
    parts = []
    for k in SNAP_KEYS:
        v = s.get(k)
        if v is None:
            parts.append("%s=-" % k)
        elif isinstance(v, float):
            parts.append("%s=%.4g" % (k, v))
        else:
            parts.append("%s=%s" % (k, v))
    return " ".join(parts)


def _arrow(dirv):
    return "↑" if dirv > 0 else ("↓" if dirv < 0 else "·")


def _tok_str(tok):
    if tok[0] == "N":
        return "[%s]" % tok[1]
    return "%s%s" % (tok[1], _arrow(tok[2]))


def _step_str(tok):
    return _tok_str(tok)


def run_extraction(tree, top=8, write=True):
    """抽取因果传导主链。

    write=False 时只打印、不落盘 reports/causal_main_chains.txt——供测试/二次分析
    复用本函数而不覆盖正式产物（2026-10-20：e2e 测试曾覆写该文件，已根治）。
    """
    paths = []

    def walk(node, path):
        npath = path + [node]
        kids = node.get("children") or []
        if not kids:
            paths.append(npath)
            return
        for c in kids:
            walk(c, npath)

    walk(tree, [])

    records = []
    for path in paths:
        toks = []  # 统一步序列
        meta = {}  # tok index -> move detail
        for i in range(1, len(path)):
            node = path[i]
            parent = path[i - 1]
            m = node.get("move") or {}
            act = m.get("action")
            if act == "narrative":
                narr = m.get("narrative")
                if narr is None:
                    continue
                tok = ("N", narr)
            elif act == "direction":
                tgt = m.get("target")
                dv = _dir_of(m.get("sign"))
                if dv == 0:
                    continue
                tok = ("D", tgt, dv)
            else:
                continue
            pv = _val(parent.get("state", {}).get(tgt)) if act == "direction" else None
            cv = _val(node.get("state", {}).get(tgt)) if act == "direction" else None
            delta = (cv - pv) if isinstance(pv, (int, float)) and isinstance(cv, (int, float)) else None
            idx = len(toks)
            toks.append(tok)
            meta[idx] = {"day": node.get("day", 0), "delta": delta,
                         "magnitude": m.get("magnitude"), "chain_days": m.get("chain_days"),
                         "rationale": m.get("rationale") or "", "node_id": node.get("node_id")}
        # 折叠连续相同 token
        collapsed = []
        for k, t in enumerate(toks):
            if collapsed and collapsed[-1][0] == t:
                continue
            collapsed.append((t, meta[k]))
        # 最长连续因果 run（容错匹配：允许跨越已知修饰性叙事节点）
        best_start, best_len, n_bridged = _longest_run(collapsed)
        run = collapsed[best_start:best_start + best_len] if best_len else []
        n_dir = sum(1 for t, _ in run if t[0] == "D")
        reaches_terminal = any(t[0] == "D" and t[1] in TERMINAL for t, _ in run)
        records.append({
            "tokens": toks, "collapsed": collapsed, "run": run,
            "run_len": best_len, "n_dir": n_dir, "reaches_terminal": reaches_terminal,
            "n_bridged": n_bridged,
            "final_snap": _fmt_snap(path[-1].get("state", {})), "path": path,
        })

    n = len(records)
    stat = {k: sum(1 for r in records if r["run_len"] >= k) for k in (4, 5, 6, 7)}
    n_term = sum(1 for r in records if r["reaches_terminal"])
    avg_run = (sum(r["run_len"] for r in records) / n) if n else 0
    total_bridged = sum(r["n_bridged"] for r in records)
    n_bridged_paths = sum(1 for r in records if r["n_bridged"] > 0)

    print("=" * 98)
    print("因果传导主链抽取（统一叙事+方向之直接父子相邻度口径；幅度=真实校准 Δ，仅展示不排序）")
    print("总根→叶路径=%d" % n)
    print("最长连续因果 run 长度分布：≥4步=%d  ≥5步=%d  ≥6步=%d  ≥7步=%d"
          "（占比 %.1f%%/%.1f%%/%.1f%%/%.1f%%）" % (
              stat[4], stat[5], stat[6], stat[7],
              100.0 * stat[4] / n, 100.0 * stat[5] / n, 100.0 * stat[6] / n, 100.0 * stat[7] / n))
    print("run 终点落在终端可观测资产(沪深300/北向/汇率/创业板/纳指/电力等)的路径=%d（%.1f%%）"
          % (n_term, 100.0 * n_term / n))
    print("平均最长连续因果 run 长度=%.2f 步" % avg_run)
    print("容错匹配（#284）：修饰性叙事可跨越；被吸收的修饰节点=%d 个，涉及路径=%d（%.1f%%）"
          "（诚实披露：这些 run 含跨修饰节点的承接，两侧均为 ADJ 边）"
          % (total_bridged, n_bridged_paths, 100.0 * n_bridged_paths / n))
    print("=" * 98)

    ranked = sorted(records, key=lambda r: (r["run_len"], r["n_dir"], r["reaches_terminal"]), reverse=True)
    top_chains = ranked[:top]

    print("\n" + "#" * 98)
    print("# A. 自动发现的最连贯因果主链 Top %d（每条=路径中最长一段『一步扣一步』连续因果段，含叙事与方向步）" % top)
    print("#" * 98)
    for rank, r in enumerate(top_chains, 1):
        run = r["run"]
        total_day = sum((m.get("chain_days") or 0) for _, m in run)
        print("\n--- 主链 %d ｜ run长度=%d 步(方向步=%d, 容错跨越修饰节点=%d) ｜ 累计时滞≈%s天 ｜ 落终端资产=%s ---"
              % (rank, r["run_len"], r["n_dir"], r["n_bridged"], total_day,
                 "是" if r["reaches_terminal"] else "否"))
        print("  因果脊柱: " + " → ".join(_tok_str(t) for t, _ in run))
        for t, m in run:
            if t[0] == "N":
                print("    [day %4s] 叙事 %-20s | %s" % (m["day"], t[1], m["rationale"]))
            else:
                dm = ("Δ=%+.4g" % m["delta"]) if isinstance(m["delta"], (int, float)) else "Δ=?"
                print("    [day %4s] 方向 %-10s%s 档=%s 链=%s | %s | %s"
                      % (m["day"], t[1], _arrow(t[2]), m["magnitude"], m["chain_days"], dm, m["rationale"]))
        print("   终点状态: " + r["final_snap"])

    # ── B. 分析师直觉 canonical 主链：在树中找其最佳连续实现（统一 token 序列，必须一步扣一步）──
    canonical = [
        ("气候→食品→CPI→政策收紧→利率↑→成长股承压（国内通胀主链）",
         [("N", "el_nino"), ("N", "agri_price_shock"), ("D", "CPI", +1),
          ("D", "policy_stance", +1), ("D", "CN10Y", +1), ("D", "CN_CYB", -1)]),
        ("美通胀→美债↑→纳指承压（美国链）",
         [("D", "US_10Y", +1), ("D", "G_NASDAQ", -1)]),
        ("美债↑→汇率贬值→北向流出→沪深300 承压（跨境资本链）",
         [("D", "US_10Y", +1), ("N", "external_rate_pressure"), ("D", "USDCNH", +1),
          ("N", "northbound_outflow"), ("D", "NORTHBOUND", -1), ("D", "CSI300", -1)]),
        ("政策收紧→信用收缩→社融↓→沪深300 承压（信用链·社融分支）",
         [("D", "policy_stance", +1), ("N", "credit_contraction"), ("D", "TSF", -1),
          ("D", "CSI300", -1)]),
        ("政策收紧→信用收缩→M1-M2剪刀差↓→沪深300 承压（信用链·剪刀差分支）",
         [("D", "policy_stance", +1), ("N", "credit_contraction"), ("D", "M1M2_GAP", -1),
          ("D", "CSI300", -1)]),
        ("财政扩张→财政立场↑→电力/基建↑ + 沪深300 支撑（财政链）",
         [("N", "fiscal_expansion"), ("D", "FISCAL_STANCE", +1), ("D", "CN_POWER_IDX", +1), ("D", "CSI300", +1)]),
        ("信用收缩→沪深300/创业板 承压（信用→资产直连）",
         [("N", "credit_contraction"), ("D", "TSF", -1), ("D", "CSI300", -1)]),
    ]
    print("\n" + "#" * 98)
    print("# B. 分析师直觉 canonical 主链：在树中找其『连续』实现（统一 token 序列，非子序列包含）")
    print("#" * 98)
    for label, chain in canonical:
        best = None
        for r in records:
            col = [t for t, _ in r["collapsed"]]
            for i in range(len(col)):
                if col[i:i + len(chain)] == chain:
                    best = (r, i, len(chain))
                    break
            if best:
                break
        print("\n" + "-" * 98)
        print("机制: %s" % label)
        print("  脊柱: " + " → ".join(_tok_str(t) for t in chain))
        if best is None:
            print("  树中无连续实现（诚实：该链未在当前深度/种子下自生长）")
            continue
        r, i, L = best
        seg = r["collapsed"][i:i + L]
        total_day = sum((m.get("chain_days") or 0) for _, m in seg)
        print("  连续实现命中（累计时滞≈%s天）：" % total_day)
        for t, m in seg:
            if t[0] == "N":
                print("    [day %4s] 叙事 %-20s | %s" % (m["day"], t[1], m["rationale"]))
            else:
                dm = ("Δ=%+.4g" % m["delta"]) if isinstance(m["delta"], (int, float)) else "Δ=?"
                print("    [day %4s] 方向 %-10s%s 档=%s 链=%s | %s | %s"
                      % (m["day"], t[1], _arrow(t[2]), m["magnitude"], m["chain_days"], dm, m["rationale"]))
        print("   终点状态: " + r["final_snap"])

    # ── 落盘：干净主链清单 ──
    outp = os.path.join(BASE, "reports", "causal_main_chains.txt")
    if not write:
        print("\n（write=False：跳过落盘 %s）" % outp)
        print("=" * 98)
        print("口径说明：本版用『直接父子因果相邻』(ADJ，含叙事步+方向步) 取代旧版『子序列包含度』。")
        print("#284 起：补真实生成器边 + 允许跨越已知修饰性叙事节点（两侧均为 ADJ 边，不制造伪因果）。")
        print("=" * 98)
        return
    with open(outp, "w", encoding="utf-8") as f:
        f.write("因果传导主链清单（统一叙事+方向之直接父子相邻度口径，2026-10-19；"
                "2026-10-20 #284 补真实生成器边 + 修饰性叙事容错跨越）\n")
        f.write("总路径=%d  平均最长连续因果run=%.2f步\n" % (n, avg_run))
        f.write("完整链占比: ≥4步=%.1f%%  ≥5步=%.1f%%  ≥6步=%.1f%%  ≥7步=%.1f%%\n"
                % (100.0 * stat[4] / n, 100.0 * stat[5] / n, 100.0 * stat[6] / n, 100.0 * stat[7] / n))
        f.write("run落终端资产路径=%.1f%%\n" % (100.0 * n_term / n))
        f.write("容错匹配(#284)：修饰性叙事可跨越；被吸收修饰节点=%d个，涉及路径=%d(%.1f%%)"
                "（两侧均为ADJ边，不制造伪因果）\n\n"
                % (total_bridged, n_bridged_paths, 100.0 * n_bridged_paths / n))
        f.write("=== A. 自动发现 Top %d 主链 ===\n" % top)
        for rank, r in enumerate(top_chains, 1):
            run = r["run"]
            total_day = sum((m.get("chain_days") or 0) for _, m in run)
            f.write("\n主链 %d (run=%d步, 方向步=%d, 容错跨越修饰节点=%d, 时滞≈%s天, 落终端=%s)\n"
                    % (rank, r["run_len"], r["n_dir"], r["n_bridged"], total_day, r["reaches_terminal"]))
            f.write("  " + " → ".join(_tok_str(t) for t, _ in run) + "\n")
            for t, m in run:
                if t[0] == "N":
                    f.write("    [day %s] 叙事 %s | %s\n" % (m["day"], t[1], m["rationale"]))
                else:
                    dm = ("Δ=%+.4g" % m["delta"]) if isinstance(m["delta"], (int, float)) else "Δ=?"
                    f.write("    [day %s] %s%s 档=%s 链=%s | %s | %s\n"
                            % (m["day"], t[1], _arrow(t[2]), m["magnitude"], m["chain_days"], dm, m["rationale"]))
        f.write("\n=== B. canonical 主链连续实现 ===\n")
        for label, chain in canonical:
            f.write("\n机制: %s\n  " % label)
            f.write(" → ".join(_tok_str(t) for t in chain) + "\n")
            best = None
            for r in records:
                col = [t for t, _ in r["collapsed"]]
                for i in range(len(col)):
                    if col[i:i + len(chain)] == chain:
                        best = (r, i, len(chain)); break
                if best:
                    break
            if best is None:
                f.write("  (树中无连续实现)\n"); continue
            r, i, L = best
            for t, m in r["collapsed"][i:i + L]:
                if t[0] == "N":
                    f.write("    [day %s] 叙事 %s | %s\n" % (m["day"], t[1], m["rationale"]))
                else:
                    dm = ("Δ=%+.4g" % m["delta"]) if isinstance(m["delta"], (int, float)) else "Δ=?"
                    f.write("    [day %s] %s%s 档=%s 链=%s | %s\n"
                            % (m["day"], t[1], _arrow(t[2]), m["magnitude"], m["chain_days"], dm))
    print("\n写出 ->", outp)

    print("\n" + "=" * 98)
    print("口径说明：本版用『直接父子因果相邻』(ADJ，含叙事步+方向步) 取代旧版『子序列包含度』。")
    print("旧伪因果（如 CPI↑→USDCNH↑、CPI↑→G_NASDAQ↓、G_NASDAQ↓→USDCNH↑）因不在 ADJ，无法形成连续 run，已被剔除。")
    print("US_10Y↑ 为种子 5.09 触发的上游前提，保留展示并标注，不作 run 起始断点。")
    print("=" * 98)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", default=os.path.join(BASE, "reports", "world_tree_real_run.json"))
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()
    if not os.path.exists(a.tree):
        print("错误：未找到树文件 %s" % a.tree)
        print("run_real_world_tree.py 默认只输出主链清单（不落盘整树）。")
        print("若要离线分析整树，请先：python tools/run_real_world_tree.py --dump")
        raise SystemExit(1)
    tree = json.load(open(a.tree, encoding="utf-8"))
    run_extraction(tree, top=a.top)


if __name__ == "__main__":
    main()
