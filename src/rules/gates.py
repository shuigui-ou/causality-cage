# -*- coding: utf-8 -*-
"""阶段门禁 —— §2.2 的阶段推进由**每源采样期数**驱动。

**量纲（第 8 次补丁修正，[H19]）**：门禁的样本单位是**该来源的采样期**，不是条目数。
  - 日频来源（数值增量型 / 热度型）：1 期 = 1 个采集日（该日全部条目聚合为 1 个日值）
  - 周频来源（周期型）：1 期 = 1 个采集周
  - 事件型来源（政策 / 公告 / 财经电报）：1 期 = 1 个采集批次（日批 → 1 期/天）

为什么不能数条目：单期条目数在不同来源间相差近百倍（实测 github_search 100 条/期
vs pbc_news 16 条/期），用条目计数会让 github 一天跨过阶段 2、pbc 两天才跨阶段 1——
**同一个门槛对两个源意味着完全不同的时间**。更实质的是，阶段 1 的稳健 z 检验需要
**跨日的日序列**：同一天的一堆条目算出的 MAD 是横截面离散度，不是时间序列波动。

**本模块只做计数与阶段判定，不含任何统计判据**，因此可安全用于阶段 0。

⚠️ **上面那张「1 期 = 多长」的表，在第 16 次补丁之前是「有声明、无消费」**（`[H31]`）：
当时的 `period_counts_by_source()` 实为 `groupby(source_id)["ingested_dt"].nunique()`
—— **纯按日计数、不读 `config/sources.json` 的 `source_period`**。后果不对称：
对 `daily` / `event` 源**恰好正确**（日批一天一期），而对 `weekly`（周期型）源会把
**1 个采集周数成 7 期 → 门禁提前跨阶段**。当时零个周期型已落地源，故**潜伏、未触发**
—— 而「**加源正是触发它的动作**」（用户第 18 轮：模板扩容很可能需要新增周期型源）。
本补丁把该表**实现化**为 `PERIOD_GRANULARITY` / `period_key()`（见下），并把
`period_counts_by_source()` 从存储层**移入本模块**（改成纯函数，可对合成输入断言）。
"""
from __future__ import annotations

from datetime import date

from .thresholds import T

# 门禁按顺序递增：先过第一个才是阶段 1，再过第二个才是阶段 2。
# 写成参数名元组（而不是写死 30/90），使 §14 成为常数的唯一出处。
GATE_PARAM_IDS = ("BASELINE_STAGE1_N", "BASELINE_STAGE2_N")

GATE_UNIT = "采样期"

# ---- §2.2 采样期的**切分粒度**：source_period → 粒度（`[H31]` 的修复点）----
# 这是 `config/sources.json` 的 `source_period` 字段**唯一的消费点**（第 16 次补丁前：零消费点）。
#
# ⚠️ 取值域**封闭**：新增取值必须先在此登记，否则 `period_key()` **显式失败** ——
#    **不静默退回按日计数**（`[H30]` 的同一条极性：机制在跑、但结果不是事实，比没有机制更糟）。
#    例：若将来要加「月度」（统计局一类）源，**必须先登记** `"monthly": "iso_month"` 并
#    补对应断言 —— 该失败是**设计意图**，不是缺陷（规则 14.8.7 的**适用域边界**）。
#
# `event` 与 `daily` 的**粒度标签不同、切分键相同**（都按采集日切）——因为日批就是事件型源
# 的采集单元（§2.2）。保留两个不同标签是为了让「为什么同键」在报告台账里**可读**，
# 而不是把它们伪装成同一件事。
PERIOD_GRANULARITY = {
    "daily": "day",         # 数值增量型 / 热度型：1 期 = 1 个采集日
    "event": "batch",       # 事件型：1 期 = 1 个采集批次（日批 ⇒ 1 期/天）
    "weekly": "iso_week",   # 周期型：1 期 = 1 个采集周（ISO 周）
}

_ISO_WEEK = "iso_week"


def period_key(ingested_dt, source_period) -> str:
    """把「采集日」映射到该来源的**采样期键**（§2.2，`[H31]`）。**纯函数。**

    为什么必须存在：门禁数的是「**采样期**数」，而「1 个采样期等于多长」由源自己的采集
    节奏决定。按日计数对 `weekly` 源会把 1 个采集周数成 7 期 —— **门禁提前跨阶段**，
    且**不报错**（正是本项目最忌的静默失效）。
    """
    granularity = PERIOD_GRANULARITY.get(source_period)
    if granularity is None:
        raise ValueError(
            "未知 source_period=%r：须先在 gates.PERIOD_GRANULARITY 登记其采样期粒度；"
            "**不得静默退回按日计数**（[H31] / [H30]）" % (source_period,))
    if granularity != _ISO_WEEK:
        return str(ingested_dt)
    iso = date.fromisoformat(str(ingested_dt)[:10]).isocalendar()
    return "%04d-W%02d" % (iso.year, iso.week)


