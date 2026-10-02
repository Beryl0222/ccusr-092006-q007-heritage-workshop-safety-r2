"""放行规则、基线失效、幂等、事故冻结/恢复的领域测试。"""

from __future__ import annotations

import unittest

from src.heritage_workshop_safety import EventStore, FakeClock, SafetyService
from src.heritage_workshop_safety.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.heritage_workshop_safety.models import SessionStatus

from world import MERCHANT, OFFICER, NOW, build_service, kid, schedule, seed_world


class ClearanceRulesTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_world(self.svc)
        schedule(self.svc, MERCHANT)

    def _enroll(self, *kids):
        for k in kids:
            self.svc.enroll(MERCHANT, "s1", k)

    def test_happy_path_cleared(self):
        self._enroll(kid("p1", 12), kid("p2", 15))
        result = self.svc.clear_session(OFFICER, "s1")
        self.assertEqual(result["decision"]["outcome"], "cleared")
        self.assertEqual(result["decision"]["eligible_participant_ids"], ["p1", "p2"])
        self.assertTrue(result["decision"]["baseline"].startswith("sha256:"))

    def test_merchant_cannot_sign_clearance(self):
        with self.assertRaises(PermissionDenied):
            self.svc.clear_session(MERCHANT, "s1")

    def test_age_out_of_range_denies_individual_but_session_restricted(self):
        # 一名适龄儿童 + 一名 6 岁幼童（合香要求 10+）
        self._enroll(kid("p1", 12), kid("p2", 6))
        result = self.svc.clear_session(OFFICER, "s1")
        decision = result["decision"]
        self.assertEqual(decision["outcome"], "restricted")
        self.assertEqual(decision["eligible_participant_ids"], ["p1"])
        self.assertIn("p2", decision["excluded"])
        self.assertEqual(decision["excluded"]["p2"][0]["code"], "age_out_of_range")

    def test_all_participants_excluded_means_denied(self):
        self._enroll(kid("p1", 5), kid("p2", 6))
        result = self.svc.clear_session(OFFICER, "s1")
        self.assertEqual(result["decision"]["outcome"], "denied")
        self.assertEqual(result["decision"]["eligible_participant_ids"], [])

    def test_guardian_confirmation_required_for_minor(self):
        self._enroll(kid("p1", 12, guardian=False))
        result = self.svc.clear_session(OFFICER, "s1")
        self.assertEqual(result["decision"]["outcome"], "denied")
        self.assertEqual(
            result["decision"]["findings"][0]["code"], "guardian_unconfirmed")

    def test_allergy_conflict_excludes_child(self):
        # b-inc-001 含 sandalwood，儿童声明对檀香过敏
        self._enroll(
            kid("p1", 12),
            kid("p2", 11, allergies=["sandalwood"]),
        )
        result = self.svc.clear_session(OFFICER, "s1")
        self.assertEqual(result["decision"]["outcome"], "restricted")
        self.assertEqual(result["decision"]["eligible_participant_ids"], ["p1"])
        self.assertEqual(
            result["decision"]["excluded"]["p2"][0]["code"], "allergy_conflict")

    def test_capacity_exceeded_blocks_whole_session(self):
        schedule(self.svc, MERCHANT, session_id="s-small", venue="v-small")
        for i in range(3):
            self.svc.enroll(MERCHANT, "s-small", kid(f"p{i}", 12))
        result = self.svc.clear_session(OFFICER, "s-small")
        self.assertEqual(result["decision"]["outcome"], "denied")
        self.assertIn("capacity_exceeded", [f["code"] for f in result["decision"]["findings"]])

    def test_emergency_facility_required_by_hazard(self):
        # 扎染有 sharp_tool+dye，需要 first_aid_kit 与 eye_wash；小位都没有
        schedule(self.svc, MERCHANT, session_id="s-dye", craft="dye",
                 venue="v-small", instructor="t-zhou", batches=("b-dye-001",))
        self.svc.enroll(MERCHANT, "s-dye", kid("p1", 10))
        result = self.svc.clear_session(OFFICER, "s-dye")
        codes = set([f["code"] for f in result["decision"]["findings"]])
        self.assertIn("emergency_facility_missing", codes)
        self.assertEqual(result["decision"]["outcome"], "denied")

    def test_instructor_not_qualified(self):
        # 林师傅只会 incense/kite，让他带扎染
        schedule(self.svc, MERCHANT, session_id="s-wrong", craft="dye",
                 instructor="t-lin", batches=("b-dye-001",))
        self.svc.enroll(MERCHANT, "s-wrong", kid("p1", 10))
        result = self.svc.clear_session(OFFICER, "s-wrong")
        self.assertIn("instructor_not_qualified", [f["code"] for f in result["decision"]["findings"]])

    def test_expired_certificate_blocks(self):
        schedule(self.svc, MERCHANT, session_id="s-old", instructor="t-old")
        self.svc.enroll(MERCHANT, "s-old", kid("p1", 10))
        result = self.svc.clear_session(OFFICER, "s-old")
        self.assertIn("certificate_expired", [f["code"] for f in result["decision"]["findings"]])

    def test_expired_material_blocks(self):
        self.svc.receive_material(MERCHANT, {
            "batch_id": "b-stale", "material_code": "x", "name": "陈料",
            "received_at": "2025-01-01T09:00:00+08:00",
            "expires_at": "2026-01-01T09:00:00+08:00",
        })
        schedule(self.svc, MERCHANT, session_id="s-stale", batches=("b-stale",))
        self.svc.enroll(MERCHANT, "s-stale", kid("p1", 10))
        result = self.svc.clear_session(OFFICER, "s-stale")
        self.assertIn("batch_expired", [f["code"] for f in result["decision"]["findings"]])

    def test_restricted_child_cannot_check_in(self):
        self._enroll(kid("p1", 12), kid("p2", 6))
        self.svc.clear_session(OFFICER, "s1")
        with self.assertRaises(ConflictError):
            self.svc.check_in(MERCHANT, "s1", "p2")
        ok = self.svc.check_in(MERCHANT, "s1", "p1")
        self.assertFalse(ok["deduplicated"])

    def test_cannot_check_in_before_clearance(self):
        self._enroll(kid("p1", 12))
        with self.assertRaises(ConflictError):
            self.svc.check_in(MERCHANT, "s1", "p1")

    def test_late_walkin_requires_reclearance(self):
        # 随到随学：先放行了 p1，p3 后到报名
        self._enroll(kid("p1", 12))
        self.svc.clear_session(OFFICER, "s1")
        self.svc.enroll(MERCHANT, "s1", kid("p3", 13))
        with self.assertRaises(ConflictError):
            self.svc.check_in(MERCHANT, "s1", "p3")
        # 重新评估签字后，p3 进入放行名单；基线随参与者变化
        self.svc.clear_session(OFFICER, "s1")
        ok = self.svc.check_in(MERCHANT, "s1", "p3")
        self.assertFalse(ok["deduplicated"])


class BaselineInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_world(self.svc)
        schedule(self.svc, MERCHANT)
        self.svc.enroll(MERCHANT, "s1", kid("p1", 12))
        self.svc.clear_session(OFFICER, "s1")
        self.baseline = self.svc.get_session("s1").latest_clearance.baseline

    def test_change_instructor_voids_signature(self):
        self.svc.register_instructor(MERCHANT, {
            "instructor_id": "t-lin2", "name": "林二师傅",
            "qualified_crafts": ["incense"],
            "certificates": {"incense_safety": "2027-12-31T23:59:59+08:00"},
        })
        self.svc.amend_session(MERCHANT, "s1", {"instructor_id": "t-lin2"})
        session = self.svc.get_session("s1")
        self.assertEqual(session.status, SessionStatus.SCHEDULED)
        from src.heritage_workshop_safety.rules import signature_current

        self.assertFalse(signature_current(self.svc.projection, session))
        # 重新评估得到新基线
        result = self.svc.clear_session(OFFICER, "s1")
        self.assertNotEqual(result["decision"]["baseline"], self.baseline)
        self.assertEqual(session.status, SessionStatus.CLEARED)

    def test_change_material_voids_signature_and_rechecks_allergy(self):
        # 换成不含檀香的批次后，过敏儿童可入场
        self.svc.enroll(MERCHANT, "s1", kid("p2", 11, allergies=["sandalwood"]))
        result_before = self.svc.clear_session(OFFICER, "s1")
        self.assertEqual(result_before["decision"]["outcome"], "restricted")

        self.svc.amend_session(MERCHANT, "s1", {"batch_ids": ["b-inc-002"]})
        with self.assertRaises(ConflictError):
            # 旧签字失效，未重新放行不能签到
            self.svc.check_in(MERCHANT, "s1", "p1")
        result_after = self.svc.clear_session(OFFICER, "s1")
        self.assertEqual(result_after["decision"]["outcome"], "cleared")

    def test_change_venue_voids_signature(self):
        self.svc.amend_session(MERCHANT, "s1", {"venue_id": "v-small"})
        self.assertEqual(self.svc.get_session("s1").status, SessionStatus.SCHEDULED)
        result = self.svc.clear_session(OFFICER, "s1")
        # v-small 仅 2 工位，1 人不超员，但合香无危险工序，故仍可放行
        self.assertEqual(result["decision"]["outcome"], "cleared")


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        seed_world(self.svc)
        schedule(self.svc, MERCHANT)
        self.svc.enroll(MERCHANT, "s1", kid("p1", 12))
        self.svc.clear_session(OFFICER, "s1")

    def test_checkin_recorded_once_with_client_token(self):
        first = self.svc.check_in(MERCHANT, "s1", "p1",
                                  client_token="dev-abc", occurred_at="2026-10-02T09:05:00+08:00")
        second = self.svc.check_in(MERCHANT, "s1", "p1", client_token="dev-abc")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["event"]["event_id"], second["event"]["event_id"])
        # 事件流里只有一条签到
        checkins = [e for e in self.svc.store.stream() if e["kind"] == "CHECKIN_RECORDED"]
        self.assertEqual(len(checkins), 1)
        self.assertEqual(checkins[0]["payload"]["client_occurred_at"],
                         "2026-10-02T09:05:00+08:00")

    def test_checkin_idempotent_even_without_token(self):
        self.svc.check_in(MERCHANT, "s1", "p1")
        again = self.svc.check_in(MERCHANT, "s1", "p1")
        self.assertTrue(again["deduplicated"])

    def test_material_issue_once_per_participant_batch(self):
        self.svc.issue_material(OFFICER, "s1", "p1", "b-inc-001",
                                client_token="iss-1", quantity=1)
        again = self.svc.issue_material(OFFICER, "s1", "p1", "b-inc-001",
                                        client_token="iss-1")
        self.assertTrue(again["deduplicated"])
        different_token = self.svc.issue_material(
            OFFICER, "s1", "p1", "b-inc-001", client_token="iss-2")
        # 业务键相同，即使换 token 也只能记一次
        self.assertTrue(different_token["deduplicated"])

    def test_offline_backfill_keeps_client_time(self):
        # 模拟现场断网，事后补传
        result = self.svc.check_in(
            MERCHANT, "s1", "p1", client_token="offline-1",
            occurred_at="2026-10-02T09:03:00+08:00")
        self.assertFalse(result["deduplicated"])
        self.assertEqual(
            result["event"]["payload"]["client_occurred_at"],
            "2026-10-02T09:03:00+08:00")

    def test_store_rejects_duplicate_event_id(self):
        with self.assertRaises(ConflictError):
            self.svc.store.append(
                "CHECKIN_RECORDED", "x", {"dupe": True}, event_id="fixed-id")
            self.svc.store.append(
                "CHECKIN_RECORDED", "x", {"dupe": True}, event_id="fixed-id")


class IncidentFreezeTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service(now="2026-10-02T08:00:00+08:00")
        seed_world(self.svc)
        # s1：上午场（未开始）；s2：下午场（未开始，同批次）；s3：其他批次
        schedule(self.svc, MERCHANT, session_id="s1",
                 start="2026-10-02T09:00:00+08:00", end="2026-10-02T10:00:00+08:00")
        schedule(self.svc, MERCHANT, session_id="s2",
                 start="2026-10-02T14:00:00+08:00", end="2026-10-02T15:00:00+08:00")
        schedule(self.svc, MERCHANT, session_id="s3", batches=("b-inc-002",),
                 start="2026-10-02T15:00:00+08:00", end="2026-10-02T16:00:00+08:00")
        for sid in ("s1", "s2", "s3"):
            self.svc.enroll(MERCHANT, sid, kid(f"{sid}-p", 12))
            self.svc.clear_session(OFFICER, sid)

    def test_incident_freezes_same_batch_not_started_sessions(self):
        report = self.svc.report_incident(OFFICER, {
            "incident_id": "inc-1", "session_id": "s1",
            "description": "有儿童接触香粉后皮疹", "severity": "health",
        })
        self.assertEqual(sorted(report["frozen_session_ids"]), ["s1", "s2"])
        self.assertEqual(self.svc.get_session("s3").status, SessionStatus.CLEARED)
        self.assertEqual(self.svc.projection.batches["b-inc-001"].status, "suspended")
        # 冻结场次禁止签到与领用
        with self.assertRaises(ConflictError):
            self.svc.check_in(MERCHANT, "s2", "s2-p")
        with self.assertRaises(ConflictError):
            self.svc.issue_material(MERCHANT, "s2", "s2-p", "b-inc-001")

    def test_started_session_excluded_from_freeze_and_checked_in_one_kept(self):
        # s1 已有签到（视为已开始），事故不再自动冻结它
        self.svc.check_in(MERCHANT, "s1", "s1-p")
        report = self.svc.report_incident(OFFICER, {
            "incident_id": "inc-2", "session_id": "s1",
            "description": "已开场后的擦伤",
        })
        self.assertEqual(report["frozen_session_ids"], ["s2"])
        self.assertEqual(self.svc.get_session("s1").status, SessionStatus.CLEARED)

    def test_merchant_cannot_report_incident(self):
        with self.assertRaises(PermissionDenied):
            self.svc.report_incident(MERCHANT, {
                "incident_id": "inc-x", "session_id": "s1", "description": "x"})

    def test_release_flow_batch_then_session(self):
        self.svc.report_incident(OFFICER, {
            "incident_id": "inc-3", "session_id": "s1", "description": "皮疹"})
        # 批次未解冻前，场次不能恢复
        with self.assertRaises(ConflictError):
            self.svc.release_session(OFFICER, "s2", note="误报")
        # 商户无权解冻
        with self.assertRaises(PermissionDenied):
            self.svc.release_batch(MERCHANT, "b-inc-001")
        self.svc.release_batch(OFFICER, "b-inc-001", note="检测合格")
        result = self.svc.release_session(OFFICER, "s2", note="复检通过，恢复")
        self.assertEqual(result["decision"]["outcome"], "cleared")
        self.assertEqual(self.svc.get_session("s2").status, SessionStatus.RELEASED)
        self.assertEqual(self.svc.get_session("s2").released_by, "o-001")
        # 恢复后重新签字生效，可以签到
        self.svc.check_in(MERCHANT, "s2", "s2-p")

    def test_release_blocked_when_recheck_fails(self):
        self.svc.report_incident(OFFICER, {
            "incident_id": "inc-4", "session_id": "s1", "description": "皮疹"})
        self.svc.release_batch(OFFICER, "b-inc-001", note="合格")
        # 恢复前把场地换成缺应急设施且超员的小位，复检应失败
        # 冻结状态不允许 amend；改为直接给 s2 再报一个 3 人超员场景：
        # 这里用新增不合格报名 + 冻结下无法 amend，故验证决策失败路径：
        # 先解冻，再利用容量——给 s2 加人到超员需要先恢复状态，
        # 因此该失败路径在 HTTP/单元中以证书过期场景验证：
        # （讲师证书在事故后到期）
        self.svc.clock.set("2028-06-01T08:00:00+08:00")
        with self.assertRaises(ValidationError) as ctx:
            self.svc.release_session(OFFICER, "s2")
        self.assertEqual(ctx.exception.args[0]["message"], "重新评估未通过，场次维持冻结")
        self.assertEqual(self.svc.get_session("s2").status, SessionStatus.FROZEN)

    def test_frozen_session_cannot_be_amended(self):
        self.svc.report_incident(OFFICER, {
            "incident_id": "inc-5", "session_id": "s1", "description": "皮疹"})
        with self.assertRaises(ConflictError):
            self.svc.amend_session(MERCHANT, "s1", {"batch_ids": ["b-inc-002"]})

    def test_release_with_rebind_to_safe_batch(self):
        self.svc.report_incident(OFFICER, {
            "incident_id": "inc-6", "session_id": "s1", "description": "皮疹"})
        # 不解除旧批次，而是在恢复时换绑无檀香配方的批次
        result = self.svc.release_session(
            OFFICER, "s2", note="更换安全批次后恢复",
            changes={"batch_ids": ["b-inc-002"]})
        self.assertEqual(result["decision"]["outcome"], "cleared")
        session = self.svc.get_session("s2")
        self.assertEqual(session.batch_ids, ["b-inc-002"])
        self.assertEqual(session.status, SessionStatus.RELEASED)
        # 旧批次仍处于冻结，未被悄悄解冻
        self.assertEqual(self.svc.projection.batches["b-inc-001"].status, "suspended")

    def test_release_with_rebind_failure_keeps_frozen(self):
        self.svc.report_incident(OFFICER, {
            "incident_id": "inc-7", "session_id": "s1", "description": "皮疹"})
        # 换绑后仍有阻塞项：新批次不存在 -> 报错且无任何事件落库，场次维持冻结
        events_before = len(self.svc.store)
        with self.assertRaises(NotFoundError):
            self.svc.release_session(
                OFFICER, "s2", changes={"batch_ids": ["b-missing"]})
        self.assertEqual(len(self.svc.store), events_before)
        self.assertEqual(self.svc.get_session("s2").status, SessionStatus.FROZEN)
        self.assertEqual(self.svc.get_session("s2").batch_ids, ["b-inc-001"])


