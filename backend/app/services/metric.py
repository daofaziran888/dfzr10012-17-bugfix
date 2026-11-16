"""流作业指标采集业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services import jobops_archive
from app.store import store

MODULE = "metric"
REQUIRED_FIELDS = ["采集器编号", "作业名称", "指标名称"]
STATUS_ORDER = ["草稿", "采集中", "已暂停", "已关闭"]
TERMINAL_STATUS = STATUS_ORDER[-1]
ACTION_RULES = {"启动采集": "采集中", "暂停采集": "已暂停", "关闭采集": "已关闭"}
NEGATIVE_ACTIONS: list[str] = []


class MetricService:
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
            rows = [row for row in rows if keyword in str(row.get("采集器编号", ""))]
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
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        archive_version: Any = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标采集器 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流作业指标采集可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or STATUS_ORDER[0])
        if current not in STATUS_ORDER:
            current = STATUS_ORDER[0]
        if current == TERMINAL_STATUS:
            if target == TERMINAL_STATUS:
                return entry, "指标采集器已关闭，重复提交按一次处理"
            return None, "指标采集器已关闭，终态不可再推进"
        if target == current:
            return entry, f"指标采集器已处于{current}，重复{action}按一次处理"
        if STATUS_ORDER.index(target) < STATUS_ORDER.index(current):
            return None, f"采集器状态只能按{'→'.join(STATUS_ORDER)}推进，不能从{current}回退到{target}"
        entry["status"] = target
        entry["pending"] = target != TERMINAL_STATUS
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        message = f"指标采集器已{action}"
        # 同步写入同一份作业实例状态数据存档，消费侧带入版本号；
        # 落库结论只采用这一份可追溯的存档，不与作业终态各记一套
        instance = jobops_archive.find_instance_by_job_name(str(entry.get("作业名称") or ""))
        if instance is not None:
            row, _, note = jobops_archive.record_transition(
                instance=instance,
                source=jobops_archive.SOURCE_METRIC,
                action=action,
                expected_version=archive_version,
            )
            if row is not None:
                entry["存档版本"] = row.get("version")
                entry["落库结论"] = row.get("处理结论")
            if note:
                message = f"{message}；{note}"
        return entry, message