def period_counts_by_source(series_pairs, period_of) -> dict:
    """§2.2 修正口径的「采样期数」：每源被成功采集的**不同期数**。**纯函数。**

    参数
    ----
    `series_pairs`：期级序列的 `(source_id, ingested_dt)` 逐行投影（存储层给出，**不含判定**）。
    `period_of`   ：`{source_id: source_period}`（由 `config.source_periods()` 派生）。

    为什么两个参数都做成**必填**（`[H31]`）：旧实现在存储层、无参数、**隐含「1 期 = 1 日」**，
    于是「该字段没人读」这件事**在调用点完全看不出来**。改为必填后，任何一次调用都必须
    显式给出「这些数据的期长从哪来」——**遗漏即 `TypeError`**（教训 19：接口也要「失败得干净」）。

    为什么纯函数而不是继续留在存储层：`[H31]` 的教训是「有声明、无消费」，而一个**读磁盘**的
    计数函数只能靠**端到端跑一遍**来验证；改成纯函数后，组 [20] 可以用**合成输入**证明
    「同一批采集日在 `weekly` 源上只算 1 期」，而无需改动任何数据。
    """
    seen: dict = {}
    for sid, ingested_dt in series_pairs:
        if sid not in period_of:
            # 期级序列里出现了「source_periods() 里没有的源」= config 与数据不一致。
            # 此时**无法知道**该源的期长 → 按日计数会是**编造**，故显式失败。
            raise ValueError(
                "期级序列含未登记 source_period 的源 %r —— config 与数据不一致；"
                "拒绝静默按日计数（[H31]）" % (sid,))
        seen.setdefault(sid, set()).add(period_key(ingested_dt, period_of[sid]))
    return {sid: len(keys) for sid, keys in seen.items()}


def stage_index(sample_count: int) -> int:
    """阶段号 = 已越过的门禁个数。

    刻意不写字面量：`len([...])` 天然产出 0 / 1 / 2，避免规则 14.4 的裸数字违规。
    """
    return len([p for p in GATE_PARAM_IDS if sample_count >= getattr(T, p)])


def may_judge(sample_count: int) -> bool:
    """阶段 0 = 只采集不判定（§1.2 `baseline_collecting`、§12 第 1 天第 4 条）。"""
    return stage_index(sample_count) > 0


def status_for(sample_count: int) -> str:
    """阶段 → 该阶段新信号的初始 status（§2.4）。"""
    return "new" if may_judge(sample_count) else "baseline_collecting"


def gate_report(sample_count: int, item_count=None, source_period=None) -> dict:
    """门禁状态的可审计快照（写入周期报告）。

    `source_period`（第 16 次补丁新增，默认 `None` → 与旧调用兼容）：把该源**实际使用的
    采样期粒度**写进载荷。为什么要写进载荷而不是只写在代码里 —— `[H31]` 的病根是
    「**声明了却没人读**」，只把读的代码补上，读者仍看不出「读到的到底是什么」；
    「不可见即等于没说」（组 [18] ⑦ 同一口径）。故载荷同时给出**声明值**与**派生值**，
    两者不一致时肉眼可辨（声明值不在 `PERIOD_GRANULARITY` 中即抛错）。
    """
    if source_period is None:
        granularity = None
    else:
        granularity = PERIOD_GRANULARITY.get(source_period)
        if granularity is None:
            raise ValueError(
                "未知 source_period=%r —— 采样期粒度未登记，拒绝出一个「期长不明」的门禁快照"
                "（[H31] / [H30]）" % (source_period,))
    return {
        "sample_unit": GATE_UNIT,
        "sample_period": source_period,
        "period_granularity": granularity,
        "sample_count": sample_count,
        "item_count": item_count,
        "stage": stage_index(sample_count),
        "may_judge": may_judge(sample_count),
        "gates": [
            {"param_id": p, "threshold": getattr(T, p), "unit": GATE_UNIT,
             "passed": sample_count >= getattr(T, p)}
            for p in GATE_PARAM_IDS
        ],
        # 规则 14.3.1：状态赋值本身也是一个判定结论，必须携带它踩的假设
        "derived_params_used": T.derived_params("§2.2.stage_gate.status_assignment"),
        "assumed_params": T.assumed_params("§2.2.stage_gate.status_assignment"),
    }
