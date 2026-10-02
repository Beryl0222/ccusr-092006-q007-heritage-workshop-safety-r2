"""事件种类定义与事件存储。

所有状态变更都以只追加事件落库；当前状态由事件回放得到，
因此材料批次、讲师、场地的任何变更都会留下可审计痕迹，
上一场的放行签字无法被静默复用。
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Callable, Iterator

from .clock import Clock, SystemClock, to_iso

# ---- 事件种类 -----------------------------------------------------------

CRAFT_VERSIONED = "CRAFT_VERSIONED"          # 工坊登记/更新技艺版本
MATERIAL_RECEIVED = "MATERIAL_RECEIVED"      # 材料批次入库
INSTRUCTOR_REGISTERED = "INSTRUCTOR_REGISTERED"  # 讲师资格登记
VENUE_REGISTERED = "VENUE_REGISTERED"        # 场地与应急设施登记
SESSION_SCHEDULED = "SESSION_SCHEDULED"      # 场次排定（绑定版本/场地/讲师/批次）
SESSION_AMENDED = "SESSION_AMENDED"          # 换材料/讲师/场地/版本（旧签字失效）
ENROLLMENT_ADDED = "ENROLLMENT_ADDED"        # 参与者报名（含年龄、监护、过敏声明）
SESSION_CLEARED = "SESSION_CLEARED"          # 放行评估（通过/限制/取消）
SESSION_FROZEN = "SESSION_FROZEN"            # 事故联动冻结
SESSION_RELEASED = "SESSION_RELEASED"        # 有权人员评估后重新放行
CHECKIN_RECORDED = "CHECKIN_RECORDED"        # 签到（幂等，允许离线补传）
MATERIAL_ISSUED = "MATERIAL_ISSUED"          # 材料领用（幂等）
PRODUCT_REGISTERED = "PRODUCT_REGISTERED"    # 游客成品登记（追溯锚点）
INCIDENT_REPORTED = "INCIDENT_REPORTED"      # 事故上报
BATCH_SUSPENDED = "BATCH_SUSPENDED"          # 批次冻结
BATCH_RELEASED = "BATCH_RELEASED"           # 批次经评估后解冻

EVENT_KINDS = [
    CRAFT_VERSIONED,
    MATERIAL_RECEIVED,
    INSTRUCTOR_REGISTERED,
    VENUE_REGISTERED,
    SESSION_SCHEDULED,
    SESSION_AMENDED,
    ENROLLMENT_ADDED,
    SESSION_CLEARED,
    SESSION_FROZEN,
    SESSION_RELEASED,
    CHECKIN_RECORDED,
    MATERIAL_ISSUED,
    PRODUCT_REGISTERED,
    INCIDENT_REPORTED,
    BATCH_SUSPENDED,
    BATCH_RELEASED,
]

# 早期领域词表保留的字段约定
REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段，返回缺失/非法字段名列表。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems


def canonical_json(data) -> str:
    """稳定序列化，用于放行基线指纹。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EventStore:
    """只追加事件存储，默认 JSONL 持久化；``path=None`` 时仅存内存。"""

    def __init__(self, path: str | Path | None = None, clock: Clock | None = None):
        self._path = Path(path) if path else None
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._events: list[dict] = []
        self._ids: dict[str, dict] = {}
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                self._events.append(event)
                self._ids[event["event_id"]] = event

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def append(
        self,
        kind: str,
        subject_id: str,
        payload: dict,
        *,
        event_id: str | None = None,
        occurred_at: str | None = None,
        actor: str | None = None,
    ) -> dict:
        if kind not in EVENT_KINDS:
            raise ValueError(f"未知事件种类: {kind}")
        event = {
            "event_id": event_id or f"evt-{uuid.uuid4().hex}",
            "kind": kind,
            "occurred_at": occurred_at or to_iso(self._clock.now()),
            "subject_id": subject_id,
            "payload": payload,
        }
        if actor:
            event["actor"] = actor
        with self._lock:
            if event["event_id"] in self._ids:
                from .errors import ConflictError

                raise ConflictError(f"事件已存在: {event['event_id']}")
            self._events.append(event)
            self._ids[event["event_id"]] = event
            if self._path:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    fh.flush()
        return event

    def get(self, event_id: str) -> dict | None:
        return self._ids.get(event_id)

    def stream(self) -> Iterator[dict]:
        with self._lock:
            yield from list(self._events)

    def query(self, predicate: Callable[[dict], bool]) -> list[dict]:
        return [e for e in self.stream() if predicate(e)]

    def __len__(self) -> int:
        return len(self._events)
