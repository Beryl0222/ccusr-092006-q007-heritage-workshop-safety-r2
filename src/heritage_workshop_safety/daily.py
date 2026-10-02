"""当日安全放行日报。

从事件流统计当天（景区本地时区）：
- 因何被限制（restricted）、取消（denied）、正常放行（cleared）；
- 因事故被冻结、经评估重新放行（released）；
- 因换材料/讲师/场地导致旧签字失效、需重新评估的场次。

日报面向安全管理人员与管理员，包含完整原因代码；面向商户的
脱敏在 HTTP 视图层处理。
"""

from __future__ import annotations

from collections import Counter

from .clock import LOCAL_TZ, parse_dt, to_iso
from .events import (
    SESSION_AMENDED,
    SESSION_CLEARED,
    SESSION_FROZEN,
    SESSION_RELEASED,
)

_OUTCOME_LABELS = {
    "cleared": "正常放行",
    "restricted": "限制放行",
    "denied": "取消（评估未通过）",
}


def _reason_summary(findings: list[dict]) -> dict:
    codes = Counter(f.get("code", "unknown") for f in findings)
    # 场次级原因（非参与者个体）排在前面，便于运营快速定位
    session_codes = Counter(
        f.get("code", "unknown")
        for f in findings
        if f.get("subject_type") != "participant"
    )
    participant_codes = codes - session_codes
    return {
        "codes": dict(codes),
        "session_level": dict(session_codes),
        "participant_level": dict(participant_codes),
        "messages": [f.get("message", "") for f in findings],
    }


def daily_report(service, day: str | None = None) -> dict:
    """``day`` 形如 ``2026-10-02``；缺省为时钟当前本地日期。"""
    clock = service.clock
    if day is None:
        day = clock.now().astimezone(LOCAL_TZ).date().isoformat()

    cleared, restricted, denied, frozen, released, amended = [], [], [], [], [], []

    for event in service.store.stream():
        local_date = parse_dt(event["occurred_at"]).astimezone(LOCAL_TZ).date().isoformat()
        if local_date != day:
            continue
        kind = event["kind"]
        payload = event["payload"]
        sid = payload.get("session_id", "")

        if kind == SESSION_CLEARED:
            findings = payload.get("findings", [])
            entry = {
                "session_id": sid,
                "at": event["occurred_at"],
                "signed_by": payload.get("signed_by", event.get("actor", "")),
                "purpose": payload.get("purpose", "scheduled_clearance"),
                "reasons": _reason_summary(findings),
                "findings": findings,
                "excluded_participant_ids": sorted(
                    {
                        f["subject_id"]
                        for f in findings
                        if f.get("subject_type") == "participant"
                    }
                ),
                "baseline": payload.get("baseline", ""),
            }
            {"cleared": cleared, "restricted": restricted, "denied": denied}[
                payload["outcome"]
            ].append(entry)

        elif kind == SESSION_FROZEN:
            frozen.append(
                {
                    "session_id": sid,
                    "at": payload.get("at", event["occurred_at"]),
                    "reason": payload.get("reason", ""),
                    "incident_id": payload.get("incident_id", ""),
                }
            )

        elif kind == SESSION_RELEASED:
            findings = payload.get("findings", [])
            released.append(
                {
                    "session_id": sid,
                    "at": payload.get("at", event["occurred_at"]),
                    "released_by": payload.get("released_by", event.get("actor", "")),
                    "note": payload.get("note", ""),
                    "reasons": _reason_summary(findings),
                    "findings": findings,
                    "outcome": payload.get("outcome", "cleared"),
                }
            )

        elif kind == SESSION_AMENDED:
            changes = payload.get("changes", {})
            amended.append(
                {
                    "session_id": sid,
                    "at": event["occurred_at"],
                    "changed_fields": sorted(changes.keys()),
                    "detail": changes,
                    "notice": "绑定要素已变更，旧签字失效，须重新评估放行",
                }
            )

    return {
        "date": day,
        "generated_at": to_iso(clock.now()),
        "summary": {
            "cleared": len(cleared),
            "restricted": len(restricted),
            "denied": len(denied),
            "frozen": len(frozen),
            "released": len(released),
            "amended_pending_recheck": len(amended),
        },
        "cleared": cleared,
        "restricted": restricted,
        "denied": denied,
        "frozen": frozen,
        "released": released,
        "amended_pending_recheck": amended,
        "labels": _OUTCOME_LABELS,
    }
