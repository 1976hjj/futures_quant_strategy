"""Serve local read-only research APIs backed by published artifacts."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from alpha_research_os.reporting.quality_overview import build_quality_overview


def make_handler(project_root: Path, allowed_origin: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/v1/health":
                self._json(HTTPStatus.OK, {"status": "ok", "service": "alpha-research-os-read-api"})
                return
            if self.path == "/api/v1/quality/overview":
                try:
                    self._json(HTTPStatus.OK, build_quality_overview(project_root))
                except (OSError, ValueError) as error:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "QUALITY_ARTIFACT_UNAVAILABLE", "detail": str(error)},
                    )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

        def do_HEAD(self) -> None:  # noqa: N802
            if self.path in {"/api/v1/health", "/api/v1/quality/overview"}:
                self.send_response(HTTPStatus.OK)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _cors(self) -> None:
            if self.headers.get("Origin") == allowed_origin:
                self.send_header("Access-Control-Allow-Origin", allowed_origin)
                self.send_header("Vary", "Origin")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--allow-origin", default="http://127.0.0.1:8871")
    args = parser.parse_args()
    root = args.project_root.resolve()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(root, args.allow_origin))
    print(f"Alpha Research OS API: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
