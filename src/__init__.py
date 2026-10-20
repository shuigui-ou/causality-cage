# -*- coding: utf-8 -*-
"""故事预判与传导链验证系统 —— 参考实现。

目录约定（对应执行规格 v1.2）：
    src/rules/    判定模块。§14.4 裸数字禁令只约束本目录。
    src/collect/  采集模块。
    src/store/    存储模块（SQLite 状态 + Parquet 只追加）。
    config/       §14.1 常数登记表、§2.1 来源清单（含 evidence）。
    tools/        检查器（§14.4 AST 裸数字检查）。
"""
