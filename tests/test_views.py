"""查询视图测试：健康信息脱敏、成品追溯、当天管控日报。"""

import unittest
from datetime import date, timedelta

from src.heritage_workshop_safety import (
    AuthorizationError,
    Principal,
    ROLE_INSTRUCTOR,
    ROLE_OFFICER,
    ROLE_OPERATOR,
    daily_report,
    session_safety_view,
    trace_product,
)

from tests.factories import (
    CST,
    DAY,
    OFFICER,
    OPERATOR,
    START,
    TEACHER,
    build_service,
    enroll,
    schedule_incense,
)


class PrivacyViewTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        schedule_incense(self.svc)
        enroll(self.svc, "S-1", "p1", birth="2014-05-01", allergies=["SANDALWOOD"], name="陈小香")
        enroll(self.svc, "S-1", "p2", birth="2015-06-01", guardian=False, name="李小护")

    def test_operator_cannot_see_health_details(self):
        view = session_safety_view(self.svc.repo, "S-1", OPERATOR)
        self.assertTrue(view["participants_hidden"])
        self.assertNotIn("participants", view)
        rendered = str(view)
        self.assertNotIn("SANDALWOOD", rendered)
        self.assertNotIn("陈小香", rendered)
        # 仅有聚合计数，商户知道有风险但不知道具体是谁
        self.assertEqual(view["allergy_conflicts"], 1)
        self.assertEqual(view["minors"], 2)

    def test_instructor_sees_only_conflict_alerts_not_full_declarations(self):
        view = session_safety_view(self.svc.repo, "S-1", TEACHER)
        self.assertTrue(view["participants_hidden"])
        self.assertNotIn("participants", view)
        alerts = {a["participant_id"]: a["conflicting_batches"] for a in view["safety_alerts"]}
        self.assertEqual(alerts, {"p1": ["B-INC-01"]})

    def test_officer_sees_full_details(self):
        view = session_safety_view(self.svc.repo, "S-1", OFFICER)
        self.assertIn("participants", view)
        by_id = {p["participant_id"]: p for p in view["participants"]}
        self.assertEqual(by_id["p1"]["allergy_declarations"], ["SANDALWOOD"])

    def test_operators_cannot_trace_products(self):
        with self.assertRaises(AuthorizationError):
            trace_product(self.svc.repo, "PR-1", OPERATOR)


class TraceabilityTest(unittest.TestCase):
    def test_product_traces_to_session_instructor_and_material_source(self):
        svc = build_service()
        schedule_incense(svc)
        enroll(svc, "S-1", "p1", birth="2014-05-01", name="陈小香")
        svc.clear_session(OFFICER, "S-1")
        svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=START)
        svc.issue_material(
            TEACHER, session_id="S-1", batch_id="B-INC-01", participant_id="p1",
            at=START + timedelta(minutes=30), event_id="is-p1",
        )
        svc.record_product(
            TEACHER, product_id="PR-1", session_id="S-1", participant_id="p1"
        )

        trace = trace_product(svc.repo, "PR-1", OFFICER)
        self.assertEqual(trace["session"]["session_id"], "S-1")
        self.assertEqual(trace["craft"], {"craft_id": "incense", "version": "2026-1"})
        self.assertEqual(trace["instructors"][0]["instructor_id"], "teacher-2")
        self.assertEqual(trace["materials"][0]["batch_id"], "B-INC-01")
        self.assertEqual(trace["materials"][0]["supplier"], "云岭香材合作社")
        self.assertEqual(trace["materials"][0]["material_name"], "檀香合香粉")
        self.assertEqual(trace["issues"][0]["event_id"], "is-p1")

    def test_product_requires_issue_chain(self):
        svc = build_service()
        schedule_incense(svc)
        enroll(svc, "S-1", "p1", birth="2014-05-01")
        svc.clear_session(OFFICER, "S-1")
        svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=START)
        with self.assertRaises(Exception):
            svc.record_product(TEACHER, product_id="PR-X", session_id="S-1", participant_id="p1")

    def test_product_remains_traceable_even_after_batch_frozen(self):
        # 成品先带走，事后批次冻结，追溯链仍指向当时的批次与供应商
        svc = build_service()
        schedule_incense(svc)
        enroll(svc, "S-1", "p1", birth="2014-05-01")
        svc.clear_session(OFFICER, "S-1")
        svc.check_in(TEACHER, session_id="S-1", participant_id="p1", at=START)
        svc.issue_material(
            TEACHER, session_id="S-1", batch_id="B-INC-01", participant_id="p1", at=START
        )
        svc.record_product(TEACHER, product_id="PR-2", session_id="S-1", participant_id="p1")
        svc.report_incident(
            TEACHER, incident_id="I-9", session_id="S-1", batch_id="B-INC-01",
            occurred_at=START + timedelta(hours=2), description="迟发皮疹",
        )
        trace = trace_product(svc.repo, "PR-2", OFFICER)
        self.assertEqual(trace["materials"][0]["batch_id"], "B-INC-01")
        self.assertEqual(trace["materials"][0]["allergens"], ["SANDALWOOD"])


