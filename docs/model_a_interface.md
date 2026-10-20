# 模型 A 接口形状：agent 每轮在场，树一层层长

> **⚠️ 状态（2026-10-14 复判 → 2026-10-14 续作已落地工具接口）**：
> 本文件是**真 Model A（每轮由 agent 重新决策 / 掌控展开策略）的设计规格**。
> **工具接口已于 2026-10-14 落地**：系统现暴露 `get_frontier(sid)` / `expand_node(sid, nid, candidates)` / `is_done(sid)` 三工具，
> agent 每轮取 frontier → 自行生成候选 → `expand_node` 显式传入 → 系统跳过内部生成、只走世界规则后处理。
> 系统现状仍有 **steppable Model C**：`run_agent_loop` 现也已改为通过 `get_frontier/expand_node(sid,nid,candidates)/is_done` 三件套驱动（与真 Model A 机制收敛），仅「谁生成候选」不同——此处=一次性注册生成函数，真 Model A=外部 agent/LLM。两者共用同一接口，零回退。
> **「agent 每轮在场」现已由工具接口支撑**；驱动这层循环的外部 agent（决定每轮生成什么候选、掌控展开策略）是接口的**使用方责任**，系统侧已就绪。
> 历史：2026-10-13 曾误将 ③ 标为「模型 A 实跑」，已于 2026-10-14 更正为 steppable Model C；同日续作把真 Model A 工具接口落地（见 `docs/capability_boundary_current.md` 第 4 项与验收清单）。

> 状态：设计步交付物（2026-10-13）。本文定义「AI 每轮出现一次、树一层层长」下，
> agent 与系统之间每轮的接口形状。配套代码改动见 §4。
> 废止的上一份建议（path_moves + event_expires_on 系统侧解叙事过期）见 §7。

## 0. 为什么上一份报告的末尾建议失效

上一份报告（Ollama 拔除、注入点干净、回归 14/14）建立在**模型 C** 上：
agent 写一次生成函数，`world_tree._grow` 递归时反复调它。
在模型 C 里，AI 只在系统外面出现**一次**（写函数那次），递归展开过程中 AI 不在场。
于是「叙事在当前 state 下还成不成立」只能由**系统的规则**去算 → 才需要 `path_moves`
+ `event_expires_on` + `narratives.json` 的 `reconciliation.SPEC`。

**模型 A 推翻了这个前提**：agent 每轮出现一次，树一层层长。agent 每轮都看得到
`path_narratives` + 当前 `state`，它**自己就知道「LPR 已经降了，宽松预期该过期了」，
于是这一轮直接不生成那个叙事**。叙事过期不需要任何系统侧机制——它在供料端就被消掉了。

所以：
- §7 列出的系统侧叙事过期机制（path_moves / event_expires_on / valid_while 接线）**整体作废**。
- 上一轮报告末尾「先加 path_moves，再把事件型过期接进 `_grow`」**作废**——那是模型 C 的解法。

## 1. 系统侧职责边界（不变）

拿到候选之后的一切，与 agent 是否每轮在场**无关**，照旧：

- 硬剪枝（`hard_prune.is_pruned`）——逻辑/常识不可能
- 约束 veto（`constraints`）——世界规则层
- 停规则（无存活候选 / L3_visited / hit_real_record / irrelevant_to_focus）
- 路径累积（`path_narratives` / `path_marks` / `ledger_suspect`）
- 账本标记（`ledger_suspect` 沿路径累积）
- 窗口 fit（`chain_days <= decisive_window_days`）

状态转移 `_apply_move`：**机械、确定性**。agent 不计算状态变化，只提案（move），
系统应用 move 得到子状态。

全局域参数：决策窗口 `decisive_window_days`（fit 阈值）——系统常量，不随 agent 改变。

## 2. agent 侧职责（接管）

模型 A 下，agent 每轮对该节点做"看着真实 state 的判断"，这些**从系统侧搬到了 agent 侧**：

- **候选生成**：全量穷举当前状态下所有可能的下一步（叙事 / 方向），**不筛不判**——剪是系统的事。
- **叙事过期 / 路径-状态关系**：agent 每轮看 `path_narratives` + 当前 `state`，自行判断某叙事是否仍成立；
  不成立则本轮**不生成它**。（无需 `path_moves` / `event_expires_on` 等系统机制。）