class TraceAndPersistenceTest(unittest.TestCase):
    def test_product_trace_chain(self):
        from src.heritage_workshop_safety import trace_product

        svc = build_service()
        seed_world(svc)
        schedule(svc, MERCHANT)
        svc.enroll(MERCHANT, "s1", kid("p1", 12, alias="乐乐"))
        svc.clear_session(OFFICER, "s1")
        svc.check_in(MERCHANT, "s1", "p1", client_token="ck-1")
        svc.issue_material(MERCHANT, "s1", "p1", "b-inc-001", client_token="is-1")
        svc.register_product(MERCHANT, {
            "product_id": "prod-1", "session_id": "s1",
            "participant_id": "p1", "name": "乐乐手作合香牌"})

        chain = trace_product(svc, "prod-1")
        self.assertEqual(chain["session"]["instructor_id"], "t-lin")
        self.assertEqual(chain["instructor"]["name"], "林师傅")
        self.assertEqual(chain["venue"]["venue_id"], "v-hall-a")
        self.assertEqual(chain["material_batches"][0]["supplier"], "云香坊")
        self.assertEqual(chain["material_batches"][0]["batch_id"], "b-inc-001")
        self.assertEqual(chain["material_issues"][0]["issue_event_id"],
                         svc.projection.material_issues[("s1", "p1", "b-inc-001")]["event_id"])

    def test_replay_from_jsonl(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            svc1 = build_service(path=str(path))
            seed_world(svc1)
            schedule(svc1, MERCHANT)
            svc1.enroll(MERCHANT, "s1", kid("p1", 12))
            svc1.clear_session(OFFICER, "s1")
            svc1.check_in(MERCHANT, "s1", "p1", client_token="ck-x")

            store2 = EventStore(str(path), clock=FakeClock(NOW))
            svc2 = SafetyService(store2)
            self.assertEqual(svc2.get_session("s1").status, SessionStatus.CLEARED)
            # 补传同一幂等键到重开的服务，仍然只记一次
            again = svc2.check_in(MERCHANT, "s1", "p1", client_token="ck-x")
            self.assertTrue(again["deduplicated"])
            checkins = [e for e in store2.stream() if e["kind"] == "CHECKIN_RECORDED"]
            self.assertEqual(len(checkins), 1)


if __name__ == "__main__":
    unittest.main()
