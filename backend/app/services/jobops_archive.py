"""作业实例状态数据存档：流作业运维与流作业指标采集共用的一份终态存档。

约定：
- 每个作业实例只保留一条存档，记录最后终态、重启次数和运行结果；
- 实例状态只能按 待启动 → 运行中 → 已暂停 → 已停止 推进，终态不可回退；
- 重复提交按一次处理：终态与重启次数都相同的写入直接复用现有存档；
- 消费侧（如流作业指标采集）写入时带入版本号，版本不一致以存档现有结论为准，
  同一实例不会同时落下「运行中重启」和「已停止」两份结论。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

ARCHIVE_MODULE = "jobops_archive"
JOB_MODULE = "jobops"
STATUS_ORDER = ["待启动", "运行中", "已暂停", "已停止"]
TERMINAL_STATUS = STATUS_ORDER[-1]
SOURCE_JOBOPS = "流作业运维"
SOURCE_METRIC = "流作业指标采集"

RESULT_BY_STATUS = {"待启动": "未运行", "已暂停": "已暂停", "已停止": "正常停止"}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def run_result(status: str, restarts: int) -> str:
    """运行结果由最后状态推导，保证重启次数、运行结果与最后状态始终一致。"""
    if status == "运行中":
        return "运行中重启" if restarts > 0 else "运行中"
    return RESULT_BY_STATUS.get(status, "未运行")


def _rows() -> list[dict[str, Any]]:
    return store.rows(ARCHIVE_MODULE)


def latest_for(instance_code: str) -> dict[str, Any] | None:
    """读取某实例的存档（每实例一条，即可追溯的那一份）。"""
    for row in _rows():
        if str(row.get("实例编号", "")) == instance_code:
            return row
    return None


def find_instance_by_job_name(job_name: str) -> dict[str, Any] | None:
    """按作业名称找到对应的 JOBO 作业实例，供指标采集模块同步落库。"""
    for row in store.rows(JOB_MODULE):
        if str(row.get("作业名称", "")) == job_name:
            return row
    return None


def _conclusion(source: str, action: str, status: str, restarts: int, result: str) -> str:
    head = f"{source}·{action}" if action else source
    return f"{head}：最后终态{status}，重启次数{restarts}，运行结果{result}"


def record_transition(
    *,
    instance: dict[str, Any],
    source: str,
    action: str = "",
    expected_version: Any = None,
) -> tuple[dict[str, Any] | None, bool, str]:
    """校验状态推进后写入存档，返回 (存档行, 是否新建/推进, 说明)。

    expected_version 为消费侧带入的存档版本号；与现有版本不一致时，
    以存档中的最后终态为准，不再落第二份结论。
    """
    code = str(instance.get("实例编号") or "").strip()
    if not code:
        return None, False, "实例编号缺失，未写入存档"
    status = str(instance.get("status") or STATUS_ORDER[0])
    if status not in STATUS_ORDER:
        status = STATUS_ORDER[0]
    restarts = _to_int(instance.get("重启次数"))
    result = run_result(status, restarts)
    current = latest_for(code)
    if current is not None:
        if expected_version is not None and _to_int(expected_version, -1) != _to_int(current.get("version")):
            return current, False, f"存档版本已推进到 v{current.get('version')}，以存档最后终态为准"
        last = str(current.get("最后终态") or STATUS_ORDER[0])
        if last == TERMINAL_STATUS and status != TERMINAL_STATUS:
            return current, False, "实例已停止，终态唯一，运行中重启类结论不再落库"
        if STATUS_ORDER.index(status) < STATUS_ORDER.index(last):
            return current, False, "存档终态只能按待启动→运行中→已暂停→已停止推进，不可回退"
        if last == status and _to_int(current.get("重启次数")) == restarts:
            return current, False, "重复提交按一次处理"
        current.update({
            "作业名称": instance.get("作业名称", current.get("作业名称")),
            "运行节点": instance.get("运行节点", current.get("运行节点")),
            "最后终态": status,
            "重启次数": restarts,
            "运行结果": result,
            "处理结论": _conclusion(source, action, status, restarts, result),
            "来源模块": source,
            "version": _to_int(current.get("version")) + 1,
            "status": status,
            "pending": status != TERMINAL_STATUS,
            "abnormal": False,
            "更新时间": datetime.now().isoformat(timespec="seconds"),
        })
        return current, True, "存档已推进"
    row = {
        "id": max((_to_int(r.get("id")) for r in _rows()), default=0) + 1,
        "实例编号": code,
        "作业名称": instance.get("作业名称", ""),
        "运行节点": instance.get("运行节点", ""),
        "最后终态": status,
        "重启次数": restarts,
        "运行结果": result,
        "处理结论": _conclusion(source, action, status, restarts, result),
        "来源模块": source,
        "version": 1,
        "status": status,
        "pending": status != TERMINAL_STATUS,
        "abnormal": False,
        "更新时间": datetime.now().isoformat(timespec="seconds"),
    }
    _rows().append(row)
    return row, True, "存档已建立"


def ensure_backfill() -> None:
    """沿着既有取数路径回补缺失记录：每个作业实例至少有一条可追溯的存档。"""
    for instance in store.rows(JOB_MODULE):
        code = str(instance.get("实例编号") or "").strip()
        if code and latest_for(code) is None:
            record_transition(instance=instance, source=SOURCE_JOBOPS, action="历史状态回补")


def list_archive(
    *,
    keyword: str | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """存档清单视图：先回补缺失记录，再按实例编号与终态过滤。"""
    ensure_backfill()
    rows = list(_rows())
    if keyword:
        rows = [
            row for row in rows
            if keyword in str(row.get("实例编号", "")) or keyword in str(row.get("作业名称", ""))
        ]
    if status:
        rows = [row for row in rows if row.get("最后终态") == status]
    total = len(rows)
    start = max(page - 1, 0) * size
    return rows[start:start + size], total
