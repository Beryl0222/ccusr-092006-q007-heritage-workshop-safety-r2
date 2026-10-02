"""事件存储与投影仓储。

所有写操作走只追加事件；投影把事件流还原成当前状态。
签到/领用的幂等等级：
  * event_id 全局唯一（客户端补传同一条不会记两次）；
  * dedup_key 业务唯一（同一人同场次重复签到、同一批次重复领用都拒绝）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .errors import ConflictError, DomainError, NotFoundError
from .events import (
    ASSIGNMENT_CHANGED,
    BATCH_FROZEN,
    BATCH_RELEASED,
    CHECKIN_RECORDED,
    CRAFT_VERSIONED,
    ENROLLMENT_ADDED,
    EVENT_KINDS,
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
    BATCH_ACTIVE,
    BATCH_FROZEN as BATCH_STATUS_FROZEN,
    Certification,
    ClearanceRecord,
    CraftVersion,
    Incident,
    Instructor,
    KeyStep,
    MaterialBatch,
    Participant,
    Product,
    Session,
    Venue,
)


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _parse_day(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


class Repository:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._event_ids: set[str] = set()
        self._dedup: set[tuple] = set()

        self.crafts: dict[tuple[str, str], CraftVersion] = {}
        self.instructors: dict[str, Instructor] = {}
        self.venues: dict[str, Venue] = {}
        self.batches: dict[str, MaterialBatch] = {}
        self.sessions: dict[str, Session] = {}
        self.products: dict[str, Product] = {}
        self.incidents: list[Incident] = []

    # ------------------------------------------------------------------ 写入

    def append(self, event: Event, dedup_key: tuple | None = None) -> Event:
        if event.kind not in EVENT_KINDS:
            raise DomainError(f"未知事件种类: {event.kind}")
        if event.event_id in self._event_ids:
            raise ConflictError(f"事件已存在，禁止重复记账: {event.event_id}")
        if dedup_key is not None and dedup_key in self._dedup:
            raise ConflictError(f"业务记录已存在，只能记一次: {dedup_key}")
        event = replace(event, seq=len(self._events), dedup_key=dedup_key)
        self._events.append(event)
        self._event_ids.add(event.event_id)
        if dedup_key is not None:
            self._dedup.add(dedup_key)
        self._apply(event)
        return event

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    # -------------------------------------------------------------- 查询辅助

    def get_session(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            raise NotFoundError(f"场次不存在: {session_id}") from exc

    def get_batch(self, batch_id: str) -> MaterialBatch:
        try:
            return self.batches[batch_id]
        except KeyError as exc:
            raise NotFoundError(f"材料批次不存在: {batch_id}") from exc

    def get_craft(self, craft_id: str, version: str) -> CraftVersion:
        try:
            return self.crafts[(craft_id, version)]
        except KeyError as exc:
            raise NotFoundError(f"技艺版本不存在: {craft_id}@{version}") from exc

    def sessions_using_batch(self, batch_id: str) -> list[Session]:
        return [s for s in self.sessions.values() if batch_id in s.batch_ids]

    # ------------------------------------------------------------------ 投影

    def _apply(self, e: Event) -> None:
        p = e.payload
        if e.kind == CRAFT_VERSIONED:
            self.crafts[(p["craft_id"], p["version"])] = CraftVersion(
                craft_id=p["craft_id"],
                version=p["version"],
                title=p["title"],
                age_min=p["age_min"],
                age_max=p["age_max"],
                key_steps=[
                    KeyStep(seq=step["seq"], name=step["name"], hazards=tuple(step.get("hazards", ())))
                    for step in p.get("key_steps", ())
                ],
                required_facilities=tuple(p.get("required_facilities", ())),
                registered_at=e.occurred_at,
            )
        elif e.kind == MATERIAL_RECEIVED:
            self.batches[p["batch_id"]] = MaterialBatch(
                batch_id=p["batch_id"],
                material_name=p["material_name"],
                supplier=p["supplier"],
                allergens=frozenset(p.get("allergens", ())),
                received_at=e.occurred_at,
            )
        elif e.kind == INSTRUCTOR_QUALIFIED:
            instructor = self.instructors.setdefault(
                p["instructor_id"], Instructor(p["instructor_id"], p["name"])
            )
            instructor.name = p["name"]
            for c in p.get("certifications", ()):
                cert = Certification(
                    code=c["code"], valid_from=_parse_day(c["valid_from"]), valid_to=_parse_day(c["valid_to"])
                )
                instructor.certifications[cert.code] = cert
        elif e.kind == VENUE_REGISTERED:
            self.venues[p["venue_id"]] = Venue(
                venue_id=p["venue_id"],
                name=p["name"],
                station_capacity=p["station_capacity"],
                facilities=frozenset(p.get("facilities", ())),
            )
        elif e.kind == SESSION_SCHEDULED:
            self.sessions[p["session_id"]] = Session(
                session_id=p["session_id"],
                craft_id=p["craft_id"],
                craft_version=p["craft_version"],
                scheduled_start=_parse_dt(p["scheduled_start"]),
                scheduled_end=_parse_dt(p["scheduled_end"]),
                venue_id=p["venue_id"],
                instructor_ids=list(p.get("instructor_ids", ())),
                batch_ids=list(p.get("batch_ids", ())),
                created_at=e.occurred_at,
            )
        elif e.kind == ENROLLMENT_ADDED:
            s = self.sessions[p["session_id"]]
            s.enrollments[p["participant_id"]] = Participant(
                participant_id=p["participant_id"],
                name=p["name"],
                birth_date=_parse_day(p["birth_date"]),
                allergy_declarations=frozenset(p.get("allergy_declarations", ())),
                guardian_confirmed=bool(p.get("guardian_confirmed", False)),
            )
            s.revision += 1
            self._void_latest(s, "ENROLLMENT_CHANGED")
        elif e.kind == ASSIGNMENT_CHANGED:
            s = self.sessions[p["session_id"]]
            if "batch_ids" in p:
                s.batch_ids = list(p["batch_ids"])
            if "instructor_ids" in p:
                s.instructor_ids = list(p["instructor_ids"])
            if "venue_id" in p:
                s.venue_id = p["venue_id"]
            s.revision += 1
            self._void_latest(s, "ASSIGNMENT_CHANGED:" + ",".join(p.get("changes", ())))
        elif e.kind == SESSION_CLEARED:
            s = self.sessions[p["session_id"]]
            s.clearance_history.append(
                ClearanceRecord(
                    at=e.occurred_at,
                    decision=p["decision"],
                    reasons=tuple(p.get("reasons", ())),
                    snapshot_revision=p["snapshot_revision"],
                    officer_id=p["officer_id"],
                    event_seq=e.seq,
                )
            )
        elif e.kind == SESSION_CANCELLED:
            s = self.sessions[p["session_id"]]
            s.cancelled_reason = p["reason"]
        elif e.kind == CHECKIN_RECORDED:
            s = self.sessions[p["session_id"]]
            s.checkins[p["participant_id"]] = _parse_dt(p["at"])
        elif e.kind == MATERIAL_ISSUED:
            s = self.sessions[p["session_id"]]
            s.issues.append(
                {
                    "batch_id": p["batch_id"],
                    "participant_id": p["participant_id"],
                    "at": _parse_dt(p["at"]),
                    "event_id": e.event_id,
                    "source": e.source,
                }
            )
        elif e.kind == PRODUCT_RECORDED:
            self.products[p["product_id"]] = Product(
                product_id=p["product_id"],
                session_id=p["session_id"],
                participant_id=p["participant_id"],
                recorded_at=e.occurred_at,
                craft_id=p["craft_id"],
                craft_version=p["craft_version"],
                instructor_ids=tuple(p["instructor_ids"]),
                batches=tuple(p["batches"]),
            )
        elif e.kind == INCIDENT_REPORTED:
            self.incidents.append(
                Incident(
                    incident_id=p["incident_id"],
                    session_id=p["session_id"],
                    batch_id=p["batch_id"],
                    occurred_at=_parse_dt(p["occurred_at"]),
                    reported_at=e.occurred_at,
                    reporter_id=e.actor,
                    description=p["description"],
                )
            )
        elif e.kind == BATCH_FROZEN:
            batch = self.batches[p["batch_id"]]
            batch.status = BATCH_STATUS_FROZEN
            batch.freeze_notes.append(p.get("reason", ""))
            for sid in p.get("affected_sessions", ()):  # 同批次未开始场次：签批当场失效
                self._void_latest(self.sessions[sid], "BATCH_FROZEN")
        elif e.kind == BATCH_RELEASED:
            batch = self.batches[p["batch_id"]]
            batch.status = BATCH_ACTIVE

    @staticmethod
    def _void_latest(s: Session, reason: str) -> None:
        if not s.clearance_history:
            return
        latest = s.clearance_history[-1]
        if latest.void_reason is None:
            s.clearance_history[-1] = replace(latest, void_reason=reason)

    # ------------------------------------------------------------- JSON 持久化

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps([_event_to_dict(e) for e in self._events], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, path: str | Path) -> None:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for item in raw:
            event = _event_from_dict(item)
            dedup_key = tuple(item["dedup_key"]) if item.get("dedup_key") else None
            self.append(event, dedup_key=dedup_key)


def _event_to_dict(e: Event) -> dict[str, Any]:
    return {
        "event_id": e.event_id,
        "kind": e.kind,
        "occurred_at": e.occurred_at.isoformat(),
        "subject_id": e.subject_id,
        "payload": e.payload,
        "actor": e.actor,
        "role": e.role,
        "source": e.source,
        "dedup_key": list(e.dedup_key) if e.dedup_key else None,
    }


def _event_from_dict(d: dict[str, Any]) -> Event:
    return Event(
        event_id=d["event_id"],
        kind=d["kind"],
        occurred_at=_parse_dt(d["occurred_at"]),
        subject_id=d["subject_id"],
        payload=d.get("payload", {}),
        actor=d.get("actor", "system"),
        role=d.get("role", "system"),
        source=d.get("source", "online"),
        dedup_key=tuple(d["dedup_key"]) if d.get("dedup_key") else None,
    )


def replay(events: Iterable[dict[str, Any]]) -> Repository:
    """从事件字典流重建仓储（离线同步后合并用）。"""
    repo = Repository()
    for item in events:
        dedup_key = tuple(item["dedup_key"]) if item.get("dedup_key") else None
        repo.append(_event_from_dict(item), dedup_key=dedup_key)
    return repo
