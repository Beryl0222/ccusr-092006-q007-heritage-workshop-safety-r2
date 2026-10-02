"""领域模型：技艺版本、材料批次、讲师、场地、场次、参与者。

这些 dataclass 是事件回放后的投影；真正的事实来源是事件流，
字段命名与事件 payload 键保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .clock import parse_dt


class SessionStatus(str, Enum):
    SCHEDULED = "scheduled"      # 已排定，尚未放行
    CLEARED = "cleared"          # 放行可开
    RESTRICTED = "restricted"    # 限制放行（如剔除个别不适用参与者）
    DENIED = "denied"            # 评估不通过（当日表现为取消）
    FROZEN = "frozen"            # 事故联动冻结
    RELEASED = "released"        # 评估恢复
    FINISHED = "finished"        # 已结束


# 危险工序 -> 该工序要求场地具备的应急设施 / 讲师证书
HAZARD_REQUIRED_FACILITIES = {
    "sharp_tool": {"first_aid_kit"},
    "heat": {"fire_extinguisher", "first_aid_kit"},
    "dye": {"eye_wash", "first_aid_kit"},
    "electrical": {"fire_extinguisher"},
}


@dataclass(frozen=True)
class KeyStep:
    step_id: str
    name: str
    hazards: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "KeyStep":
        return cls(
            step_id=data["step_id"],
            name=data["name"],
            hazards=tuple(data.get("hazards", [])),
        )

    def to_payload(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "name": self.name, "hazards": list(self.hazards)}


@dataclass
class CraftVersion:
    craft_code: str
    version: str
    name: str
    age_min: int
    age_max: int
    key_steps: list[KeyStep] = field(default_factory=list)
    required_certificates: tuple[str, ...] = ()
    registered_at: str = ""
    registered_by: str = ""

    @property
    def ref(self) -> str:
        return f"{self.craft_code}@{self.version}"

    @property
    def hazards(self) -> set[str]:
        result: set[str] = set()
        for step in self.key_steps:
            result.update(step.hazards)
        return result

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "CraftVersion":
        return cls(
            craft_code=data["craft_code"],
            version=data["version"],
            name=data["name"],
            age_min=int(data["age_min"]),
            age_max=int(data["age_max"]),
            key_steps=[KeyStep.from_payload(s) for s in data.get("key_steps", [])],
            required_certificates=tuple(data.get("required_certificates", [])),
            registered_at=data.get("registered_at", ""),
            registered_by=data.get("registered_by", ""),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "craft_code": self.craft_code,
            "version": self.version,
            "name": self.name,
            "age_min": self.age_min,
            "age_max": self.age_max,
            "key_steps": [s.to_payload() for s in self.key_steps],
            "required_certificates": list(self.required_certificates),
            "registered_at": self.registered_at,
            "registered_by": self.registered_by,
        }


@dataclass
class MaterialBatch:
    batch_id: str
    material_code: str
    name: str
    supplier: str
    allergens: tuple[str, ...]
    received_at: str
    expires_at: str
    status: str = "active"  # active | suspended | released
    suspended_reason: str = ""
    suspended_at: str = ""

    def is_expired(self, now) -> bool:
        return parse_dt(self.expires_at) <= now

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "MaterialBatch":
        return cls(
            batch_id=data["batch_id"],
            material_code=data["material_code"],
            name=data["name"],
            supplier=data.get("supplier", ""),
            allergens=tuple(data.get("allergens", [])),
            received_at=data["received_at"],
            expires_at=data["expires_at"],
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "material_code": self.material_code,
            "name": self.name,
            "supplier": self.supplier,
            "allergens": list(self.allergens),
            "received_at": self.received_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "suspended_reason": self.suspended_reason,
            "suspended_at": self.suspended_at,
        }


@dataclass
class Instructor:
    instructor_id: str
    name: str
    # 证书名 -> 到期时间（ISO 字符串）
    certificates: dict[str, str] = field(default_factory=dict)
    qualified_crafts: tuple[str, ...] = ()
    registered_at: str = ""

    def cert_valid(self, cert: str, now) -> bool:
        expiry = self.certificates.get(cert)
        return bool(expiry) and parse_dt(expiry) > now

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "Instructor":
        return cls(
            instructor_id=data["instructor_id"],
            name=data["name"],
            certificates=dict(data.get("certificates", {})),
            qualified_crafts=tuple(data.get("qualified_crafts", [])),
            registered_at=data.get("registered_at", ""),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "instructor_id": self.instructor_id,
            "name": self.name,
            "certificates": dict(self.certificates),
            "qualified_crafts": list(self.qualified_crafts),
            "registered_at": self.registered_at,
        }


@dataclass
class Venue:
    venue_id: str
    name: str
    station_count: int
    emergency_facilities: tuple[str, ...] = ()
    registered_at: str = ""

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "Venue":
        return cls(
            venue_id=data["venue_id"],
            name=data["name"],
            station_count=int(data["station_count"]),
            emergency_facilities=tuple(data.get("emergency_facilities", [])),
            registered_at=data.get("registered_at", ""),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "venue_id": self.venue_id,
            "name": self.name,
            "station_count": self.station_count,
            "emergency_facilities": list(self.emergency_facilities),
            "registered_at": self.registered_at,
        }


@dataclass(frozen=True)
class Participant:
    """报名参与者。``allergy_declarations`` 属于健康信息，受角色视图保护。"""

    participant_id: str
    age: int
    guardian_confirmed: bool
    allergy_declarations: tuple[str, ...] = ()
    alias: str = ""

    @property
    def is_minor(self) -> bool:
        return self.age < 18

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "Participant":
        return cls(
            participant_id=data["participant_id"],
            age=int(data["age"]),
            guardian_confirmed=bool(data.get("guardian_confirmed", False)),
            allergy_declarations=tuple(data.get("allergy_declarations", [])),
            alias=data.get("alias", ""),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "age": self.age,
            "guardian_confirmed": self.guardian_confirmed,
            "allergy_declarations": list(self.allergy_declarations),
            "alias": self.alias,
        }


@dataclass
class Enrollment:
    session_id: str
    participant: Participant
    enrolled_at: str


@dataclass
class ClearanceRecord:
    """一次放行评估签字及其基线指纹。"""

    outcome: str  # cleared | restricted | denied
    findings: list[dict]
    baseline: str
    signed_by: str
    signed_at: str
    event_id: str
    eligible_participant_ids: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "findings": list(self.findings),
            "baseline": self.baseline,
            "signed_by": self.signed_by,
            "signed_at": self.signed_at,
            "event_id": self.event_id,
            "eligible_participant_ids": list(self.eligible_participant_ids),
        }


@dataclass
class Session:
    session_id: str
    craft_code: str
    version: str
    venue_id: str
    instructor_id: str
    batch_ids: list[str]
    scheduled_start: str
    scheduled_end: str
    status: SessionStatus = SessionStatus.SCHEDULED
    clearances: list[ClearanceRecord] = field(default_factory=list)
    frozen_reason: str = ""
    frozen_at: str = ""
    released_by: str = ""
    released_at: str = ""
    release_note: str = ""
    created_at: str = ""

    @property
    def craft_ref(self) -> str:
        return f"{self.craft_code}@{self.version}"

    @property
    def latest_clearance(self) -> ClearanceRecord | None:
        return self.clearances[-1] if self.clearances else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "craft_code": self.craft_code,
            "version": self.version,
            "venue_id": self.venue_id,
            "instructor_id": self.instructor_id,
            "batch_ids": list(self.batch_ids),
            "scheduled_start": self.scheduled_start,
            "scheduled_end": self.scheduled_end,
            "status": self.status.value,
            "clearances": [c.to_payload() for c in self.clearances],
            "frozen_reason": self.frozen_reason,
            "frozen_at": self.frozen_at,
            "released_by": self.released_by,
            "released_at": self.released_at,
            "release_note": self.release_note,
            "created_at": self.created_at,
        }