class DailyReportTest(unittest.TestCase):
    def test_report_lists_restricted_cancelled_and_recleared(self):
        svc = build_service()
        # S-A：先因过敏受限，换批次后重新放行
        schedule_incense(svc, "S-A")
        enroll(svc, "S-A", "p1", birth="2014-05-01", allergies=["SANDALWOOD"])
        svc.clear_session(OFFICER, "S-A")  # RESTRICTED
        svc.change_assignment(OPERATOR, session_id="S-A", batch_ids=["B-INC-02"])
        svc.clear_session(OFFICER, "S-A")  # 重新放行 CLEARED

        # S-B：正常放行
        schedule_incense(svc, "S-B")
        enroll(svc, "S-B", "p2", birth="2014-05-01")
        svc.clear_session(OFFICER, "S-B")

        # S-C：取消
        schedule_incense(svc, "S-C")
        enroll(svc, "S-C", "p3", birth="2014-05-01")
        svc.cancel_session(OFFICER, "S-C", reason="暴雨红色预警")

        # S-D：事故冻结后经评估解冻并重新放行
        schedule_incense(svc, "S-D")
        enroll(svc, "S-D", "p4", birth="2014-05-01")
        svc.clear_session(OFFICER, "S-D")
        svc.report_incident(
            TEACHER, incident_id="I-1", session_id="S-D", batch_id="B-INC-01",
            occurred_at=START, description="疑似过敏",
        )
        svc.release_batch(OFFICER, batch_id="B-INC-01", assessment="第三方检测合格")
        svc.clear_session(OFFICER, "S-D")  # 冻结作废后的重新放行

        report = daily_report(svc.repo, DAY)
        restricted_ids = {r["session_id"] for r in report["restricted"]}
        self.assertIn("S-A", restricted_ids)

        recleared_ids = {r["session_id"] for r in report["recleared"]}
        self.assertEqual(recleared_ids, {"S-A", "S-D"})

        cancelled = {c["session_id"]: c["reason"] for c in report["cancelled"]}
        self.assertEqual(cancelled, {"S-C": "暴雨红色预警"})

        released = {r["batch_id"]: r["assessment"] for r in report["released_batches"]}
        self.assertEqual(released, {"B-INC-01": "第三方检测合格"})

        self.assertEqual(report["summary"]["restricted"], 1)
        self.assertEqual(report["summary"]["recleared"], 2)
        self.assertEqual(report["summary"]["cancelled"], 1)
        self.assertEqual(report["summary"]["cleared"], 1)  # 只有 S-B 是首次即放行

    def test_report_empty_for_quiet_day(self):
        svc = build_service()
        report = daily_report(svc.repo, date(2026, 10, 4))
        self.assertEqual(report["summary"], {"restricted": 0, "cancelled": 0, "recleared": 0, "cleared": 0})


if __name__ == "__main__":
    unittest.main()
