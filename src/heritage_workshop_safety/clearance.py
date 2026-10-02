"""放行评审：根据场次当前快照评估是否可开。

评审是纯函数，不落字；签批由应用服务记录并附快照版本号。
更换材料/讲师/场地或报名变化会抬升 revision，旧签批的快照版本随即对不上而失效。

不通过原因码：
  AGE_OUT_OF_RANGE    参与者年龄超出技艺适龄范围
  GUARDIAN_UNCONFIRMED 未成年参与者缺少监护确认
  ALLERGY_CONFLICT    参与者过敏声明命中某批次香粉/染料的过敏原
  CAPACITY_EXCEEDED   报名人数超过工位容量
  FACILITY_MISSING    场地缺少该技艺要求的应急设施
  CERT_MISSING        关键步骤含危险源（如刀具），但无持有效资格的讲师看护
  BATCH_FROZEN        所配材料批次处于事故冻结中
  REFERENCE_INVALID   技艺/批次/讲师/场地引用缺失
  SESSION_CANCELLED   场次已取消
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    DECISION_CLEARED,
    DECISION_RESTRICTED,
    HAZARD_CERT,
    MaterialBatch,
    Session,
)
from .repository import Repository

# 未满该年龄视为未成年人，需要监护人确认
MINOR_AGE = 18


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ClearanceDecision:
    decision: str
    reasons: tuple[Reason, ...]
    snapshot_revision: int

    @property
    def allowed(self) -> bool:
        return self.decision == DECISION_CLEARED

    def reason_dicts(self) -> list[dict[str, str]]:
        return [r.as_dict() for r in self.reasons]


def evaluate_clearance(repo: Repository, session: Session) -> ClearanceDecision:
    reasons: list[Reason] = []

    if session.cancelled:
        reasons.append(Reason("SESSION_CANCELLED", f"场次已取消：{session.cancelled_reason}"))
        return _restricted(reasons, session)

    craft = repo.crafts.get((session.craft_id, session.craft_version))
    if craft is None:
        reasons.append(
            Reason("REFERENCE_INVALID", f"技艺版本不存在: {session.craft_id}@{session.craft_version}")
        )
        return _restricted(reasons, session)

    venue = repo.venues.get(session.venue_id)
    if venue is None:
        reasons.append(Reason("REFERENCE_INVALID", f"场地不存在: {session.venue_id}"))

    instructors = [repo.instructors.get(i) for i in session.instructor_ids]
    if any(i is None for i in instructors):
        reasons.append(Reason("REFERENCE_INVALID", "存在未登记的看护讲师"))
    instructors = [i for i in instructors if i is not None]

    batches: list[MaterialBatch] = []
    for bid in session.batch_ids:
        batch = repo.batches.get(bid)
        if batch is None:
            reasons.append(Reason("REFERENCE_INVALID", f"材料批次不存在: {bid}"))
        else:
            batches.append(batch)
    if not session.batch_ids:
        reasons.append(Reason("REFERENCE_INVALID", "未配备任何材料批次"))

    day = session.scheduled_start.date()

    # ---- 逐人检查：年龄、监护、过敏 ----
    for pid, person in session.enrollments.items():
        age = person.age_at(day)
        if not craft.age_min <= age <= craft.age_max:
            reasons.append(
                Reason(
                    "AGE_OUT_OF_RANGE",
                    f"参与者 {pid} 年龄 {age} 超出适龄 {craft.age_min}-{craft.age_max}",
                )
            )
        if age < MINOR_AGE and not person.guardian_confirmed:
            reasons.append(Reason("GUARDIAN_UNCONFIRMED", f"未成年参与者 {pid} 未完成监护确认"))
        for batch in batches:
            hit = person.allergy_declarations & batch.allergens
            if hit:
                reasons.append(
                    Reason(
                        "ALLERGY_CONFLICT",
                        f"参与者 {pid} 对批次 {batch.batch_id} 的 {sorted(hit)} 过敏",
                    )
                )

    # ---- 工位容量 ----
    if venue is not None and len(session.enrollments) > venue.station_capacity:
        reasons.append(
            Reason(
                "CAPACITY_EXCEEDED",
                f"报名 {len(session.enrollments)} 人超过工位容量 {venue.station_capacity}",
            )
        )

    # ---- 应急设施 ----
    if venue is not None:
        missing_facilities = sorted(set(craft.required_facilities) - venue.facilities)
        if missing_facilities:
            reasons.append(
                Reason("FACILITY_MISSING", f"场地 {venue.venue_id} 缺少应急设施: {missing_facilities}")
            )

    # ---- 危险源看护：关键步骤里出现的每种危险源，都需有效资格讲师 ----
    hazards = {hazard for step in craft.key_steps for hazard in step.hazards}
    for hazard in sorted(hazards):
        cert_code = HAZARD_CERT.get(hazard, hazard)
        covered = any(
            (cert := instructor.certifications.get(cert_code)) is not None and cert.valid_at(day)
            for instructor in instructors
        )
        if not covered:
            reasons.append(
                Reason(
                    "CERT_MISSING",
                    f"危险源 {hazard} 需要持 {cert_code} 且在有效期内的看护讲师",
                )
            )

    # ---- 事故冻结批次 ----
    for batch in batches:
        if batch.status == "FROZEN":
            reasons.append(Reason("BATCH_FROZEN", f"材料批次 {batch.batch_id} 已冻结，禁止使用"))

    decision = DECISION_CLEARED if not reasons else DECISION_RESTRICTED
    return ClearanceDecision(decision, tuple(reasons), session.revision)


def _restricted(reasons: list[Reason], session: Session) -> ClearanceDecision:
    return ClearanceDecision(DECISION_RESTRICTED, tuple(reasons), session.revision)
