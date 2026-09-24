"""Loopback-only browser console. Credentials and customer records stay local."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .core import InstalledResolutionApp, LiveOpsHub

STATIC = Path(__file__).parent / "static"
MAX_BODY = 16_384


def require_loopback(host: str) -> None:
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("only a numeric loopback address is permitted")
    except ValueError:
        raise ValueError("only a numeric loopback address is permitted") from None


def make_handler(hub: LiveOpsHub, keys: dict[str, str]):
    if set(keys) != {"operator", "reviewer", "sender"} or any(len(v) < 32 for v in keys.values()):
        raise ValueError("three distinct role keys of at least 32 characters are required")
    if len(set(keys.values())) != 3:
        raise ValueError("role keys must be distinct")

    class Handler(BaseHTTPRequestHandler):
        server_version = "A2ZLiveOps/0.1"

        def log_message(self, *_args):
            # Request paths can contain sensitive identifiers; do not log them.
            return

        def _headers(self, code: int, mime: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers()

        def _json(self, code: int, payload: dict | list) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            self._headers(code, "application/json; charset=utf-8")
            self.wfile.write(data)

        def _role(self) -> str | None:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return None
            token = header[7:]
            for role, expected in keys.items():
                if hmac.compare_digest(token, expected):
                    return role
            return None

        def _allowed(self, *roles: str) -> bool:
            if self._role() in roles:
                return True
            self._json(403, {"error": "role key required for this action"})
            return False

        def _body(self) -> dict:
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit() or int(raw_length) > MAX_BODY:
                raise ValueError("invalid body length")
            raw = self.rfile.read(int(raw_length))
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                return self._json(200, {"status": "ok", "scope": "local-pilot"})
            if path in ("/", "/app.js", "/style.css"):
                name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
                mime = "text/html" if path == "/" else "text/javascript" if path.endswith(".js") else "text/css"
                self._headers(200, f"{mime}; charset=utf-8")
                return self.wfile.write((STATIC / name).read_bytes())
            if path in ("/api/drafts", "/api/summary", "/api/events"):
                if not self._allowed("operator", "reviewer", "sender"):
                    return
                if path == "/api/drafts":
                    return self._json(200, hub.drafts())
                if path == "/api/summary":
                    return self._json(200, hub.summary())
                return self._json(200, hub.events())
            self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            grants = {"/api/import": "operator", "/api/demo": "operator",
                      "/api/review": "reviewer", "/api/outcome": "reviewer", "/api/send": "sender"}
            if path not in grants:
                return self._json(404, {"error": "not found"})
            if not self._allowed(grants[path]):
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "application/json required"})
            try:
                body = self._body()
                if path == "/api/import":
                    result = hub.import_ticket(body.get("ticket_id"), body.get("locale", "en"))
                elif path == "/api/demo":
                    result = hub.import_demo(body.get("locale", "en"))
                elif path == "/api/review":
                    result = hub.review(body.get("draft_id"), body.get("approve"))
                elif path == "/api/outcome":
                    result = hub.outcome(body.get("draft_id"), body.get("status"))
                else:
                    result = hub.send(body.get("draft_id"))
                self._json(200, result)
            except (ValueError, TypeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid request or state; inspect local records"})
            except Exception:
                self._json(409, {"error": "action uncertain; reconcile local app and Zendesk before retry"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local A2Z LiveOps operator pilot")
    parser.add_argument("--app-target", type=Path, required=True)
    parser.add_argument("--knowledge", type=Path, required=True)
    parser.add_argument("--resolution-db", type=Path, required=True)
    parser.add_argument("--hub-db", type=Path, required=True)
    parser.add_argument("--customer-key", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--zendesk-subdomain", default="")
    parser.add_argument("--demo-ticket", type=Path)
    parser.add_argument("--reviewer-label", required=True)
    parser.add_argument("--sender-label", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args(argv)
    require_loopback(args.host)
    keys = {role: os.environ.get(f"LIVEOPS_{role.upper()}_KEY", "") for role in ("operator", "reviewer", "sender")}
    app = InstalledResolutionApp(args.app_target, args.knowledge, args.resolution_db,
                                 args.zendesk_subdomain, args.expected_commit, args.demo_ticket)
    hub = LiveOpsHub(args.hub_db, args.customer_key, app, args.reviewer_label, args.sender_label)
    with ThreadingHTTPServer((args.host, args.port), make_handler(hub, keys)) as server:
        print(f"LiveOps local pilot: http://{args.host}:{args.port}/", flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
