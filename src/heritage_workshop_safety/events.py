"""领域事件种类与最小交换字段。

事件是只追加的事实（event sourcing）。所有状态变更都先落事件，
再由仓储投影成当前状态；离线补传依靠 event_id 与业务去重键保证只记一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 登记类
CRAFT_VERSIONED = "CRAFT_VERSIONED"        # 工坊登记技艺版本与关键步骤
MATERIAL_RECEIVED = "MATERIAL_RECEIVED"    # 材料批次入库
INSTRUCTOR_QUALIFIED = "INSTRUCTOR_QUALIFIED"  # 讲师资格登记/续期
VENUE_REGISTERED = "VENUE_REGISTERED"      # 场地与应急设施登记

# 场次类
SESSION_SCHEDULED = "SESSION_SCHEDULED"    # 场次排期
ENROLLMENT_ADDED = "ENROLLMENT_ADDED"      # 报名（含过敏声明等健康信息，属受限字段）
ASSIGNMENT_CHANGED = "ASSIGNMENT_CHANGED"  # 更换材料/讲师/场地，原签批立即失效
SESSION_CLEARED = "SESSION_CLEARED"        # 放行或限制放行签批（含当时快照）
SESSION_CANCELLED = "SESSION_CANCELLED"    # 场次取消

# 现场类（均支持离线补传，且只能记一次）
CHECKIN_RECORDED = "CHECKIN_RECORDED"
MATERIAL_ISSUED = "MATERIAL_ISSUED"
PRODUCT_RECORDED = "PRODUCT_RECORDED"      # 游客带走的成品登记，追溯锚点

# 事故类
INCIDENT_REPORTED = "INCIDENT_REPORTED"
BATCH_FROZEN = "BATCH_FROZEN"
BATCH_RELEASED = "BATCH_RELEASED"          # 有权人员评估后解冻恢复

EVENT_KINDS = [
    CRAFT_VERSIONED,
    MATERIAL_RECEIVED,
    INSTRUCTOR_QUALIFIED,
    VENUE_REGISTERED,
    SESSION_SCHEDULED,
    ENROLLMENT_ADDED,
    ASSIGNMENT_CHANGED,
    SESSION_CLEARED,
    SESSION_CANCELLED,
    CHECKIN_RECORDED,
    MATERIAL_ISSUED,
    PRODUCT_RECORDED,
    INCIDENT_REPORTED,
    BATCH_FROZEN,
    BATCH_RELEASED,
]

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")


@dataclass(frozen=True)
class Event:
    """一条不可变事件。seq 由存储分配，dedup_key 用于离线补传去重。"""

    event_id: str
    kind: str
    occurred_at: datetime
    subject_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"
    role: str = "system"
    source: str = "online"  # online / offline
    dedup_key: tuple | None = None
    seq: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "subject_id": self.subject_id,
            "payload": self.payload,
            "actor": self.actor,
            "role": self.role,
            "source": self.source,
            "seq": self.seq,
        }


def validate_event(record: dict) -> list[str]:
    """检查事件是否具备可交换的最小字段（保持领域资料约定）。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
