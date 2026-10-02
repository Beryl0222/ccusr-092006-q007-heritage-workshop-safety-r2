"""健康信息脱敏与当日日报测试。"""

from __future__ import annotations

import unittest

from src.heritage_workshop_safety import daily_report, redact_health

from world import MERCHANT, OFFICER, build_service, kid, schedule, seed_world


class RedactionTest(unittest.TestCase):
    def test_merchant_sees_masked_allergy(self):
        payload = kid("p1", 10, allergies=["sandalwood"])
        view = redact_health(payload, MERCHANT)
        self.assertEqual(view["allergy_declarations"], ["***"])
        self.assertEqual(view["allergy_count"], 1)
        self.assertTrue(view["health_redacted"])
        # 原声明不出现在任何字段里
        self.assertNotIn("sandalwood", str(view))

    def test_officer_sees_full_health(self):
        payload = kid("p1", 10, allergies=["sandalwood"])
        view = redact_health(payload, OFFICER)
        self.assertEqual(view["allergy_declarations"], ["sandalwood"])
        self.assertFalse(view["health_redacted"])

    def test_no_allergy_declaration_stays_empty(self):
        view = redact_health(kid("p1", 10), MERCHANT)
        self.assertEqual(view["allergy_declarations"], [])
        self.assertEqual(view["allergy_count"], 0)


class DailyReportTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service(now="2026-10-02T08:00:00+08:00")
        seed_world(self.svc)

    def _session(self, sid, **kw):
        schedule(self.svc, MERCHANT, session_id=sid, **kw)

    def test_report_explains_restricted_denied_released(self):
        # s1：过敏限制；s2：容量取消；s3：正常放行后冻结再恢复
        self._session("s1")
        self.svc.enroll(MERCHANT, "s1", kid("s1-ok", 12))
        self.svc.enroll(MERCHANT, "s1", kid("s1-all", 11, allergies=["sandalwood"]))
        self.svc.clear_session(OFFICER, "s1")

        self._session("s2", venue="v-small")
        for i in range(3):
            self.svc.enroll(MERCHANT, "s2", kid(f"s2-{i}", 12))
        self.svc.clear_session(OFFICER, "s2")

        self._session("s3", start="2026-10-02T14:00:00+08:00",
                      end="2026-10-02T15:00:00+08:00")
        self.svc.enroll(MERCHANT, "s3", kid("s3-p", 12))
        self.svc.clear_session(OFFICER, "s3")
        # s1 已签到开场（视为已开始），事故不再连带冻结它
        self.svc.check_in(MERCHANT, "s1", "s1-ok")
        self.svc.report_incident(OFFICER, {
            "incident_id": "inc-d", "session_id": "s3", "description": "复核"})
        self.svc.release_batch(OFFICER, "b-inc-001", note="检测合格")
        self.svc.release_session(OFFICER, "s3", note="恢复")

        report = daily_report(self.svc, "2026-10-02")
        self.assertEqual(report["summary"]["restricted"], 1)
        self.assertEqual(report["summary"]["denied"], 1)
        self.assertEqual(report["summary"]["frozen"], 1)
        self.assertEqual(report["summary"]["released"], 1)
        self.assertEqual(report["summary"]["cleared"], 2)  # s1 初次 + s3 恢复重签

        restricted = report["restricted"][0]
        self.assertEqual(restricted["session_id"], "s1")
        self.assertEqual(restricted["reasons"]["codes"]["allergy_conflict"], 1)
        self.assertTrue(
            any("过敏" in m for m in restricted["reasons"]["messages"]))

        denied = report["denied"][0]
        self.assertEqual(denied["session_id"], "s2")
        self.assertEqual(denied["reasons"]["session_level"]["capacity_exceeded"], 1)

        released = report["released"][0]
        self.assertEqual(released["session_id"], "s3")
        self.assertEqual(released["released_by"], "o-001")

    def test_report_flags_amended_session_pending_recheck(self):
        self._session("s1")
        self.svc.enroll(MERCHANT, "s1", kid("p1", 12))
        self.svc.clear_session(OFFICER, "s1")
        self.svc.amend_session(MERCHANT, "s1", {"venue_id": "v-small"})
        report = daily_report(self.svc, "2026-10-02")
        self.assertEqual(report["summary"]["amended_pending_recheck"], 1)
        entry = report["amended_pending_recheck"][0]
        self.assertEqual(entry["changed_fields"], ["venue_id"])
        self.assertIn("旧签字失效", entry["notice"])

    def test_report_empty_for_quiet_day(self):
        report = daily_report(self.svc, "2026-09-01")
        self.assertEqual(report["summary"], {
            "cleared": 0, "restricted": 0, "denied": 0,
            "frozen": 0, "released": 0, "amended_pending_recheck": 0,
        })

    def test_report_excludes_other_days(self):
        self.svc.clock.set("2026-10-03T08:30:00+08:00")
        schedule(
            self.svc, MERCHANT, session_id="s-future",
            start="2026-10-03T09:00:00+08:00", end="2026-10-03T10:00:00+08:00")
        self.svc.enroll(MERCHANT, "s-future", kid("p1", 12))
        self.svc.clear_session(OFFICER, "s-future")
        report = daily_report(self.svc, "2026-10-02")
        self.assertEqual(report["summary"]["cleared"], 0)
        report_next = daily_report(self.svc, "2026-10-03")
        self.assertEqual(report_next["summary"]["cleared"], 1)


if __name__ == "__main__":
    unittest.main()
