# -*- coding: utf-8 -*-
"""§2.3 去重与合并。

规格的三元组是 `同一 entity + 同一 action + |collected_at 之差| ≤ DEDUP_WINDOW_H`。
但 **`entity` / `action` 来自 §13 的 LLM 抽取节点**，而 §13.4 已裁定第 1 天–第 1 周
不引入任何 LLM 节点。因此第 1 天的去重**只能用确定性键**：

    指纹 = sha256(normalize(标题或正文首句))

**这是一个明确的边界，不是偷工减料**：
 - 确定性键能合并「同源重复」与「同标题跨源重复」；
 - 但无法合并「同一事件的不同表述」（那需要 entity/action 语义归并）；
 - 故第 1 天的 `sources` 合并数会**低于** LLM 抽取接入后的水平，该差异记入周期报告。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from .thresholds import T

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】\-—…]+")


def normalize(text: str) -> str:
    t = _TAGS.sub("", text or "")
    t = _WS.sub("", t)
    t = _PUNCT.sub("", t)
    return t.casefold()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:16]


def snapshot_hash(raw_content: str, collected_at: str) -> str:
    """§1.1 `input_snapshot_hash` = sha256(raw_content + collected_at)。"""
    return hashlib.sha256(("%s\u0000%s" % (raw_content or "", collected_at or "")).encode("utf-8")).hexdigest()


def _parse(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def dedup_batch(items) -> list:
    """按指纹 + 时间窗合并。返回合并后的条目，`sources` 已累加。"""
    window = timedelta(hours=T.DEDUP_WINDOW_H)
    merged = {}
    order = []
    for it in items:
        key = it.get("content_fingerprint") or fingerprint(it.get("raw_content", ""))
        it["content_fingerprint"] = key
        hit = None
        for cand_key in merged:
            head = merged[cand_key]
            if cand_key != key:
                continue
            t_new, t_old = _parse(it.get("collected_at")), _parse(head.get("collected_at"))
            if t_new is None or t_old is None or abs(t_new - t_old) <= window:
                hit = head
                break
        if hit is None:
            it.setdefault("sources", [it.get("source_name")])
            merged[key] = it
            order.append(key)
        else:
            for s in it.get("sources", [it.get("source_name")]):
                if s not in hit["sources"]:
                    hit["sources"].append(s)
    return [merged[k] for k in order]


def dedup_report(before: int, after: int) -> dict:
    return {
        "rule_applied": "§2.3",
        "window_param": "DEDUP_WINDOW_H",
        "window_hours": T.DEDUP_WINDOW_H,
        "before": before,
        "after": after,
        "merged": before - after,
        "derived_params_used": [],
        "assumed_params": T.assumed_params("§2.3.dedup.merge_triple"),
        "boundary_note": (
            "第 1 天无 LLM 节点（§13.4），entity/action 不可得，故只用确定性指纹键；"
            "跨源语义合并待 §13 的抽取节点接入后启用，届时合并数会上升。"
        ),
    }
