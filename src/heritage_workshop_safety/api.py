"""HTTP JSON API（标准库实现，无第三方依赖）。

鉴权约定：网关完成真实登录后透传 ``X-Actor-Id`` 与 ``X-Actor-Role``
（merchant / officer / admin）。本地开发形态下可信；生产部署必须置于
鉴权网关之后，不得直接暴露给商户终端。

响应中的参与者健康信息按角色脱敏；签到、领用、成品、报名支持
``client_token`` 幂等与 ``occurred_at`` 离线补传。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .daily import daily_report
from .errors import DomainError
from .security import Actor, Role, redact_decision, redact_health
from .traceability import trace_product


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HeritageSafety/0.1"

        # ---- 基础 ----------------------------------------------------- #
        def log_message(self, fmt, *args):  # 安静些
            return

        def _send(self, status: int, body) -> None:
            data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _ok(self, body) -> None:
            self._send(200, body)

        def _actor(self) -> Actor:
            actor_id = self.headers.get("X-Actor-Id", "")
            role_raw = self.headers.get("X-Actor-Role", "")
            if not actor_id or not role_raw:
                from .errors import PermissionDenied

                raise PermissionDenied("缺少 X-Actor-Id / X-Actor-Role 头")
            try:
                role = Role(role_raw)
            except ValueError:
                from .errors import PermissionDenied

                raise PermissionDenied(f"未知角色: {role_raw}")
            return Actor(actor_id=actor_id, role=role,
                         display_name=self.headers.get("X-Actor-Name", ""))

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise DomainError(f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return data

        def _opts(self, body: dict) -> dict:
            return {
                "client_token": body.get("client_token"),
                "occurred_at": body.get("occurred_at"),
            }

        def _redact_findings_list(self, findings: list[dict], actor: Actor) -> list[dict]:
            return redact_decision({"findings": findings}, actor)["findings"]

        # ---- 路由 ----------------------------------------------------- #
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            try:
                actor = self._actor()
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                self._route(method, path, query, actor)
            except DomainError as exc:
                status = getattr(exc, "http_status", 400)
                payload = {"error": getattr(exc, "code", "domain_error"),
                           "message": str(exc)}
                if isinstance(exc.args and exc.args[0], dict):
                    payload["detail"] = exc.args[0]
                self._send(status, payload)
            except Exception as exc:  # noqa: BLE001 - 兜底，不让连接裸奔
                self._send(500, {"error": "internal_error", "message": str(exc)})

        def _route(self, method, path, query, actor: Actor):
            p = path.split("/")
            body = self._body() if method == "POST" else {}

            if method == "GET" and path == "/healthz":
                return self._ok({"status": "ok"})

            if method == "POST" and path == "/crafts":
                return self._ok(service.register_craft(actor, body))
            if method == "POST" and path == "/materials":
                return self._ok(service.receive_material(actor, body))
            if method == "POST" and path == "/instructors":
                return self._ok(service.register_instructor(actor, body))
            if method == "POST" and path == "/venues":
                return self._ok(service.register_venue(actor, body))

            if method == "POST" and path == "/sessions":
                return self._ok(service.schedule_session(actor, body))
            if method == "GET" and path == "/sessions":
                sessions = service.list_sessions(day=(query.get("day") or [None])[0])
                if not actor.can_view_health:
                    for view in sessions:
                        for c in view.get("clearances", []):
                            c["findings"] = self._redact_findings_list(
                                c.get("findings", []), actor)
                return self._ok({"sessions": sessions})

            m = re.fullmatch(r"/sessions/([^/]+)", path)
            if m:
                sid = m.group(1)
                if method == "GET":
                    return self._session_view(sid, actor)

            m = re.fullmatch(r"/sessions/([^/]+)/amend", path)
            if m and method == "POST":
                return self._ok(service.amend_session(actor, m.group(1), body))

            m = re.fullmatch(r"/sessions/([^/]+)/enrollments", path)
            if m and method == "POST":
                result = service.enroll(actor, m.group(1), body, **self._opts(body))
                return self._ok(result)

            m = re.fullmatch(r"/sessions/([^/]+)/clearance/preview", path)
            if m and method == "GET":
                decision = service.preview_clearance(actor, m.group(1))
                return self._ok(redact_decision(decision, actor))

            m = re.fullmatch(r"/sessions/([^/]+)/clearance", path)
            if m and method == "POST":
                result = service.clear_session(actor, m.group(1))
                result["decision"] = redact_decision(result["decision"], actor)
                return self._ok(result)

            m = re.fullmatch(r"/sessions/([^/]+)/checkins", path)
            if m and method == "POST":
                result = service.check_in(
                    actor, m.group(1), body["participant_id"], **self._opts(body))
                return self._ok(result)

            m = re.fullmatch(r"/sessions/([^/]+)/issues", path)
            if m and method == "POST":
                result = service.issue_material(
                    actor, m.group(1), body["participant_id"], body["batch_id"],
                    quantity=body.get("quantity", 1), **self._opts(body))
                return self._ok(result)

            m = re.fullmatch(r"/sessions/([^/]+)/reassess", path)
            if m and method == "GET":
                decisions = service.assess_frozen_sessions(actor)
                return self._ok(
                    {"sessions": [redact_decision(d, actor) for d in decisions]})

            m = re.fullmatch(r"/sessions/([^/]+)/release", path)
            if m and method == "POST":
                result = service.release_session(
                    actor, m.group(1), body.get("note", ""),
                    changes=body.get("changes"))
                result["decision"] = redact_decision(result["decision"], actor)
                return self._ok(result)

            if method == "POST" and path == "/products":
                return self._ok(service.register_product(actor, body, **self._opts(body)))

            m = re.fullmatch(r"/products/([^/]+)/trace", path)
            if m and method == "GET":
                if not actor.can_trace:
                    from .errors import PermissionDenied

                    raise PermissionDenied("仅安全管理人员/管理员可执行成品追溯")
                return self._ok(trace_product(service, m.group(1)))

            if method == "POST" and path == "/incidents":
                return self._ok(service.report_incident(actor, body))

            m = re.fullmatch(r"/batches/([^/]+)/release", path)
            if m and method == "POST":
                return self._ok(
                    service.release_batch(actor, m.group(1), body.get("note", "")))

            if method == "GET" and path == "/reports/daily":
                return self._daily_view(query, actor)

            self._send(404, {"error": "not_found", "message": f"无此端点: {path}"})

        # ---- 视图 ----------------------------------------------------- #
        def _session_view(self, session_id: str, actor: Actor):
            session = service.get_session(session_id)
            view = session.to_payload()
            participants = [
                redact_health(p.to_payload(), actor)
                for p in service.projection.participants_of(session_id)
            ]
            view["participants"] = participants
            # 签字历史同样按角色脱敏，避免健康信息从 clearances 泄漏
            redacted_clearances = []
            for c in view["clearances"]:
                c = dict(c)
                c["findings"] = self._redact_findings_list(c["findings"], actor)
                redacted_clearances.append(c)
            view["clearances"] = redacted_clearances
            if session.latest_clearance:
                clearance = session.latest_clearance.to_payload()
                clearance["findings"] = self._redact_findings_list(
                    clearance["findings"], actor)
                view["latest_clearance"] = clearance
            view["checked_in_participant_ids"] = sorted(
                pid for sid, pid in service.projection.checkins if sid == session_id)
            self._ok(view)

        def _daily_view(self, query, actor: Actor):
            day = (query.get("day") or [None])[0]
            report = daily_report(service, day)
            if not actor.can_view_health:
                for section in ("cleared", "restricted", "denied", "released"):
                    for entry in report.get(section, []):
                        redacted = self._redact_findings_list(
                            entry.get("findings", []), actor)
                        entry["findings"] = redacted
                        entry["reasons"]["messages"] = [
                            f.get("message", "") for f in redacted]
                report["health_redacted"] = True
            self._ok(report)

    return Handler


def create_server(service, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.daemon_threads = True
    return server


def serve(service, host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover
    httpd = create_server(service, host, port)
    print(f"非遗体验安全放行服务监听 http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
