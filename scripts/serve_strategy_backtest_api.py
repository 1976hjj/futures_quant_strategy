"""Serve the independent configurable strategy-backtest API."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import threading
import uuid
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from alpha_research_os.kernel.canonical import canonical_json_bytes  # noqa: E402
from alpha_research_os.portfolio.rotation_backtest import (  # noqa: E402
    RotationBacktestRequest,
    preflight_rotation,
    preview_rotation,
)
from alpha_research_os.portfolio.strategy_backtest import (  # noqa: E402
    StrategyBacktestRequest,
    preflight,
    preview,
)
from alpha_research_os.reporting.factor_catalog_overview import build_factor_catalog_overview  # noqa: E402


def strategy_options(project_root: Path) -> dict[str, Any]:
    factors = []
    for item in build_factor_catalog_overview(project_root):
        if not item["calculated"] or not item["latest_release_id"] or item.get("accuracy_status") == "FAIL":
            continue
        coverage = item.get("coverage") or {}
        factors.append(
            {
                "factor_id": item["factor_id"],
                "release_id": item["latest_release_id"],
                "chinese_name": item["chinese_name"],
                "source_collection": item["source_collection"],
                "category": item["category"],
                "expected_direction": item.get("expected_direction") or "HIGH",
                "start": coverage.get("start"),
                "end": coverage.get("end"),
            }
        )
    return {
        "factors": sorted(
            factors,
            key=lambda item: (item["source_collection"], item["category"], item["chinese_name"]),
        ),
        "universes": [{"id": "ALL-A-PIT", "name": "历史全 A 股票池"}],
        "defaults": {
            "target_count": 50,
            "retention_rank": 75,
            "rebalance_sessions": 5,
            "minimum_listed_sessions": 60,
            "initial_cash_cny": 1_000_000,
        },
    }


def rotation_options(project_root: Path) -> dict[str, Any]:
    """Expose rotation capabilities while reusing the published factor catalogue."""

    options = strategy_options(project_root)
    factor_catalog = build_factor_catalog_overview(project_root)
    factors = [
        {
            "factor_id": item["factor_id"],
            "factor_version": item["factor_version"],
            "release_id": item["latest_release_id"],
            "chinese_name": item["chinese_name"],
            "source_collection": item["source_collection"],
            "category": item["category"],
            "expected_direction": item.get("expected_direction") or "HIGH",
            "calculated": bool(item["calculated"]),
            "status": item["status"],
            "status_label": item["status_label"],
            "start": (item.get("coverage") or {}).get("start"),
            "end": (item.get("coverage") or {}).get("end"),
        }
        for item in factor_catalog
        if item.get("accuracy_status") != "FAIL"
    ]
    return {
        **options,
        "factors": sorted(
            factors,
            key=lambda item: (
                not item["calculated"],
                item["source_collection"],
                item["category"],
                item["chinese_name"],
            ),
        ),
        "factor_counts": {
            "total": len(factors),
            "calculated": sum(1 for item in factors if item["calculated"]),
            "needs_calculation": sum(1 for item in factors if not item["calculated"]),
        },
        "strategy_type": "ROTATION",
        "candidate_kinds": [{"id": "FACTOR", "name": "因子选股组合"}],
        "signal_metrics": [
            {"id": "TRAILING_RETURN", "name": "区间收益"},
            {"id": "EXCESS_RETURN", "name": "相对基准超额收益"},
            {"id": "RISK_ADJUSTED_RETURN", "name": "风险调整收益"},
        ],
        "allocation_modes": [
            {"id": "WINNER_TAKE_ALL", "name": "领先组合全仓"},
            {"id": "WINNER_TILT", "name": "领先组合倾斜"},
            {"id": "SCORE_WEIGHTED", "name": "按正得分分配"},
        ],
        "industry_controls": [
            {"id": "NONE", "name": "不限制"},
            {"id": "CAP", "name": "行业权重上限"},
        ],
        "limits": {"minimum_candidates": 2, "maximum_candidates": 8},
        "rotation_defaults": {
            "lookback_sessions": 20,
            "decision_interval_sessions": 1,
            "switch_threshold": 0.01,
            "confirmation_periods": 1,
            "minimum_hold_periods": 1,
            "allocation_mode": "WINNER_TAKE_ALL",
            "winner_weight": 0.70,
            "maximum_industry_weight": 0.25,
        },
    }


class StrategyJobManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.run_root = project_root / "reports" / "strategy_backtests"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.active_job_id: str | None = None
        self.stopped_jobs: set[str] = set()

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        is_rotation = payload.get("strategy_type") == "ROTATION"
        if is_rotation:
            request = RotationBacktestRequest.model_validate(payload)
            preflight_rotation(self.project_root, request)
            runner = "scripts/run_rotation_backtest.py"
        else:
            request = StrategyBacktestRequest.model_validate(payload)
            preflight(self.project_root, request)
            runner = "scripts/run_strategy_backtest.py"
        with self.lock:
            if self.running():
                raise RuntimeError(f"strategy job {self.active_job_id} is already running")
            job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            request_path = self.run_root / f"{job_id}.request.json"
            result_path = self.run_root / f"{job_id}.result.json"
            log_path = self.run_root / f"{job_id}.log"
            progress_path = self.run_root / f"{job_id}.progress.json"
            request_path.write_bytes(canonical_json_bytes(request) + b"\n")
            stream = log_path.open("wb")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join((str(SRC_ROOT), str(PROJECT_ROOT)))
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    runner,
                    "--project-root", str(self.project_root),
                    "--request", str(request_path),
                    "--result", str(result_path),
                    "--progress", str(progress_path),
                ],
                cwd=self.project_root,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            self.active_job_id = job_id
            threading.Thread(target=self._wait_and_close, args=(self.process, stream), daemon=True).start()
        return self.status(job_id)

    @staticmethod
    def _wait_and_close(process: subprocess.Popen[bytes], stream: Any) -> None:
        process.wait()
        stream.close()

    def stop(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if self.active_job_id != job_id or not self.running():
                raise ValueError("strategy job is not running")
            assert self.process is not None
            self.stopped_jobs.add(job_id)
            self.process.terminate()
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        if not job_id.replace("-", "").isalnum():
            raise ValueError("invalid job id")
        request_path = self.run_root / f"{job_id}.request.json"
        result_path = self.run_root / f"{job_id}.result.json"
        log_path = self.run_root / f"{job_id}.log"
        progress_path = self.run_root / f"{job_id}.progress.json"
        if not request_path.exists():
            raise FileNotFoundError(job_id)
        active = self.active_job_id == job_id and self.running()
        stored_result = json.loads(result_path.read_bytes()) if result_path.exists() else None
        # Transaction rows can be numerous.  They are deliberately loaded only
        # by the year-specific endpoint, not whenever a result card is opened.
        result = (
            {key: value for key, value in stored_result.items() if key != "trades"}
            if isinstance(stored_result, dict)
            else stored_result
        )
        exit_code = self.process.poll() if self.active_job_id == job_id and self.process is not None else None
        if stored_result:
            status = "PASS"
        elif job_id in self.stopped_jobs:
            status = "STOPPED"
        elif active:
            status = "RUNNING"
        elif exit_code not in (None, 0):
            status = "FAIL"
        else:
            status = "STOPPED"
        log_tail = log_path.read_bytes()[-12_000:].decode("utf-8", errors="replace") if log_path.exists() else ""
        progress_detail: dict[str, Any] = {}
        if progress_path.exists():
            try:
                loaded_progress = json.loads(progress_path.read_bytes())
                if isinstance(loaded_progress, dict):
                    progress_detail = loaded_progress
            except (OSError, json.JSONDecodeError):
                pass
        if result:
            phase, progress = "回测完成", 100
        elif active and progress_detail:
            phase = str(progress_detail.get("phase") or "正在运行")
            progress = int(progress_detail.get("progress") or 1)
        elif "publishing backtest report" in log_tail:
            phase, progress = "生成报告", 90
        elif "running continuous account" in log_tail:
            phase, progress = "连续账户回放", 20
        else:
            phase, progress = "准备数据", 5
        request = json.loads(request_path.read_bytes())
        created_at = datetime.fromtimestamp(request_path.stat().st_mtime).astimezone()
        updated_paths = (request_path, log_path, result_path, progress_path)
        return {
            "job_id": job_id, "status": status, "phase": phase, "progress": progress,
            "name": request["name"],
            "strategy_type": request.get("strategy_type", "FACTOR"),
            "log_tail": log_tail, "result": result,
            "trade_detail_available": bool(isinstance(stored_result, dict) and "trades" in stored_result),
            "execution_model_valid": bool(
                isinstance(stored_result, dict)
                and stored_result.get("execution_model", {}).get("version") == "2.0.0"
            ),
            "created_at": created_at.isoformat(),
            "updated_at": datetime.fromtimestamp(
                max(path.stat().st_mtime for path in updated_paths if path.exists())
            ).astimezone().isoformat(),
            "request": request,
            "process_alive": active,
            "elapsed_seconds": max(0, int((datetime.now().astimezone() - created_at).total_seconds())),
            **{
                key: progress_detail.get(key)
                for key in (
                    "heartbeat_at", "processed_sessions", "total_sessions", "current_session",
                    "rebalance_count", "position_count", "query_progress",
                )
            },
        }

    def trades(self, job_id: str, year: int, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Return one year's persisted filled transactions without loading all years into the UI."""
        if not job_id.replace("-", "").isalnum():
            raise ValueError("invalid job id")
        result_path = self.run_root / f"{job_id}.result.json"
        if not result_path.exists():
            raise FileNotFoundError(job_id)
        loaded = json.loads(result_path.read_bytes())
        trades = loaded.get("trades") if isinstance(loaded, dict) else None
        if not isinstance(trades, list):
            return {"job_id": job_id, "year": year, "available": False, "total": 0, "trades": []}
        yearly = [item for item in trades if str(item.get("session", "")).startswith(f"{year}-")]
        yearly.sort(key=lambda item: (str(item.get("session", "")), int(item.get("rebalance_id", 0))), reverse=True)
        page = yearly[offset: offset + limit]
        realized_pnls = [
            float(item["realized_pnl_cny"])
            for item in yearly
            if item.get("side") == "SELL" and item.get("realized_pnl_cny") is not None
        ]
        daily = loaded.get("daily") if isinstance(loaded, dict) else None
        year_daily = [
            item for item in (daily or [])
            if str(item.get("session", "")).startswith(f"{year}-")
        ]
        performance = None
        if year_daily:
            first_nav = float(year_daily[0].get("nav") or 0)
            first_return = float(year_daily[0].get("daily_return") or 0)
            start_nav = first_nav / (1 + first_return) if 1 + first_return else first_nav
            end_nav = float(year_daily[-1].get("nav") or 0)
            performance = {
                "start_nav_cny": round(start_nav, 2),
                "end_nav_cny": round(end_nav, 2),
                "profit_cny": round(end_nav - start_nav, 2),
                "return": end_nav / start_nav - 1 if start_nav else None,
                "dividend_cash_cny": round(
                    sum(float(item.get("dividend_cash") or 0) for item in year_daily), 2
                ),
            }
        return {
            "job_id": job_id,
            "year": year,
            "available": True,
            "total": len(yearly),
            "offset": offset,
            "limit": limit,
            "performance": performance,
            "summary": {
                "buy_amount_cny": sum(
                    float(item.get("amount_cny") or 0) for item in yearly if item.get("side") == "BUY"
                ),
                "sell_amount_cny": sum(
                    float(item.get("amount_cny") or 0) for item in yearly if item.get("side") == "SELL"
                ),
                "commission_cny": sum(float(item.get("commission_cny") or 0) for item in yearly),
                "stamp_duty_cny": sum(float(item.get("stamp_duty_cny") or 0) for item in yearly),
                "transfer_fee_cny": sum(float(item.get("transfer_fee_cny") or 0) for item in yearly),
                "total_cost_cny": sum(float(item.get("total_cost_cny") or 0) for item in yearly),
                "realized_pnl_cny": sum(realized_pnls) if realized_pnls else None,
                "rebalance_count": len({item.get("rebalance_id") for item in yearly}),
            },
            "trades": page,
        }

    def list(self) -> list[dict[str, Any]]:
        """Return every persisted backtest, newest first.

        Request/result/log files are the source of truth so history survives browser
        refreshes and API restarts instead of depending on an in-memory job id.
        """
        jobs: list[dict[str, Any]] = []
        for request_path in self.run_root.glob("*.request.json"):
            job_id = request_path.name.removesuffix(".request.json")
            try:
                item = self.status(job_id)
                result = item.pop("result", None)
                item.pop("log_tail", None)
                item["result_summary"] = result.get("summary") if result else None
                jobs.append(item)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        jobs.sort(key=lambda item: (item["created_at"], item["job_id"]), reverse=True)
        return jobs

    def delete(self, job_id: str) -> dict[str, Any]:
        if not job_id.replace("-", "").isalnum():
            raise ValueError("invalid job id")
        with self.lock:
            if self.active_job_id == job_id and self.running():
                raise RuntimeError("a running strategy job cannot be deleted")
            request_path = self.run_root / f"{job_id}.request.json"
            if not request_path.exists():
                raise FileNotFoundError(job_id)
            deleted: list[str] = []
            for suffix in ("request.json", "result.json", "progress.json", "log"):
                path = self.run_root / f"{job_id}.{suffix}"
                if path.exists():
                    path.unlink()
                    deleted.append(path.name)
            self.stopped_jobs.discard(job_id)
        return {"job_id": job_id, "deleted": True, "deleted_files": deleted}


