"""维度映射业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.standardmapping import ACTIVE_RULE_STATUS, standard_mapping_service
from app.store import store

MODULE = "dimensionmap"
REQUIRED_FIELDS = ["规则编号", "原始维度名", "标准维度名"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []


class DimensionmapService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        # 落库点：登记即同步标准结果，草稿规则参与引用关系但不改当前口径。
        standard_mapping_service.sync_all()
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于维度映射可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 落库点：口径变更（生效 / 废弃 / 提交评审）后重建标准结果与版本。
        summary = standard_mapping_service.sync_all()
        note = self._conflict_note(entry, target, summary)
        return entry, f"映射规则已{action}{note}"

    @staticmethod
    def _conflict_note(entry: dict[str, Any], target: str, summary: dict[str, Any]) -> str:
        """生效动作落在冲突维度上时，把这次权衡结论回给调用方，而不是静默覆盖。"""
        if target != ACTIVE_RULE_STATUS:
            return ""
        source = str(entry.get("原始维度名") or "")
        for conflict in summary.get("冲突", []):
            if conflict.get("源维度") != source:
                continue
            adopted = conflict.get("采用") or {}
            if adopted.get("规则ID") == entry.get("id"):
                return f"；当前口径采用本规则（标准维度 {adopted.get('标准维度')}）"
            return (
                f"；同源维度存在更高优先口径，本次未采用本规则，"
                f"当前口径仍为 {adopted.get('规则编号')}（标准维度 {adopted.get('标准维度')}）"
            )
        return ""
