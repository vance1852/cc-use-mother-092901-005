"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .charter import CharterService
from .errors import DomainError, PermissionDenied, ValidationError
from .service import DomainService
from .storage import Database


def build_services(database: Database) -> tuple[DomainService, CharterService]:
    """在同一数据库上构造基础服务与旅游包车联审服务。"""

    return DomainService(database), CharterService(database)


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          charter: CharterService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    inspection_key = headers.get("X-Inspection-Key", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if charter is not None:
            result = _route_charter(charter, method, parsed.path, query, body,
                                    actor_id, inspection_key)
            if result is not None:
                return result
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _route_charter(charter: CharterService, method: str, path: str,
                   query: dict[str, list[str]], body: dict[str, Any],
                   actor_id: str, inspection_key: str) -> tuple[int, dict[str, Any]] | None:
    """分派旅游包车联审相关路由；未命中返回 None。"""

    if method == "POST" and path == "/reviewer-regions":
        receipt = charter.assign_reviewer_region(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and path == "/region-licenses":
        receipt = charter.register_region_license(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and path == "/region-license-status":
        receipt = charter.update_region_license_status(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and path == "/inspection-keys":
        receipt = charter.mint_inspection_key(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and path == "/charter-groups":
        receipt = charter.submit_charter_group(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and path == "/segment-decisions":
        receipt = charter.decide_segment(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and path == "/trip-events":
        receipt = charter.register_trip_event(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "POST" and path == "/trip-start":
        receipt = charter.mark_trip_started(actor_id=actor_id, **body)
        return 200 if receipt["replayed"] else 201, receipt
    if method == "GET" and path == "/charter-groups":
        group_id = query.get("group_id", [""])[0]
        if not group_id:
            raise ValidationError("group_id 不能为空")
        return 200, charter.get_group(group_id)
    if method == "GET" and path == "/inspection/verify":
        group_id = query.get("group_id", [""])[0]
        if not group_id:
            raise ValidationError("group_id 不能为空")
        if not inspection_key:
            raise PermissionDenied("需要 X-Inspection-Key")
        return 200, charter.inspection_verify(api_key=inspection_key, group_id=group_id)
    return None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    charter: CharterService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(
            self.service, self.command, self.path, body,
            {"X-Actor-Id": self.headers.get("X-Actor-Id", ""),
             "X-Inspection-Key": self.headers.get("X-Inspection-Key", "")},
            charter=self.charter)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动跨区域旅游包车联审协同服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service, Handler.charter = build_services(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
