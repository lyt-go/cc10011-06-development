"""多维下钻业务规则：路径状态流转 + 下钻路径版本快照。

快照生成的数据流：

1. 读取下钻路径配置（起始维度、层级序列、路径深度、默认度量、聚合粒度）
2. 逐层读取**标准维度映射结果**，把每个层级绑定到一个可追溯的标准维度版本
   （冲突维度在写入标准结果时已完成一次权衡，快照只接收唯一结论并留痕）
3. 按层级前缀对维度事实做分层聚合，产出「分层聚合明细数据集」
4. 每层记录数据截止时间、来源批次与计算时间，保证新鲜度可追溯
5. 快照头部保存层级序列、路径配置指纹与各层版本绑定，任何一层口径变化都能被核对出来

上游未完成（路径未启用 / 层级序列不完整 / 维度缺标准映射 / 度量字段缺失）时直接拒绝生成，
并把这次提交记入处理台账。多个提交同时到达时用一把锁把「校验 + 受理」做成原子操作，
同一路径同一上游基准只接纳处理顺序最靠前的那个，其余提交返回被拒原因和已受理顺序。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta
from typing import Any

from app.services.standardmapping import (
    now_text,
    parse_dimensions,
    standard_mapping_service,
)
from app.store import store

MODULE = "drilldown"
SNAPSHOT_MODULE = "drilldownpathversion"
LAYER_MODULE = "drilldownlayerresult"
FACT_MODULE = "drilldown_dimension_fact"
LEDGER_MODULE = "drilldown_generation_ledger"

REQUIRED_FIELDS = ["路径编号", "起始维度", "下钻层级序列"]
STATUS_ORDER = ["草稿", "路径评审中", "已启用", "已归档"]
ACTION_RULES = {"提交路径评审": "路径评审中", "启用路径": "已启用", "归档路径": "已归档"}
NEGATIVE_ACTIONS = []
ACTIVE_STATUS = "已启用"

DEFAULT_SORT = "层级序号"
SORT_FIELDS = {"层级序号", "度量值", "维度路径", "数据截止时间", "记录数"}

_accept_lock = threading.Lock()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _fingerprint(parts: list[Any]) -> str:
    raw = "|".join(_text(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class DrilldownService:
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
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
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
        # 落库点：下钻路径引用的维度同步进标准维度映射结果（只更新引用关系）。
        standard_mapping_service.sync_module(MODULE)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维下钻可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 落库点：层级配置变化后刷新标准维度引用关系；已生成快照不回溯修改。
        standard_mapping_service.sync_module(MODULE)
        return entry, f"下钻路径已{action}"


class DrilldownSnapshotService:
    """下钻路径版本快照：校验上游、绑定版本、分层聚合、可追溯查询。"""

    def list_snapshots(
        self,
        *,
        path_id: int | None = None,
        start_dimension: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(SNAPSHOT_MODULE))
        if path_id is not None:
            rows = [row for row in rows if int(row.get("路径ID", 0)) == path_id]
        if start_dimension:
            rows = [row for row in rows if row.get("起始维度") == start_dimension]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return store.find(SNAPSHOT_MODULE, snapshot_id)

    def latest_snapshot(self, path_id: int) -> dict[str, Any] | None:
        rows = [row for row in store.rows(SNAPSHOT_MODULE) if int(row.get("路径ID", 0)) == path_id]
        if not rows:
            return None
        return max(rows, key=lambda row: int(row.get("id", 0)))

    # ---------- 上游校验 ----------

    def inspect_path(self, path_id: int) -> dict[str, Any]:
        """检查路径配置与标准维度是否齐备；返回校验结论，不写库。"""
        path = store.find(MODULE, path_id)
        issues: list[str] = []
        bindings: list[dict[str, Any]] = []
        if path is None:
            return {"路径": None, "问题": [f"下钻路径 {path_id} 不存在"], "层绑定": []}
        if path.get("status") != ACTIVE_STATUS:
            issues.append(f"下钻路径当前状态为「{path.get('status')}」，路径配置尚未完成启用")
        levels = parse_dimensions(path.get("下钻层级序列"))
        if not levels:
            issues.append("下钻层级序列为空，无法确定逐层聚合顺序")
        start_dimension = _text(path.get("起始维度"))
        if not start_dimension:
            issues.append("起始维度为空")
        elif levels and start_dimension != levels[0]:
            issues.append(
                f"起始维度「{start_dimension}」与层级序列首层「{levels[0]}」不一致，层级序列不可信"
            )
        depth_text = _text(path.get("路径深度"))
        if depth_text:
            depth = _to_int(depth_text)
            if depth and depth != len(levels):
                issues.append(f"路径深度 {depth} 与层级序列层数 {len(levels)} 不一致")
        metric = _text(path.get("默认度量"))
        facts = store.rows(FACT_MODULE)
        if not metric:
            issues.append("默认度量为空，无法做分层聚合")
        elif facts and metric not in facts[0]:
            issues.append(f"维度事实中没有度量字段「{metric}」，聚合口径无法落地")
        if not facts:
            issues.append("维度事实为空，没有任何可聚合的数据")

        for index, level in enumerate(levels, start=1):
            standard = standard_mapping_service.current_standard(level)
            if standard is None:
                issues.append(f"第 {index} 层维度「{level}」没有已生效的标准维度映射")
                continue
            if facts and level not in facts[0]:
                issues.append(f"第 {index} 层维度「{level}」在维度事实中缺少字段")
                continue
            binding = {
                "层级序号": index,
                "维度名": level,
                "标准维度": standard.get("标准维度"),
                "标准维度版本ID": standard.get("版本ID"),
                "标准维度版本编号": standard.get("版本编号"),
                "标准维度规则编号": standard.get("规则编号"),
                "口径指纹": standard.get("口径指纹"),
                "冲突": bool(standard.get("冲突")),
                "权衡说明": standard.get("权衡说明"),
            }
            if standard.get("版本ID") is None:
                issues.append(
                    f"第 {index} 层维度「{level}」的标准映射缺少版本记录，无法形成可追溯口径"
                )
            bindings.append(binding)

        path_fingerprint = _fingerprint([
            start_dimension,
            ">".join(levels),
            _text(path.get("路径深度")),
            metric,
            _text(path.get("每层聚合粒度")),
        ])
        bindings_fingerprint = _fingerprint([
            f"{item['层级序号']}:{item['维度名']}:{item['标准维度版本ID']}:{item['口径指纹']}"
            for item in bindings
        ])
        return {
            "路径": path,
            "层级序列": levels,
            "起始维度": start_dimension,
            "度量": metric,
            "问题": issues,
            "层绑定": bindings,
            "路径配置指纹": path_fingerprint,
            "口径基准": bindings_fingerprint,
        }

    # ---------- 提交受理 ----------

    def submit(self, path_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """提交一次快照生成：校验 + 受理在锁内完成，只接纳首个通过校验的顺序。"""
        submitter = _text(values.get("提交人")) or "值班管理员"
        batch = _text(values.get("提交批次")) or f"批次-{now_text()}"
        with _accept_lock:
            inspection = self.inspect_path(path_id)
            path = inspection.get("路径") or {}
            submitted_at = now_text()
            if inspection.get("问题"):
                ledger = self._record_ledger(
                    path_id=path_id,
                    path_number=_text(path.get("路径编号")),
                    submitter=submitter,
                    batch=batch,
                    submitted_at=submitted_at,
                    accepted=False,
                    reason="上游未完成： " + "；".join(inspection["问题"]),
                    inspection=inspection,
                )
                return None, (
                    f"不能生成分层聚合明细数据集：{'；'.join(inspection['问题'])}"
                    f"（提交已记入处理台账 #{ledger['id']}，校验未通过）"
                )

            base = f"{inspection['路径配置指纹']}|{inspection['口径基准']}"
            existing = None if values.get("重新生成") else self._find_accepted(path_id, base)
            if existing is not None:
                snapshot_id = existing.get("快照ID")
                merged = self.get_snapshot(int(snapshot_id)) if snapshot_id else None
                ledger = self._record_ledger(
                    path_id=path_id,
                    path_number=_text(path.get("路径编号")),
                    submitter=submitter,
                    batch=batch,
                    submitted_at=submitted_at,
                    accepted=False,
                    reason=(
                        f"重复提交已合并：同一路径同一上游口径已由处理顺序 "
                        f"#{existing['受理序号']} 受理"
                    ),
                    inspection=inspection,
                )
                ledger["快照ID"] = snapshot_id
                ledger["快照编号"] = existing.get("快照编号")
                ledger["受理结果"] = "已合并"
                if merged is not None:
                    return merged, (
                        f"提交已合并到处理顺序 #{existing['受理序号']} 的既有快照 "
                        f"{existing.get('快照编号')}（{merged['层级数']} 层、{merged['明细行数']} 条明细），"
                        f"未重复生成；本次提交记为台账 #{ledger['id']}，顺序在后。"
                        f"如需在相同上游口径下重算，请显式选择重新生成"
                    )
                return None, (
                    f"提交未被接纳：同一路径、同一上游口径已由处理顺序 "
                    f"#{existing['受理序号']} 的提交（{existing.get('提交人')}）受理；"
                    f"本次提交记为台账 #{ledger['id']} 顺序在后"
                )

            ledger = self._record_ledger(
                path_id=path_id,
                path_number=_text(path.get("路径编号")),
                submitter=submitter,
                batch=batch,
                submitted_at=submitted_at,
                accepted=True,
                reason="首个通过校验的提交，按此处理顺序生成快照",
                inspection=inspection,
            )
            snapshot = self._build_snapshot(path, inspection, ledger, submitter, batch)
            ledger["快照ID"] = snapshot["id"]
            ledger["快照编号"] = snapshot["快照编号"]
            ledger["受理结果"] = "已生成"
            return snapshot, (
                f"分层聚合明细数据集已生成：快照 {snapshot['快照编号']}，"
                f"{snapshot['层级数']} 层、{snapshot['明细行数']} 条明细，受理顺序 #{ledger['受理序号']}"
            )

    def _find_accepted(self, path_id: int, base: str) -> dict[str, Any] | None:
        accepted = [
            row
            for row in store.rows(LEDGER_MODULE)
            if row.get("路径ID") == path_id and row.get("受理") and row.get("上游基准") == base
        ]
        if not accepted:
            return None
        return min(accepted, key=lambda row: int(row.get("受理序号", 0)))

    def _record_ledger(
        self,
        *,
        path_id: int,
        path_number: str,
        submitter: str,
        batch: str,
        submitted_at: str,
        accepted: bool,
        reason: str,
        inspection: dict[str, Any],
    ) -> dict[str, Any]:
        accepted_rows = [row for row in store.rows(LEDGER_MODULE) if row.get("受理")]
        sequence = max((int(row.get("受理序号", 0)) for row in accepted_rows), default=0) + 1
        ledger = {
            "id": store.next_id(LEDGER_MODULE),
            "路径ID": path_id,
            "路径编号": path_number,
            "提交人": submitter,
            "提交批次": batch,
            "提交时间": submitted_at,
            "受理": accepted,
            "受理序号": sequence if accepted else None,
            "处理顺序": sequence if accepted else None,
            "受理结果": "已受理" if accepted else "已拒绝",
            "裁决说明": reason,
            "上游基准": (
                f"{inspection.get('路径配置指纹')}|{inspection.get('口径基准')}"
                if accepted
                else None
            ),
            "层级序列": inspection.get("层级序列") or [],
            "快照ID": None,
            "快照编号": None,
        }
        store.rows(LEDGER_MODULE).append(ledger)
        return ledger

    # ---------- 分层聚合 ----------

    def _build_snapshot(
        self,
        path: dict[str, Any],
        inspection: dict[str, Any],
        ledger: dict[str, Any],
        submitter: str,
        batch: str,
    ) -> dict[str, Any]:
        levels: list[str] = inspection["层级序列"]
        metric: str = inspection["度量"]
        facts = list(store.rows(FACT_MODULE))
        computed_at = now_text()
        existing = [
            row for row in store.rows(SNAPSHOT_MODULE) if int(row.get("路径ID", 0)) == int(path["id"])
        ]
        version = max((int(row.get("版本序号", 0)) for row in existing), default=0) + 1
        snapshot_id = store.next_id(SNAPSHOT_MODULE)
        snapshot_number = f"SNIP-{snapshot_id:04d}"
        bindings = inspection["层绑定"]
        layer_summaries: list[dict[str, Any]] = []
        detail_rows: list[dict[str, Any]] = []
        detail_id = store.next_id(LAYER_MODULE)
        all_batches: set[str] = set()
        layer_freshness: dict[int, str] = {}

        for depth in range(1, len(levels) + 1):
            group_levels = levels[:depth]
            groups: dict[tuple[str, ...], dict[str, Any]] = {}
            for fact in facts:
                key = tuple(_text(fact.get(level)) or "（空）" for level in group_levels)
                bucket = groups.setdefault(key, {"metric": 0, "rows": 0, "freshness": "", "batches": set()})
                bucket["metric"] += _to_int(fact.get(metric))
                bucket["rows"] += 1
                updated = _text(fact.get("更新时间"))
                if updated > bucket["freshness"]:
                    bucket["freshness"] = updated
                batch_text = _text(fact.get("来源批次"))
                if batch_text:
                    bucket["batches"].add(batch_text)
                    all_batches.add(batch_text)
            binding = next(item for item in bindings if item["层级序号"] == depth)
            layer_total = sum(bucket["metric"] for bucket in groups.values())
            layer_rows = 0
            layer_freshness_value = ""
            for key, bucket in sorted(groups.items(), key=lambda item: (-item[1]["metric"], item[0])):
                layer_rows += 1
                if bucket["freshness"] > layer_freshness_value:
                    layer_freshness_value = bucket["freshness"]
                detail_rows.append({
                    "id": detail_id,
                    "快照ID": snapshot_id,
                    "快照编号": snapshot_number,
                    "路径ID": path.get("id"),
                    "路径编号": path.get("路径编号"),
                    "数据集编号": f"DS-DRILLDOWN-{int(path.get('id', 0)):04d}-V{version}",
                    "层级序号": depth,
                    "层级维度": levels[depth - 1],
                    "层级路径": " > ".join(group_levels),
                    "分组维度": list(group_levels),
                    "分组值": list(key),
                    "维度路径": " / ".join(
                        f"{level}={value}" for level, value in zip(group_levels, key)
                    ),
                    "度量": metric,
                    "度量值": bucket["metric"],
                    "记录数": bucket["rows"],
                    "数据截止时间": bucket["freshness"],
                    "来源批次": sorted(bucket["batches"]),
                    "计算时间": computed_at,
                    "标准维度": binding["标准维度"],
                    "标准维度版本ID": binding["标准维度版本ID"],
                    "标准维度版本编号": binding["标准维度版本编号"],
                    "口径指纹": binding["口径指纹"],
                })
                detail_id += 1
            layer_freshness[depth] = layer_freshness_value
            layer_summaries.append({
                "层级序号": depth,
                "层级维度": levels[depth - 1],
                "层级路径": " > ".join(group_levels),
                "明细行数": layer_rows,
                "度量小计": layer_total,
                "数据截止时间": layer_freshness_value,
                "来源批次": sorted(all_batches),
                "计算时间": computed_at,
                "标准维度": binding["标准维度"],
                "标准维度版本ID": binding["标准维度版本ID"],
                "标准维度版本编号": binding["标准维度版本编号"],
                "口径指纹": binding["口径指纹"],
                "冲突": binding["冲突"],
            })
        store.rows(LAYER_MODULE).extend(detail_rows)

        snapshot = {
            "id": snapshot_id,
            "快照编号": snapshot_number,
            "版本序号": version,
            "路径ID": path.get("id"),
            "路径编号": path.get("路径编号"),
            "起始维度": inspection["起始维度"],
            "层级序列": levels,
            "层级数": len(levels),
            "路径深度": path.get("路径深度"),
            "路径配置指纹": inspection["路径配置指纹"],
            "口径基准": inspection["口径基准"],
            "数据集编号": f"DS-DRILLDOWN-{int(path.get('id', 0)):04d}-V{version}",
            "数据集名称": f"{path.get('路径编号')} 分层聚合明细（{'>'.join(levels)}）",
            "默认度量": metric,
            "每层聚合粒度": path.get("每层聚合粒度"),
            "缓存秒数": _to_int(path.get("缓存秒数")) or 0,
            "状态": "已生成",
            "生成时间": computed_at,
            "提交人": submitter,
            "提交批次": batch,
            "受理序号": ledger["受理序号"],
            "数据新鲜度": min((value for value in layer_freshness.values() if value), default=""),
            "各层新鲜度": [
                {"层级序号": depth, "数据截止时间": value}
                for depth, value in sorted(layer_freshness.items())
            ],
            "来源批次": sorted(all_batches),
            "明细行数": len(detail_rows),
            "层汇总": layer_summaries,
            "版本绑定": bindings,
            "上游问题": [],
        }
        store.rows(SNAPSHOT_MODULE).append(snapshot)
        path["最近快照ID"] = snapshot_id
        path["最近快照ID"] = snapshot_id
        path["最近快照编号"] = snapshot_number
        path["数据新鲜度"] = snapshot["数据新鲜度"]
        self._refresh_cache(snapshot)
        return snapshot

    # ---------- 口径核对 / 查询 ----------

    def check_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        """核对快照绑定的版本与当前标准口径、路径配置是否仍然一致。"""
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return None
        path = store.find(MODULE, int(snapshot.get("路径ID", 0)))
        layers: list[dict[str, Any]] = []
        drifted = False
        for binding in snapshot.get("版本绑定", []):
            current = standard_mapping_service.current_standard(binding["维度名"])
            current_version = current.get("版本ID") if current else None
            if current is None:
                state = "标准维度已失效"
            elif current_version == binding["标准维度版本ID"]:
                state = "一致"
            else:
                state = "口径已变更"
            if state != "一致":
                drifted = True
            layers.append({
                "层级序号": binding["层级序号"],
                "维度名": binding["维度名"],
                "快照绑定版本ID": binding["标准维度版本ID"],
                "快照绑定版本编号": binding["标准维度版本编号"],
                "快照标准维度": binding["标准维度"],
                "当前版本ID": current_version,
                "当前版本编号": current.get("版本编号") if current else None,
                "当前标准维度": current.get("标准维度") if current else None,
                "状态": state,
            })
        current_path = None
        path_changed = False
        if path is not None:
            current_path = _fingerprint([
                _text(path.get("起始维度")),
                ">".join(parse_dimensions(path.get("下钻层级序列"))),
                _text(path.get("路径深度")),
                _text(path.get("默认度量")),
                _text(path.get("每层聚合粒度")),
            ])
            path_changed = current_path != snapshot.get("路径配置指纹")
        return {
            "快照ID": snapshot_id,
            "快照编号": snapshot.get("快照编号"),
            "路径ID": snapshot.get("路径ID"),
            "路径编号": snapshot.get("路径编号"),
            "起始维度": snapshot.get("起始维度"),
            "层级序列": snapshot.get("层级序列"),
            "口径漂移": drifted,
            "路径配置已变更": path_changed,
            "路径当前指纹": current_path,
            "层级核对": layers,
            "结论": self._drift_conclusion(drifted, path_changed),
        }

    @staticmethod
    def _drift_conclusion(drifted: bool, path_changed: bool) -> str:
        if path_changed:
            return "下钻路径层级序列已变更：快照仍保护原层级顺序，请重新生成后再对外展示"
        if drifted:
            return "标准维度口径已变更：快照仍绑定生成时的版本，缓存中的明细仍按旧维度展示"
        return "快照绑定版本与当前标准口径一致"

    def query_layers(
        self,
        snapshot_id: int,
        *,
        start_dimension: str | None = None,
        level: int | None = None,
        locate: str | None = None,
        sort_field: str = DEFAULT_SORT,
        sort_order: str = "asc",
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return {"error": f"下钻路径版本快照 {snapshot_id} 不存在"}
        if sort_field not in SORT_FIELDS:
            return {"error": f"排序字段「{sort_field}」不支持，可选：{'、'.join(sorted(SORT_FIELDS))}"}
        if start_dimension and start_dimension != snapshot.get("起始维度"):
            return {
                "error": (
                    f"快照 {snapshot.get('快照编号')} 的起始维度是「{snapshot.get('起始维度')}」，"
                    f"不能按「{start_dimension}」读取"
                )
            }
        rows = [
            row for row in store.rows(LAYER_MODULE) if int(row.get("快照ID", 0)) == snapshot_id
        ]
        if level is not None:
            rows = [row for row in rows if int(row.get("层级序号", 0)) == level]
        located_level = None
        if locate:
            needle = _text(locate)
            hits = [
                row
                for row in rows
                if needle in row.get("维度路径", "")
                or needle in [str(value) for value in row.get("分组值", [])]
            ]
            if hits:
                # 同一分组值可能出现在多个层级，优先定位到最早出现它的那一层。
                located_level = min(int(row.get("层级序号", 0)) for row in hits)
                rows = sorted(hits, key=lambda row: int(row.get("层级序号", 0)))
            else:
                rows = []
        reverse = sort_order.lower() == "desc"
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(int(row.get("层级序号", 0)), []).append(row)
        ordered: list[dict[str, Any]] = []
        for depth in sorted(grouped):
            ordered.extend(sorted(grouped[depth], key=lambda row: self._sort_key(row, sort_field), reverse=reverse))
        rows = ordered
        total = len(rows)
        start = max(page - 1, 0) * size
        return {
            "snapshot": snapshot,
            "核对": self.check_snapshot(snapshot_id),
            "起始维度": snapshot.get("起始维度"),
            "层级序列": snapshot.get("层级序列"),
            "定位层级": located_level,
            "items": rows[start:start + size],
            "total": total,
            "page": page,
            "size": size,
            "排序": {"字段": sort_field, "方向": "desc" if reverse else "asc"},
        }

    @staticmethod
    def _sort_key(row: dict[str, Any], field: str) -> tuple[Any, ...]:
        """层内排序：度量值按数值比较，其余按文本比较，末位用度量值兜底。"""
        if field == "度量值":
            return (_to_int(row.get("度量值")), row.get("维度路径", ""))
        if field == "层级序号":
            return (int(row.get("层级序号", 0)), row.get("维度路径", ""))
        return (str(row.get(field, "")), row.get("维度路径", ""))

    # ---------- 缓存 ----------

    def _refresh_cache(self, snapshot: dict[str, Any]) -> None:
        cache = getattr(self, "_cache", None)
        if cache is None:
            cache = {}
            self._cache = cache
        ttl = int(snapshot.get("缓存秒数", 0) or 0)
        cached_at = datetime.now().astimezone()
        cache[int(snapshot.get("路径ID", 0))] = {
            "快照ID": snapshot["id"],
            "快照编号": snapshot["快照编号"],
            "缓存时间": cached_at.strftime("%Y-%m-%d %H:%M:%S"),
            "过期时间": (cached_at + timedelta(seconds=ttl)).strftime("%Y-%m-%d %H:%M:%S") if ttl else None,
            "过期时刻": cached_at + timedelta(seconds=ttl) if ttl else None,
            "起始维度": snapshot.get("起始维度"),
            "层级序列": snapshot.get("层级序列"),
            "版本绑定": snapshot.get("版本绑定"),
        }

    def cached_drill(self, path_id: int) -> dict[str, Any]:
        """读取明细下钻的缓存结果；缓存仍按生成时的维度展示，并给出漂移提示。"""
        cache = getattr(self, "_cache", None) or {}
        entry = cache.get(path_id)
        if entry is None:
            latest = self.latest_snapshot(path_id)
            if latest is None:
                return {"缓存命中": False, "items": [], "提示": "该路径还没有生成下钻路径版本快照"}
            return {
                "缓存命中": False,
                "items": self._snapshot_rows(latest["id"]),
                "snapshot": latest,
                "提示": "缓存未命中，已按最新快照返回（建议先核对口径漂移）",
            }
        now = datetime.now().astimezone()
        expired = bool(entry.get("过期时刻") and now > entry["过期时刻"])
        drift = self.check_snapshot(int(entry["快照ID"])) or {}
        rows = self._snapshot_rows(int(entry["快照ID"]))
        return {
            "缓存命中": True,
            "缓存过期": expired,
            "缓存时间": entry.get("缓存时间"),
            "过期时间": entry.get("过期时间"),
            "快照ID": entry["快照ID"],
            "快照编号": entry["快照编号"],
            "起始维度": entry.get("起始维度"),
            "层级序列": entry.get("层级序列"),
            "版本绑定": entry.get("版本绑定"),
            "核对": drift,
            "items": rows,
            "提示": (
                "缓存中的明细下钻结果仍按生成时的标准维度展示，"
                f"当前口径核对结论：{drift.get('结论')}"
                if drift.get("口径漂移") or drift.get("路径配置已变更")
                else "缓存中的明细下钻结果与当前口径一致"
            ),
        }

    def _snapshot_rows(self, snapshot_id: int) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(LAYER_MODULE) if int(row.get("快照ID", 0)) == snapshot_id
        ]

    def list_ledger(
        self,
        *,
        path_id: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(LEDGER_MODULE))
        if path_id is not None:
            rows = [row for row in rows if int(row.get("路径ID", 0)) == path_id]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total


drilldown_snapshot_service = DrilldownSnapshotService()
