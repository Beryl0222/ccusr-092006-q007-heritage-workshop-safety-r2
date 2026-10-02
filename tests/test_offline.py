"""离线补传与幂等测试：签到/领用即使离线补传也只能记一次。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.heritage_workshop_safety import (
    ConflictError,
    Event,
    Principal,
    Repository,
    ROLE_OPERATOR,
    replay,
)

from tests.factories import (
    OFFICER,
    OPERATOR,
    START,
    TEACHER,
    build_service,
    enroll,
    schedule_incense,
)


def cleared_session(svc, sid="S-1"):
    schedule_incense(svc, sid)
    enroll(svc, sid, "p1", birth="2014-05-01")
    svc.clear_session(OFFICER, sid)


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        cleared_session(self.svc)

    def test_checkin_recorded_once_with_same_event_id(self):
        from datetime import timedelta

        at = START + timedelta(minutes=5)
        self.svc.check_in(
            TEACHER, session_id="S-1", participant_id="p1", at=at,
            event_id="offline-ci-1", source="offline",
        )
        # 网络恢复后同一条事件重放补传
        with self.assertRaises(ConflictError):
            self.svc.check_in(
                TEACHER, session_id="S-1", participant_id="p1", at=at,
                event_id="offline-ci-1", source="offline",
            )
        self.assertEqual(len(self.svc.repo.get_session("S-1").checkins), 1)

    def test_checkin_blocked_by_business_dedup_even_with_new_event_id(self):
        from datetime import timedelta

        at = START + timedelta(minutes=5)
        self.svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=at, event_id="ci-a")
        with self.assertRaises(ConflictError):
            self.svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=at, event_id="ci-b")
        self.assertEqual(len(self.svc.repo.get_session("S-1").checkins), 1)

    def test_material_issue_recorded_once_per_participant_batch(self):
        from datetime import timedelta

        at = START + timedelta(minutes=10)
        self.svc.issue_material(
            TEACHER, session_id="S-1", batch_id="B-INC-01", participant_id="p1",
            at=at, event_id="is-1", source="offline",
        )
        # 同一批次同人只能领一次（防止离线端重复扫码）
        with self.assertRaises(ConflictError):
            self.svc.issue_material(
                TEACHER, session_id="S-1", batch_id="B-INC-01", participant_id="p1",
                at=at, event_id="is-2",
            )
        self.assertEqual(len(self.svc.repo.get_session("S-1").issues), 1)

    def test_issue_refused_when_batch_frozen(self):
        self.svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=START)
        self.svc.report_incident(
            TEACHER, incident_id="I-1", session_id="S-1", batch_id="B-INC-01",
            occurred_at=START, description="过敏",
        )
        with self.assertRaises(ConflictError):
            self.svc.issue_material(
                TEACHER, session_id="S-1", batch_id="B-INC-01", participant_id="p1"
            )

    def test_checkin_requires_valid_clearance(self):
        schedule_incense(self.svc, "S-2")
        enroll(self.svc, "S-2", "p2", birth="2014-05-01")
        with self.assertRaises(ConflictError):
            self.svc.check_in(TEACHER, session_id="S-2", participant_id="p2")

    def test_checkin_rejected_after_signature_voided_then_recleared(self):
        # 已签批，开课前换料，签字失效 → 签到被拒；重新放行后方可签到
        self.svc.change_assignment(OPERATOR, session_id="S-1", batch_ids=["B-INC-02"])
        with self.assertRaises(ConflictError):
            self.svc.check_in(TEACHER, session_id="S-1", participant_id="p1")
        self.svc.clear_session(OFFICER, "S-1")
        self.svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=START)
        self.assertEqual(len(self.svc.repo.get_session("S-1").checkins), 1)


class EventLogMergeTest(unittest.TestCase):
    def test_replay_offline_log_dedups_on_retransmit(self):
        from datetime import timedelta

        svc = build_service()
        cleared_session(svc)
        at = START + timedelta(minutes=20)
        svc.issue_material(
            TEACHER, session_id="S-1", batch_id="B-INC-01", participant_id="p1",
            at=at, event_id="offline-is-9", source="offline",
        )
        dumped = [
            {
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
            for e in svc.repo.events
        ]
        restored = replay(dumped)
        self.assertEqual(len(restored.events), len(dumped))
        self.assertEqual(restored.get_session("S-1").issues[0]["source"], "offline")

        # 整批重传：event_id 冲突，拒绝重复记账
        with self.assertRaises(ConflictError):
            for d in dumped:
                restored.append(
                    Event(
                        event_id=d["event_id"],
                        kind=d["kind"],
                        occurred_at=datetime.fromisoformat(d["occurred_at"]),
                        subject_id=d["subject_id"],
                        payload=d["payload"],
                    ),
                    dedup_key=tuple(d["dedup_key"]) if d["dedup_key"] else None,
                )

    def test_persistence_roundtrip(self):
        svc = build_service()
        cleared_session(svc)
        svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=START, event_id="ci-x")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.json"
            svc.repo.save(path)
            restored = Repository()
            restored.load(path)
            self.assertEqual(len(restored.events), len(svc.repo.events))
            self.assertIn("S-1", restored.sessions)
            self.assertEqual(restored.get_session("S-1").checkins["p1"], START)


if __name__ == "__main__":
    unittest.main()
