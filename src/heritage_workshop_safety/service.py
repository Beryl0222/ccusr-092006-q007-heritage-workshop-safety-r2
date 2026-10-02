"""应用服务层：登记、排期、报名、放行、签到、领用、成品、事故处置。

所有写操作都经过这里：先回放投影、做角色与领域校验，再追加事件。
签到与材料领用以业务键 + ``client_token`` 双重幂等，离线补传安全。
"""

from __future__ import annotations

from .clock import LOCAL_TZ, Clock, SystemClock, parse_dt
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
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
    EventStore,
)
from .models import SessionStatus
from .projection import Projection
from .rules import evaluate
from .security import Actor


class SafetyService:
    def __init__(self, store: EventStore, clock: Clock | None = None):
        self.store = store
        self.clock = clock or getattr(store, "_clock", None) or SystemClock()
        self.projection = Projection.from_events(store.stream())

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _append(self, kind: str, subject_id: str, payload: dict, actor: Actor, **kw):
        # 事件追加与投影更新在同一把锁内，保证多线程写串行化
        with self.store.lock:
            event = self.store.append(
                kind, subject_id, payload, actor=actor.actor_id, **kw
            )
            self.projection.apply(event)
        return event

    def _replay(self):
        self.projection = Projection.from_events(self.store.stream())

    def _require_actor(self, actor: Actor) -> None:
        if not isinstance(actor, Actor):
            raise ValidationError("缺少操作人 Actor")

    def _get_session(self, session_id: str):
        session = self.projection.sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"场次不存在: {session_id}")
        return session

    def _dedup_by_token(self, client_token: str | None):
        """命中幂等键则返回首个事件，否则 None。"""
        if client_token and client_token in self.projection.client_tokens:
            event = self.projection.client_tokens[client_token]
            return {"deduplicated": True, "event": event}
        return None

    @staticmethod
    def _stamp(payload: dict, client_token: str | None, occurred_at: str | None) -> dict:
        if client_token:
            payload = dict(payload)
            payload["client_token"] = client_token
        if occurred_at:
            payload = dict(payload)
            payload["client_occurred_at"] = occurred_at
        return payload

    # ------------------------------------------------------------------ #
    # 登记域（商户可登记自己的资料，管理员/安全员亦可）
    # ------------------------------------------------------------------ #

    def register_craft(self, actor: Actor, data: dict):
        self._require_actor(actor)
        required = ("craft_code", "version", "name", "age_min", "age_max")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"技艺登记缺少字段: {missing}")
        if int(data["age_min"]) > int(data["age_max"]):
            raise ValidationError("适龄下限不能大于上限")
        payload = {
            "craft_code": data["craft_code"],
            "version": str(data["version"]),
            "name": data["name"],
            "age_min": int(data["age_min"]),
            "age_max": int(data["age_max"]),
            "key_steps": data.get("key_steps", []),
            "required_certificates": data.get("required_certificates", []),
        }
        ref = f"{payload['craft_code']}@{payload['version']}"
        return self._append(CRAFT_VERSIONED, ref, payload, actor)

    def receive_material(self, actor: Actor, data: dict):
        self._require_actor(actor)
        required = ("batch_id", "material_code", "name", "received_at", "expires_at")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"材料入库缺少字段: {missing}")
        if parse_dt(data["expires_at"]) <= parse_dt(data["received_at"]):
            raise ValidationError("保质期必须晚于入库时间")
        if data["batch_id"] in self.projection.batches:
            raise ConflictError(f"批次已存在: {data['batch_id']}")
        payload = {
            "batch_id": data["batch_id"],
            "material_code": data["material_code"],
            "name": data["name"],
            "supplier": data.get("supplier", ""),
            "allergens": sorted(set(data.get("allergens", []))),
            "received_at": data["received_at"],
            "expires_at": data["expires_at"],
        }
        return self._append(MATERIAL_RECEIVED, data["batch_id"], payload, actor)

    def register_instructor(self, actor: Actor, data: dict):
        self._require_actor(actor)
        required = ("instructor_id", "name")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"讲师登记缺少字段: {missing}")
        payload = {
            "instructor_id": data["instructor_id"],
            "name": data["name"],
            "certificates": data.get("certificates", {}),
            "qualified_crafts": data.get("qualified_crafts", []),
        }
        return self._append(INSTRUCTOR_REGISTERED, data["instructor_id"], payload, actor)

    def register_venue(self, actor: Actor, data: dict):
        self._require_actor(actor)
        required = ("venue_id", "name", "station_count")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"场地登记缺少字段: {missing}")
        if int(data["station_count"]) <= 0:
            raise ValidationError("工位数量必须为正数")
        payload = {
            "venue_id": data["venue_id"],
            "name": data["name"],
            "station_count": int(data["station_count"]),
            "emergency_facilities": data.get("emergency_facilities", []),
        }
        return self._append(VENUE_REGISTERED, data["venue_id"], payload, actor)

    # ------------------------------------------------------------------ #
    # 场次排期与报名
    # ------------------------------------------------------------------ #

    def schedule_session(self, actor: Actor, data: dict):
        self._require_actor(actor)
        required = (
            "session_id", "craft_code", "version", "venue_id",
            "instructor_id", "batch_ids", "scheduled_start", "scheduled_end",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"场次排期缺少字段: {missing}")
        if data["session_id"] in self.projection.sessions:
            raise ConflictError(f"场次已存在: {data['session_id']}")
        craft_ref = f"{data['craft_code']}@{data['version']}"
        if craft_ref not in self.projection.crafts:
            raise NotFoundError(f"技艺版本未登记: {craft_ref}")
        if data["venue_id"] not in self.projection.venues:
            raise NotFoundError(f"场地未登记: {data['venue_id']}")
        if data["instructor_id"] not in self.projection.instructors:
            raise NotFoundError(f"讲师未登记: {data['instructor_id']}")
        for bid in data["batch_ids"]:
            if bid not in self.projection.batches:
                raise NotFoundError(f"材料批次未登记: {bid}")
        if parse_dt(data["scheduled_end"]) <= parse_dt(data["scheduled_start"]):
            raise ValidationError("结束时间必须晚于开始时间")
        payload = {
            "session_id": data["session_id"],
            "craft_code": data["craft_code"],
            "version": str(data["version"]),
            "venue_id": data["venue_id"],
            "instructor_id": data["instructor_id"],
            "batch_ids": list(data["batch_ids"]),
            "scheduled_start": data["scheduled_start"],
            "scheduled_end": data["scheduled_end"],
        }
        return self._append(SESSION_SCHEDULED, data["session_id"], payload, actor)

    def _validate_changes(self, session, changes: dict) -> dict:
        """校验换绑目标存在，返回事件用的 from/to 明细。"""
        allowed = {"venue_id", "instructor_id", "batch_ids", "version"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(f"不允许的变更项: {sorted(unknown)}")
        if not changes:
            raise ValidationError("未提供任何变更")
        detail = {}
        if "venue_id" in changes:
            new_venue = changes["venue_id"]
            if new_venue not in self.projection.venues:
                raise NotFoundError(f"场地未登记: {new_venue}")
            detail["venue_id"] = {"from": session.venue_id, "to": new_venue}
        if "instructor_id" in changes:
            new_ins = changes["instructor_id"]
            if new_ins not in self.projection.instructors:
                raise NotFoundError(f"讲师未登记: {new_ins}")
            detail["instructor_id"] = {"from": session.instructor_id, "to": new_ins}
        if "batch_ids" in changes:
            for bid in changes["batch_ids"]:
                if bid not in self.projection.batches:
                    raise NotFoundError(f"材料批次未登记: {bid}")
            detail["batch_ids"] = {"from": list(session.batch_ids), "to": list(changes["batch_ids"])}
        if "version" in changes:
            new_ref = f"{session.craft_code}@{changes['version']}"
            if new_ref not in self.projection.crafts:
                raise NotFoundError(f"技艺版本未登记: {new_ref}")
            detail["version"] = {"from": session.version, "to": str(changes["version"])}
        return detail

    def amend_session(self, actor: Actor, session_id: str, changes: dict):
        """更换材料/讲师/场地（或技艺版本）：旧签字立即作废。"""
        self._require_actor(actor)
        session = self._get_session(session_id)
        detail = self._validate_changes(session, changes)
        if session.status == SessionStatus.FROZEN:
            raise ConflictError("场次已冻结，不能直接变更，请走事故评估恢复流程")
        payload = {"session_id": session_id, "changes": detail, "reason": "binding_changed"}
        return self._append(SESSION_AMENDED, session_id, payload, actor)

    def enroll(self, actor: Actor, session_id: str, participant: dict, *,
               client_token: str | None = None, occurred_at: str | None = None):
        self._require_actor(actor)
        self._get_session(session_id)
        dup = self._dedup_by_token(client_token)
        if dup:
            return dup
        if "participant_id" not in participant or "age" not in participant:
            raise ValidationError("报名缺少 participant_id 或 age")
        existing = self.projection.participant(session_id, participant["participant_id"])
        if existing is not None:
            raise ConflictError(
                f"参与者已报名: {participant['participant_id']}"
            )
        payload = self._stamp(
            {
                "session_id": session_id,
                "participant": {
                    "participant_id": participant["participant_id"],
                    "age": int(participant["age"]),
                    "guardian_confirmed": bool(participant.get("guardian_confirmed", False)),
                    "allergy_declarations": list(participant.get("allergy_declarations", [])),
                    "alias": participant.get("alias", ""),
                },
            },
            client_token,
            occurred_at,
        )
        event = self._append(ENROLLMENT_ADDED, session_id, payload, actor)
        return {"deduplicated": False, "event": event}

    # ------------------------------------------------------------------ #
    # 放行评估与签字
    # ------------------------------------------------------------------ #

    def preview_clearance(self, actor: Actor, session_id: str) -> dict:
        self._require_actor(actor)
        session = self._get_session(session_id)
        decision = evaluate(self.projection, session, self.clock.now())
        return decision.to_payload()

    def clear_session(self, actor: Actor, session_id: str) -> dict:
        """执行检查并签字落库；返回决策与事件。换绑后必须重新签字。"""
        self._require_actor(actor)
        if not actor.can_clear:
            raise PermissionDenied("仅安全管理人员/管理员可签字放行")
        session = self._get_session(session_id)
        if session.status == SessionStatus.FROZEN:
            raise ConflictError("场次处于冻结状态，请先完成事故评估恢复")
        decision = evaluate(self.projection, session, self.clock.now())
        payload = {
            "session_id": session_id,
            "outcome": decision.outcome,
            "findings": decision.findings,
            "eligible_participant_ids": decision.eligible_participant_ids,
            "baseline": decision.baseline,
        }
        event = self._append(SESSION_CLEARED, session_id, payload, actor)
        return {"decision": decision.to_payload(), "event": event}

    # ------------------------------------------------------------------ #
    # 签到与材料领用（幂等，可离线补传）
    # ------------------------------------------------------------------ #

    def check_in(self, actor: Actor, session_id: str, participant_id: str, *,
                 client_token: str | None = None, occurred_at: str | None = None) -> dict:
        self._require_actor(actor)
        session = self._get_session(session_id)
        dup = self._dedup_by_token(client_token)
        if dup:
            return dup
        if self.projection.participant(session_id, participant_id) is None:
            raise NotFoundError("该参与者未报名本场次")
        key = (session_id, participant_id)
        if key in self.projection.checkins:
            return {"deduplicated": True, "event": self.projection.checkins[key]}
        if session.status not in (
            SessionStatus.CLEARED, SessionStatus.RESTRICTED, SessionStatus.RELEASED,
        ):
            raise ConflictError("场次尚未完成安全放行签字，禁止签到")
        latest = session.latest_clearance
        # 放行签字后随到随学的新报名者不在已签名单内，必须重新评估
        if latest is not None and participant_id not in latest.eligible_participant_ids:
            raise ConflictError("参与者不在最新放行名单内，请重新评估签字后再签到")
        payload = self._stamp(
            {"session_id": session_id, "participant_id": participant_id},
            client_token,
            occurred_at,
        )
        event = self._append(CHECKIN_RECORDED, session_id, payload, actor)
        return {"deduplicated": False, "event": event}

    def issue_material(self, actor: Actor, session_id: str, participant_id: str,
                       batch_id: str, *, quantity: int = 1,
                       client_token: str | None = None, occurred_at: str | None = None) -> dict:
        self._require_actor(actor)
        session = self._get_session(session_id)
        dup = self._dedup_by_token(client_token)
        if dup:
            return dup
        if self.projection.participant(session_id, participant_id) is None:
            raise NotFoundError("该参与者未报名本场次")
        if batch_id not in session.batch_ids:
            raise ValidationError("该批次不属于本场次登记材料")
        batch = self.projection.batches[batch_id]
        if batch.status == "suspended":
            raise ConflictError(f"批次 {batch_id} 已冻结，禁止领用")
        if batch.is_expired(self.clock.now()):
            raise ConflictError(f"批次 {batch_id} 已过保质期，禁止领用")
        if session.status not in (
            SessionStatus.CLEARED, SessionStatus.RESTRICTED, SessionStatus.RELEASED,
        ):
            raise ConflictError("场次尚未完成安全放行签字，禁止领用")
        latest = session.latest_clearance
        if latest is not None and participant_id not in latest.eligible_participant_ids:
            raise ConflictError("参与者不在最新放行名单内，请重新评估签字后再领用")
        key = (session_id, participant_id, batch_id)
        if key in self.projection.material_issues:
            return {"deduplicated": True, "event": self.projection.material_issues[key]}
        payload = self._stamp(
            {
                "session_id": session_id,
                "participant_id": participant_id,
                "batch_id": batch_id,
                "quantity": int(quantity),
            },
            client_token,
            occurred_at,
        )
        event = self._append(MATERIAL_ISSUED, session_id, payload, actor)
        return {"deduplicated": False, "event": event}

    def register_product(self, actor: Actor, data: dict, *,
                         client_token: str | None = None, occurred_at: str | None = None) -> dict:
        """登记游客带走的成品，作为追溯锚点。"""
        self._require_actor(actor)
        required = ("product_id", "session_id", "name")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"成品登记缺少字段: {missing}")
        dup = self._dedup_by_token(client_token)
        if dup:
            return dup
        session = self._get_session(data["session_id"])
        if data["product_id"] in self.projection.products:
            raise ConflictError(f"成品编号已存在: {data['product_id']}")
        # 成品所用批次：默认本场全部批次；若现场记录实际领用则取交集
        batch_ids = list(session.batch_ids)
        if data.get("batch_ids"):
            unknown = [b for b in data["batch_ids"] if b not in session.batch_ids]
            if unknown:
                raise ValidationError(f"成品批次不属于本场次: {unknown}")
            batch_ids = list(data["batch_ids"])
        if data.get("participant_id"):
            if self.projection.participant(session.session_id, data["participant_id"]) is None:
                raise NotFoundError("成品关联的参与者未报名本场次")
        payload = self._stamp(
            {
                "product_id": data["product_id"],
                "session_id": session.session_id,
                "participant_id": data.get("participant_id", ""),
                "name": data["name"],
                "batch_ids": batch_ids,
                "craft_ref": session.craft_ref,
            },
            client_token,
            occurred_at,
        )
        event = self._append(PRODUCT_REGISTERED, data["product_id"], payload, actor)
        return {"deduplicated": False, "event": event}

    # ------------------------------------------------------------------ #
    # 事故：冻结同批次未开始场次，再评估恢复
    # ------------------------------------------------------------------ #

    def _session_started(self, session) -> bool:
        if self.projection.session_has_checkins(session.session_id):
            return True
        return parse_dt(session.scheduled_start) <= self.clock.now()

    def report_incident(self, actor: Actor, data: dict) -> dict:
        self._require_actor(actor)
        if not actor.can_handle_incident:
            raise PermissionDenied("仅安全管理人员/管理员可上报事故并触发冻结")
        required = ("incident_id", "session_id", "description")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"事故上报缺少字段: {missing}")
        session = self._get_session(data["session_id"])
        batch_ids = list(data.get("batch_ids") or session.batch_ids)
        for bid in batch_ids:
            if bid not in self.projection.batches:
                raise NotFoundError(f"材料批次不存在: {bid}")

        produced = []
        incident_payload = {
            "incident_id": data["incident_id"],
            "session_id": session.session_id,
            "description": data["description"],
            "severity": data.get("severity", "unspecified"),
            "batch_ids": batch_ids,
        }
        produced.append(
            self._append(INCIDENT_REPORTED, session.session_id, incident_payload, actor)
        )

        # 1) 冻结相关批次
        reason = f"事故 {data['incident_id']}"
        for bid in batch_ids:
            batch = self.projection.batches[bid]
            if batch.status != "suspended":
                produced.append(
                    self._append(
                        BATCH_SUSPENDED,
                        bid,
                        {"batch_id": bid, "reason": reason},
                        actor,
                    )
                )

        # 2) 冻结同批次尚未开始的场次（已评估取消的场次无需再冻）
        frozen_sessions = []
        for sid, other in self.projection.sessions.items():
            if other.status in (SessionStatus.FROZEN, SessionStatus.DENIED):
                continue
            if self._session_started(other):
                continue
            if set(other.batch_ids) & set(batch_ids):
                produced.append(
                    self._append(
                        SESSION_FROZEN,
                        sid,
                        {"session_id": sid, "reason": reason,
                         "incident_id": data["incident_id"]},
                        actor,
                    )
                )
                frozen_sessions.append(sid)
        return {
            "incident_id": data["incident_id"],
            "suspended_batch_ids": batch_ids,
            "frozen_session_ids": frozen_sessions,
            "events": produced,
        }

    def release_batch(self, actor: Actor, batch_id: str, note: str = "") -> dict:
        """有权人员评估材料批次后解冻批次（场次仍需逐场评估恢复）。"""
        self._require_actor(actor)
        if not actor.can_handle_incident:
            raise PermissionDenied("仅安全管理人员/管理员可解冻批次")
        batch = self.projection.batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"批次不存在: {batch_id}")
        if batch.status != "suspended":
            raise ConflictError(f"批次未处于冻结状态: {batch_id}")
        event = self._append(
            BATCH_RELEASED,
            batch_id,
            {"batch_id": batch_id, "note": note, "assessed_by": actor.actor_id},
            actor,
        )
        return {"event": event}

    def assess_frozen_sessions(self, actor: Actor) -> list[dict]:
        """列出当前冻结场次的重新评估结果（不落库），供恢复决策。"""
        self._require_actor(actor)
        result = []
        for sid, session in self.projection.sessions.items():
            if session.status != SessionStatus.FROZEN:
                continue
            decision = evaluate(self.projection, session, self.clock.now()).to_payload()
            result.append(decision)
        return result

    def release_session(self, actor: Actor, session_id: str, note: str = "",
                        changes: dict | None = None) -> dict:
        """对冻结场次重新执行全部检查，通过后由有权人员签字恢复。

        ``changes`` 允许在恢复时一并换绑材料/讲师/场地/版本（例如停用事故
        批次、改用安全批次）；评估在临时视图上先跑通，任何失败都不落事件。
        """
        self._require_actor(actor)
        if not actor.can_handle_incident:
            raise PermissionDenied("仅安全管理人员/管理员可评估恢复场次")
        session = self._get_session(session_id)
        if session.status != SessionStatus.FROZEN:
            raise ConflictError("场次未处于冻结状态")

        import dataclasses

        tentative = session
        detail = {}
        if changes:
            detail = self._validate_changes(session, changes)
            tentative = dataclasses.replace(
                session,
                venue_id=detail.get("venue_id", {}).get("to", session.venue_id),
                instructor_id=detail.get("instructor_id", {}).get("to", session.instructor_id),
                batch_ids=detail.get("batch_ids", {}).get("to", list(session.batch_ids)),
                version=detail.get("version", {}).get("to", session.version),
            )

        blocked_batches = [
            bid for bid in tentative.batch_ids
            if bid in self.projection.batches
            and self.projection.batches[bid].status == "suspended"
        ]
        if blocked_batches:
            raise ConflictError(
                f"关联批次仍处于冻结状态，请先评估解冻或更换批次: {blocked_batches}"
            )
        decision = evaluate(self.projection, tentative, self.clock.now())
        if decision.blocking_findings:
            raise ValidationError({
                "message": "重新评估未通过，场次维持冻结",
                "decision": decision.to_payload(),
            })

        # 评估通过后才落库：换绑（若有）-> 重新签字 -> 恢复
        if changes:
            self._append(
                SESSION_AMENDED, session_id,
                {"session_id": session_id, "changes": detail,
                 "reason": "post_incident_rebind"},
                actor,
            )
        clear_event = self._append(
            SESSION_CLEARED,
            session_id,
            {
                "session_id": session_id,
                "outcome": decision.outcome,
                "findings": decision.findings,
                "eligible_participant_ids": decision.eligible_participant_ids,
                "baseline": decision.baseline,
                "purpose": "post_incident_recheck",
            },
            actor,
        )
        release_event = self._append(
            SESSION_RELEASED,
            session_id,
            {
                "session_id": session_id,
                "note": note,
                "released_by": actor.actor_id,
                "clearance_event_id": clear_event["event_id"],
                "outcome": decision.outcome,
                "findings": decision.findings,
                "eligible_participant_ids": decision.eligible_participant_ids,
                "baseline": decision.baseline,
            },
            actor,
        )
        return {
            "decision": decision.to_payload(),
            "clearance_event": clear_event,
            "release_event": release_event,
        }

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def get_session(self, session_id: str):
        return self._get_session(session_id)

    def list_sessions(self, day: str | None = None) -> list[dict]:
        items = list(self.projection.sessions.values())
        if day:
            items = [
                s for s in items
                if parse_dt(s.scheduled_start).astimezone(LOCAL_TZ).date().isoformat() == day
            ]
        return [s.to_payload() for s in sorted(items, key=lambda s: s.scheduled_start)]
