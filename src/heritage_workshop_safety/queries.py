"""查询视图：隐私脱敏的场次视图、成品追溯、当天管控日报。

健康信息保护原则：
  * operator（商户）只能看到聚合风险计数，看不到“谁对什么过敏”；
  * instructor（讲师）只能看到与现场安全直接相关的冲突提醒；
  * officer（管理人员）可看完整明细并执行追溯。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .clearance import evaluate_clearance
from .events import (
    BATCH_RELEASED,
    SESSION_CANCELLED,
    SESSION_CLEARED,
)
from .models import (
    ROLE_INSTRUCTOR,
    ROLE_OFFICER,
    ROLE_OPERATOR,
)
from .repository import Repository
from .service import Principal


def _batch_view(repo: Repository, batch_id: str, *, reveal_allergens: bool) -> dict[str, Any]:
    b = repo.get_batch(batch_id)
    view = {
        "batch_id": b.batch_id,
        "material_name": b.material_name,
        "supplier": b.supplier,
        "status": b.status,
    }
    if reveal_allergens:
        view["allergens"] = sorted(b.allergens)
    return view


# 哪些角色可以看到与健康信息关联的明细
_HEALTH_REVEAL_ROLES = {ROLE_OFFICER}
# 原因码中只有这些的 detail 含个人健康关联信息
_SENSITIVE_REASON_CODES = {"ALLERGY_CONFLICT"}


def _reason_view(code: str, detail: str, reveal: bool) -> dict[str, str]:
    if not reveal and code in _SENSITIVE_REASON_CODES:
        return {"code": code, "detail": "存在参与者过敏声明与配给材料冲突，详情请联系安全管理人员"}
    return {"code": code, "detail": detail}


def session_safety_view(repo: Repository, session_id: str, principal: Principal) -> dict[str, Any]:
    """按角色返回场次视图；商户视图不含任何个人健康明细。"""
    session = repo.get_session(session_id)
    decision = evaluate_clearance(repo, session)
    day = session.scheduled_start.date()
    reveal_health = principal.role in _HEALTH_REVEAL_ROLES

    base: dict[str, Any] = {
        "session_id": session.session_id,
        "craft_id": session.craft_id,
        "craft_version": session.craft_version,
        "scheduled_start": session.scheduled_start.isoformat(),
        "venue_id": session.venue_id,
        "instructor_ids": list(session.instructor_ids),
        "batch_ids": [
            _batch_view(repo, b, reveal_allergens=reveal_health)
            for b in session.batch_ids if b in repo.batches
        ],
        "revision": session.revision,
        "enrolled": len(session.enrollments),
        "cancelled": session.cancelled,
        "cancelled_reason": session.cancelled_reason,
        "decision": decision.decision,
        "reasons": [_reason_view(r.code, r.detail, reveal_health) for r in decision.reasons],
        "valid_clearance": session.valid_clearance() is not None,
    }
    valid = session.valid_clearance()
    base["signed_off"] = bool(valid and valid.decision == DECISION_CLEARED)

    people = list(session.enrollments.values())
    ages = [p.age_at(day) for p in people]
    base["minors"] = sum(1 for a in ages if a < 18)
    base["guardian_confirmed"] = sum(1 for p in people if p.guardian_confirmed)

    if principal.role == ROLE_OPERATOR:
        # 仅聚合信号：几个孩子、几份监护确认、几个过敏冲突，不返回个人对应关系
        allergy_conflicts = sum(1 for r in decision.reasons if r.code == "ALLERGY_CONFLICT")
        base["allergy_conflicts"] = allergy_conflicts
        base["participants_hidden"] = True
        return base

    if principal.role == ROLE_INSTRUCTOR:
        # 现场需要知道“谁与哪批材料冲突”，但不暴露申报词表之外的健康细节
        base["safety_alerts"] = [
            {
                "participant_id": pid,
                "conflicting_batches": [
                    bid
                    for bid in session.batch_ids
                    if bid in repo.batches
                    and person.allergy_declarations & repo.batches[bid].allergens
                ],
            }
            for pid, person in session.enrollments.items()
            if any(
                person.allergy_declarations & repo.batches[bid].allergens
                for bid in session.batch_ids
                if bid in repo.batches
            )
        ]
        base["participants_hidden"] = True
        return base

    # officer：完整明细
    principal.require(ROLE_OFFICER)
    base["participants"] = [
        {
            "participant_id": p.participant_id,
            "name": p.name,
            "age": p.age_at(day),
            "allergy_declarations": sorted(p.allergy_declarations),
            "guardian_confirmed": p.guardian_confirmed,
            "checked_in": p.participant_id in session.checkins,
        }
        for p in people
    ]
    base["clearance_history"] = [
        {
            "at": rec.at.isoformat(),
            "decision": rec.decision,
            "reasons": [dict(r) for r in rec.reasons],
            "snapshot_revision": rec.snapshot_revision,
            "officer_id": rec.officer_id,
            "void_reason": rec.void_reason,
        }
        for rec in session.clearance_history
    ]
    return base


def trace_product(repo: Repository, product_id: str, principal: Principal) -> dict[str, Any]:
    """从一件成品追到：场次、指导者、技艺版本、材料批次与来源供应商。"""
    principal.require(ROLE_OFFICER)
    product = repo.products.get(product_id)
    if product is None:
        from .errors import NotFoundError

        raise NotFoundError(f"成品不存在: {product_id}")
    session = repo.get_session(product.session_id)
    return {
        "product_id": product.product_id,
        "recorded_at": product.recorded_at.isoformat(),
        "participant_id": product.participant_id,
        "session": {
            "session_id": session.session_id,
            "scheduled_start": session.scheduled_start.isoformat(),
            "venue_id": session.venue_id,
        },
        "craft": {"craft_id": product.craft_id, "version": product.craft_version},
        "instructors": [
            {
                "instructor_id": iid,
                "name": (instructor.name if (instructor := repo.instructors.get(iid)) else None),
                "certifications": sorted(instructor.certifications.keys()) if instructor else [],
            }
            for iid in product.instructor_ids
        ],
        "materials": [
            {
                "batch_id": item["batch_id"],
                "material_name": item["material_name"],
                "supplier": repo.batches[item["batch_id"]].supplier
                if item["batch_id"] in repo.batches
                else None,
                "allergens": sorted(repo.batches[item["batch_id"]].allergens)
                if item["batch_id"] in repo.batches
                else [],
            }
            for item in product.batches
        ],
        "issues": [dict(issue) for issue in session.issues if issue["participant_id"] == product.participant_id],
    }


# ----------------------------------------------------------------- 管控日报

def daily_report(repo: Repository, day: date) -> dict[str, Any]:
    """当天活动的限制 / 取消 / 重新放行清单及原因。

    以事件实际发生时间（occurred_at）归日，离线补传按其发生时刻计入当天。
    扫描整条事件流，逐场次维护签批状态机，以还原每个签批发生当时的前情：
    本次 CLEARED 之前，该场出现过 RESTRICTED，或曾有 CLEARED 被换料/换人/换场/
    报名变化/批次冻结作废，即记为“重新放行”。
    """
    restricted: list[dict[str, Any]] = []
    cancelled: list[dict[str, Any]] = []
    released_batches: list[dict[str, Any]] = []
    # 每场次当天的放行签批先缓存，最终按当天最后一次放行归类为“首次放行/重新放行”，
    # 避免把当天后来被作废的旧放行计入 cleared。
    clear_candidates: dict[str, tuple[dict[str, Any], bool]] = {}

    # sid -> {"phase": None|"cleared"|"restricted"|"voided", "ever_blocked": bool,
    #         "block_reason": str}
    state: dict[str, dict[str, Any]] = {}

    def phase_of(sid: str) -> dict[str, Any]:
        return state.setdefault(sid, {"phase": None, "ever_blocked": False, "block_reason": None})

    def invalidate(sid: str, reason: str) -> None:
        st = phase_of(sid)
        if st["phase"] in ("cleared", "restricted"):
            st["phase"] = "voided"
            st["block_reason"] = reason
            st["ever_blocked"] = True

    for event in repo.events:
        p = event.payload
        kind = event.kind

        if kind == "ENROLLMENT_ADDED":
            invalidate(p["session_id"], "ENROLLMENT_CHANGED")
        elif kind == "ASSIGNMENT_CHANGED":
            invalidate(p["session_id"], "ASSIGNMENT_CHANGED:" + ",".join(p.get("changes", ())))
        elif kind == "BATCH_FROZEN":
            for sid in p.get("affected_sessions", ()):
                invalidate(sid, "BATCH_FROZEN")
        elif kind == SESSION_CLEARED:
            sid = p["session_id"]
            st = phase_of(sid)
            entry = {
                "session_id": sid,
                "at": event.occurred_at.isoformat(),
                "officer_id": p["officer_id"],
                "snapshot_revision": p.get("snapshot_revision"),
                "reasons": p.get("reasons", []),
            }
            if p["decision"] == "RESTRICTED":
                st["phase"] = "restricted"
                st["ever_blocked"] = True
                st["block_reason"] = "RESTRICTED"
                if event.occurred_at.date() == day:
                    restricted.append(entry)
            else:
                st["phase"] = "cleared"
                if event.occurred_at.date() == day:
                    clear_candidates[sid] = (entry, st["ever_blocked"], st["block_reason"])
                st["block_reason"] = None
        elif kind == SESSION_CANCELLED and event.occurred_at.date() == day:
            cancelled.append(
                {
                    "session_id": p["session_id"],
                    "at": event.occurred_at.isoformat(),
                    "reason": p["reason"],
                    "actor": event.actor,
                }
            )
        elif kind == BATCH_RELEASED and event.occurred_at.date() == day:
            released_batches.append(
                {
                    "batch_id": p["batch_id"],
                    "at": event.occurred_at.isoformat(),
                    "assessment": p["assessment"],
                    "officer_id": p["officer_id"],
                }
            )

    cleared: list[dict[str, Any]] = []
    recleared: list[dict[str, Any]] = []
    for _sid, (entry, ever_blocked, block_reason) in clear_candidates.items():
        if ever_blocked:
            recleared.append({**entry, "reopened_after": block_reason or "RESTRICTED"})
        else:
            cleared.append(entry)

    return {
        "date": day.isoformat(),
        "restricted": restricted,
        "cancelled": cancelled,
        "recleared": recleared,
        "cleared": cleared,
        "released_batches": released_batches,
        "summary": {
            "restricted": len(restricted),
            "cancelled": len(cancelled),
            "recleared": len(recleared),
            "cleared": len(cleared),
        },
    }
