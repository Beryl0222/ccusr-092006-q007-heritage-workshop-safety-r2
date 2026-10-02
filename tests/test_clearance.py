"""放行评审规则测试：年龄、监护、过敏、容量、设施、刀具看护、冻结。"""

import unittest

from src.heritage_workshop_safety import (
    DECISION_CLEARED,
    DECISION_RESTRICTED,
    Principal,
    ROLE_OFFICER,
    SafetyService,
    evaluate_clearance,
)

from tests.factories import (
    END,
    OFFICER,
    OPERATOR,
    START,
    TEACHER,
    build_service,
    enroll,
    schedule_incense,
    schedule_kite,
)


def decision(svc, sid):
    return evaluate_clearance(svc.repo, svc.repo.get_session(sid))


def codes(svc, sid):
    return {r.code for r in decision(svc, sid).reasons}


class ClearanceRulesTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_all_checks_pass_gives_cleared(self):
        schedule_incense(self.svc)
        enroll(self.svc, "S-1", "p1", birth="2014-05-01")  # 12 岁，已监护
        d = decision(self.svc, "S-1")
        self.assertEqual(d.decision, DECISION_CLEARED)
        self.assertEqual(d.reasons, ())

    def test_age_out_of_range(self):
        schedule_incense(self.svc)
        enroll(self.svc, "S-1", "young", birth="2022-01-01")  # 4 岁 < 8
        enroll(self.svc, "S-1", "old", birth="1940-01-01")    # 86 岁 > 80
        self.assertIn("AGE_OUT_OF_RANGE", codes(self.svc, "S-1"))

    def test_minor_without_guardian_blocks(self):
        schedule_incense(self.svc)
        enroll(self.svc, "S-1", "p1", birth="2014-05-01", guardian=False)
        self.assertIn("GUARDIAN_UNCONFIRMED", codes(self.svc, "S-1"))

    def test_adult_needs_no_guardian_confirmation(self):
        schedule_incense(self.svc)
        enroll(self.svc, "S-1", "adult", birth="1990-01-01", guardian=False)
        self.assertNotIn("GUARDIAN_UNCONFIRMED", codes(self.svc, "S-1"))

    def test_allergy_conflict_with_incense_powder(self):
        schedule_incense(self.svc)
        enroll(self.svc, "S-1", "p1", birth="2014-05-01", allergies=["SANDALWOOD"])
        self.assertIn("ALLERGY_CONFLICT", codes(self.svc, "S-1"))

    def test_capacity_exceeded(self):
        schedule_incense(self.svc, venue_id="venue-2")  # 容量 1
        enroll(self.svc, "S-1", "p1", birth="2014-05-01")
        enroll(self.svc, "S-1", "p2", birth="2014-05-01")
        self.assertIn("CAPACITY_EXCEEDED", codes(self.svc, "S-1"))

    def test_missing_emergency_facility(self):
        # 风筝只要求急救包，场地具备；改为合香 + venue-2（缺洗眼器）
        schedule_incense(self.svc, venue_id="venue-2")
        self.assertIn("FACILITY_MISSING", codes(self.svc, "S-1"))

    def test_cutting_requires_certified_instructor(self):
        schedule_kite(self.svc, certified=False)  # teacher-2 无刀具看护证
        self.assertIn("CERT_MISSING", codes(self.svc, "S-KITE"))

    def test_cutting_ok_with_certified_instructor_in_validity(self):
        schedule_kite(self.svc, certified=True)
        self.assertNotIn("CERT_MISSING", codes(self.svc, "S-KITE"))

    def test_expired_certification_does_not_count(self):
        # teacher-1 的刀具证续期为已过期
        self.svc.register_instructor(
            OPERATOR,
            instructor_id="teacher-1",
            name="王护",
            certifications=[{"code": "CUTTING_TOOL_SUPERVISION", "valid_from": "2024-01-01", "valid_to": "2024-12-31"}],
        )
        schedule_kite(self.svc, certified=True)
        self.assertIn("CERT_MISSING", codes(self.svc, "S-KITE"))

    def test_frozen_batch_blocks_clearance(self):
        schedule_incense(self.svc)
        enroll(self.svc, "S-1", "p1", birth="2014-05-01")
        self.svc.clear_session(OFFICER, "S-1")
        # 用同批次再造一个未开始场次，事故后应受限
        schedule_incense(self.svc, "S-2")
        enroll(self.svc, "S-2", "p2", birth="2014-05-01")
        self.svc.report_incident(
            TEACHER,
            incident_id="I-1",
            session_id="S-1",
            batch_id="B-INC-01",
            occurred_at=START,
            description="一名儿童接触香粉后皮疹",
        )
        self.assertIn("BATCH_FROZEN", codes(self.svc, "S-2"))

    def test_only_officer_may_sign_clearance(self):
        schedule_incense(self.svc)
        with self.assertRaises(Exception):
            self.svc.clear_session(TEACHER, "S-1")


if __name__ == "__main__":
    unittest.main()
