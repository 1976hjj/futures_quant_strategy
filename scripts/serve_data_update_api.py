"""Independent data-management API; credentials belong only to the selected paid source."""

from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for import_root in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from scripts.bank_processing import (  # noqa: E402
    ProcessingRequest,
    processing_inventory,
    processing_plan,
    table_page,
)
from scripts.bank_processing_api import BankProcessingManager  # noqa: E402
from scripts.data_update import DataUpdateRequest, inventory, plan  # noqa: E402
from scripts.data_update_api import DataUpdateManager  # noqa: E402


def make_handler(root: Path, manager: DataUpdateManager, origins: set[str], strategy_url: str):
    processing = BankProcessingManager(root)
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin")
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass  # Browser navigation can cancel an otherwise completed read.

        def do_OPTIONS(self) -> None:  # noqa: N802
            origin = self.headers.get("Origin")
            if origin not in origins:
                self.reply(HTTPStatus.FORBIDDEN, {"error": "ORIGIN_NOT_ALLOWED"})
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/api/v1/health":
                    self.reply(HTTPStatus.OK, {"status": "ok", "service": "data-management"})
                elif path == "/api/v1/data/inventory":
                    self.reply(HTTPStatus.OK, inventory(root))
                elif path == "/api/v1/data/processing/inventory":
                    self.reply(HTTPStatus.OK, processing_inventory(root))
                elif path == "/api/v1/data/processing/jobs/latest":
                    self.reply(HTTPStatus.OK, {"job": processing.latest()})
                elif path.startswith("/api/v1/data/processing/jobs/"):
                    self.reply(HTTPStatus.OK, processing.status(path.rsplit("/", 1)[-1]))
                elif path in ("/api/v1/data/processing/gaps", "/api/v1/data/processing/values"):
                    query = parse_qs(urlparse(self.path).query)
                    def get(key, default=None):
                        return query.get(key, [default])[0]
                    self.reply(HTTPStatus.OK, table_page(
                        root, "DERIVED" if path.endswith("gaps") else "INDICATORS", get("asset_id"),
                        int(get("page", "1")), int(get("page_size", "20")), get("factor_id"), get("field"),
                    ))
                elif path == "/api/v1/data/jobs/latest":
                    self.reply(HTTPStatus.OK, {"job": manager.latest()})
                elif path.startswith("/api/v1/data/jobs/"):
                    self.reply(HTTPStatus.OK, manager.status(path.rsplit("/", 1)[-1]))
                else:
                    self.reply(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            except FileNotFoundError:
                self.reply(HTTPStatus.NOT_FOUND, {"error": "JOB_NOT_FOUND"})
            except ValueError as error:
                self.reply(HTTPStatus.BAD_REQUEST, {"detail": str(error)})

        def do_POST(self) -> None:  # noqa: N802
            try:
                origin = self.headers.get("Origin")
                if origin and origin not in origins:
                    self.reply(HTTPStatus.FORBIDDEN, {"error": "ORIGIN_NOT_ALLOWED"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    raise ValueError("request body must be between 1 byte and 1 MB")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                path = urlparse(self.path).path
                if path == "/api/v1/data/plan":
                    self.reply(HTTPStatus.OK, plan(root, DataUpdateRequest.model_validate(payload)))
                elif path == "/api/v1/data/jobs":
                    if processing.running():
                        raise RuntimeError("数据处理正在运行，请完成后再更新来源数据")
                    # The existing backtest API keeps its active process and queue.
                    # Its DataUpdateManager also checks these shared durable job files.
                    try:
                        with urlopen(strategy_url + "/strategy/queue", timeout=5) as response:
                            queue = json.load(response)
                    except (OSError, URLError) as error:
                        raise RuntimeError(
                            "cannot verify backtest queue; retry when the strategy API is connected"
                        ) from error
                    if queue.get("running_count") or queue.get("queued_count"):
                        raise RuntimeError("strategy backtests are running or queued; wait before updating data")
                    self.reply(HTTPStatus.ACCEPTED, manager.start(payload))
                elif path == "/api/v1/data/processing/plan":
                    self.reply(HTTPStatus.OK, processing_plan(root, ProcessingRequest.model_validate(payload)))
                elif path == "/api/v1/data/processing/jobs":
                    self.reply(HTTPStatus.ACCEPTED, processing.start(payload))
                else:
                    self.reply(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            except (ValueError, ValidationError) as error:
                self.reply(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
            except RuntimeError as error:
                self.reply(HTTPStatus.CONFLICT, {"detail": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--port", type=int, default=8774)
    parser.add_argument("--strategy-api-url", default="http://127.0.0.1:8773/api/v1")
    args = parser.parse_args()
    root = args.project_root.resolve()
    handler = make_handler(root, DataUpdateManager(root),
                           {"http://127.0.0.1:8872", "http://localhost:8872"}, args.strategy_api_url.rstrip("/"))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Data management API: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