def make_handler(project_root: Path, origins: set[str], manager: StrategyJobManager):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/v1/health":
                self._json(HTTPStatus.OK, {"status": "ok", "service": "strategy-backtest"})
                return
            if path == "/api/v1/strategy/options":
                self._json(HTTPStatus.OK, strategy_options(project_root))
                return
            if path == "/api/v1/rotation/options":
                self._json(HTTPStatus.OK, rotation_options(project_root))
                return
            if path == "/api/v1/strategy/jobs":
                self._json(HTTPStatus.OK, {"jobs": manager.list()})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[:4] == ["api", "v1", "strategy", "jobs"]:
                try:
                    self._json(HTTPStatus.OK, manager.status(parts[4]))
                except FileNotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "JOB_NOT_FOUND"})
                return
            if len(parts) == 6 and parts[:4] == ["api", "v1", "strategy", "jobs"] and parts[5] == "report":
                result_path = manager.run_root / f"{parts[4]}.result.json"
                if not result_path.exists():
                    self._json(HTTPStatus.NOT_FOUND, {"error": "REPORT_NOT_FOUND"})
                    return
                body = result_path.read_bytes()
                self._bytes(HTTPStatus.OK, body, mimetypes.types_map[".json"] + "; charset=utf-8")
                return
            if len(parts) == 6 and parts[:4] == ["api", "v1", "strategy", "jobs"] and parts[5] == "trades":
                try:
                    query = parse_qs(parsed.query)
                    year = int((query.get("year") or [""])[0])
                    offset = max(0, int((query.get("offset") or ["0"])[0]))
                    limit = min(250, max(1, int((query.get("limit") or ["100"])[0])))
                    self._json(HTTPStatus.OK, manager.trades(parts[4], year, offset, limit))
                except FileNotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "REPORT_NOT_FOUND"})
                except ValueError as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_TRADE_QUERY", "detail": str(error)})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                payload = self._body()
                parts = path.strip("/").split("/")
                if len(parts) == 6 and parts[:4] == ["api", "v1", "strategy", "jobs"] and parts[5] == "stop":
                    self._json(HTTPStatus.OK, manager.stop(parts[4]))
                    return
                if path == "/api/v1/strategy/preview":
                    preview_date_text = str(payload.pop("preview_date", payload.get("start", "")))
                    request = StrategyBacktestRequest.model_validate(payload)
                    self._json(
                        HTTPStatus.OK,
                        preview(project_root, request, date.fromisoformat(preview_date_text)),
                    )
                    return
                if path == "/api/v1/rotation/preflight":
                    rotation_request = RotationBacktestRequest.model_validate(payload)
                    self._json(HTTPStatus.OK, preflight_rotation(project_root, rotation_request))
                    return
                if path == "/api/v1/rotation/preview":
                    preview_date_text = str(payload.pop("preview_date", payload.get("start", "")))
                    rotation_request = RotationBacktestRequest.model_validate(payload)
                    self._json(
                        HTTPStatus.OK,
                        preview_rotation(
                            project_root,
                            rotation_request,
                            date.fromisoformat(preview_date_text),
                        ),
                    )
                    return
                if path == "/api/v1/rotation/jobs":
                    self._json(HTTPStatus.ACCEPTED, manager.start(payload))
                    return
                request = StrategyBacktestRequest.model_validate(payload)
                if path == "/api/v1/strategy/preflight":
                    self._json(HTTPStatus.OK, preflight(project_root, request))
                    return
                if path == "/api/v1/strategy/jobs":
                    self._json(HTTPStatus.ACCEPTED, manager.start(payload))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            except ValidationError as error:
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": "INVALID_REQUEST", "detail": error.errors(include_context=False)},
                )
            except (ValueError, FileNotFoundError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": type(error).__name__, "detail": str(error)})
            except RuntimeError as error:
                self._json(HTTPStatus.CONFLICT, {"error": "JOB_ALREADY_RUNNING", "detail": str(error)})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            parts = path.strip("/").split("/")
            if len(parts) != 5 or parts[:4] != ["api", "v1", "strategy", "jobs"]:
                self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
                return
            try:
                self._json(HTTPStatus.OK, manager.delete(parts[4]))
            except FileNotFoundError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "JOB_NOT_FOUND"})
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_JOB_ID", "detail": str(error)})
            except RuntimeError as error:
                self._json(HTTPStatus.CONFLICT, {"error": "JOB_IS_RUNNING", "detail": str(error)})

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("request body must be between 1 byte and 1 MB")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self._bytes(status, body, "application/json; charset=utf-8")

        def _bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--allow-origin", action="append")
    args = parser.parse_args()
    root = args.project_root.resolve()
    origins = set(args.allow_origin or ("http://127.0.0.1:8872", "http://localhost:8872"))
    manager = StrategyJobManager(root)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(root, origins, manager))
    print(f"Strategy backtest API: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
