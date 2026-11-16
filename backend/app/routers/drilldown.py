"""多维下钻接口：维护下钻路径，并生成 / 读取下钻路径版本快照。

路由声明顺序有讲究：所有固定路径（export、snapshots、ledger…）必须排在
``/{entry_id}`` 前面，否则会被当成路径 ID 解析掉。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.drilldown import (
    DEFAULT_SORT,
    SORT_FIELDS,
    DrilldownService,
    drilldown_snapshot_service,
)
from app.services.standardmapping import standard_mapping_service

router = APIRouter(prefix="/api/drilldown", tags=["多维下钻"])

service = DrilldownService()
snapshots = drilldown_snapshot_service

LIST_FIELDS = ["路径编号", "起始维度", "下钻层级序列", "每层聚合粒度", "默认度量", "路径深度", "缓存秒数", "数据新鲜度"]
STATUSES = ["草稿", "路径评审中", "已启用", "已归档"]
PAGE_MAX = 200


def _page(size: int) -> None:
    if size > PAGE_MAX:
        raise HTTPException(status_code=400, detail=f"每页最多 {PAGE_MAX} 条，请缩小分页范围")


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路径编号检索"),
    status: str | None = Query(default=None, description="草稿、路径评审中、已启用、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按路径编号与状态过滤多维下钻列表；没有数据时返回空页，不报错。"""
    _page(size)
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条下钻路径，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="下钻路径已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出多维下钻清单：返回全量路径配置。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "drilldown", "total": total, "items": items}


# ---------- 逐层结果与快照 ----------


@router.get("/snapshots")
def list_snapshots(
    path_id: int | None = Query(default=None, description="按下钻路径过滤"),
    start_dimension: str | None = Query(default=None, description="按起始维度过滤"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """列出已生成的下钻路径版本快照，可按起始维度定位。"""
    _page(size)
    items, total = snapshots.list_snapshots(
        path_id=path_id, start_dimension=start_dimension, page=page, size=size
    )
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int) -> dict[str, Any]:
    """读取快照头部：层级序列、版本绑定、各层新鲜度。"""
    snapshot = snapshots.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"下钻路径版本快照 {snapshot_id} 不存在")
    return snapshot


@router.get("/snapshots/{snapshot_id}/layers")
def query_layers(
    snapshot_id: int,
    start_dimension: str | None = Query(default=None, description="按起始维度筛选，需与快照一致"),
    level: int | None = Query(default=None, description="只看某一层"),
    locate: str | None = Query(default=None, description="按分组值定位所在层"),
    sort_field: str = Query(default=DEFAULT_SORT, description="、".join(sorted(SORT_FIELDS))),
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = 1,
    size: int = 50,
) -> dict[str, Any]:
    """读取分层聚合明细：可筛选起始维度、按层定位并排序。"""
    _page(size)
    result = snapshots.query_layers(
        snapshot_id,
        start_dimension=start_dimension,
        level=level,
        locate=locate,
        sort_field=sort_field,
        sort_order=sort_order,
        page=page,
        size=size,
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/snapshots/{snapshot_id}/check")
def check_snapshot(snapshot_id: int) -> dict[str, Any]:
    """核对快照绑定版本与当前标准维度口径是否漂移。"""
    result = snapshots.check_snapshot(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"下钻路径版本快照 {snapshot_id} 不存在")
    return result


# ---------- 处理台账 ----------


@router.get("/ledger")
def list_ledger(
    path_id: int | None = Query(default=None, description="按下钻路径过滤"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """快照生成提交台账：保留每个提交的处理顺序与受理结果。"""
    _page(size)
    items, total = snapshots.list_ledger(path_id=path_id, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


# ---------- 详情 / 动作 / 快照生成 ----------


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    """读取单条下钻路径明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条下钻路径执行提交路径评审、启用路径、归档路径；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/precheck")
def precheck_path(entry_id: int) -> dict[str, Any]:
    """生成前先看上游是否齐备：路径状态、层级序列、标准维度绑定与缺失项。"""
    inspection = snapshots.inspect_path(entry_id)
    if inspection.get("路径") is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在")
    return inspection


@router.post("/{entry_id}/snapshots", response_model=ActionResult)
def generate_snapshot(entry_id: int, payload: EntryPayload) -> ActionResult:
    """生成下钻路径版本快照；上游未完成或顺序在后时返回可读的拒绝原因。"""
    snapshot, message = snapshots.submit(entry_id, payload.values)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.get("/{entry_id}/cache")
def cached_drill(entry_id: int) -> dict[str, Any]:
    """读取明细下钻的缓存结果；口径漂移时仍按旧维度展示并明确提示。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return snapshots.cached_drill(entry_id)


@router.get("/{entry_id}/standards")
def path_standards(entry_id: int) -> dict[str, Any]:
    """读取该路径每一层绑定的标准维度版本（生成前预览当前口径）。"""
    inspection = snapshots.inspect_path(entry_id)
    if inspection.get("路径") is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在")
    paths = inspection["路径"]
    return {
        "路径ID": entry_id,
        "路径编号": paths.get("路径编号"),
        "起始维度": inspection.get("起始维度"),
        "层级序列": inspection.get("层级序列"),
        "层绑定": inspection.get("层绑定"),
        "上游问题": inspection.get("问题"),
        "缺少标准映射": standard_mapping_service.missing_dimensions(),
    }
