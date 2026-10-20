# -*- coding: utf-8 -*-
"""B 轨（事实标签轨）发布前护栏 —— §11 的 4 条硬约束中可机器校验的两条。

约束 1：`verification_signal.metric_type` 不得为 `price`。
约束 4：报告中不得出现「看多 / 看空 / 买入 / 目标价 / 涨跌幅」类字词。

§11 说明：选定 B 轨后，「不输出买卖建议」不再是免责声明，而是**被结构性保证的约束**。
本模块就是那条保证的实现。
"""
from __future__ import annotations

# §11 约束 4 的词表（正则护栏，与 §9.2 恒等式校验同属发布前检查）
#
# ⚠️ **本项目有两套词表，职责不同，改词时必须看清落到哪一套**：
#   本表 = §11「不得输出方向性 / 买卖建议」（事实标签轨 B 的边界）；
#   src/pipeline.py 的 PREDICTION_CLAIMS = §14.3.2「不得声称预测力 / 不得给未校准时长」。
#   判定标准：**方向词 / 建议词 → 本表；预测力词 / 时长词 → 那张表。**
#   两表都是**断言**（命中即抛）。第 12 次补丁扩容时发现二者此前**互不知情**（[H33]）。
FORBIDDEN_WORDS = (
    "看多", "看空", "做多", "做空",
    "买入", "卖出", "买进", "抛出",
    "目标价", "目标位", "止盈", "止损",
    "涨跌幅", "涨幅", "跌幅", "涨停", "跌停",
    "加仓", "减仓", "建仓", "清仓", "仓位建议",
    # 第 12 次补丁新增：影响面极易被读成投资建议（「影响面 → 利好谁」），
    # 而 §11 声明不输出买卖建议 → 影响面的措辞必须停在「可观测的影响对象清单」。
    "利好", "受益", "看涨", "看跌",
)

# §11 约束 1：verification_signal 的 metric_type 不得为价格类
FORBIDDEN_METRIC_TYPES = ("price",)


class GuardViolation(Exception):
    pass


def scan_text(text: str, where: str = "(未命名)") -> list:
    """扫描文本中的违禁词，返回命中列表（不抛异常，供报告层聚合）。"""
    hits = []
    for w in FORBIDDEN_WORDS:
        if w in text:
            idx = text.find(w)
            hits.append({"where": where, "word": w, "context": text[max(idx - 18, 0): idx + 18]})
    return hits


def scan_report(report: dict) -> list:
    """扫描整份报告（递归所有字符串值）。"""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, "%s.%s" % (path, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%s]" % (path, i))
        elif isinstance(node, str):
            hits.extend(scan_text(node, path))

    walk(report, "report")
    return hits


def check_metric_type(metric_type: str) -> None:
    """§11 约束 1。"""
    if metric_type in FORBIDDEN_METRIC_TYPES:
        raise GuardViolation(
            "§11 约束 1 违规：verification_signal.metric_type = %r。"
            "B 轨下链的终点必须是事实（如「扩产公告 +3 条」），不是方向。" % metric_type
        )


def assert_report_clean(report: dict) -> None:
    """发布前强制检查。命中即拒绝出报告（§11 约束 4）。"""
    hits = scan_report(report)
    if hits:
        raise GuardViolation(
            "§11 约束 4 违规：报告含 %d 处违禁字词 -> %s"
            % (len(hits), "; ".join("%s: %s" % (h["where"], h["word"]) for h in hits))
        )
