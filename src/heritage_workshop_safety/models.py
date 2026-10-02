"""领域模型：技艺版本、材料批次、讲师、场地、场次、成品、事故。

这些 dataclass 是事件流的投影状态，本身不做鉴权；规则见 ``clearance``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# 角色
ROLE_OPERATOR = "operator"    # 工坊商户：登记与排期，健康信息仅可见聚合
ROLE_OFFICER = "officer"      # 运营联合体安全管理人员：放行签批、冻结/恢复、追溯
ROLE_INSTRUCTOR = "instructor"  # 讲师：签到、材料领用、现场必要的过敏提醒

# 危险源 → 该危险源要求的讲师资格证
HAZARD_CERT = {
    "CUTTING_TOOL": "CUTTING_TOOL_SUPERVISION",  # 刀具工位
    "HEAT_SOURCE": "HEAT_SOURCE_SUPERVISION",
}

# 放行决定
DECISION_CLEARED = "CLEARED"        # 可开
DECISION_RESTRICTED = "RESTRICTED"  # 不予开（限制放行）

# 批次状态
BATCH_ACTIVE = "ACTIVE"
BATCH_FROZEN = "FROZEN"


@dataclass(frozen=True)
class KeyStep:
    seq: int
    name: str
    hazards: tuple[str, ...] = ()


@dataclass
class CraftVersion:
    craft_id: str
    version: str
    title: str
    age_min: int
    age_max: int
    key_steps: list[KeyStep] = field(default_factory=list)
    required_facilities: tuple[str, ...] = ()
    required_certs: tuple[str, ...] = ()
    registered_at: datetime | None = None


@dataclass(frozen=True)
class Certification:
    code: str
    valid_from: date
    valid_to: date

    def valid_at(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to


@dataclass
class Instructor:
    instructor_id: str
    name: str
    certifications: dict[str, Certification] = field(default_factory=dict)


@dataclass
class Venue:
    venue_id: str
    name: str
    station_capacity: int
    facilities: frozenset[str] = frozenset()


@dataclass
class MaterialBatch:
    batch_id: str
    material_name: str
    supplier: str
    allergens: frozenset[str] = frozenset()
    received_at: datetime | None = None
    status: str = BATCH_ACTIVE
    freeze_notes: list[str] = field(default_factory=list)


@dataclass
class Participant:
    participant_id: str
    name: str
    birth_date: date
    allergy_declarations: frozenset[str] = frozenset()  # 健康信息：受限字段
    guardian_confirmed: bool = False

    def age_at(self, day: date) -> int:
        age = day.year - self.birth_date.year
        if (day.month, day.day) < (self.birth_date.month, self.birth_date.day):
            age -= 1
        return age


@dataclass(frozen=True)
class ClearanceRecord:
    """一次放行评估签批；snapshot_revision 为签批时的排课版本。"""

    at: datetime
    decision: str
    reasons: tuple[dict[str, Any], ...]
    snapshot_revision: int
    officer_id: str
    event_seq: int = -1
    void_reason: str | None = None


@dataclass
class Session:
    session_id: str
    craft_id: str
    craft_version: str
    scheduled_start: datetime
    scheduled_end: datetime
    venue_id: str
    instructor_ids: list[str] = field(default_factory=list)
    batch_ids: list[str] = field(default_factory=list)
    revision: int = 1
    enrollments: dict[str, Participant] = field(default_factory=dict)
    clearance_history: list[ClearanceRecord] = field(default_factory=list)
    checkins: dict[str, datetime] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    cancelled_reason: str | None = None
    created_at: datetime | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancelled_reason is not None

    @property
    def started(self) -> bool:
        return bool(self.checkins)

    @property
    def current_clearance(self) -> ClearanceRecord | None:
        return self.clearance_history[-1] if self.clearance_history else None

    def valid_clearance(self) -> ClearanceRecord | None:
        rec = self.current_clearance
        if rec is None or rec.void_reason is not None:
            return None
        if rec.snapshot_revision != self.revision:
            return None
        return rec if rec.decision == DECISION_CLEARED else None


@dataclass
class Product:
    product_id: str
    session_id: str
    participant_id: str
    recorded_at: datetime
    craft_id: str
    craft_version: str
    instructor_ids: tuple[str, ...]
    batches: tuple[dict[str, str], ...]  # [{batch_id, material_name}]


@dataclass
class Incident:
    incident_id: str
    session_id: str
    batch_id: str
    occurred_at: datetime
    reported_at: datetime
    reporter_id: str
    description: str
