"""Serve the independent configurable strategy-backtest API."""

from __future__ import annotations

import argparse
import hashlib
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
from alpha_research_os.portfolio.shadow_health import ShadowHealthSpec  # noqa: E402
from alpha_research_os.portfolio.strategy_backtest import (  # noqa: E402
    ALL_UNIVERSE_SEGMENTS,
    UNIVERSE_SEGMENT_NAMES,
    StrategyBacktestRequest,
    _csi300_benchmark,
    preflight,
    preview,
)
from alpha_research_os.reporting.factor_catalog_overview import build_factor_catalog_overview  # noqa: E402
from scripts.data_update import DataUpdateRequest  # noqa: E402
from scripts.data_update import inventory as data_inventory  # noqa: E402
from scripts.data_update import plan as data_plan  # noqa: E402
from scripts.data_update_api import DataUpdateManager  # noqa: E402


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
        "universe_segments": [
            {"id": segment, "name": UNIVERSE_SEGMENT_NAMES[segment]}
            for segment in ALL_UNIVERSE_SEGMENTS
        ],
        "defaults": {
            "target_count": 50,
            "retention_rank": 75,
            "rebalance_sessions": 5,
            "minimum_listed_sessions": 60,
            "initial_cash_cny": 1_000_000,
        },
        "shadow_health_defaults": ShadowHealthSpec().model_dump(mode="json"),
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
            # Starting a job must return promptly; the worker performs the
            # complete backtest after the lightweight request validation.
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
        if isinstance(result, dict) and not isinstance(result.get("benchmark"), dict):
            benchmark = self._benchmark_for_result(result)
            if benchmark is not None:
                result["benchmark"] = benchmark
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
        else:
            # Until the worker publishes its progress file, keep the job at the
            # initial value. Log output is not an authoritative progress source.
            phase, progress = "准备数据", 3
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
                    "completed_parameter_sets", "total_parameter_sets",
                    "remaining_parameter_sets", "current_parameters",
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

    @staticmethod
    def _summary_path(result_path: Path) -> Path:
        return result_path.with_name(
            result_path.name.removesuffix(".result.json") + ".summary.json"
        )

    @staticmethod
    def _experiment_kind(request: dict[str, Any]) -> str:
        """Classify persisted jobs without changing their immutable requests."""
        if request.get("strategy_type") == "ROTATION":
            return "ROTATION"
        score_rules = request.get("score_rules")
        if isinstance(score_rules, list) and len(score_rules) == 1:
            return "SINGLE_FACTOR"
        return "MULTI_FACTOR"

    @staticmethod
    def _comparison_key(request: dict[str, Any]) -> str | None:
        if StrategyJobManager._experiment_kind(request) != "SINGLE_FACTOR":
            return None
        ignored = {"name", "score_rules"}
        comparable = {key: value for key, value in request.items() if key not in ignored}
        return "sha256:" + hashlib.sha256(canonical_json_bytes(comparable)).hexdigest()

    def _benchmark_for_result(self, result: dict[str, Any]) -> dict[str, Any] | None:
        """Supply a newly available benchmark to an immutable legacy result."""
        if isinstance(result.get("benchmark"), dict):
            return result["benchmark"]
        daily = result.get("daily")
        if not isinstance(daily, list):
            return None
        try:
            sessions = [date.fromisoformat(str(item["session"])) for item in daily]
        except (KeyError, TypeError, ValueError):
            return None
        return _csi300_benchmark(self.project_root, sessions)

    def _result_listing_summary(self, result_path: Path) -> dict[str, Any]:
        """Read a tiny immutable sidecar, creating it once for legacy results."""
        summary_path = self._summary_path(result_path)
        result_stat = result_path.stat()
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_bytes())
                if (
                    isinstance(summary, dict)
                    and summary.get("schema_version") == "3"
                    and summary.get("result_size") == result_stat.st_size
                    and summary.get("result_mtime_ns") == result_stat.st_mtime_ns
                ):
                    return summary
            except (OSError, json.JSONDecodeError):
                pass
        result = json.loads(result_path.read_bytes())
        if not isinstance(result, dict):
            raise ValueError("backtest result must be an object")
        summary = {
            "schema_version": "3",
            "result_size": result_stat.st_size,
            "result_mtime_ns": result_stat.st_mtime_ns,
            "result_summary": result.get("summary"),
            "benchmark_summary": (self._benchmark_for_result(result) or {}).get("summary"),
            "trade_detail_available": "trades" in result,
            "execution_model_valid": result.get("execution_model", {}).get("version") == "2.0.0",
        }
        temporary = summary_path.with_name(f".{summary_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(canonical_json_bytes(summary) + b"\n")
        os.replace(temporary, summary_path)
        return summary

    def _list_item(self, request_path: Path) -> dict[str, Any]:
        job_id = request_path.name.removesuffix(".request.json")
        result_path = self.run_root / f"{job_id}.result.json"
        progress_path = self.run_root / f"{job_id}.progress.json"
        log_path = self.run_root / f"{job_id}.log"
        request = json.loads(request_path.read_bytes())
        active = self.active_job_id == job_id and self.running()
        exit_code = (
            self.process.poll()
            if self.active_job_id == job_id and self.process is not None
            else None
        )
        result_summary: dict[str, Any] | None = None
        progress_detail: dict[str, Any] = {}
        if result_path.exists():
            listing = self._result_listing_summary(result_path)
            status = "PASS"
            phase = "回测完成"
            progress = 100
            result_summary = listing.get("result_summary")
        else:
            listing = {}
            if job_id in self.stopped_jobs:
                status = "STOPPED"
            elif active:
                status = "RUNNING"
            elif exit_code not in (None, 0):
                status = "FAIL"
            else:
                status = "STOPPED"
            if progress_path.exists():
                try:
                    loaded = json.loads(progress_path.read_bytes())
                    if isinstance(loaded, dict):
                        progress_detail = loaded
                except (OSError, json.JSONDecodeError):
                    pass
            phase = str(progress_detail.get("phase") or "准备数据")
            progress = int(progress_detail.get("progress") or 3)
        created_at = datetime.fromtimestamp(request_path.stat().st_mtime).astimezone()
        updated_paths = (request_path, log_path, result_path, progress_path, self._summary_path(result_path))
        progress_payload = {
            key: progress_detail.get(key)
            for key in (
                "heartbeat_at", "processed_sessions", "total_sessions", "current_session",
                "rebalance_count", "position_count", "query_progress",
                "completed_parameter_sets", "total_parameter_sets",
                "remaining_parameter_sets", "current_parameters",
            )
        }
        listing_request = {"start": request.get("start"), "end": request.get("end")}
        for field in (
            "universe_id", "universe_segments", "rebalance_sessions", "target_count",
            "retention_rank", "score_rules", "filter_rules", "minimum_cash_fraction",
        ):
            if request.get(field) is not None:
                listing_request[field] = request[field]
        shadow_health = request.get("shadow_health")
        if isinstance(shadow_health, dict):
            listing_request["shadow_health"] = {
                "experiment_variant": shadow_health.get("experiment_variant", "S0")
            }
        candidates = request.get("candidates")
        if isinstance(candidates, list):
            listing_request["candidates"] = [
                {"rebalance_sessions": candidate.get("rebalance_sessions")}
                for candidate in candidates
                if isinstance(candidate, dict)
            ]
        experiment_kind = self._experiment_kind(request)
        benchmark_summary = listing.get("benchmark_summary") or {}
        comparison_summary = dict(result_summary or {})
        benchmark_return = benchmark_summary.get("total_return")
        if result_summary and benchmark_return is not None:
            comparison_summary["benchmark_total_return"] = benchmark_return
            comparison_summary["excess_return"] = result_summary.get("total_return", 0) - benchmark_return
        maximum_drawdown = comparison_summary.get("maximum_drawdown")
        annualized_return = comparison_summary.get("annualized_return")
        if maximum_drawdown not in (None, 0) and annualized_return is not None:
            comparison_summary["calmar"] = annualized_return / abs(maximum_drawdown)
        score_rules = request.get("score_rules") or []
        single_factor = score_rules[0] if experiment_kind == "SINGLE_FACTOR" else None
        return {
            "job_id": job_id,
            "status": status,
            "phase": phase,
            "progress": progress,
            "name": request.get("name", ""),
            "strategy_type": request.get("strategy_type", "FACTOR"),
            "experiment_kind": experiment_kind,
            "single_factor": single_factor,
            "comparison_key": self._comparison_key(request),
            "trade_detail_available": bool(listing.get("trade_detail_available")),
            "execution_model_valid": bool(listing.get("execution_model_valid")),
            "created_at": created_at.isoformat(),
            "updated_at": datetime.fromtimestamp(
                max(path.stat().st_mtime for path in updated_paths if path.exists())
            ).astimezone().isoformat(),
            "request": listing_request,
            "process_alive": active,
            "elapsed_seconds": max(0, int((datetime.now().astimezone() - created_at).total_seconds())),
            "result_summary": comparison_summary or None,
            **progress_payload,
        }

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Return every persisted backtest, newest first.

        Request/result/log files are the source of truth so history survives browser
        refreshes and API restarts instead of depending on an in-memory job id.
        """
        jobs: list[dict[str, Any]] = []
        for request_path in self.run_root.glob("*.request.json"):
            try:
                item = self._list_item(request_path)
                if kind == "single-factor" and item["experiment_kind"] != "SINGLE_FACTOR":
                    continue
                if kind == "history" and item["experiment_kind"] == "SINGLE_FACTOR":
                    continue
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
            for suffix in ("request.json", "result.json", "summary.json", "progress.json", "log"):
                path = self.run_root / f"{job_id}.{suffix}"
                if path.exists():
                    path.unlink()
                    deleted.append(path.name)
            self.stopped_jobs.discard(job_id)
        return {"job_id": job_id, "deleted": True, "deleted_files": deleted}


def make_handler(project_root: Path, origins: set[str], manager: StrategyJobManager,
                 data_manager: DataUpdateManager | None = None):
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
            if path == "/api/v1/data/inventory":
                self._json(HTTPStatus.OK, data_inventory(project_root))
                return
            if path == "/api/v1/data/jobs/latest" and data_manager is not None:
                self._json(HTTPStatus.OK, {"job": data_manager.latest()})
                return
            if path.startswith("/api/v1/data/jobs/") and data_manager is not None:
                try:
                    self._json(HTTPStatus.OK, data_manager.status(path.rsplit("/", 1)[-1]))
                except FileNotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "JOB_NOT_FOUND"})
                except ValueError as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_JOB_ID", "detail": str(error)})
                return
            if path == "/api/v1/rotation/options":
                self._json(HTTPStatus.OK, rotation_options(project_root))
                return
            if path == "/api/v1/strategy/jobs":
                kind = (parse_qs(parsed.query).get("kind") or [None])[0]
                if kind not in (None, "single-factor", "history"):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_KIND"})
                    return
                self._json(HTTPStatus.OK, {"jobs": manager.list(kind)})
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
                if path == "/api/v1/data/plan":
                    self._json(HTTPStatus.OK, data_plan(project_root, DataUpdateRequest.model_validate(payload)))
                    return
                if path == "/api/v1/data/jobs" and data_manager is not None:
                    if manager.running():
                        raise RuntimeError("a strategy backtest is running; wait before updating the warehouse")
                    self._json(HTTPStatus.ACCEPTED, data_manager.start(payload))
                    return
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
                    if data_manager is not None and data_manager.running():
                        raise RuntimeError("a data update is running; wait before starting a backtest")
                    self._json(HTTPStatus.ACCEPTED, manager.start(payload))
                    return
                request = StrategyBacktestRequest.model_validate(payload)
                if path == "/api/v1/strategy/preflight":
                    self._json(HTTPStatus.OK, preflight(project_root, request))
                    return
                if path == "/api/v1/strategy/jobs":
                    if data_manager is not None and data_manager.running():
                        raise RuntimeError("a data update is running; wait before starting a backtest")
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
    data_manager = DataUpdateManager(root)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(root, origins, manager, data_manager))
    print(f"Strategy backtest API: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
