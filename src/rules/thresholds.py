# -*- coding: utf-8 -*-
"""参数访问层 —— §14.1 假设常数登记表的唯一读入口。

规则 14.4.4：判定模块禁止写裸数字，必须写 `T.OUTBREAK_HIGH` 这类形式。
规则 14.3.1/14.3.3：`assumed_params` 由本模块按依赖闭包自动算出，**不允许手写**。
"""
from __future__ import annotations

import json

from ..paths import THRESHOLDS_JSON


class Thresholds:
    """登记表的只读访问器。"""

    def __init__(self, path=None):
        doc = json.loads((path or THRESHOLDS_JSON).read_text(encoding="utf-8"))
        self._doc = doc
        self._params = doc["params"]
        self._decl = doc["rule_declarations"]
        self.rule_version = doc["rule_version"]

    # ---- 取值 ----

    def __getattr__(self, name):
        # 只处理全大写参数名，避免误吞内部属性（如 _doc）
        if name.isupper():
            try:
                return self._params[name]["value"]
            except KeyError:
                raise AttributeError("未登记的常数: %s（§14.1 要求所有影响判定的常数必须登记）" % name)
        raise AttributeError(name)

    def meta(self, name) -> dict:
        return self._params[name]

    def basis(self, name) -> str:
        return self._params[name]["basis"]

    def all_ids(self) -> list:
        return sorted(self._params)

    # ---- 规则 14.3.3 依赖闭包 ----

    def _closure(self, name: str) -> set:
        """返回该参数所依赖的全部 `assumed` 参数（含自身，若自身即 assumed）。

        要点（§14.5 注释）：derived 参数**自身不出现在**结果里，但它依赖的
        assumed 参数**必须出现** —— 这就是「derived 不等于免责」的形式化。
        """
        out = set()
        node = self._params[name]
        if node["basis"] == "assumed":
            out.add(name)
        for dep in node.get("depends_on", []):
            out |= self._closure(dep)
        return out

    def assumed_params(self, rule_id: str) -> list:
        """某条规则产出的结论必须携带的未校准参数清单（规则 14.3.1 + 14.3.3）。"""
        decl = self._decl[rule_id]
        used = list(decl.get("direct", [])) + list(decl.get("derived", []))
        out = set()
        for p in used:
            out |= self._closure(p)
        return sorted(out)

    def derived_params(self, rule_id: str) -> list:
        """该规则用到的 derived 参数（输出中的 `derived_params_used` 字段）。"""
        return sorted(self._decl[rule_id].get("derived", []))

    def declared_rules(self) -> list:
        return sorted(k for k in self._decl if not k.startswith("_"))


T = Thresholds()
