"""成品追溯：一件成品 -> 场次 -> 指导者 -> 场地 -> 材料来源。

依据事件流重建证据链：成品登记指向场次，场次绑定技艺版本、
讲师、场地与材料批次；材料领用事件提供每参与者实际领取记录。
"""

from __future__ import annotations

from .errors import NotFoundError


def trace_product(service, product_id: str) -> dict:
    """返回成品的完整追溯链。仅应在 can_trace 角色下调用。"""
    proj = service.projection
    product = proj.products.get(product_id)
    if product is None:
        raise NotFoundError(f"成品不存在: {product_id}")

    session = proj.sessions.get(product["session_id"])
    if session is None:
        raise NotFoundError(f"成品关联场次缺失: {product['session_id']}")

    craft = proj.crafts.get(session.craft_ref)
    instructor = proj.instructors.get(session.instructor_id)
    venue = proj.venues.get(session.venue_id)

    issues = []
    for (sid, pid, bid), event in sorted(
        proj.material_issues.items(), key=lambda kv: kv[1]["occurred_at"]
    ):
        if sid != session.session_id:
            continue
        if product.get("participant_id") and pid != product["participant_id"]:
            continue
        issues.append(
            {
                "participant_id": pid,
                "batch_id": bid,
                "quantity": event["payload"].get("quantity", 1),
                "issued_at": event["payload"].get("client_occurred_at")
                or event["occurred_at"],
                "issue_event_id": event["event_id"],
                "offline_backfill": "client_occurred_at" in event["payload"],
            }
        )

    batches = []
    for bid in product.get("batch_ids", session.batch_ids):
        batch = proj.batches.get(bid)
        if batch is None:
            batches.append({"batch_id": bid, "missing": True})
            continue
        batches.append(
            {
                "batch_id": batch.batch_id,
                "material_code": batch.material_code,
                "name": batch.name,
                "supplier": batch.supplier,
                "allergens": list(batch.allergens),
                "received_at": batch.received_at,
                "expires_at": batch.expires_at,
                "status": batch.status,
                "suspended_reason": batch.suspended_reason,
            }
        )

    latest = session.latest_clearance
    return {
        "product_id": product_id,
        "product_name": product.get("name", ""),
        "participant_id": product.get("participant_id", ""),
        "craft_ref": session.craft_ref,
        "craft_name": craft.name if craft else None,
        "key_steps": [s.to_payload() for s in craft.key_steps] if craft else [],
        "session": {
            "session_id": session.session_id,
            "scheduled_start": session.scheduled_start,
            "scheduled_end": session.scheduled_end,
            "status": session.status.value,
            "venue_id": session.venue_id,
            "instructor_id": session.instructor_id,
        },
        "instructor": instructor.to_payload() if instructor else None,
        "venue": venue.to_payload() if venue else None,
        "material_batches": batches,
        "material_issues": issues,
        "last_clearance": latest.to_payload() if latest else None,
        "frozen_history": [
            {
                "reason": session.frozen_reason,
                "at": session.frozen_at,
            }
        ] if session.frozen_at else [],
        "release": {
            "released_by": session.released_by,
            "released_at": session.released_at,
            "note": session.release_note,
        } if session.released_at else None,
    }
