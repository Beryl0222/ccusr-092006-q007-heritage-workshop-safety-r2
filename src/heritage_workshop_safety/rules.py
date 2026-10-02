"""安全放行规则引擎。

引擎只读投影、产出决策，不落库；签字由服务层附带基线指纹落库。
基线指纹覆盖技艺版本、材料批次（含状态）、讲师（含证书）、
场地（含应急设施）与全部参与者声明——其中任一变化都会改变指纹，
旧签字无法沿用到新一场。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .events import canonical_json
from .models import HAZARD_REQUIRED_FACILITIES, Session, SessionStatus

BLOCK = "block"
WARN = "warn"

DENIED = SessionStatus.DENIED.value
RESTRICTED = SessionStatus.RESTRICTED.value
CLEARED = SessionStatus.CLEARED.value


def finding(code: str, severity: str, message: str, **subject) -> dict:
    item = {"code": code, "severity": severity, "message": message}
    item.update(subject)
    return item


@dataclass
class Decision:
    session_id: str
    outcome: str = ""  # cleared | restricted | denied
    findings: list[dict] = field(default_factory=list)
    eligible_participant_ids: list[str] = field(default_factory=list)
    excluded: dict[str, list[dict]] = field(default_factory=dict)
    baseline: str = ""

    @property
    def blocking_findings(self) -> list[dict]:
        return [f for f in self.findings if f["severity"] == BLOCK]

    def reason_codes(self) -> list[str]:
        return [f["code"] for f in self.findings]

    def to_payload(self) -> dict:
        return {
            "session_id": self.session_id,
            "outcome": self.outcome,
            "findings": self.findings,
            "eligible_participant_ids": self.eligible_participant_ids,
            "excluded": self.excluded,
            "baseline": self.baseline,
        }


def compute_baseline(projection, session: Session) -> str:
    craft = projection.crafts.get(session.craft_ref)
    batches = [
        projection.batches[bid].to_payload()
        for bid in session.batch_ids
        if bid in projection.batches
    ]
    instructor = projection.instructors.get(session.instructor_id)
    venue = projection.venues.get(session.venue_id)
    participants = sorted(
        (p.to_payload() for p in projection.participants_of(session.session_id)),
        key=lambda x: x["participant_id"],
    )
    material = {
        "craft_ref": session.craft_ref,
        "craft": craft.to_payload() if craft else None,
        "batch_ids": list(session.batch_ids),
        "batches": sorted(batches, key=lambda b: b["batch_id"]),
        "instructor_id": session.instructor_id,
        "instructor": instructor.to_payload() if instructor else None,
        "venue_id": session.venue_id,
        "venue": venue.to_payload() if venue else None,
        "participants": participants,
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def evaluate(projection, session: Session, now) -> Decision:
    """对场次执行全部放行检查。"""
    result = Decision(session_id=session.session_id)
    findings: list[dict] = []

    # ---- 技艺版本 -----------------------------------------------------
    craft = projection.crafts.get(session.craft_ref)
    if craft is None:
        findings.append(
            finding(
                "craft_not_found",
                BLOCK,
                f"技艺版本 {session.craft_ref} 未登记",
                subject_type="craft",
                subject_id=session.craft_ref,
            )
        )

    # ---- 材料批次：存在、未冻结、未过期，汇总过敏原 --------------------
    all_allergens: set[str] = set()
    for bid in session.batch_ids:
        batch = projection.batches.get(bid)
        if batch is None:
            findings.append(
                finding(
                    "batch_not_found",
                    BLOCK,
                    f"材料批次 {bid} 不存在",
                    subject_type="batch",
                    subject_id=bid,
                )
            )
            continue
        if batch.status == "suspended":
            findings.append(
                finding(
                    "batch_suspended",
                    BLOCK,
                    f"材料批次 {batch.name}({bid}) 已冻结：{batch.suspended_reason}",
                    subject_type="batch",
                    subject_id=bid,
                )
            )
        if batch.is_expired(now):
            findings.append(
                finding(
                    "batch_expired",
                    BLOCK,
                    f"材料批次 {batch.name}({bid}) 已过保质期 {batch.expires_at}",
                    subject_type="batch",
                    subject_id=bid,
                )
            )
        all_allergens.update(batch.allergens)

    # ---- 讲师：资格与证书有效期 ---------------------------------------
    instructor = projection.instructors.get(session.instructor_id)
    if instructor is None:
        findings.append(
            finding(
                "instructor_not_found",
                BLOCK,
                f"讲师 {session.instructor_id} 未登记",
                subject_type="instructor",
                subject_id=session.instructor_id,
            )
        )
    elif craft is not None:
        if session.craft_code not in instructor.qualified_crafts:
            findings.append(
                finding(
                    "instructor_not_qualified",
                    BLOCK,
                    f"讲师 {instructor.name} 不具备 {craft.name} 的执教资格",
                    subject_type="instructor",
                    subject_id=instructor.instructor_id,
                )
            )
        for cert in craft.required_certificates:
            if not instructor.cert_valid(cert, now):
                expiry = instructor.certificates.get(cert)
                detail = f"，证书 {cert} 已于 {expiry} 到期" if expiry else f"，缺少证书 {cert}"
                findings.append(
                    finding(
                        "certificate_expired" if expiry else "certificate_missing",
                        BLOCK,
                        f"讲师 {instructor.name}{detail}",
                        subject_type="instructor",
                        subject_id=instructor.instructor_id,
                    )
                )

    # ---- 场地：存在、工位容量、应急设施覆盖危险工序 --------------------
    venue = projection.venues.get(session.venue_id)
    participants = projection.participants_of(session.session_id)
    if venue is None:
        findings.append(
            finding(
                "venue_not_found",
                BLOCK,
                f"场地 {session.venue_id} 未登记",
                subject_type="venue",
                subject_id=session.venue_id,
            )
        )
    else:
        headcount = len(participants)
        if headcount > venue.station_count:
            findings.append(
                finding(
                    "capacity_exceeded",
                    BLOCK,
                    f"报名 {headcount} 人超过工位容量 {venue.station_count}",
                    subject_type="venue",
                    subject_id=venue.venue_id,
                )
            )
        if craft is not None:
            required: set[str] = set()
            for hazard in craft.hazards:
                required.update(HAZARD_REQUIRED_FACILITIES.get(hazard, set()))
            missing = required - set(venue.emergency_facilities)
            for facility in sorted(missing):
                findings.append(
                    finding(
                        "emergency_facility_missing",
                        BLOCK,
                        f"场地 {venue.name} 缺少应急设施：{facility}",
                        subject_type="venue",
                        subject_id=venue.venue_id,
                    )
                )

    # ---- 参与者：适龄、监护确认、过敏冲突 ------------------------------
    eligible: list[str] = []
    for p in participants:
        problems: list[dict] = []
        if craft is not None and not (craft.age_min <= p.age <= craft.age_max):
            problems.append(
                finding(
                    "age_out_of_range",
                    BLOCK,
                    f"{p.age} 岁不在适龄范围 {craft.age_min}-{craft.age_max}",
                    subject_type="participant",
                    subject_id=p.participant_id,
                )
            )
        if p.is_minor and not p.guardian_confirmed:
            problems.append(
                finding(
                    "guardian_unconfirmed",
                    BLOCK,
                    "未成年人缺少监护确认",
                    subject_type="participant",
                    subject_id=p.participant_id,
                )
            )
        conflicts = all_allergens.intersection(p.allergy_declarations)
        if conflicts:
            problems.append(
                finding(
                    "allergy_conflict",
                    BLOCK,
                    f"过敏声明与材料成分冲突：{sorted(conflicts)}",
                    subject_type="participant",
                    subject_id=p.participant_id,
                )
            )
        if problems:
            result.excluded[p.participant_id] = problems
            findings.extend(problems)
        else:
            eligible.append(p.participant_id)

    result.findings = findings
    result.baseline = compute_baseline(projection, session)

    # ---- 汇总结论 ------------------------------------------------------
    blocking = [f for f in findings if f["severity"] == BLOCK]
    blocking_sessions = [f for f in blocking if f.get("subject_type") != "participant"]
    if blocking_sessions:
        result.outcome = DENIED
    elif result.excluded:
        if not eligible:
            result.outcome = DENIED
        else:
            result.outcome = RESTRICTED
    else:
        result.outcome = CLEARED
    result.eligible_participant_ids = sorted(eligible)
    return result


def signature_current(projection, session: Session) -> bool:
    """最近一次签字的基线是否仍与当前事实一致。"""
    latest = session.latest_clearance
    if latest is None or latest.outcome not in (CLEARED, RESTRICTED, "released"):
        return False
    return latest.baseline == compute_baseline(projection, session)
