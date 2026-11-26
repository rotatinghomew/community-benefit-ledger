"""HTTP adapter.  The same :class:`QuotaService` can be embedded without HTTP."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .errors import QuotaServiceError
from .service import QuotaService


def _json_default(value: Any) -> Any:
    # Service results are normally plain JSON data; this only protects the
    # adapter if a datetime or other simple object slips through.
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def create_handler(service: QuotaService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "QuotaService/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter tests
            return

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise QuotaServiceError("request body must be a UTF-8 JSON object", code="bad_json") from exc
            if not isinstance(value, dict):
                raise QuotaServiceError("request body must be a JSON object", code="bad_json")
            return value

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                if parts == ["health"] or parts == ["v1", "health"]:
                    self._send(200, {"ok": True})
                elif len(parts) == 4 and parts[:2] == ["v1", "views"] and parts[2] == "resident":
                    self._send(200, service.resident_view(parts[3], query.get("household"), query.get("at")))
                elif len(parts) == 4 and parts[:2] == ["v1", "views"] and parts[2] == "merchant":
                    self._send(200, service.merchant_view(parts[3], query.get("at")))
                elif parts == ["v1", "admin"]:
                    self._send(200, service.admin_view())
                else:
                    self._send(404, {"error": True, "code": "not_found", "message": "unknown route"})
            except QuotaServiceError as exc:
                self._send(exc.http_status, exc.to_dict())
            except Exception as exc:  # pragma: no cover - adapter boundary
                self._send(500, {"error": True, "code": "internal", "message": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                body = self._body()
                result = self._route_post(parts, body)
                status = 201 if isinstance(result, dict) and result.get("_created") else 200
                if isinstance(result, dict) and "_created" in result:
                    result.pop("_created")
                self._send(status, result)
            except QuotaServiceError as exc:
                self._send(exc.http_status, exc.to_dict())
            except Exception as exc:  # pragma: no cover - adapter boundary
                self._send(500, {"error": True, "code": "internal", "message": str(exc)})

        def _route_post(self, parts: list[str], body: dict[str, Any]) -> Any:
            # Generic action endpoint: /v1/actions/cart/quote -> cart.quote
            if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "actions":
                action = ".".join(parts[2:])
                return service.call(action, body)

            if parts == ["v1", "sources"] and body.get("kind") and body.get("record"):
                return service.ingest(str(body["kind"]), body["record"])

            # Online ordering channel.
            if parts == ["v1", "online", "carts"]:
                return service.create_cart(body)
            if len(parts) == 5 and parts[:2] == ["v1", "online"] and parts[2] == "carts" and parts[4] == "quote":
                return service.quote_cart(parts[3], body.get("at"))
            if len(parts) == 5 and parts[:2] == ["v1", "online"] and parts[2] == "carts" and parts[4] == "confirm":
                body.pop("cart_id", None)
                body.pop("cart", None)
                return service.confirm_cart(parts[3], **body)
            if len(parts) == 4 and parts[:2] == ["v1", "online"] and parts[2] == "orders":
                if parts[3].endswith(":delivery"):
                    order_id = parts[3].split(":", 1)[0]
                    body.pop("order_id", None)
                    return service.confirm_delivery(order_id, **body)
            if len(parts) == 5 and parts[:2] == ["v1", "online"] and parts[2] == "orders" and parts[4] == "delivery":
                body.pop("order_id", None)
                return service.confirm_delivery(parts[3], **body)

            # Terminal upload channel.
            if parts == ["v1", "terminal", "receipts"]:
                return service.submit_receipt(body, received_at=body.get("received_at"))
            if parts == ["v1", "terminal", "closures"]:
                return service.submit_closure(body)

            # Operator/administration channel.
            if parts == ["v1", "admin", "mandates", "revoke"]:
                return service.revoke_mandate(body["mandate"], body.get("revoked_at"))
            if parts == ["v1", "admin", "sequences", "void"]:
                return service.void_sequence(**body)
            if parts == ["v1", "admin", "receipts", "verify"]:
                return service.verify_receipt(**body)
            if parts == ["v1", "admin", "leases", "verify"]:
                return service.verify_lease_receipts(**body)
            if parts == ["v1", "admin", "deliveries", "verify"]:
                return service.verify_delivery(**body)
            if parts == ["v1", "admin", "settlements"]:
                if body.get("action") == "apply":
                    return service.apply_settlement(**{k: v for k, v in body.items() if k != "action"})
                return service.issue_settlement(**body)
            if parts == ["v1", "admin", "deliveries", "refund"]:
                return service.refund_delivery(**body)
            if parts == ["v1", "admin", "receipts", "void"]:
                return service.void_receipt(**body)

            raise QuotaServiceError("unknown API route", code="not_found")

    return Handler


def create_server(
    service: Optional[QuotaService] = None,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    service = service or QuotaService()
    handler = create_handler(service)
    return ThreadingHTTPServer((host, port), handler)


def main(argv: Optional[list[str]] = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the quota service HTTP adapter")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    parser.add_argument("--samples", default=os.getenv("QUOTA_SAMPLES"), help="optional NDJSON sample directory to load")
    args = parser.parse_args(argv)
    service = QuotaService()
    if args.samples:
        service.load_ndjson_dir(args.samples)
    httpd = create_server(service, args.host, args.port)
    print(f"quota service listening on http://{args.host}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