- **每候选的 judgment 字段**（agent 判，系统必须尊重，见 §4 的 `_normalize` 修正）：
  - `chain_days`：这条链跨多久——**时间尺度的判断面，由 agent 给**
  - `polarity`：dovish / hawkish 等——agent 的判断
  - `rationale`：理由，供审计
  - `confidence`（可选）：epistemic confidence

## 3. 每轮接口形状（核心交付）

### 3.1 系统 → agent（每轮输入）

```json
{
  "node_id": "n12",
  "state": { "CPI": {"value": 1.2, "kind": "interpreted"}, "...": "..." },
  "path_narratives": ["policy_easing_expectation", "demand_weak"],
  "path_marks": ["...", "..."],
  "ledger_suspect": ["...", "..."],
  "as_of": "2026-10-13",
  "decision_window_days": 10,
  "frontier_state": "expanding"
}
```

- `path_narratives`：路径累积叙事集——agent 据此**不重复生成** + 做**叙事过期**（已不成立的不生成）。
- `as_of`：时间游标——agent 据此判断「哪些事件已落地」。
- `decision_window_days`：系统侧域参数——agent 供料时参考，使 `chain_days` 不超窗（但超窗由系统最终裁决，agent 不自我剪枝）。

### 3.2 agent → 系统（每轮输出）

```json
[
  {
    "action": "narrative",
    "narrative": "policy_easing_expectation",
    "chain_days": 10,
    "polarity": "dovish",
    "rationale": "低通胀打开货币政策空间，LPR 仍 >0",
    "confidence": 0.8
  },
  {
    "action": "direction",
    "target": "CPI",
    "sign": "up",
    "chain_days": 2,
    "rationale": "需求弱但基数效应推升读数"
  }
]
```

- agent **不做剪枝**，输出全量可能候选；系统随后剪。
- `chain_days` / `polarity` / `rationale` 由 agent 提供；系统**不得用常量覆盖**（见 §4）。

### 3.3 系统每轮存（持久化 / 跨轮交接）

```json
{
  "tree": { "n0": {"children": ["n1","n2"], "...": "..."}, "...": "..." },
  "nodes": {
    "n12": {
      "state": {"...": "..."},
      "path_narratives": ["..."],
      "path_marks": ["..."],
      "ledger_suspect": ["..."],
      "pruned_moves": [{"move": "...", "layer": "hard_prune", "reason": "..."}]
    }
  },
  "frontier": ["n13", "n14"],
  "as_of": "2026-10-13",
  "done": false
}
```

每轮闭环：**load checkpoint → agent 展开一个 frontier 节点（或一层）→ 系统 prune + append → save checkpoint**。
状态序列化在系统侧一次完成；**agent 无状态跨轮**——这把「跨轮编排成本」显式化（见 §6）。

## 4. 代码落点（让接口成真）

### 4.1 `_normalize` 必须尊重 agent 输入（已修）
`tools/ai_candidate.py` 原 `_normalize` **无条件**用 `candidate_source` 常量覆盖
`chain_days` / `polarity`，agent 供的料被「人写的常量库」削平。修正为：
agent 提供则用，未提供才回退 `candidate_source` 常量作兜底；并透传 `rationale`。

### 4.2 `_dedupe_opposite_readings_dict` 读候选自带 polarity（已修）
`tools/world_tree.py` 原从 `candidate_source.NARRATIVE_POLARITY` 重算 polarity，
忽略 agent 在候选上给的 `polarity`。修正为：先读 `c.get("polarity")`，None 才回退常量表。

### 4.3 暴露 `expand_node(sid, nid, candidates)` 原子 API（**2026-10-14 已落地**）
把 `_grow` 的「单节点展开」抽成可由外部 agent 循环驱动的函数；递归 `build_world_tree`
退化为该 API 的薄包装（或仅用于离线/测试）。这是模型 A 的真正架构切换点——
当前 `build_world_tree` 一次性递归、内部调 `generate`，是模型 C 的形态。

**已落地形态（真 Model A 工具接口）**：
- `get_frontier(sid)` → 返回当前待展开节点 id 列表（agent 每轮取它决定下一个 expand 目标）。
- `expand_node(sid, nid, candidates)` → agent 每轮**显式传入候选列表** `candidates`，系统跳过内部
  `ai_candidate.generate`（`_grow` 的 `raw_override` 形参），直接用传入候选走硬剪枝/约束/走步/路径累积/`_apply_move` 后处理；
  `nid` 展开后从 frontier 移除（待展开集合收缩）。候选生成完全搬到 agent 侧。
