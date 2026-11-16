"""标准维度映射结果与版本：把各模块的维度口径收敛成一份可追溯的当前口径。

数据流与落库点（沿用现有服务层写库的位置，不在路由里做业务判断）：

1. ``dimensionmap`` 新建 / 提交评审 / 生效 / 废弃
   → 落库 ``dimensionmap_standard``（每个源维度一行当前口径）
   → 口径变化时追加 ``dimensionmap_version``（同一源维度一版一条，可追溯）
2. ``realtimemetric``、``comparison``、``filterpreset``、``eventanalysis``、
   ``segmentprofile``、``drilldown`` 的新建与状态流转
   → 重新扫描这些模块实际引用的维度名，写回标准结果的「覆盖模块 / 引用字段」
   → 只更新引用关系，不产生新版本（口径没变就不该换版本）
3. 服务启动
   → 执行一次全量同步，保证全新进程里标准结果与版本已经就位
4. ``drilldown`` 生成路径版本快照
   → 读取标准结果里的当前版本号并写进快照与逐层结果，绝不读实时规则表

冲突处理是一次真实权衡，而不是「最后一条覆盖」：
同一源维度存在多条「已生效」规则时，按 映射优先级 > 最近更新时间 > 登记顺序 选出一条，
被放弃的规则、放弃原因和受影响的下游模块一并写进版本记录与快照绑定信息里；
每个分层结果只会落在一个标准维度版本上，生成的快照因此可解释、可复核。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

STANDARD_MODULE = "dimensionmap_standard"
VERSION_MODULE = "dimensionmap_version"
RULE_MODULE = "dimensionmap"

ACTIVE_RULE_STATUS = "已生效"
PRIORITY_RANK = {"高": 0, "中": 1, "低": 2}

# 各业务模块里承载「维度名」的字段；同步时按这些字段回填引用关系。
DIMENSION_FIELDS: dict[str, list[str]] = {
    "realtimemetric": ["维度字段组"],
    "comparison": ["对比维度"],
    "filterpreset": ["维度条件组"],
    "eventanalysis": ["分组维度"],
    "segmentprofile": ["画像维度"],
    "drilldown": ["起始维度", "下钻层级序列"],
}

SEPARATORS = (">", ",", "，", "、", "/", "|", " ")


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def parse_dimensions(raw: Any) -> list[str]:
    """把「province > city」「channel,category」这类写法拆成有序维度名。"""
    text = str(raw or "").strip()
    if not text:
        return []
    for separator in SEPARATORS[1:]:
        text = text.replace(separator, ">")
    tokens = [token.strip() for token in text.split(">")]
    return [token for token in tokens if token]


def _date_key(value: Any) -> int:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return int(digits) if digits else 0


def _rule_rank(rule: dict[str, Any]) -> tuple[int, int, int]:
    """排序键越小越优先：优先级高 → 最近更新时间新 → 登记顺序靠后。"""
    return (
        PRIORITY_RANK.get(str(rule.get("映射优先级") or ""), 9),
        -_date_key(rule.get("最近更新时间")),
        -int(rule.get("id", 0)),
    )


def _rule_brief(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "规则ID": rule.get("id"),
        "规则编号": rule.get("规则编号"),
        "标准维度": rule.get("标准维度名"),
        "映射优先级": rule.get("映射优先级"),
        "最近更新时间": rule.get("最近更新时间"),
        "值转换逻辑": rule.get("值转换逻辑"),
    }


def _fingerprint(rule: dict[str, Any]) -> str:
    parts = [
        str(rule.get("标准维度名") or ""),
        str(rule.get("映射类型") or ""),
        str(rule.get("值转换逻辑") or ""),
        str(rule.get("默认取值") or ""),
        str(rule.get("映射优先级") or ""),
        str(rule.get("规则编号") or ""),
    ]
    return "|".join(parts)


def _usage_map() -> dict[str, list[dict[str, str]]]:
    """扫描各模块引用的原始维度名，得到 源维度 → [{模块, 字段}]。"""
    usage: dict[str, list[dict[str, str]]] = {}
    for module, fields in DIMENSION_FIELDS.items():
        for row in store.rows(module):
            for field in fields:
                for dimension in parse_dimensions(row.get(field)):
                    entries = usage.setdefault(dimension, [])
                    item = {"模块": module, "字段": field}
                    if item not in entries:
                        entries.append(item)
    return usage


class StandardMappingService:
    """标准维度映射结果与版本快照的唯一读写入口。"""

    def list_standards(
        self,
        *,
        keyword: str | None = None,
        conflict_only: bool = False,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(STANDARD_MODULE))
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("源维度", "")) or keyword in str(row.get("标准维度", ""))
            ]
        if conflict_only:
            rows = [row for row in rows if row.get("冲突")]
        return sorted(rows, key=lambda row: str(row.get("源维度", "")))

    def list_versions(self, source: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(VERSION_MODULE))
        if source:
            rows = [row for row in rows if row.get("源维度") == source]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def current_standard(self, source: str) -> dict[str, Any] | None:
        row = store.find_by(STANDARD_MODULE, 源维度=source)
        if row is None or not row.get("有效"):
            return None
        return row

    def missing_dimensions(self) -> list[dict[str, Any]]:
        """被其他模块引用、但没有任何已生效标准映射的维度。"""
        usage = _usage_map()
        missing: list[dict[str, Any]] = []
        for dimension, references in sorted(usage.items()):
            if self.current_standard(dimension) is None:
                missing.append({"源维度": dimension, "引用": references})
        return missing

    def sync_all(self) -> dict[str, Any]:
        """全量同步标准结果、版本与引用关系；返回一份可展示的同步摘要。"""
        synced_at = now_text()
        usage = _usage_map()
        effective_rules: dict[str, list[dict[str, Any]]] = {}
        for rule in store.rows(RULE_MODULE):
            if rule.get("status") != ACTIVE_RULE_STATUS:
                continue
            source = str(rule.get("原始维度名") or "").strip()
            if not source:
                continue
            effective_rules.setdefault(source, []).append(rule)

        created_versions = 0
        conflicts: list[dict[str, Any]] = []
        for source, candidates in sorted(effective_rules.items()):
            ordered = sorted(candidates, key=_rule_rank)
            winner = ordered[0]
            tradeoff = self._tradeoff_text(source, ordered)
            if len(ordered) > 1:
                conflicts.append({
                    "源维度": source,
                    "采用": _rule_brief(winner),
                    "候选项": [_rule_brief(item) for item in ordered],
                    "权衡说明": tradeoff,
                })
            created_versions += self._upsert_standard(source, ordered, tradeoff, usage, synced_at)

        # 已没有生效规则的源维度：保留历史行但标记失效，避免旧口径继续被下钻引用。
        for row in store.rows(STANDARD_MODULE):
            source = str(row.get("源维度") or "")
            if source and source not in effective_rules:
                if row.get("有效"):
                    row["有效"] = False
                    row["状态"] = "已失效"
                    row["同步时间"] = synced_at
                    created_versions += self._append_version(
                        source=source,
                        rule=None,
                        ordered=[],
                        tradeoff=f"源维度 {source} 已无生效映射，当前口径失效，引用它的下钻快照必须重新生成",
                        usage=usage.get(source, []),
                        synced_at=synced_at,
                        state="已失效",
                    )
                    invalid_version = store.rows(VERSION_MODULE)[-1]
                    row["版本ID"] = invalid_version["id"]
                    row["版本编号"] = invalid_version["版本编号"]
                row["引用"] = usage.get(source, [])
                row["覆盖模块"] = sorted({item["模块"] for item in usage.get(source, [])})

        return {
            "同步时间": synced_at,
            "标准维度数": sum(1 for row in store.rows(STANDARD_MODULE) if row.get("有效")),
            "版本总数": len(store.rows(VERSION_MODULE)),
            "新增版本数": created_versions,
            "冲突数": len(conflicts),
            "冲突": conflicts,
            "缺少标准映射": self.missing_dimensions(),
        }

    def sync_module(self, module: str) -> None:
        """其他模块写入维度映射后，把引用关系同步回标准结果。

        落库点与模块自身的 create/action 在同一个服务调用里，口径没变就不追加版本。
        """
        usage = _usage_map()
        for row in store.rows(STANDARD_MODULE):
            references = usage.get(str(row.get("源维度") or ""), [])
            row["引用"] = references
            row["覆盖模块"] = sorted({item["模块"] for item in references})
            if row.get("有效"):
                row["同步时间"] = now_text()

    def _tradeoff_text(self, source: str, ordered: list[dict[str, Any]]) -> str:
        if len(ordered) == 1:
            return f"源维度 {source} 只有一条已生效映射，直接采用 {ordered[0].get('规则编号')}"
        winner = ordered[0]
        displaced = "、".join(
            f"{item.get('规则编号')}（标准维度 {item.get('标准维度名')}）" for item in ordered[1:]
        )
        return (
            f"源维度 {source} 有 {len(ordered)} 条已生效映射同时可用，属于新旧口径冲突。"
            f"按 映射优先级 > 最近更新时间 > 登记顺序 取 {winner.get('规则编号')}"
            f"（标准维度 {winner.get('标准维度名')}，优先级 {winner.get('映射优先级')}，"
            f"最近更新 {winner.get('最近更新时间')}）；"
            f"被替换：{displaced}。权衡代价：被替换规则的下游视图在重算快照前仍按旧口径展示，"
            f"本次选择以优先级和更新时间更新的一方为准，冲突信息随快照留痕。"
        )

    def _upsert_standard(
        self,
        source: str,
        ordered: list[dict[str, Any]],
        tradeoff: str,
        usage: dict[str, list[dict[str, str]]],
        synced_at: str,
    ) -> int:
        winner = ordered[0]
        references = usage.get(source, [])
        fingerprint = _fingerprint(winner)
        row = store.find_by(STANDARD_MODULE, 源维度=source)
        created = 0
        if row is None:
            row = {
                "id": store.next_id(STANDARD_MODULE),
                "源维度": source,
            }
            store.rows(STANDARD_MODULE).append(row)
        changed = row.get("口径指纹") != fingerprint or not row.get("有效")
        row.update({
            "标准维度": winner.get("标准维度名"),
            "映射类型": winner.get("映射类型"),
            "值转换逻辑": winner.get("值转换逻辑"),
            "默认取值": winner.get("默认取值"),
            "映射优先级": winner.get("映射优先级"),
            "规则ID": winner.get("id"),
            "规则编号": winner.get("规则编号"),
            "口径指纹": fingerprint,
            "有效": True,
            "状态": "已生效",
            "同步时间": synced_at,
            "引用": references,
            "覆盖模块": sorted({item["模块"] for item in references}),
            "候选规则": [_rule_brief(item) for item in ordered],
            "冲突": len(ordered) > 1,
            "权衡说明": tradeoff,
        })
        if changed:
            created = self._append_version(
                source=source,
                rule=winner,
                ordered=ordered,
                tradeoff=tradeoff,
                usage=references,
                synced_at=synced_at,
                state="已生效",
            )
            version = store.rows(VERSION_MODULE)[-1]
            row["版本ID"] = version["id"]
            row["版本编号"] = version["版本编号"]
        return created

    def _append_version(
        self,
        *,
        source: str,
        rule: dict[str, Any] | None,
        ordered: list[dict[str, Any]],
        tradeoff: str,
        usage: list[dict[str, str]],
        synced_at: str,
        state: str,
    ) -> int:
        version_id = store.next_id(VERSION_MODULE)
        store.rows(VERSION_MODULE).append({
            "id": version_id,
            "版本编号": f"VMAP-{version_id:04d}",
            "源维度": source,
            "标准维度": (rule or {}).get("标准维度名") if rule else None,
            "规则ID": (rule or {}).get("id") if rule else None,
            "规则编号": (rule or {}).get("规则编号") if rule else None,
            "映射类型": (rule or {}).get("映射类型") if rule else None,
            "值转换逻辑": (rule or {}).get("值转换逻辑") if rule else None,
            "默认取值": (rule or {}).get("默认取值") if rule else None,
            "映射优先级": (rule or {}).get("映射优先级") if rule else None,
            "口径指纹": _fingerprint(rule) if rule else "",
            "口径状态": state,
            "候选规则": [_rule_brief(item) for item in ordered],
            "冲突": len(ordered) > 1,
            "权衡说明": tradeoff,
            "引用": usage,
            "记录时间": synced_at,
        })
        return 1


standard_mapping_service = StandardMappingService()
