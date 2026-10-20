# -*- coding: utf-8 -*-
"""判定模块（确定性代码，不经 LLM）。

本目录受 §14.4 裸数字禁令约束：任何影响判定的常数必须来自
`config/thresholds.json`，经 `T.<PARAM_ID>` 读取。
检查命令：python tools/check_bare_numbers.py
"""
