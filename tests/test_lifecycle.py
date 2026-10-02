"""场次生命周期：签批随换料/换人/换场/报名变化失效，事故冻结与恢复。"""

import unittest

from src.heritage_workshop_safety import (
    DECISION_CLEARED,
    DECISION_RESTRICTED,
    ConflictError,
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


class SignatureInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        schedule_incense(self.svc)
        enroll(self.svc, "S-1", "p1", birth="2014-05-01")

    def test_clearance_valid_after_signing(self):
        self.svc.clear_session(OFFICER, "S-1")
        self.assertIsNotNone(self.svc.repo.get_session("S-1").valid_clearance())

    def test_new_enrollment_voids_clearance(self):
        self.svc.clear_session(OFFICER, "S-1")
        enroll(self.svc, "S-1", "p2", birth="2015-01-01")
        session = self.svc.repo.get_session("S-1")
        self.assertIsNone(session.valid_clearance())
        self.assertIsNotNone(session.current_clearance.void_reason)
        # 重新签批后恢复有效
        self.svc.clear_session(OFFICER, "S-1")
        self.assertIsNotNone(self.svc.repo.get_session("S-1").valid_clearance())

    def test_change_material_voids_clearance(self):
        self.svc.clear_session(OFFICER, "S-1")
        self.svc.change_assignment(OPERATOR, session_id="S-1", batch_ids=["B-INC-02"])
        self.assertIsNone(self.svc.repo.get_session("S-1").valid_clearance())

    def test_change_instructor_voids_clearance(self):
        self.svc.clear_session(OFFICER, "S-1")
        self.svc.change_assignment(OPERATOR, session_id="S-1", instructor_ids=["teacher-1"])
        self.assertIsNone(self.svc.repo.get_session("S-1").valid_clearance())

    def test_change_venue_voids_clearance(self):
        self.svc.clear_session(OFFICER, "S-1")
        # venue-2 缺洗眼器，换场后重新评估还应受限
        self.svc.change_assignment(OPERATOR, session_id="S-1", venue_id="venue-2")
        self.assertIsNone(self.svc.repo.get_session("S-1").valid_clearance())
        self.svc.clear_session(OFFICER, "S-1")
        latest = self.svc.repo.get_session("S-1").current_clearance
        self.assertEqual(latest.decision, DECISION_RESTRICTED)

    def test_noop_change_rejected(self):
        with self.assertRaises(ConflictError):
            self.svc.change_assignment(OPERATOR, session_id="S-1", batch_ids=["B-INC-01"])

    def test_cannot_reuse_signature_after_restriction_then_fix(self):
        # 配一个过敏批次并签出限制
        self.svc.change_assignment(OPERATOR, session_id="S-1", batch_ids=["B-INC-02"])
        enroll(self.svc, "S-1", "p2", birth="2015-01-01", allergies=["JASMINE"])
        self.svc.clear_session(OFFICER, "S-1")
        self.assertEqual(
            self.svc.repo.get_session("S-1").current_clearance.decision, DECISION_RESTRICTED
        )
        # 换回无冲突批次，必须重新签字
        self.svc.change_assignment(OPERATOR, session_id="S-1", batch_ids=["B-INC-01"])
        self.assertIsNone(self.svc.repo.get_session("S-1").valid_clearance())
        self.svc.clear_session(OFFICER, "S-1")
        self.assertEqual(
            self.svc.repo.get_session("S-1").current_clearance.decision, DECISION_CLEARED
        )

    def test_duplicate_signing_while_still_valid_rejected(self):
        self.svc.clear_session(OFFICER, "S-1")
        with self.assertRaises(ConflictError):
            self.svc.clear_session(OFFICER, "S-1")


class IncidentFreezeTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        schedule_incense(self.svc, "S-1")
        schedule_incense(self.svc, "S-2")
        for sid in ("S-1", "S-2"):
            enroll(self.svc, sid, "p1", birth="2014-05-01")
            self.svc.clear_session(OFFICER, sid)

    def test_incident_freezes_batch_and_unstarted_sessions(self):
        # S-1 已经签到（开始），S-2 未开始
        self.svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=START)
        events = self.svc.report_incident(
            TEACHER,
            incident_id="I-1",
            session_id="S-1",
            batch_id="B-INC-01",
            occurred_at=START,
            description="香粉疑似引发过敏",
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(self.svc.repo.get_batch("B-INC-01").status, "FROZEN")
        # 未开始的 S-2 签批被作废
        self.assertIsNone(self.svc.repo.get_session("S-2").valid_clearance())
        self.assertIsNotNone(self.svc.repo.get_session("S-2").current_clearance.void_reason)
        # 已开始的 S-1 历史签批不做事后作废
        self.assertIsNone(self.svc.repo.get_session("S-1").current_clearance.void_reason)

    def test_frozen_batch_blocks_new_signoff_until_release(self):
        self.svc.report_incident(
            TEACHER,
            incident_id="I-1",
            session_id="S-1",
            batch_id="B-INC-01",
            occurred_at=START,
            description="香粉疑似引发过敏",
        )
        self.svc.clear_session(OFFICER, "S-2")
        self.assertEqual(
            self.svc.repo.get_session("S-2").current_clearance.decision, DECISION_RESTRICTED
        )
        # 解冻后，旧签字依然无效，必须重新评估放行
        self.svc.release_batch(OFFICER, batch_id="B-INC-01", assessment="复检合格，供应商提供检测报告")
        self.assertEqual(self.svc.repo.get_batch("B-INC-01").status, "ACTIVE")
        self.assertIsNone(self.svc.repo.get_session("S-2").valid_clearance())
        self.svc.clear_session(OFFICER, "S-2")
        self.assertEqual(
            self.svc.repo.get_session("S-2").current_clearance.decision, DECISION_CLEARED
        )

    def test_only_officer_may_release_batch(self):
        self.svc.report_incident(
            TEACHER,
            incident_id="I-1",
            session_id="S-1",
            batch_id="B-INC-01",
            occurred_at=START,
            description="x",
        )
        with self.assertRaises(Exception):
            self.svc.release_batch(TEACHER, batch_id="B-INC-01", assessment="恢复")

    def test_release_requires_assessment(self):
        self.svc.report_incident(
            TEACHER,
            incident_id="I-1",
            session_id="S-1",
            batch_id="B-INC-01",
            occurred_at=START,
            description="x",
        )
        with self.assertRaises(Exception):
            self.svc.release_batch(OFFICER, batch_id="B-INC-01", assessment="  ")

    def test_cancel_session_flow(self):
        ev = self.svc.cancel_session(OFFICER, "S-2", reason="讲师请假")
        self.assertTrue(self.svc.repo.get_session("S-2").cancelled)
        with self.assertRaises(ConflictError):
            self.svc.check_in(TEACHER, session_id="S-2", participant_id="p1")


if __name__ == "__main__":
    unittest.main()
