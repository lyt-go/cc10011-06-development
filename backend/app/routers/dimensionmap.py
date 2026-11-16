"""维度映射接口：维护映射规则，并对外提供标准维度映射结果与版本。

路由顺序：固定路径（standards / versions / sync / export）排在 ``/{entry_id}`` 之前。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dimensionmap import DimensionmapService
from app.services.standardmapping import standard_mapping_service

router = APIRouter(prefix="/api/dimensionmap", tags=["维度映射"])

service = DimensionmapService()

LIST_FIELDS = ["规则编号", "原始维度名", "标准维度名", "映射类型", "值转换逻辑", "默认取值", "映射优先级", "最近更新时间"]
STATUSES = ["草稿", "规则评审中", "已生效", "已废弃"]


def _page(size: int) -> None:
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤维度映射列表；没有数据时返回空页，不报错。"""
    _page(size)
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条映射规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="映射规则已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出维度映射清单：返回全量映射规则。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dimensionmap", "total": total, "items": items}


@router.get("/standards")
def list_standards(
    keyword: str | None = Query(default=None, description="按源维度或标准维度检索"),
    conflict_only: bool = Query(default=False, description="只看存在新旧口径冲突的维度"),
) -> dict[str, Any]:
    """标准维度映射结果：每个源维度一条当前口径，附版本号与冲突权衡说明。"""
    items = standard_mapping_service.list_standards(keyword=keyword, conflict_only=conflict_only)
    return {
        "items": items,
        "total": len(items),
        "缺少标准映射": standard_mapping_service.missing_dimensions(),
    }


@router.get("/versions")
def list_versions(source: str | None = Query(default=None, description="按源维度过滤")) -> dict[str, Any]:
    """标准维度映射版本：同一源维度每次口径变化追加一版，供下钻快照追溯。"""
    items = standard_mapping_service.list_versions(source)
    return {"items": items, "total": len(items)}


@router.post("/sync")
def sync_standards() -> dict[str, Any]:
    """手工触发一次全量同步：重建标准结果、追加版本并刷新各模块引用关系。"""
    return standard_mapping_service.sync_all()


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    """读取单条映射规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"映射规则 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条映射规则执行提交规则评审、生效规则、废弃规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
