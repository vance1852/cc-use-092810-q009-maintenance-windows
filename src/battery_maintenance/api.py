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
from .service import MaintenancePlanService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: MaintenancePlanService) -> None:
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
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/stations":
                return Response(201, self.service.register_station(actor, payload))
            if method == "POST" and path == "/devices":
                return Response(201, self.service.register_device(actor, payload))
            if method == "POST" and path == "/technicians":
                return Response(201, self.service.register_technician(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "technicians" and parts[2] == "unavailability":
                return Response(201, self.service.add_unavailability(actor, parts[1], payload))
            if method == "POST" and path == "/bays":
                return Response(201, self.service.register_bay(actor, payload))
            if method == "POST" and path == "/spare-parts":
                return Response(201, self.service.restock_part(actor, payload))
            if method == "POST" and path == "/evidence":
                return Response(201, self.service.record_evidence(actor, payload))
            if method == "POST" and path == "/risk-rule-sets":
                return Response(201, self.service.publish_rule_set(actor, payload))
            if method == "POST" and path == "/plans":
                return Response(201, self.service.generate_plan(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "approve":
                return Response(200, self.service.approve_plan(actor, parts[1], int(payload["expected_revision"])))
            if method == "GET" and len(parts) == 2 and parts[0] == "plans":
                return Response(200, self.service.plan_detail(actor, parts[1]))
            if method == "POST" and path == "/signals":
                return Response(201, self.service.record_signal(
                    actor, payload["device_id"], payload["signal_kind"], payload.get("payload", {})))
            if method == "POST" and len(parts) == 3 and parts[0] == "windows" and parts[2] == "receipts":
                return Response(201, self.service.receive_receipt(
                    actor, parts[1], payload["event"], payload.get("idempotency_key"),
                    payload.get("payload", {})))
            if method == "POST" and len(parts) == 3 and parts[0] == "windows" and parts[2] == "extend":
                return Response(201, self.service.extend_window(
                    actor, parts[1], payload["new_latest_date"], payload["reason"],
                    payload["risk_acceptor_id"]))
            if method == "GET" and path == "/operations/view":
                station = query.get("station_id", [None])[0]
                return Response(200, self.service.operations_view(actor, station))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except MaintenanceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BatteryMaintenance/1"

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
    parser = argparse.ArgumentParser(description="启动储能电池检修计划服务")
    parser.add_argument("--database", type=Path, default=Path("battery_maintenance.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(MaintenancePlanService(connection))))
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
