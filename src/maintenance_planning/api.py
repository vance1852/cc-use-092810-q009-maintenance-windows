"""无第三方依赖的维修计划 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import MaintenanceError, ValidationFailed
from .service import MaintenanceService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: MaintenanceService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)

            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/evidence":
                return Response(201, self.service.record_evidence(actor, payload))
            if method == "POST" and path == "/rulebooks":
                return Response(201, self.service.publish_rulebook(actor, payload))
            if method == "POST" and path == "/activities":
                return Response(201, self.service.register_activity(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "batteries" and parts[2] == "activity":
                return Response(200, self.service.assign_activity(actor, parts[1], payload["activity_id"]))
            if method == "POST" and path == "/crews":
                return Response(201, self.service.register_crew(
                    actor, payload["crew_id"], payload["display_name"],
                    int(payload["size"]), payload.get("certifications", [])))
            if method == "POST" and path == "/spares":
                return Response(200, self.service.upsert_spare_stock(
                    actor, payload["spare_kind"], int(payload["available_quantity"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "facilities" and parts[2] == "quota":
                return Response(200, self.service.set_facility_quota(
                    actor, parts[1], payload["service_date"], payload["shutdown_quota_kwh"]))
            if method == "GET" and path == "/risk/backlog":
                return Response(200, self.service.risk_backlog(
                    actor, query.get("rulebook_id", [""])[0], int(query.get("version", ["0"])[0])))
            if method == "POST" and path == "/plans/generate":
                return Response(200, self.service.generate_plan(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "plans":
                return Response(200, self.service.get_plan(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "approve":
                return Response(200, self.service.approve_plan(
                    actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and path == "/health-events":
                return Response(201, self.service.record_health_event(actor, payload))
            if method == "POST" and path == "/evidence/refresh-invalidations":
                return Response(200, self.service.refresh_evidence_invalidations(actor))
            if method == "POST" and len(parts) == 4 and parts[0] == "windows" and parts[2] == "receipts":
                return Response(200, self.service.record_receipt(
                    actor, int(parts[1]), parts[3], str(payload["client_receipt_key"]), payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "windows" and parts[2] == "postponements":
                return Response(201, self.service.request_postponement(
                    actor, int(parts[1]), payload["requested_date"],
                    payload["new_latest_date"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "postponements" and parts[2] == "resolve":
                return Response(200, self.service.resolve_postponement(
                    actor, int(parts[1]), bool(payload["approve"]), str(payload.get("note", ""))))
            if method == "GET" and len(parts) == 2 and parts[0] == "windows":
                return Response(200, self.service.window_detail(actor, int(parts[1])))
            if method == "GET" and path == "/operations/dashboard":
                plan_id = query.get("plan_id", [None])[0]
                return Response(200, self.service.operations_dashboard(actor, plan_id))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except MaintenanceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MaintenancePlanner/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动储能电池维修计划服务")
    parser.add_argument("--database", type=Path, default=Path("maintenance_planning.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(MaintenanceService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
