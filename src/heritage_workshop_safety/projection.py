"""事件回放投影：把事件流重建成当前世界状态。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import (
    BATCH_RELEASED,
    BATCH_SUSPENDED,
    CHECKIN_RECORDED,
    CRAFT_VERSIONED,
    ENROLLMENT_ADDED,
    INCIDENT_REPORTED,
    INSTRUCTOR_REGISTERED,
    MATERIAL_ISSUED,
    MATERIAL_RECEIVED,
    PRODUCT_REGISTERED,
    SESSION_AMENDED,
    SESSION_CLEARED,
    SESSION_FROZEN,
    SESSION_RELEASED,
    SESSION_SCHEDULED,
    VENUE_REGISTERED,
)
from .models import (
    ClearanceRecord,
    CraftVersion,
    Enrollment,
    Instructor,
    MaterialBatch,
    Participant,
    Session,
    SessionStatus,
    Venue,
)


@dataclass
class Incident:
    incident_id: str
    session_id: str
    description: str
    severity: str
    batch_ids: list[str]
    reported_at: str
    reported_by: str


@dataclass
class Projection:
    crafts: dict[str, CraftVersion] = field(default_factory=dict)
    batches: dict[str, MaterialBatch] = field(default_factory=dict)
    instructors: dict[str, Instructor] = field(default_factory=dict)
    venues: dict[str, Venue] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    enrollments: dict[str, list[Enrollment]] = field(default_factory=dict)
    checkins: dict[tuple[str, str], dict] = field(default_factory=dict)
    material_issues: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    products: dict[str, dict] = field(default_factory=dict)
    incidents: list[Incident] = field(default_factory=list)
    # client_token -> 首个使用它的事件（离线补传幂等）
    client_tokens: dict[str, dict] = field(default_factory=dict)

    def participants_of(self, session_id: str) -> list[Participant]:
        return [e.participant for e in self.enrollments.get(session_id, [])]

    def participant(self, session_id: str, participant_id: str) -> Participant | None:
        for enrol in self.enrollments.get(session_id, []):
            if enrol.participant.participant_id == participant_id:
                return enrol.participant
        return None

    def session_has_checkins(self, session_id: str) -> bool:
        return any(sid == session_id for sid, _ in self.checkins)

    def issue_count_for(self, session_id: str, batch_id: str) -> int:
        return sum(
            1
            for sid, _, bid in self.material_issues
            if sid == session_id and bid == batch_id
        )

    @classmethod
    def from_events(cls, events) -> "Projection":
        p = cls()
        for event in events:
            p.apply(event)
        return p

    def apply(self, event: dict) -> None:
        kind = event["kind"]
        payload = event.get("payload", {})
        occurred_at = event["occurred_at"]
        actor = event.get("actor", "")

        token = payload.get("client_token")
        if token and token not in self.client_tokens:
            self.client_tokens[token] = event

        if kind == CRAFT_VERSIONED:
            craft = CraftVersion.from_payload(payload)
            if not craft.registered_at:
                craft.registered_at = occurred_at
                craft.registered_by = actor
            self.crafts[craft.ref] = craft

        elif kind == MATERIAL_RECEIVED:
            batch = MaterialBatch.from_payload(payload)
            self.batches[batch.batch_id] = batch

        elif kind == BATCH_SUSPENDED:
            batch = self.batches.get(payload["batch_id"])
            if batch:
                batch.status = "suspended"
                batch.suspended_reason = payload.get("reason", "")
                batch.suspended_at = payload.get("at", occurred_at)

        elif kind == BATCH_RELEASED:
            batch = self.batches.get(payload["batch_id"])
            if batch:
                batch.status = "released"
                batch.suspended_reason = ""

        elif kind == INSTRUCTOR_REGISTERED:
            instructor = Instructor.from_payload(payload)
            if not instructor.registered_at:
                instructor.registered_at = occurred_at
            self.instructors[instructor.instructor_id] = instructor

        elif kind == VENUE_REGISTERED:
            venue = Venue.from_payload(payload)
            if not venue.registered_at:
                venue.registered_at = occurred_at
            self.venues[venue.venue_id] = venue

        elif kind == SESSION_SCHEDULED:
            session = Session(
                session_id=payload["session_id"],
                craft_code=payload["craft_code"],
                version=payload["version"],
                venue_id=payload["venue_id"],
                instructor_id=payload["instructor_id"],
                batch_ids=list(payload["batch_ids"]),
                scheduled_start=payload["scheduled_start"],
                scheduled_end=payload["scheduled_end"],
                created_at=occurred_at,
            )
            self.sessions[session.session_id] = session
            self.enrollments.setdefault(session.session_id, [])

        elif kind == SESSION_AMENDED:
            session = self.sessions.get(payload["session_id"])
            if not session:
                return
            changes = payload.get("changes", {})
            if "venue_id" in changes:
                session.venue_id = changes["venue_id"]["to"]
            if "instructor_id" in changes:
                session.instructor_id = changes["instructor_id"]["to"]
            if "batch_ids" in changes:
                session.batch_ids = list(changes["batch_ids"]["to"])
            if "version" in changes:
                session.version = changes["version"]["to"]
            # 换材料/讲师/场地：上一轮签字立即失效，场次回到待放行
            session.status = SessionStatus.SCHEDULED
            session.frozen_reason = ""
            session.frozen_at = ""

        elif kind == ENROLLMENT_ADDED:
            participant = Participant.from_payload(payload["participant"])
            self.enrollments.setdefault(payload["session_id"], []).append(
                Enrollment(
                    session_id=payload["session_id"],
                    participant=participant,
                    enrolled_at=occurred_at,
                )
            )

        elif kind == SESSION_CLEARED:
            session = self.sessions.get(payload["session_id"])
            if not session:
                return
            record = ClearanceRecord(
                outcome=payload["outcome"],
                findings=payload.get("findings", []),
                baseline=payload["baseline"],
                signed_by=payload.get("signed_by", actor),
                signed_at=payload.get("signed_at", occurred_at),
                event_id=event["event_id"],
                eligible_participant_ids=list(payload.get("eligible_participant_ids", [])),
            )
            session.clearances.append(record)
            session.status = SessionStatus(payload["outcome"])

        elif kind == SESSION_FROZEN:
            session = self.sessions.get(payload["session_id"])
            if session:
                session.status = SessionStatus.FROZEN
                session.frozen_reason = payload.get("reason", "")
                session.frozen_at = payload.get("at", occurred_at)

        elif kind == SESSION_RELEASED:
            session = self.sessions.get(payload["session_id"])
            if session:
                session.status = SessionStatus.RELEASED
                session.released_by = payload.get("released_by", actor)
                session.released_at = payload.get("at", occurred_at)
                session.release_note = payload.get("note", "")
                # 重新签字记录由配套的 SESSION_CLEARED 事件产生，这里不重复合成

        elif kind == CHECKIN_RECORDED:
            key = (payload["session_id"], payload["participant_id"])
            self.checkins.setdefault(key, event)

        elif kind == MATERIAL_ISSUED:
            key = (
                payload["session_id"],
                payload["participant_id"],
                payload["batch_id"],
            )
            self.material_issues.setdefault(key, event)

        elif kind == PRODUCT_REGISTERED:
            self.products[payload["product_id"]] = payload

        elif kind == INCIDENT_REPORTED:
            self.incidents.append(
                Incident(
                    incident_id=payload["incident_id"],
                    session_id=payload["session_id"],
                    description=payload.get("description", ""),
                    severity=payload.get("severity", ""),
                    batch_ids=list(payload.get("batch_ids", [])),
                    reported_at=occurred_at,
                    reported_by=actor,
                )
            )
