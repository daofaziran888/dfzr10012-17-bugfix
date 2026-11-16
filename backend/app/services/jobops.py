"""流作业运维业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services import jobops_archive
from app.store import store

MODULE = "jobops"
REQUIRED_FIELDS = ["实例编号", "作业名称", "运行节点"]
STATUS_ORDER = ["待启动", "运行中", "已暂停", "已停止"]
TERMINAL_STATUS = STATUS_ORDER[-1]
ACTION_RULES = {"启动作业": "运行中", "暂停作业": "已暂停", "停止作业": "已停止"}
NEGATIVE_ACTIONS: list[str] = []


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class JobopsService:
    def _normalize(self, entry: dict[str, Any]) -> dict[str, Any]:
        """兼容历史结果：补齐缺失字段，运行计数容错为整数，实例状态跟随当前状态。"""
        status = str(entry.get("status") or STATUS_ORDER[0])
        if status not in STATUS_ORDER:
            status = STATUS_ORDER[0]
        entry["status"] = status
        entry["实例状态"] = status
        entry["重启次数"] = _to_int(entry.get("重启次数"))
        entry["异常次数"] = _to_int(entry.get("异常次数"))
        return entry

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 沿着既有取数路径回补缺失的存档记录，历史实例保持可追溯
        jobops_archive.ensure_backfill()
        rows = [self._normalize(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("实例编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._normalize(entry)

    def list_archive(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """作业实例状态数据存档清单：展示每条实例的处理结论。"""
        return jobops_archive.list_archive(keyword=keyword, status=status, page=page, size=size)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["实例状态"] = STATUS_ORDER[0]
        entry["启动时间"] = str(values.get("启动时间") or "")
        entry["运行时长"] = str(values.get("运行时长") or "0分钟")
        entry["异常次数"] = _to_int(values.get("异常次数"))
        entry["重启次数"] = _to_int(values.get("重启次数"))
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"作业实例 {entry_id} 不存在或已归档"
        self._normalize(entry)
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流作业运维可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = entry["status"]
        if current == TERMINAL_STATUS:
            if target == TERMINAL_STATUS:
                return entry, "作业实例已停止，重复提交按一次处理"
            return None, "作业实例已停止，终态不可再推进"
        if target == current:
            return entry, f"作业实例已处于{current}，重复{action}按一次处理"
        if STATUS_ORDER.index(target) < STATUS_ORDER.index(current):
            return None, f"实例状态只能按{'→'.join(STATUS_ORDER)}推进，不能从{current}回退到{target}"
        # 停止与暂停并发时先到先赢，只接受一个终态；重启次数随实例保留，动作不改写
        entry["status"] = target
        entry["实例状态"] = target
        entry["pending"] = target != TERMINAL_STATUS
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 校验通过后写入作业实例状态数据存档，保留最后终态、重启次数和运行结果
        jobops_archive.record_transition(instance=entry, source=jobops_archive.SOURCE_JOBOPS, action=action)
        return entry, f"作业实例已{action}，最后终态{target}，重启次数{entry['重启次数']}"
