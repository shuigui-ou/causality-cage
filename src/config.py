# -*- coding: utf-8 -*-
"""配置读取（config/*.json 的唯一入口）。"""
from __future__ import annotations

import json

from .paths import COLLECTION_PARAMS_JSON, SOURCES_JSON, THRESHOLDS_JSON


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_sources() -> dict:
    return _load(SOURCES_JSON)


def enabled_sources() -> dict:
    doc = load_sources()
    return {k: v for k, v in doc["sources"].items() if v.get("enabled")}


def disabled_sources() -> dict:
    doc = load_sources()
    return {k: v for k, v in doc.get("disabled_sources", {}).items() if not k.startswith("_")}


def collection_params(source_id: str) -> dict:
    doc = _load(COLLECTION_PARAMS_JSON)
    return {k: v for k, v in doc["sources"].get(source_id, {}).items() if not k.startswith("_")}


def source_periods() -> dict:
    """`{source_id: source_period}` —— 采样的**节奏**（`daily` / `weekly` / `event`）。

    用途（§2.2 / `[H31]`）：门禁的「采样期数」以**每源的期长**为单位，而期长由该字段决定。
    这是它的**唯一取值入口**（`config/` 的唯一入口是本模块）——第 16 次补丁前它是
    「有声明、无消费」：`config/sources.json` 里写了、`src/` 里零个读者。

    覆盖面刻意取**四个块**（不只 `sources`）：`verified_not_landed` 也声明该字段，
    而期级序列里若出现一个「查不到期长」的源，计数就无从谈起 → 取全，让缺声明**显形**。

    ⚠️ 只收**已声明**该字段的条目（值为空/缺失即不收）：于是「源存在、期长未知」在
    `gates.period_counts_by_source()` 里表现为 `KeyError` 式的**显式失败**，而不是按日计数。
    """
    doc = load_sources()
    out = {}
    for blk in ("sources", "verified_not_landed", "probe_unusable", "disabled_sources"):
        for sid, meta in (doc.get(blk) or {}).items():
            if sid.startswith("_") or not isinstance(meta, dict):
                continue
            if meta.get("source_period"):
                out[sid] = meta["source_period"]
    return out


def rule_version() -> str:
    return _load(THRESHOLDS_JSON)["rule_version"]
