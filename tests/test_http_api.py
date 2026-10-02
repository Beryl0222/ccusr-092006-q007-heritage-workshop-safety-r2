"""HTTP API 端到端测试（真实 socket 上起线程服务）。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from src.heritage_workshop_safety.api import make_handler
from world import ADMIN, MERCHANT, OFFICER, build_service, kid, seed_world


class ApiClient:
    def __init__(self, base: str, actor):
        self.base = base
        self.headers = {
            "Content-Type": "application/json",
            "X-Actor-Id": actor.actor_id,
            "X-Actor-Role": actor.role.value,
        }

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path, body)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = build_service()
        seed_world(cls.svc)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.svc))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.merchant = ApiClient(self.base, MERCHANT)
        self.officer = ApiClient(self.base, OFFICER)
        self.admin = ApiClient(self.base, ADMIN)

    def test_healthz(self):
        status, body = self.merchant.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_actor_headers_required(self):
        req = urllib.request.Request(self.base + "/healthz", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_full_flow_restriction_redaction_trace_and_report(self):
        # 商户排期、报名（一名过敏儿童）
        status, _ = self.merchant.post("/sessions", {
            "session_id": "h1", "craft_code": "incense", "version": "1",
            "venue_id": "v-hall-a", "instructor_id": "t-lin",
            "batch_ids": ["b-inc-001"],
            "scheduled_start": "2026-10-02T09:00:00+08:00",
            "scheduled_end": "2026-10-02T10:30:00+08:00",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.merchant.post("/sessions/h1/enrollments", {
            **kid("p1", 12), "client_token": "e-p1"})[0], 200)
        self.assertEqual(self.merchant.post("/sessions/h1/enrollments", {
            **kid("p2", 11, allergies=["sandalwood"]), "client_token": "e-p2"})[0], 200)

        # 商户不能签字
        status, body = self.merchant.post("/sessions/h1/clearance", {})
        self.assertEqual(status, 403)

        # 安全员签字：限制放行
        status, body = self.officer.post("/sessions/h1/clearance", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["outcome"], "restricted")

        # 商户预检：findings 与嵌套 excluded 都不得出现具体过敏原
        status, preview = self.merchant.get("/sessions/h1/clearance/preview")
        self.assertEqual(status, 200)
        self.assertNotIn("sandalwood", json.dumps(preview, ensure_ascii=False))
        self.assertEqual(preview["excluded"]["p2"][0]["code"], "allergy_conflict")

        # 商户视图：看不到具体过敏原
        status, merchant_view = self.merchant.get("/sessions/h1")
        self.assertEqual(status, 200)
        allergies = {p["participant_id"]: p["allergy_declarations"]
                     for p in merchant_view["participants"]}
        self.assertEqual(allergies["p2"], ["***"])
        self.assertNotIn("sandalwood", json.dumps(merchant_view, ensure_ascii=False))

        # 安全员视图：可见完整健康信息
        _, officer_view = self.officer.get("/sessions/h1")
        self.assertIn(
            "sandalwood",
            [a for p in officer_view["participants"]
             for a in p["allergy_declarations"]])

        # 限制名单儿童不能签到
        status, body = self.merchant.post("/sessions/h1/checkins",
                                          {"participant_id": "p2"})
        self.assertEqual(status, 409)
        status, body = self.merchant.post("/sessions/h1/checkins",
                                          {"participant_id": "p1",
                                           "client_token": "ck-1"})
        self.assertEqual(status, 200)
        # 幂等补传
        status, body2 = self.merchant.post("/sessions/h1/checkins", {
            "participant_id": "p1", "client_token": "ck-1",
            "occurred_at": "2026-10-02T09:05:00+08:00"})
        self.assertEqual(status, 200)
        self.assertTrue(body2["deduplicated"])

        # 领用 + 成品
        status, _ = self.merchant.post(
            "/sessions/h1/issues",
            {"participant_id": "p1", "batch_id": "b-inc-001",
             "client_token": "iss-1"})
        self.assertEqual(status, 200)
        status, _ = self.merchant.post("/products", {
            "product_id": "prod-h1", "session_id": "h1",
            "participant_id": "p1", "name": "合香牌"})
        self.assertEqual(status, 200)

        # 商户不能追溯；安全员可以，链完整
        status, _ = self.merchant.get("/products/prod-h1/trace")
        self.assertEqual(status, 403)
        status, trace = self.officer.get("/products/prod-h1/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["instructor"]["instructor_id"], "t-lin")
        self.assertEqual(trace["material_batches"][0]["batch_id"], "b-inc-001")
        self.assertEqual(trace["material_batches"][0]["supplier"], "云香坊")

        # 日报：商户拿不到过敏原字样，安全员拿得到
        _, merchant_report = self.merchant.get("/reports/daily?day=2026-10-02")
        self.assertTrue(merchant_report.get("health_redacted"))
        self.assertNotIn("sandalwood", json.dumps(merchant_report, ensure_ascii=False))
        _, officer_report = self.officer.get("/reports/daily?day=2026-10-02")
        self.assertIn("sandalwood", json.dumps(officer_report, ensure_ascii=False))
        self.assertEqual(officer_report["summary"]["restricted"], 1)

    def test_incident_freeze_and_release_flow(self):
        self.merchant.post("/sessions", {
            "session_id": "h2", "craft_code": "incense", "version": "1",
            "venue_id": "v-hall-a", "instructor_id": "t-lin",
            "batch_ids": ["b-inc-001"],
            "scheduled_start": "2026-10-02T13:00:00+08:00",
            "scheduled_end": "2026-10-02T14:00:00+08:00",
        })
        self.merchant.post("/sessions/h2/enrollments", kid("p1", 12))
        self.officer.post("/sessions/h2/clearance", {})

        status, body = self.merchant.post("/incidents", {
            "incident_id": "inc-h", "session_id": "h2", "description": "皮疹"})
        self.assertEqual(status, 403)

        status, body = self.officer.post("/incidents", {
            "incident_id": "inc-h", "session_id": "h2", "description": "皮疹"})
        self.assertEqual(status, 200)
        self.assertIn("h2", body["frozen_session_ids"])

        # 冻结期间签到被拒
        status, _ = self.merchant.post(
            "/sessions/h2/checkins", {"participant_id": "p1"})
        self.assertEqual(status, 409)

        status, _ = self.officer.post(
            "/batches/b-inc-001/release", {"note": "检测合格"})
        self.assertEqual(status, 200)
        status, body = self.officer.post(
            "/sessions/h2/release", {"note": "复检通过"})
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["outcome"], "cleared")

        status, view = self.officer.get("/sessions/h2")
        self.assertEqual(view["status"], "released")
        status, _ = self.merchant.post(
            "/sessions/h2/checkins", {"participant_id": "p1"})
        self.assertEqual(status, 200)

    def test_amend_forces_reclearance(self):
        self.merchant.post("/sessions", {
            "session_id": "h3", "craft_code": "incense", "version": "1",
            "venue_id": "v-hall-a", "instructor_id": "t-lin",
            "batch_ids": ["b-inc-001"],
            "scheduled_start": "2026-10-02T15:00:00+08:00",
            "scheduled_end": "2026-10-02T16:00:00+08:00",
        })
        self.merchant.post("/sessions/h3/enrollments", kid("p1", 12))
        self.officer.post("/sessions/h3/clearance", {})
        # 换无檀香批次
        status, body = self.merchant.post("/sessions/h3/amend",
                                          {"batch_ids": ["b-inc-002"]})
        self.assertEqual(status, 200)
        # 旧签字失效，签到被拒
        status, _ = self.merchant.post(
            "/sessions/h3/checkins", {"participant_id": "p1"})
        self.assertEqual(status, 409)
        # 重新签字后可签到
        status, body = self.officer.post("/sessions/h3/clearance", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["outcome"], "cleared")
        status, _ = self.merchant.post(
            "/sessions/h3/checkins", {"participant_id": "p1"})
        self.assertEqual(status, 200)

    def test_validation_error_shape(self):
        status, body = self.merchant.post("/sessions", {"session_id": "bad"})
        self.assertEqual(status, 422)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
