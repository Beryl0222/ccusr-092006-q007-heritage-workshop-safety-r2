"""应用服务：把用例翻译成只追加事件，集中处理鉴权与幂等。

写接口一律要求传入 :class:`Principal`。健康信息（过敏声明）只随报名写入事件流，
商户侧查询视图做脱敏，见 ``queries.py``。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Sequence

from .clearance import evaluate_clearance
from .errors import AuthorizationError, ConflictError, DomainError
from .events import (
    ASSIGNMENT_CHANGED,
    BATCH_FROZEN,
    BATCH_RELEASED,
    CHECKIN_RECORDED,
    CRAFT_VERSIONED,
    ENROLLMENT_ADDED,
    INCIDENT_REPORTED,
    INSTRUCTOR_QUALIFIED,
    MATERIAL_ISSUED,
    MATERIAL_RECEIVED,
    PRODUCT_RECORDED,
    SESSION_CANCELLED,
    SESSION_CLEARED,
    SESSION_SCHEDULED,
    VENUE_REGISTERED,
    Event,
)
from .models import (
    ROLE_INSTRUCTOR,
    ROLE_OFFICER,
    ROLE_OPERATOR,
)
from .repository import Repository


class Principal:
    def __init__(self, user_id: str, role: str):
        if role not in {ROLE_OPERATOR, ROLE_OFFICER, ROLE_INSTRUCTOR}:
            raise DomainError(f"未知角色: {role}")
        self.user_id = user_id
        self.role = role

    def require(self, *roles: str) -> None:
        if self.role not in roles:
            raise AuthorizationError(f"角色 {self.role} 无权执行此操作，需要 {roles}")


class SafetyService:
    def __init__(self, repo: Repository, clock=None):
        self.repo = repo
        self.clock = clock

    # --------------------------------------------------------------- 基础设施

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now().astimezone()

    def _emit(
        self,
        principal: Principal,
        kind: str,
        subject_id: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
        source: str = "online",
        dedup_key: tuple | None = None,
    ) -> Event:
        return self.repo.append(
            Event(
                event_id=event_id or uuid.uuid4().hex,
                kind=kind,
                occurred_at=occurred_at or self._now(),
                subject_id=subject_id,
                payload=payload,
                actor=principal.user_id,
                role=principal.role,
                source=source,
            ),
            dedup_key=dedup_key,
        )

    # ------------------------------------------------------------- 工坊登记

    def register_craft_version(
        self,
        principal: Principal,
        *,
        craft_id: str,
        version: str,
        title: str,
        age_min: int,
        age_max: int,
        key_steps: Sequence[dict[str, Any]] = (),
        required_facilities: Sequence[str] = (),
    ) -> Event:
        principal.require(ROLE_OPERATOR)
        if (craft_id, version) in self.repo.crafts:
            raise ConflictError(f"技艺版本已登记: {craft_id}@{version}")
        if age_min < 0 or age_min > age_max:
            raise DomainError("适龄范围不合法")
        return self._emit(
            principal,
            CRAFT_VERSIONED,
            craft_id,
            {
                "craft_id": craft_id,
                "version": version,
                "title": title,
                "age_min": age_min,
                "age_max": age_max,
                "key_steps": list(key_steps),
                "required_facilities": list(required_facilities),
            },
        )

    def receive_material(
        self,
        principal: Principal,
        *,
        batch_id: str,
        material_name: str,
        supplier: str,
        allergens: Sequence[str] = (),
    ) -> Event:
        principal.require(ROLE_OPERATOR)
        if batch_id in self.repo.batches:
            raise ConflictError(f"材料批次已入库: {batch_id}")
        return self._emit(
            principal,
            MATERIAL_RECEIVED,
            batch_id,
            {
                "batch_id": batch_id,
                "material_name": material_name,
                "supplier": supplier,
                "allergens": sorted(allergens),
            },
        )

    def register_instructor(
        self,
        principal: Principal,
        *,
        instructor_id: str,
        name: str,
        certifications: Sequence[dict[str, str]] = (),
    ) -> Event:
        """资格登记；同一讲师再次登记可续期/增补证书。"""
        principal.require(ROLE_OPERATOR)
        return self._emit(
            principal,
            INSTRUCTOR_QUALIFIED,
            instructor_id,
            {
                "instructor_id": instructor_id,
                "name": name,
                "certifications": list(certifications),
            },
        )

    def register_venue(
        self,
        principal: Principal,
        *,
        venue_id: str,
        name: str,
        station_capacity: int,
        facilities: Sequence[str] = (),
    ) -> Event:
        principal.require(ROLE_OPERATOR)
        if venue_id in self.repo.venues:
            raise ConflictError(f"场地已登记: {venue_id}")
        if station_capacity <= 0:
            raise DomainError("工位容量必须为正数")
        return self._emit(
            principal,
            VENUE_REGISTERED,
            venue_id,
            {
                "venue_id": venue_id,
                "name": name,
                "station_capacity": station_capacity,
                "facilities": sorted(facilities),
            },
        )

    # --------------------------------------------------------------- 场次排期

    def schedule_session(
        self,
        principal: Principal,
        *,
        session_id: str,
        craft_id: str,
        craft_version: str,
        scheduled_start: datetime,
        scheduled_end: datetime,
        venue_id: str,
        instructor_ids: Sequence[str] = (),
        batch_ids: Sequence[str] = (),
    ) -> Event:
        principal.require(ROLE_OPERATOR)
        if session_id in self.repo.sessions:
            raise ConflictError(f"场次已排期: {session_id}")
        if scheduled_end <= scheduled_start:
            raise DomainError("场次结束时间必须晚于开始时间")
        return self._emit(
            principal,
            SESSION_SCHEDULED,
            session_id,
            {
                "session_id": session_id,
                "craft_id": craft_id,
                "craft_version": craft_version,
                "scheduled_start": scheduled_start.isoformat(),
                "scheduled_end": scheduled_end.isoformat(),
                "venue_id": venue_id,
                "instructor_ids": list(instructor_ids),
                "batch_ids": list(batch_ids),
            },
        )

    def add_enrollment(
        self,
        principal: Principal,
        *,
        session_id: str,
        participant_id: str,
        name: str,
        birth_date: date | str,
        allergy_declarations: Sequence[str] = (),
        guardian_confirmed: bool = False,
    ) -> Event:
        """报名。报名变化会抬升场次版本，使既有签批失效，必须重新评估。"""
        principal.require(ROLE_OPERATOR, ROLE_OFFICER)
        session = self.repo.get_session(session_id)
        if participant_id in session.enrollments:
            raise ConflictError(f"参与者已报名: {participant_id}")
        if isinstance(birth_date, date) and not isinstance(birth_date, str):
            birth_date = birth_date.isoformat()
        return self._emit(
            principal,
            ENROLLMENT_ADDED,
            session_id,
            {
                "session_id": session_id,
                "participant_id": participant_id,
                "name": name,
                "birth_date": str(birth_date),
                "allergy_declarations": sorted(allergy_declarations),
                "guardian_confirmed": guardian_confirmed,
            },
        )

    def change_assignment(
        self,
        principal: Principal,
        *,
        session_id: str,
        batch_ids: Sequence[str] | None = None,
        instructor_ids: Sequence[str] | None = None,
        venue_id: str | None = None,
    ) -> Event:
        """更换材料/讲师/场地：相关检查全部重新生效，不能沿用上一场签字。"""
        principal.require(ROLE_OPERATOR)
        session = self.repo.get_session(session_id)
        if session.cancelled:
            raise ConflictError("场次已取消，不能再调整排课")
        if session.started:
            raise ConflictError("场次已开始，不能更换材料/讲师/场地")

        changes: list[str] = []
        payload: dict[str, Any] = {"session_id": session_id}
        if batch_ids is not None and list(batch_ids) != session.batch_ids:
            payload["batch_ids"] = list(batch_ids)
            changes.append("MATERIAL")
        if instructor_ids is not None and list(instructor_ids) != session.instructor_ids:
            payload["instructor_ids"] = list(instructor_ids)
            changes.append("INSTRUCTOR")
        if venue_id is not None and venue_id != session.venue_id:
            payload["venue_id"] = venue_id
            changes.append("VENUE")
        if not changes:
            raise ConflictError("排课没有任何变化")
        payload["changes"] = changes
        return self._emit(principal, ASSIGNMENT_CHANGED, session_id, payload)

    def cancel_session(self, principal: Principal, session_id: str, reason: str) -> Event:
        principal.require(ROLE_OPERATOR, ROLE_OFFICER)
        session = self.repo.get_session(session_id)
        if session.cancelled:
            raise ConflictError("场次已取消")
        if not reason.strip():
            raise DomainError("取消原因不能为空")
        return self._emit(
            principal, SESSION_CANCELLED, session_id, {"session_id": session_id, "reason": reason}
        )

    # --------------------------------------------------------------- 放行签批

    def clear_session(self, principal: Principal, session_id: str) -> Event:
        """按当前快照评估并签批；只有管理人员有权签字。"""
        principal.require(ROLE_OFFICER)
        session = self.repo.get_session(session_id)
        if session.cancelled:
            raise ConflictError("场次已取消，无需放行")

        decision = evaluate_clearance(self.repo, session)
        current = session.current_clearance
        if (
            current is not None
            and current.void_reason is None
            and current.snapshot_revision == session.revision
            and current.decision == decision.decision
            and [r for r in current.reasons] == decision.reason_dicts()
        ):
            raise ConflictError("该场次当前签批仍然有效，禁止重复签字")

        return self._emit(
            principal,
            SESSION_CLEARED,
            session_id,
            {
                "session_id": session_id,
                "decision": decision.decision,
                "reasons": decision.reason_dicts(),
                "snapshot_revision": decision.snapshot_revision,
                "officer_id": principal.user_id,
            },
        )

    # --------------------------------------------------------- 现场（可离线）

    def check_in(
        self,
        principal: Principal,
        *,
        session_id: str,
        participant_id: str,
        at: datetime | None = None,
        event_id: str | None = None,
        source: str = "online",
    ) -> Event:
        """签到。在线/离线同一入口；同场次同人只能记一次。"""
        principal.require(ROLE_INSTRUCTOR, ROLE_OFFICER)
        session = self.repo.get_session(session_id)
        if session.cancelled:
            raise ConflictError("场次已取消，不能签到")
        if participant_id not in session.enrollments:
            raise DomainError("该参与者未报名本批次场次")
        if session.valid_clearance() is None:
            raise ConflictError("场次没有有效的放行签批，不能签到开场")
        return self._emit(
            principal,
            CHECKIN_RECORDED,
            session_id,
            {"session_id": session_id, "participant_id": participant_id, "at": (at or self._now()).isoformat()},
            event_id=event_id,
            occurred_at=at,
            source=source,
            dedup_key=("CHECKIN", session_id, participant_id),
        )

    def issue_material(
        self,
        principal: Principal,
        *,
        session_id: str,
        batch_id: str,
        participant_id: str,
        at: datetime | None = None,
        event_id: str | None = None,
        source: str = "online",
    ) -> Event:
        """材料领用。同场次、同批次、同人只能记一次；批次冻结时拒绝。"""
        principal.require(ROLE_INSTRUCTOR, ROLE_OFFICER)
        session = self.repo.get_session(session_id)
        if session.cancelled:
            raise ConflictError("场次已取消，不能领用材料")
        if participant_id not in session.enrollments:
            raise DomainError("该参与者未报名本批次场次")
        if batch_id not in session.batch_ids:
            raise ConflictError("该批次未配给本批次场次")
        batch = self.repo.get_batch(batch_id)
        if batch.status == "FROZEN":
            raise ConflictError(f"材料批次 {batch_id} 已冻结，停止领用")
        return self._emit(
            principal,
            MATERIAL_ISSUED,
            session_id,
            {
                "session_id": session_id,
                "batch_id": batch_id,
                "participant_id": participant_id,
                "at": (at or self._now()).isoformat(),
            },
            event_id=event_id,
            occurred_at=at,
            source=source,
            dedup_key=("ISSUE", session_id, batch_id, participant_id),
        )

    def record_product(
        self,
        principal: Principal,
        *,
        product_id: str,
        session_id: str,
        participant_id: str,
    ) -> Event:
        """登记游客带走的成品，固化当时场次、指导者与材料批次，作为追溯锚点。"""
        principal.require(ROLE_INSTRUCTOR, ROLE_OFFICER)
        session = self.repo.get_session(session_id)
        if participant_id not in session.enrollments:
            raise DomainError("该参与者未报名本批次场次")
        if product_id in self.repo.products:
            raise ConflictError(f"成品已登记: {product_id}")
        participant_issues = [i for i in session.issues if i["participant_id"] == participant_id]
        if not participant_issues:
            raise ConflictError("该参与者尚无材料领用记录，无法登记成品追溯链")
        batches = [
            {"batch_id": b, "material_name": self.repo.get_batch(b).material_name}
            for b in dict.fromkeys(i["batch_id"] for i in participant_issues)
        ]
        return self._emit(
            principal,
            PRODUCT_RECORDED,
            product_id,
            {
                "product_id": product_id,
                "session_id": session_id,
                "participant_id": participant_id,
                "craft_id": session.craft_id,
                "craft_version": session.craft_version,
                "instructor_ids": list(session.instructor_ids),
                "batches": batches,
            },
        )

    # ------------------------------------------------------------- 事故处置

    def report_incident(
        self,
        principal: Principal,
        *,
        incident_id: str,
        session_id: str,
        batch_id: str,
        occurred_at: datetime,
        description: str,
    ) -> list[Event]:
        """事故上报：先冻结同批次所有未开始场次的签批，批次本身立即停用。"""
        principal.require(ROLE_INSTRUCTOR, ROLE_OFFICER)
        session = self.repo.get_session(session_id)
        if batch_id not in session.batch_ids:
            raise ConflictError("事故批次与场次配给不符")
        if not description.strip():
            raise DomainError("事故描述不能为空")
        if any(i.incident_id == incident_id for i in self.repo.incidents):
            raise ConflictError(f"事故已登记: {incident_id}")

        events = [
            self._emit(
                principal,
                INCIDENT_REPORTED,
                incident_id,
                {
                    "incident_id": incident_id,
                    "session_id": session_id,
                    "batch_id": batch_id,
                    "occurred_at": occurred_at.isoformat(),
                    "description": description,
                },
            )
        ]

        affected = [
            s.session_id
            for s in self.repo.sessions_using_batch(batch_id)
            if not s.started and not s.cancelled
        ]
        events.append(
            self._emit(
                principal,
                BATCH_FROZEN,
                batch_id,
                {
                    "batch_id": batch_id,
                    "reason": f"事故 {incident_id} 触发冻结",
                    "incident_id": incident_id,
                    "affected_sessions": affected,
                },
            )
        )
        return events

    def release_batch(
        self, principal: Principal, *, batch_id: str, assessment: str
    ) -> Event:
        """有权人员评估后解冻批次；相关场次仍须重新评估签批才能开。"""
        principal.require(ROLE_OFFICER)
        batch = self.repo.get_batch(batch_id)
        if batch.status != "FROZEN":
            raise ConflictError(f"批次 {batch_id} 未处于冻结状态")
        if not assessment.strip():
            raise DomainError("恢复评估意见不能为空")
        return self._emit(
            principal,
            BATCH_RELEASED,
            batch_id,
            {"batch_id": batch_id, "assessment": assessment, "officer_id": principal.user_id},
        )
