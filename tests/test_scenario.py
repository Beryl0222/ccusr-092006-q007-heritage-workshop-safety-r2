"""端到端场景：合香过敏儿童拦截、事故冻结换场恢复、成品追溯与日报。"""

import unittest
from datetime import timedelta

from src.heritage_workshop_safety import daily_report, session_safety_view, trace_product

from tests.factories import (
    DAY,
    OFFICER,
    OPERATOR,
    START,
    TEACHER,
    build_service,
    enroll,
    schedule_incense,
)


class EndToEndScenarioTest(unittest.TestCase):
    def test_full_day_story(self):
        svc = build_service()

        # 1) 上午合香场：一名申报檀香过敏的儿童报名，另一名正常
        schedule_incense(svc, "S-MORNING")
        enroll(svc, "S-MORNING", "kid-allergy", birth="2015-03-01", allergies=["SANDALWOOD"], name="过敏儿童")
        enroll(svc, "S-MORNING", "kid-ok", birth="2016-09-01", name="正常儿童")

        # 2) 管理人员评估：因过敏冲突受限，商户视图看不到过敏儿童是谁
        svc.clear_session(OFFICER, "S-MORNING")
        merchant_view = session_safety_view(svc.repo, "S-MORNING", OPERATOR)
        self.assertEqual(merchant_view["decision"], "RESTRICTED")
        self.assertEqual(merchant_view["allergy_conflicts"], 1)
        self.assertNotIn("过敏儿童", str(merchant_view))

        # 3) 更换为无过敏原的花香粉批次，相关检查重新生效并重新放行
        svc.change_assignment(OPERATOR, session_id="S-MORNING", batch_ids=["B-INC-02"])
        svc.clear_session(OFFICER, "S-MORNING")
        self.assertIsNotNone(svc.repo.get_session("S-MORNING").valid_clearance())

        # 4) 开场签到、领用、成品带走
        at_open = START + timedelta(minutes=15)
        for pid in ("kid-allergy", "kid-ok"):
            svc.check_in(TEACHER, session_id="S-MORNING", participant_id=pid, at=at_open)
            svc.issue_material(
                TEACHER, session_id="S-MORNING", batch_id="B-INC-02",
                participant_id=pid, at=at_open + timedelta(minutes=10),
            )
        svc.record_product(
            TEACHER, product_id="PR-M-1",
            session_id="S-MORNING", participant_id="kid-allergy",
        )

        # 5) 下午还有一场配檀香粉：先放行
        schedule_incense(svc, "S-AFTERNOON")
        enroll(svc, "S-AFTERNOON", "kid-pm", birth="2017-01-01")
        svc.clear_session(OFFICER, "S-AFTERNOON")
        self.assertIsNotNone(svc.repo.get_session("S-AFTERNOON").valid_clearance())

        # 6) 上午场事后报告皮疹（与檀香无关、按流程上报关联批次后改判由评估环节处理）。
        #    这里演示：事故涉及 B-INC-02，同批次未开始场次不存在，下午场配 B-INC-01 不受影响；
        #    再对 B-INC-01 直接出事故，下午场立即冻结。
        svc.report_incident(
            TEACHER, incident_id="I-M", session_id="S-MORNING", batch_id="B-INC-02",
            occurred_at=at_open + timedelta(hours=1), description="离园后反馈不适，待评估",
        )
        self.assertIsNotNone(svc.repo.get_session("S-AFTERNOON").valid_clearance())
        schedule_incense(svc, "S-EXTRA")
        enroll(svc, "S-EXTRA", "kid-x", birth="2017-01-01")
        svc.clear_session(OFFICER, "S-EXTRA")
        svc.report_incident(
            TEACHER, incident_id="I-A", session_id="S-AFTERNOON", batch_id="B-INC-01",
            occurred_at=START + timedelta(hours=3), description="香粉异味",
        )
        # 同批次未开始场次：S-AFTERNOON（未签到）与 S-EXTRA 的签批都被冻结作废
        self.assertIsNone(svc.repo.get_session("S-AFTERNOON").valid_clearance())
        self.assertIsNone(svc.repo.get_session("S-EXTRA").valid_clearance())

        # 7) 有权人员评估：檀香批次复检合格解冻，相关场次重新评估放行
        svc.release_batch(OFFICER, batch_id="B-INC-01", assessment="第三方检测合格，恢复使用")
        svc.clear_session(OFFICER, "S-AFTERNOON")
        self.assertIsNotNone(svc.repo.get_session("S-AFTERNOON").valid_clearance())

        # 8) 从一件成品追到指导者与材料来源
        trace = trace_product(svc.repo, "PR-M-1", OFFICER)
        self.assertEqual(trace["materials"][0]["batch_id"], "B-INC-02")
        self.assertEqual(trace["session"]["session_id"], "S-MORNING")
        self.assertEqual([i["instructor_id"] for i in trace["instructors"]], ["teacher-2"])

        # 9) 当天日报：限制、重新放行、解冻批次齐全
        report = daily_report(svc.repo, DAY)
        self.assertEqual({r["session_id"] for r in report["restricted"]}, {"S-MORNING"})
        recleared = {r["session_id"]: r["reopened_after"] for r in report["recleared"]}
        self.assertIn("S-MORNING", recleared)
        self.assertIn("S-AFTERNOON", recleared)
        released = {r["batch_id"] for r in report["released_batches"]}
        self.assertEqual(released, {"B-INC-01"})


if __name__ == "__main__":
    unittest.main()
