# -*- coding: utf-8 -*-
"""爆点模板库的消费端：匹配 + 提醒 + 缺口工单。**本项目第一个「产出侧」模块。**

它回答的是：「这个故事属于哪一类 / 历史上同类故事的爆点在哪个位置 / 现在走到哪一环 /
下一环该看什么信号 / 这个信号当前有没有已落地源」。

三条设计决定，每条都对应一个本项目已登记的教训：

1. **频率字段恒为空**。实例数为 0 时统计量不含信息量（见 `config/burst_templates.json`
   的 `frequency_policy`）。宁可输出 `null` + `basis=uncalibrated`，也不输出
   「看着精确、没有出处」的数字（`[H18]` 那一族的病根）。
2. **每个「下一验证信号」必须挂源状态**。复用 §2.5 / `[H25]` 的四分类。
   否则提醒指不到可观测的东西 → 永远无法回标 → 闭环在第一环就断。
3. **源状态是「派生」的，不是「信模板自己写的」**。模板只作**声明**，引擎从
   `config/sources.json` **重新推导**，两者不一致即上报（`[H27]` 的教训：
   一派生一硬编码必然漂移）。
4. **影响面不是一个字段，是一批「必须挂源状态」的位置**。链回答「传导到哪了」，
   影响面回答「传导到谁了 / 影响多大 / 多长时间」——而后者的**大部分当前不可观测**。
   所以影响面的产出不是新增一个可断言字段，而是**把「不可观测」写成可跟踪的工单**：
   每个 target 都挂**派生**的源状态，非 `collector_landed` 者报缺口。
   时长（time_scale）恒 `null` + `uncalibrated`，按裁定**改写为 confirmation_signal**
   ——「需什么信号才确认影响结束」——于是时间尺度问题变成采集工单，而不是一个预测。
   （三个先验均为**非数值型**：按 `[H26]` 模式外置，**不进任何 `assumed_params` 闭包**。）
5. **源状态按「圈层 × 观测域」派生，不是只按圈层**（`[H32]`）。圈层答「哪一类信息」，
   观测域答「哪一群对象」——两者**正交**。只按圈层派生时，落一个**异域**源（如仅覆盖美股的
   SEC）会静默撤掉「A 股机构行为不可观测」这条缺口 —— 那是**假可见**。故目标声明
   `required_scope`，派生只认同圈层 + 同观测域的源；而两个投影（§2.5 圈层覆盖度 /
   影响面源状态）共用**同一个上游** `_enabled_source_scopes()`，避免 `[H27]` 的漂移。
6. **两道闸：观测域 × 指名源**（第 15 次补丁，`[H32]` 在**子域**层面复发）。第 5 条只挡
   **异域**源；落一个**同域但异子域**的源（`industry` 的**订单/招标**源）会把 T002
   「算力硬件**产能/交付**」这类目标机械翻成 `collector_landed` —— **同圈层同域有源 ≠
   本对象可观测**。故目标还须**指名**观测它的源（`observed_by`；空列表 = 显式声明
   「无源可观测」）。两道闸都过才算 `collector_landed`。

8. **「替代观测」必须有第四个位置**（第 42 轮）。装配替代观测源（如用央行公开市场操作
   代替「A 股机构给政策定价」）时，三态派生**没有位置表达「有源，但测的不是这个目标」**
   —— 指名它即机械翻成 `collector_landed`，比「无源」更危险：无源至少诚实地说不可观测，
   假绿的却会被读成结论。故新增第四态 `substitute_observed`：源在 `sources.json` 里声明
   `observation_caliber=substitute`，指名它即命中；多源指名时取**最低**等级上报
   （诚实原则：按最保守的那个报）。

7. **「已知局限」也是派生的，不是手写的**（第 17 次补丁）。模板内可能出现**相邻同键**的环
   （`(sphere, source_class)` 相同）→ 单靠键无法区分这两环。第 10 次补丁把这句局限写成了
   **手写清单**，而它与模板集的演化**天然不同步**（新增模板又添新处，手写版不会自己更新）
   —— 同 `[H33]`「枚举式覆盖面必有漏项且不自曝」。现由 `matching_limitations()` 从签名
   **派生**并写进报告载荷。

本模块**不参与触发**（`participates_in_trigger = False`），只进报告。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_PATH = ROOT / "config" / "burst_templates.json"
SOURCES_PATH = ROOT / "config" / "sources.json"


def load_templates() -> dict:
    return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))


def load_sources() -> dict:
    return json.loads(SOURCES_PATH.read_text(encoding="utf-8"))


# **非覆盖口径**：指名它 ⇒ 目标不得翻成 collector_landed（第 42 轮 substitute / 第 43 轮
# aggregate_inferred）。`observation_calibers()` 是唯一取值入口，本集合随它演化 —— 新口径
# 必须在 `sources.json` 声明，并在此登记，否则会**绕过这道闸**（同 [H27] 单点原则）。
_NON_COVERING_CALIBERS = frozenset({"substitute", "aggregate_inferred"})

# 影响面派生状态的**等级序**（低 → 高）：多源指名时取最低者上报。
_STATUS_RANK = {0: "undetermined", 1: "substitute_observed",
                2: "partially_covered", 3: "collector_landed"}


def observation_calibers() -> dict:
    """从 `config/sources.json` 派生 {源 id: observation_caliber}——**单一真源**。

    只有声明了 `observation_caliber` 的源才有键。第 42 轮引入：为「替代观测」这类
    「有源但不是同一个东西」的源留一个可机器读的位置（同 [H27]：派生，不采信声明）。
    """
    return {sid: m["observation_caliber"]
            for sid, m in load_sources().get("sources", {}).items()
            if not sid.startswith("_") and m.get("observation_caliber")}


def source_status_index() -> dict:
    """从 `config/sources.json` 派生 {源 id: 状态}——**单一真源，不手工维护**。

    四个块对应 [H25] 的四分类。`sources` 块内只取 `enabled` 的源（按 [H25] 的不变量
    `enabled ⟺ collector_landed`，二者互为充要，故取其一即可）。
    """
    doc = load_sources()
    idx = {}
    for sid, meta in doc.get("sources", {}).items():
        if sid.startswith("_"):
            continue
        if meta.get("enabled") and meta.get("collector_landed"):
            idx[sid] = "collector_landed"
    for blk, status in (("verified_not_landed", "verified_not_landed"),
                        ("probe_unusable", "probe_unusable"),
                        ("disabled_sources", "undetermined")):
        for sid, meta in doc.get(blk, {}).items():
            if sid.startswith("_"):
                continue
            idx.setdefault(sid, status)
    return idx


def derive_status(obj, index: dict) -> str:
    if not obj:
        return "undetermined"
    return index.get(obj, "unknown")


def subject_observability(tpl: dict) -> list:
    """六类前置条件 · **主体条件**（最小合法落地，第 52 轮裁定②）。

    主体是**链定义的一部分**：每条 `structure` 环的 `object` 即该环的**主体 / 源**。
    主体条件 = 检查该主体是否**存在且可观测**：
      - `object` 非空 且 从 `config/sources.json` 派生的源状态 == `collector_landed` ⇒ 可观测；
      - 否则不可观测（`object` 为空 = 无源；已声明但采集器未落地 = 源未达），需新源。

    **立场（M-A · 监测者而非市场参与者）**：本函数**只做可观测性检查**，
    不断言「链被市场激活」——后者是市场事实，断言即越界 `§11 B 轨`。不可观测的环
    只标注 `reason`，供输出层打印「该环的主体不可观测，需要新源」——
    **不写任何未校准枚举**（不写「主体是否存在：是/否」的自由判断，只反映源事实）。

    与 `source_status_index()` 同源（单一真源，不手写），故本函数**不引入新数据源**，
    属第 52 轮裁定②要求的「最小改动：把隐式主体检查变显式」。
    """
    idx = source_status_index()
    out = []
    for lv in tpl.get("structure", []):
        obj = lv.get("object")
        st = derive_status(obj, idx)
        obs = (st == "collector_landed")
        out.append({
            "level": lv.get("level"),
            "sphere": lv.get("sphere"),
            "source_class": lv.get("source_class"),
            "object": obj,
            "subject_status": st,
            "subject_observable": obs,
            "reason": (None if obs else ("no_source" if not obj else "source_not_landed")),
        })
    return out


def _enabled_source_scopes() -> dict:
    """已落地源的 `(圈层, 观测域)` → [源 id]。**两个投影的共同上游。**

    为什么收成一个上游（`[H27]` 的教训）：圈层覆盖度（§2.5）与影响面源状态是**两个投影**，
    但必须来自同一份事实。若各写一遍，一派生一手写必然漂移。

    ⚠️ 因此「已落地源必须声明 `coverage_scope`」是一条**硬前提**：少声明一个，
    它所属的圈层会整体从 `sphere_status_index()` 里消失（缺口**多报**，不静默）——
    由 tests/conformance.py 组 [18] 断言，不靠提醒。
    """
    doc = load_sources()
    out: dict = {}
    for sid, meta in doc.get("sources", {}).items():
        if sid.startswith("_"):
            continue
        if meta.get("enabled") and meta.get("collector_landed"):
            for sc in (meta.get("coverage_scope") or []):
                out.setdefault((meta.get("sphere"), sc), []).append(sid)
    return out


def sphere_status_index() -> dict:
    """`{圈层: 状态}` —— **圈层级**投影（§2.5 覆盖度表用）。

    由 `config/sources.json` **派生**（经上面的共同上游），与 §2.5 覆盖度表共用同一判定。

    ⚠️ 本函数**只答「这个圈层有没有已落地源」**，不答「某个影响对象能不能被观测」——
    后者需要第二维（观测域），见 `scope_status_index()` / `target_scope_status()`（`[H32]`）。
    """
    doc = load_sources()
    tax = doc.get("sphere_taxonomy", {})
    has = {sph for (sph, _sc) in _enabled_source_scopes()}
    return {sph: ("collector_landed" if sph in has else "undetermined")
            for sph in tax.get("spheres", {})}


def scope_status_index() -> dict:
    """`{(圈层, 观测域): 状态}` —— **目标级**投影（`[H32]` 修复）。

    为什么需要第二维：`capital` 圈层里 SEC 仅覆盖**美股**。影响面的源状态若**只按圈层派生**，
    落 SEC 会让「A 股机构行为不可观测」这句话**静默消失**（缺口工单被错误撤掉）——那是
    **假可见**，比看不见更危险。目标声明 `required_scope`，派生只认同圈层且观测域命中的源。
    """
    return {k: "collector_landed" for k in _enabled_source_scopes()}


def target_scope_status(target_kind, required_scope, scope_index,
                        observed_by=None, source_index=None,
                        calibers=None) -> str:
    """某影响目标的源状态。**纯函数**（输入全部由调用方传入，故可对**合成输入**断言）。

    四态：`collector_landed` / `partially_covered` / `substitute_observed` / `undetermined`。

    **两道闸**（第 15 次补丁新增第二道 —— `[H32]` 在**子域**层面的复发）：

      第一道（观测域，第 14 次补丁）：目标声明 `required_scope`，派生只认同圈层且观测域命中的源。
        它挡的是**异域**源（落 SEC 仅美股不会填掉需 A 股的缺口）。

      第二道（`observed_by`，第 15 次补丁）：目标**指名**观测它的源。**同圈层、同观测域内有源，
        不等于本对象可观测** —— 落一个 `(industry, cn)` 的**订单/招标**源会把
        「算力硬件**产能/交付**」这类目标机械翻成 `collector_landed`，那是**假可见**。
        `observed_by` 为空列表 = **显式声明「无源可观测」** → `undetermined`。

    为什么写成纯函数：组 [18] / [19] 因此能在**不改动磁盘**的前提下证明四条方向的命题——
    ① 落一个**异域**源**不会**填掉需 `cn` 的目标缺口；
    ② 落一个**同域**源**会**填掉它（防「修过头」变成永不闭合的假缺口）；
    ③ 同域有源但目标**未指名**任何源 ⇒ **仍不可观测**（第二道闸）；
    ④ 指名了源、但其中只有部分已落地 ⇒ `partially_covered`。

    `observed_by is None` 时**退回**纯域级派生（保持第 14 次补丁的行为与组 [18] 的既有断言）。
    """
    req = list(required_scope or [])
    if not req:
        return "undetermined"
    covered = {sc for (sph, sc) in scope_index if sph == target_kind}
    hit = [sc for sc in req if sc in covered]
    if not hit:
        return "undetermined"
    base = "collector_landed" if len(hit) == len(req) else "partially_covered"

    # —— 第二道闸：目标必须**指名**观测它的源（未声明 observed_by ⇒ 退回旧行为）——
    if observed_by is None:
        return base
    named = list(observed_by)
    if not named:
        # 显式声明「没有任何源能观测本对象」—— 比靠 (圈层, 域) 反推更精确，也更诚实
        return "undetermined"
    if source_index is None:
        return base

    # —— 第三道闸：指名的源若是**非 collector 口径**（替代观测 / 聚合推断），只得到
    #    `substitute_observed`（第 42 轮立 substitute，第 43 轮扩至 aggregate_inferred）——
    #    「源存在」与「源测的是同一个东西」在派生层原本是**两个问题**，三态派生没有
    #    位置写第二个。于是装一个替代观测源会把目标机械翻成 collector_landed —— 那正是
    #    [H32] 要防的「假可见」，且比无源更隐蔽。**取最低等级**（多源指名时按最保守报）。
    if calibers:
        # ⚠ 第 43 轮：这里**不能**只判 == "substitute"。东财资金流是**真·A 股资本行为数据**（不是央行操作的替身），
        #   但 f62「主力」是按单笔成交金额分档的**推断**，不是机构身份；只判 substitute会把它翻成
        #   collector_landed —— **假可见第五面**：同圈层、同域、数据真实且正确，粒度却是聚合推断，
        #   不等于「机构在给这个定价」。结论强度与 substitute **同级** ⇒ 复用同一档，不新增状态。
        def _grade(sid):
            if (calibers.get(sid) or "") in _NON_COVERING_CALIBERS:
                return 1
            return {"collector_landed": 3, "partially_covered": 2}.get(
                source_index.get(sid), 0)
        lo = min((_grade(x) for x in named), default=None)
        if lo is not None and lo < 3:
            return _STATUS_RANK[lo]

    flags = [source_index.get(s) for s in named]
    if all(f == "collector_landed" for f in flags):
        return base
    if any(f == "collector_landed" for f in flags):
        return "partially_covered"
    return "undetermined"


def coverage() -> list:
    """每个模板的**可闭环环数 / 缺口环数**——由派生状态算出，不看模板自己的声明。"""
    tpls = load_templates()["templates"]
    index = source_status_index()
    out = []
    for tid, tpl in tpls.items():
        levels = tpl["structure"]
        gaps = [lv for lv in levels if derive_status(lv.get("object"), index) != "collector_landed"]
        out.append({
            "template_id": tid,
            "name": tpl["name"],
            "purpose": tpl["purpose"],
            "levels": len(levels),
            "closable_levels": len(levels) - len(gaps),
            "gap_levels": [lv["level"] for lv in gaps],
            "burst_levels": [bp["level"] for bp in tpl.get("burst_points", [])],
            "instances": len(tpl.get("instances", [])),
        })
    return sorted(out, key=lambda r: r["template_id"])


def workorders() -> list:
    """缺口工单：**每一个不可观测的环就是一条工单**。

    这是本模块当前唯一不需要等样本就能交付的东西——把「某圈层为空」这句模糊的话，
    换成「模板 T002 第 2 环需要产业圈层的『事件型』源，当前候选端点 8 个实测可用 0」。
    """
    tpls = load_templates()["templates"]
    index = source_status_index()
    out = []
    for tid, tpl in tpls.items():
        for lv in tpl["structure"]:
            status = derive_status(lv.get("object"), index)
            if status == "collector_landed":
                continue
            out.append({
                "template_id": tid,
                "level": lv["level"],
                "sphere": lv.get("sphere"),
                "source_class": lv.get("source_class"),
                "object": lv.get("object"),
                "derived_status": status,
                "why": lv.get("gap"),
            })
    return out


def mismatches() -> list:
    """模板**声明**的源状态 vs 引擎**派生**的状态——不一致即上报。

    这是「声明 vs 引擎」反查模式的复用（同 §14.5 的两个示例反查依赖闭包引擎）：
    示例/模板写下的期望值，用来反查引擎；对不上说明其中一边错了。
    """
    tpls = load_templates()["templates"]
    index = source_status_index()
    bad = []
    for tid, tpl in tpls.items():
        for lv in tpl["structure"]:
            declared = lv.get("signal_source_status")
            derived = derive_status(lv.get("object"), index)
            if declared != derived:
                bad.append({"template_id": tid, "level": lv["level"],
                            "declared": declared, "derived": derived})
        for bp in tpl.get("burst_points", []):
            declared = bp.get("signal_source_status")
            derived = derive_status(bp.get("signal_object") or _level_object(tpl, bp["level"]), index)
            if declared != derived:
                bad.append({"template_id": tid, "burst_level": bp["level"],
                            "declared": declared, "derived": derived})
    return bad


def impact_surfaces() -> list:
    """每个模板的影响面清单 + **派生**的源状态。**这一步就是「挂源状态」。**

    链是「传导到哪了」，影响面是「传导到谁了」。但影响面的对象当前**大多不可观测**——
    所以每条都带 `derived_status`：不是 `collector_landed` 的，报的是**缺口**而不是结论。
    """
    tpls = load_templates()["templates"]
    sidx = scope_status_index()
    srcidx = source_status_index()
    calibers = observation_calibers()
    out = []
    for tid, tpl in tpls.items():
        isf = tpl.get("impact_surface") or {}
        targets = []
        for t in isf.get("targets", []):
            kind = t.get("target_kind")
            ts = t.get("time_scale") or {}
            targets.append({
                "target_kind": kind,
                "target": t.get("target"),
                "degree": t.get("degree"),
                # 目标声明的观测域（既供派生，也进报告——让「缺的是哪个域」肉眼可见）
                "required_scope": t.get("required_scope"),
                # 目标**指名**的观测源（第 15 次补丁）：同圈层同域有源 ≠ 本对象可观测（[H32] 子域层复发）
                "observed_by": t.get("observed_by"),
                "time_scale": {"value": ts.get("value"), "basis": ts.get("basis")},
                "confirmation_signal": t.get("confirmation_signal"),
                # 派生，不采信模板声明（同 [H27]）：两道闸 = 观测域 + 指名源
                "derived_status": target_scope_status(
                    kind, t.get("required_scope"), sidx, t.get("observed_by"),
                    srcidx, calibers),
                "gap": t.get("gap"),
            })
        out.append({
            "template_id": tid,
            "scope": isf.get("_scope"),
            "targets": targets,
            "instantiation_gap": isf.get("_instantiation_gap"),
        })
    return out


def _gap_kinds(target: dict) -> list:
    """某条影响面的**缺口类型**——由两个已知事实**派生**，不手写。

      ① `source_gap`            : 该圈层无已落地源 ⟺ 影响是否落到该对象不可观测
      ② `duration_uncalibrated` : `time_scale.value is None` ⟺ 影响持续多久不可知

    为什么必须派生：工单这个词声称「有缺口」。若某个 target 两个条件都不满足却仍出现在
    工单表里，那就是一条**没有缺口的工单**——「工单」成了空话，与 `[H30]` / `[H31]`
    同族（产出「有机制」的假象）。本函数的存在就是为了让「工单必有缺口」成**可断言**的事：
    组 [16] 断言 `gap_kinds` 非空，且与 `derived_status` / `time_scale.value` 一致。
    """
    kinds = []
    if target.get("derived_status") != "collector_landed":
        kinds.append("source_gap")
    ts = target.get("time_scale") or {}
    if ts.get("value") is None:
        kinds.append("duration_uncalibrated")
    return kinds


def impact_workorders() -> list:
    """影响面工单：**每条 = 一个当前不可观测的影响对象**，并标明它缺的是哪一类。

    两类缺口的来源不同，由 `_gap_kinds()` 派生：
      ① `source_gap`            —— 影响是否真的落到该对象（受该圈层源状态约束）；
      ② `duration_uncalibrated` —— 影响持续多久（`time_scale` 恒 `null` + `uncalibrated`，
         按裁定改写成 `confirmation_signal`，于是它成为**工单**而不是一个预测）。

    ⚠️ **本表与 `workorders()` 不可相加**：后者数「链上的环不可观测」，
    前者数「影响面上的对象不可观测」；两者可能**同源于同一圈层**
    （T002 的第 2、3 环与它的一个 target 同源），相加会把一个缺口算两遍。
    """
    out = []
    for blk in impact_surfaces():
        for t in blk["targets"]:
            kinds = _gap_kinds(t)
            if not kinds:                     # 无缺口 → 不该出现在工单里
                continue
            out.append({
                "template_id": blk["template_id"],
                "target_kind": t["target_kind"],
                "target": t["target"],
                "degree": t["degree"],
                "derived_status": t["derived_status"],
                "gap_kinds": kinds,
                "source_gap": t["gap"] if "source_gap" in kinds else None,
                "confirmation_signal": t["confirmation_signal"],
            })
    return out


def _level_object(tpl: dict, level: int):
    for lv in tpl["structure"]:
        if lv["level"] == level:
            return lv.get("object")
    return None


def signature(tpl: dict) -> list:
    """可匹配签名 = **每个环**的 `(sphere, source_class)`，含无源环（其 `source_class` 为 `None`）。

    为什么**不跳过**无源环（这里改过一次，原因值得记下）：跳过无源环会让 T002 的五环
    在签名里塌成两环 → 一条两级的链可能被判成「与 T002 完整匹配」，而 T002 中间三环
    系统根本看不见。保留 `None` 之后，匹配必然在第一个无源环处断开，
    「有缺口」这件事就**体现在匹配数上**，而不是被静默吞掉。
    """
    return [(lv.get("sphere"), lv.get("source_class")) for lv in tpl["structure"]]


def matching_limitations() -> list:
    """派生「精确匹配的已知局限」——**不手写**（`[H27]` / `[H33]` 同族）。

    判据：**同一模板内相邻两环的匹配键相同** ⇒ 单靠匹配键无法区分这两环，
    只能靠**级别顺序位置**参与匹配（属人工核）。`sphere` 为 `None` 的环不计入
    （那是「连圈层都判不了」的环，不是「同键」）。

    为什么改为派生（第 17 次补丁）：第 10 次补丁把这个局限写成了一句**手写清单**，
    而实测其覆盖与事实不同步（另有模板的第 2/3 环同形），且第 17 次新增的两个模板里
    又添了一处 —— **手写清单必然与模板集的演化解耦**。现改为从 `signature()` 派生，
    「记了什么」恒等于「实际有什么」，并写进报告载荷（读者不必翻模板自己去数）。
    """
    out = []
    for tid, tpl in load_templates()["templates"].items():
        sig = signature(tpl)
        for i in range(len(sig) - 1):
            if sig[i] == sig[i + 1] and sig[i][0] is not None:
                out.append({
                    "template_id": tid,
                    "levels": [i + 1, i + 2],
                    "key": [sig[i][0], sig[i][1]],
                })
    return out


def non_numeric_rings() -> list:
    """派生「事件型 / 非数值型」环——消费了 `provides_numeric == False` 的源的环。

    为什么需要它（[H32] 第 4 次复发的直接防御）：环评受理源给的是事件与实体、不是数值，
    若有人日后把它塞进 `numeric_incremental` 判据，会**静默**算出一个「产能 z 值」。
    把「事件型 / 非数值型」从一句提醒变成**不变量**（§14.8.6：把不变量变成断言，不写成提醒）。

    判据：环带 `object`、且该源 `provides_numeric is False`。返回每个这样的环的
    (模板, 级别, 源, value_kind, 环自带的 quantity_ack)，供组 [22] 校验「消费非数值源的环必带非空 quantity_ack」，
    也供 `reminder()` 暴露给读者（不可见即等于没说）。
    """
    src_doc = load_sources()
    srcs = src_doc.get("sources", {})
    out = []
    for tid, tpl in load_templates()["templates"].items():
        for lv in tpl["structure"]:
            obj = lv.get("object")
            if not obj:
                continue
            meta = srcs.get(obj) or {}
            if meta.get("provides_numeric") is False:
                out.append({
                    "template_id": tid,
                    "level": lv["level"],
                    "object": obj,
                    "value_kind": meta.get("value_kind"),
                    "quantity_ack": lv.get("quantity_ack"),
                })
    return out


def match(chain_levels) -> list:
    """把一条链（按级别的 (sphere, source_class) 序列）对每个模板做**前缀精确匹配**。

    `chain_levels`：形如 `[("tech", "numeric_incremental"), ("media", "heat")]`。
    **不做模糊匹配**——相似度阈值会引入未校准常数。
    """
    tpls = load_templates()["templates"]
    out = []
    for tid, tpl in tpls.items():
        sig = signature(tpl)
        n = 0
        for got, want in zip(chain_levels, sig):
            if got != want:
                break
            n += 1
        out.append({
            "template_id": tid,
            "name": tpl["name"],
            "matched_levels": n,
            "template_levels": len(sig),
            "full_match": bool(sig) and n == len(sig),
        })
    return sorted(out, key=lambda r: (-r["matched_levels"], r["template_id"]))


def reminder(chain_levels=None) -> dict:
    """报告载荷。阶段 0 无链 → `matched` 为空，但**模板覆盖度与工单照常产出**。"""
    tpls = load_templates()
    chain_levels = list(chain_levels or [])
    cov = coverage()
    wo = workorders()
    gap_levels = sum(len(r["gap_levels"]) for r in cov)
    total_levels = sum(r["levels"] for r in cov)
    return {
        "basis": tpls["basis"],
        "participates_in_trigger": tpls["participates_in_trigger"],
        "instance_source": tpls["instance_source"],
        "templates_loaded": len(tpls["templates"]),
        "levels_total": total_levels,
        "levels_closable": total_levels - gap_levels,
        "levels_gap": gap_levels,
        "chains_matched_this_cycle": len(chain_levels),
        "matched": match(chain_levels),
        "no_chain_note": (
            "本周期没有走完的链（阶段 0 全量 baseline_collecting），故没有触发任何提醒。"
            "模板库已就绪：链一旦生成即可按 (圈层, 来源型) 精确匹配。"
            if not chain_levels else
            "本周期有链进入匹配。"
        ),
        "coverage": cov,
        "workorders": wo,
        # 精确匹配的已知局限（`[H27]` / `[H33]`）：同模板内**相邻同键**的环 —— 派生而非手写，
        # 否则模板集一演化，清单就与事实脱钩（第 10 次补丁的手写版即已漏项）。
        "matching_limitations": matching_limitations(),
        # 事件型 / 非数值型环：消费 provides_numeric==False 源的环（`[H32]` 第 4 次复发防御）。
        # 环评受理类源给的是事件与实体、不是数值；把「事件型 / 非数值型」从提醒变不变量，
        # 读者不必翻源自己去数哪些环不可当数值源喂给 numeric_incremental 判据。
        "non_numeric_rings": non_numeric_rings(),
        # 影响面：链回答「传导到哪了」，这里回答「传导到谁了 / 影响多大 / 多长时间」。
        # 与 workorders 是**两个不同对象**的工单，不可相加（见 impact_workorders docstring）。
        "impact_surface": impact_surfaces(),
        "impact_workorders": impact_workorders(),
        "declared_vs_derived_mismatch": mismatches(),
        "frequency_policy": tpls["frequency_policy"],
        "disclosure": (
            "提醒的**位置字段必填**（当前第 N 环 + 模板说第 N+1 环要看什么）；"
            "**频率字段在系统自产实例之前恒为 null + basis=uncalibrated**——实例数为 0 时"
            "该统计量不含信息量。每个「下一验证信号」都挂**派生**的源状态："
            "凡不是 collector_landed 的环，提醒报的是**缺口**而不是结论。"
            "**影响面**（impact_surface）是一条与链正交的维度：链回答「传导到哪了」，"
            "它回答「传导到谁了 / 影响多大 / 多长时间」。它是**类级**的（该类故事的典型影响面），"
            "不是本故事的实例影响面（实例化需链 payload，阶段 0 chains 为空）；"
            "**持续时间恒 null + uncalibrated**——按裁定改写成 confirmation_signal（需什么信号才确认影响结束），"
            "于是时长问题变成采集工单而非预测。"
            "影响面工单（impact_workorders）与环工单（workorders）**不可相加**：二者可能同源于同一圈层。"
            "**模板数量是表达力，不是可观测能力**（第 17 次补丁）：可观测能力只由**已落地源**决定，"
            "新增模板不改变任何源的状态 —— 它只让「系统能认出的故事类型」变多，不改变「系统能看见什么」。"
            "本模块不参与触发（participates_in_trigger = false），同 §14.9.4 裁定 (ii) 的模式。"
        ),
    }