- `is_done(sid)` → `len(frontier)==0`，agent 每轮轮询判定是否展开完毕。

系统侧职责（硬剪枝 / 约束 veto / 停规则 / 路径累积 / 账本标记 / 窗口 fit / `_apply_move`）**不变**，
唯一搬移到 agent 的是「候选生成」本身（含 `narrative_retire` 信号，仍由系统机械从 `path_narratives` 移除）。
`steppable Model C`（`run_agent_loop`）现已同样通过本三件套驱动（见 world_tree.py:911），与真 Model A 机制收敛，
仅「谁生成候选」不同（注册生成函数 vs 外部 agent/LLM），零回退。另附 `tools/example_agent_loop.py` 演示外部 agent 每轮调三工具。
验收：`tools/verify_model_a_tools.py` = **19/19**（M1–M7：frontier/is_done 初始、agent 候选产出子节点且 LPR up 显式硬剪枝、幂等、驱动到 done、narrative_retire shrinking、agent 候选路径状态空间 == Model C、重构后 `run_agent_loop` 经三件套驱动结果与显式 agent 路径及 build_world_tree 三者零偏差）。

### 4.4 `candidate_source` 彻底退为常量库 + 工具
仅作 agent 未给字段时的**回退**，与 `impact_layer` / `run_full_loop` 的实用枚举工具。
不再有「生成器」语义。

## 5. 性能：不是消失，是换形态（修正上一份报告的误判）

上一份报告称「不调模型服务，超时问题消失」——这是**模型 C 下的判断**
（agent 写函数一次，函数在进程内被反复调）。在模型 A 下：

- 一次建树跨 **N 轮对话**，每轮都要：agent 生成 + 系统展开 + checkpoint 序列化/反序列化。
- 性能问题从「进程内调模型超时」→「**跨轮编排成本**」。不是消失，是挪了地方。

缓解（接口层面已埋点）：
- checkpoint 持久化使每轮只处理 O(1 个 frontier 节点) 的上下文，不重算全树。
- frontier 批处理（一轮展开一层而非一节点）。
- agent 每**节点**调一次（非每候选）。
- 增量 prompt：只发该节点 delta + 路径上下文，非全树。

## 6. 时间尺度的最终分配

时间尺度是**领域参数**（一步跨多长），不是判断。但其中「这条链跨多久」是判断面，
交给 agent；「全局窗口」与「状态如何随 move 变」是系统域参数/机械语义，系统定。

| 维度 | 归属 | 理由 |
|---|---|---|
| 每候选 `chain_days`（链跨多久） | **agent**（判断面） | 「看具体 state 判断这条链要多久」正是 agent 每轮在场该做的 |
| 全局决策窗口 `decisive_window_days` | **系统**（域参数） | 「一步跨多长」是领域常量，非判断 |
| 状态如何随 move 变（`_apply_move`） | **系统**（机械） | 确定性状态转移，agent 不计算 |
| 叙事是否仍成立（过期） | **agent**（每轮看 state） | 模型 A 下由 agent 供料时体现，无需系统机制 |
| 叙事有效期 schema（`narratives.json`） | **仅文档/参考** | 其 `valid_while` 可作文档沉淀 agent 的判据参考，但**不再接线到引擎**（见 §7） |

## 7. 因模型 A 而作废的待办

- `config/narratives.json` 的 `reconciliation.SPEC`（path_moves / event_expires_on / valid_while 系统侧落地）：
  **作废**。这些是为「AI 只出现一次」（模型 C）设计的；模型 A 下叙事过期由 agent 供料时体现。
  该文件降级为「叙事语义参考表」，不再作为引擎接线目标（已在该文件 reconciliation 段标注 VOIDED）。
- 上一轮报告末尾建议「先加 `path_moves`，再把事件型过期接进 `_grow`」：**作废**，基于模型 C。
- `config/narratives.json` 中 6 个 state 型叙事的 `blocked_by`（缺指标 / 时间尺度卡点）：
  在模型 A 下不再是「系统侧阻塞」，而是「agent 在供料时是否能判断该状态已翻转」的可用性提示——
  指标未接入时 agent 同样无法判断，故仍是有待补的数据缺口，但解法是补数据 + 让 agent 用，
  不是建系统侧谓词。
