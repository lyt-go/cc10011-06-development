"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

内部表说明（不出现在运营概览/模块列表里）：
- dimensionmap_standard  标准维度映射结果（一条来源维度一条当前口径）
- dimensionmap_version   标准维度映射版本（同一来源每次口径变更追加一版）
- drilldown_dimension_fact  多维下钻用到的维度事实明细
- drilldownpathversion   下钻路径版本快照（头部）
- drilldownlayerresult   下钻路径逐层聚合结果（含分组明细）
- drilldown_generation_ledger  快照生成提交记录（只接纳首个通过校验的顺序）
"""
from __future__ import annotations

import copy
from typing import Any

from app.seed import SEED_ROWS

INTERNAL_MODULES = {
    "dimensionmap_standard",
    "dimensionmap_version",
    "drilldown_dimension_fact",
    "drilldownpathversion",
    "drilldownlayerresult",
    "drilldown_generation_ledger",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self.reset()

    def reset(self) -> None:
        """恢复示例数据：测试与本地重放都从这里拿到干净状态。"""
        self._tables = {
            name: copy.deepcopy(rows) for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        """对外可见的业务模块，内部落库表不参与运营概览统计。"""
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def find_by(self, module: str, **fields: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if all(row.get(key) == value for key, value in fields.items()):
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
