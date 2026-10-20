# -*- coding: utf-8 -*-
"""
feeds.py — 真实数据源支路（开源包内为**降级桩**）

诚实声明：本开源包**不包含真实数据采集实现**（涉及特定数据源接口与凭证）。
需要真实源时请自行接入，或参考 README「接上你自己的大模型 / 数据源」。

当前行为：
  - build_constraint_feed() / build_narrative_feed() 返回**空集 + 明确标注**，
    使use_real_feeds=True 时不会把「没接源」误显示成「源里没有东西」。
  - 引擎在 use_real_feeds=False 时完全不调用本模块（demo / tests 即如此）。
"""


class _SourceUnavailable(RuntimeError):
    pass


def _unavailable(name):
    return {"constraints": [], "narratives": [], "events": [],
            "source_status": {"%s" % name: "not_bundled: 真实数据源未随开源包发布"}}


def build_constraint_feed():
    return _unavailable("constraint_feed")


def build_narrative_feed():
    return _unavailable("narrative_feed")


def _collect_event_calendar():
    return _unavailable("event_calendar")
