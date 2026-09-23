"""Serve the local M4 research control API and manage one pipeline job at a time."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
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

import duckdb
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from alpha_research_os.factors.alpha158 import alpha158_catalog  # noqa: E402
from alpha_research_os.factors.jqdata import jqdata_catalog  # noqa: E402
from alpha_research_os.factors.library import m4_2_factor_entries  # noqa: E402
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash  # noqa: E402
from alpha_research_os.orchestration import M4PipelineConfig  # noqa: E402
from alpha_research_os.reporting import (  # noqa: E402
    build_factor_asset_library,
    build_factor_catalog_overview,
    query_factor_assets,
    query_factor_catalog,
)
from scripts.factor_compute_runtime import accuracy_status  # noqa: E402

STAGE_LABELS = {
    "m4_1": "基础收益关系",
    "m4_2": "复权、缩尾和中性化",
    "m4_3": "稳健统计与多重检验",
    "m4_4": "Walk-Forward 与市场环境",
    "m4_5": "去重、聚类与增量价值",
    "m4_6": "成交、成本、冲击和容量",
}
PIPELINE_STAGE_BY_UI = {
    "m4_1": ("basic_evidence", "audit_basic_evidence"),
    "m4_2": ("processed",),
    "m4_3": ("basic_evidence", "audit_basic_evidence", "robustness", "audit_robustness"),
    "m4_4": ("walk_forward", "audit_walk_forward"),
    "m4_5": ("walk_forward", "redundancy", "audit_walk_forward", "audit_redundancy"),
    "m4_6": ("execution", "audit_execution"),
}
PIPELINE_STAGE_ORDER = (
    "processed",
    "basic_evidence",
    "audit_basic_evidence",
    "robustness",
    "audit_robustness",
    "walk_forward",
    "redundancy",
    "audit_walk_forward",
    "audit_redundancy",
    "execution",
    "audit_execution",
    "factor_explorer",
    "audit_factor_explorer",
)


def _ordered_stages(stages: set[str]) -> list[str]:
    return [stage for stage in PIPELINE_STAGE_ORDER if stage in stages]


class M4RunRequest(BaseModel):
    factor_release_id: str
    stages: tuple[str, ...] = Field(min_length=1)
    window_start: date
    window_end: date
    holding_sessions: int = 5
    quantile_count: int = Field(default=5, ge=2, le=20)
    minimum_pairs_per_session: int = Field(default=20, ge=3)
    processed_variants: tuple[str, ...] = ("WINSORIZED_ZSCORE", "SIZE_NEUTRALIZED")
    selection_quantile: float = Field(default=0.20, gt=0, lt=1)
    capital_scenarios_cny: tuple[int, ...] = (1_000_000, 10_000_000, 100_000_000)
    buy_commission_bps: float = Field(default=3.0, ge=0)
    sell_commission_bps: float = Field(default=3.0, ge=0)
    sell_stamp_duty_bps: float = Field(default=5.0, ge=0)
    base_slippage_bps: float = Field(default=2.0, ge=0)
    square_root_impact_bps: float = Field(default=20.0, ge=0)
    maximum_slippage_bps: float = Field(default=100.0, ge=0)
    maximum_participation_rate: float = Field(default=0.10, gt=0, le=1)

    @field_validator("holding_sessions")
    @classmethod
    def supported_holding_period(cls, value: int) -> int:
        if value not in {5, 10, 20, 30}:
            raise ValueError("holding_sessions must be 5, 10, 20, or 30")
        return value

    @field_validator("stages")
    @classmethod
    def known_unique_stages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or not set(value).issubset(STAGE_LABELS):
            raise ValueError("stages must be unique M4.1-M4.6 identifiers")
        return value

    @field_validator("processed_variants")
    @classmethod
    def known_variants(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"WINSORIZED_ZSCORE", "SIZE_NEUTRALIZED"}
        if len(value) != len(set(value)) or not set(value).issubset(allowed):
            raise ValueError("unsupported processed variant")
        return value

    @model_validator(mode="after")
    def valid_scope(self) -> M4RunRequest:
        if self.window_end < self.window_start:
            raise ValueError("window_end must not precede window_start")
        if tuple(sorted(set(self.capital_scenarios_cny))) != self.capital_scenarios_cny:
            raise ValueError("capital scenarios must be sorted and unique")
        if any(value <= 0 for value in self.capital_scenarios_cny):
            raise ValueError("capital scenarios must be positive")
        return self


class FactorComputeRequest(BaseModel):
    factor_id: str
    factor_version: str
    start: date
    end: date

    @model_validator(mode="after")
    def valid_scope(self) -> FactorComputeRequest:
        if self.end < self.start:
            raise ValueError("end must not precede start")
        catalog = {item.factor_id: item for item in (*alpha158_catalog(), *jqdata_catalog())}
        catalog.update({item.spec.factor_id: item.spec for item in m4_2_factor_entries()})
        item = catalog.get(self.factor_id)
        if item is None:
            raise ValueError("factor is not in the current, Alpha158, or JQData catalog")
        if item.factor_version != self.factor_version:
            raise ValueError("factor version does not match the catalog")
        return self


class FactorBatchRequest(BaseModel):
    factors: tuple[dict[str, str], ...] = Field(min_length=1, max_length=195)
    start: date
    end: date
    stages: tuple[str, ...] = ()
    holding_sessions: int = 5
    quantile_count: int = Field(default=5, ge=2, le=20)
    minimum_pairs_per_session: int = Field(default=20, ge=3)
    processed_variants: tuple[str, ...] = ("WINSORIZED_ZSCORE", "SIZE_NEUTRALIZED")
    selection_quantile: float = Field(default=0.20, gt=0, lt=1)
    capital_scenarios_cny: tuple[int, ...] = (1_000_000, 10_000_000, 100_000_000)
    buy_commission_bps: float = Field(default=3.0, ge=0)
    sell_commission_bps: float = Field(default=3.0, ge=0)
    sell_stamp_duty_bps: float = Field(default=5.0, ge=0)
    base_slippage_bps: float = Field(default=2.0, ge=0)
    square_root_impact_bps: float = Field(default=20.0, ge=0)
    maximum_slippage_bps: float = Field(default=100.0, ge=0)
    maximum_participation_rate: float = Field(default=0.10, gt=0, le=1)

    @model_validator(mode="after")
    def valid_scope(self) -> FactorBatchRequest:
        if self.end < self.start:
            raise ValueError("end must not precede start")
        if len({item.get("factor_id") for item in self.factors}) != len(self.factors):
            raise ValueError("batch factors must be unique")
        catalog = {item.factor_id: item for item in (*alpha158_catalog(), *jqdata_catalog())}
        catalog.update({item.spec.factor_id: item.spec for item in m4_2_factor_entries()})
        for item in self.factors:
            known = catalog.get(item.get("factor_id"))
            if known is None or known.factor_version != item.get("factor_version"):
                raise ValueError(f"factor or version is not in the current catalog: {item.get('factor_id')}")
        if len(self.stages) != len(set(self.stages)) or not set(self.stages).issubset(STAGE_LABELS):
            raise ValueError("unsupported or duplicate M4 stages")
        if self.holding_sessions not in {5, 10, 20, 30}:
            raise ValueError("holding_sessions must be 5, 10, 20, or 30")
        if "m4_5" in self.stages and len(self.factors) < 2:
            raise ValueError("M4.5 needs at least two selected factors")
        return self


def _batch_stage_closure(stages: tuple[str, ...]) -> list[str]:
    required = set(stages)
    if "m4_5" in required:
        required.update(("m4_1", "m4_2", "m4_3", "m4_4"))
    if "m4_4" in required:
        required.update(("m4_1", "m4_2", "m4_3"))
    if "m4_3" in required:
        required.update(("m4_1", "m4_2"))
    if "m4_6" in required:
        required.add("m4_1")
    return [stage for stage in STAGE_LABELS if stage in required]


def _factor_releases(project_root: Path) -> list[dict[str, Any]]:
    catalog_items = build_factor_catalog_overview(project_root)
    name_index = {
        (item["factor_id"], item["factor_version"]): (item["chinese_name"], item["english_name"])
        for item in catalog_items
    }
    name_by_id = {item["factor_id"]: (item["chinese_name"], item["english_name"]) for item in catalog_items}

    def localized_name(item: dict[str, Any]) -> tuple[str, str]:
        return name_index.get(
            (item["factor_id"], item["factor_version"]),
            name_by_id.get(item["factor_id"], (item["factor_id"], item["factor_id"])),
        )

    result = []
    for path in sorted((project_root / "data" / "factor_store" / "releases").glob("*/manifest.json")):
        payload = json.loads(path.read_bytes())
        request = payload["request"]
        verification_path = path.parent / "accuracy_verification.json"
        verification = json.loads(verification_path.read_bytes()) if verification_path.exists() else {}
        result.append(
            {
                "release_id": payload["release_id"],
                "factor_count": payload["factor_count"],
                "instrument_count": payload["instrument_count"],
                "session_count": payload["session_count"],
                "start": request["start"],
                "end": request["end"],
                "variant": request["variant"],
                "accuracy_status": verification.get("status", "NOT_REQUIRED"),
                "factors": [
                    {
                        "factor_id": item["factor_id"],
                        "factor_version": item["factor_version"],
                        "chinese_name": localized_name(item)[0],
                        "english_name": localized_name(item)[1],
                    }
                    for item in request["factors"]
                ],
            }
        )
    return sorted(result, key=lambda item: (item["end"], item["factor_count"]), reverse=True)


def _open_sessions(database: Path) -> list[date]:
    with duckdb.connect(str(database), read_only=True) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT cal_date FROM research.trading_calendar WHERE exchange='SSE' AND is_open ORDER BY cal_date"
            ).fetchall()
        ]


def _walk_forward_folds(database: Path, request: M4RunRequest) -> list[dict[str, Any]]:
    sessions = [value for value in _open_sessions(database) if request.window_start <= value <= request.window_end]
    by_year: dict[int, list[date]] = {}
    for session in sessions:
        by_year.setdefault(session.year, []).append(session)
    eligible_years = [year for year in sorted(by_year) if year - 2 in by_year and year - 1 in by_year]
    test_years = eligible_years[-3:]
    if not test_years:
        raise ValueError("Walk-Forward requires at least three calendar years in the selected window")
    index = {session: position for position, session in enumerate(sessions)}
    folds = []
    for test_year in test_years:
        test_candidates = [value for value in by_year[test_year] if value.month > 1 or value.day >= 10]
        validation_candidates = [value for value in by_year[test_year - 1] if value.month > 1 or value.day >= 10]
        if not test_candidates or not validation_candidates:
            raise ValueError(f"insufficient sessions for walk-forward fold {test_year}")
        test_start = test_candidates[0]
        validation_start = validation_candidates[0]
        train_end_index = index[validation_start] - request.holding_sessions - 1
        validation_end_index = index[test_start] - request.holding_sessions - 1
        if train_end_index < 0 or validation_end_index <= index[validation_start]:
            raise ValueError(f"selected window is too short for {request.holding_sessions}-session protection")
        folds.append(
            {
                "fold_id": f"WF-{test_year}-H{request.holding_sessions}",
                "train_start": request.window_start.isoformat(),
                "train_end": sessions[train_end_index].isoformat(),
                "validation_start": validation_start.isoformat(),
                "validation_end": sessions[validation_end_index].isoformat(),
                "test_start": test_start.isoformat(),
                "test_end": min(by_year[test_year][-1], request.window_end).isoformat(),
                "exposure_status": "RETROSPECTIVE_DIAGNOSTIC",
                "label_horizon_sessions": request.holding_sessions,
                "purge_sessions": request.holding_sessions,
                "embargo_sessions": request.holding_sessions,
            }
        )
    return folds


def _semantic_tag(request: M4RunRequest) -> str:
    return content_hash(request.model_dump(mode="json")).removeprefix("sha256:")[:12].upper()


def build_pipeline_config(project_root: Path, request: M4RunRequest, job_id: str) -> M4PipelineConfig:
    releases = {item["release_id"]: item for item in _factor_releases(project_root)}
    release = releases.get(request.factor_release_id)
    if release is None:
        raise ValueError("selected factor release does not exist")
    if release.get("accuracy_status") == "FAIL":
        raise ValueError("selected factor release failed accuracy verification and cannot start a new M4 run")
    if request.window_start < date.fromisoformat(release["start"]) or request.window_end > date.fromisoformat(
        release["end"]
    ):
        raise ValueError("selected window must stay inside the factor release coverage")
    selected = set(request.stages)
    stages = {stage for name in selected for stage in PIPELINE_STAGE_BY_UI[name]}
    tag = _semantic_tag(request)
    config: dict[str, Any] = {
        "schema_version": "1",
        "batch_id": f"M4-UI-{job_id}",
        "paths": {
            "database": "data/warehouse/alpha_research.duckdb",
            "factor_store": "data/factor_store",
            "evidence_store": "data/evidence_store",
            "report": f"reports/m4_runs/{job_id}.json",
        },
        "stages": _ordered_stages(stages),
        "raw_factor_release_id": request.factor_release_id,
        "processed_factor_release_ids": [],
        "processed_variants": list(request.processed_variants) if "m4_2" in selected else [],
        "basic_evidence": {
            "window_start": request.window_start.isoformat(),
            "window_end": request.window_end.isoformat(),
            "holding_sessions": request.holding_sessions,
            "quantile_count": request.quantile_count,
            "minimum_pairs_per_session": request.minimum_pairs_per_session,
        },
    }
    if "m4_3" in selected:
        config["robustness"] = {"family_id": f"M4-3-UI-{tag}"}
    if selected & {"m4_4", "m4_5"}:
        config["walk_forward"] = {
            "family_id": f"M4-4-UI-{tag}",
            "window_start": request.window_start.isoformat(),
            "window_end": request.window_end.isoformat(),
            "folds": _walk_forward_folds(project_root / "data" / "warehouse" / "alpha_research.duckdb", request),
        }
    if "m4_5" in selected:
        config["redundancy"] = {
            "family_id": f"M4-5-UI-{tag}",
            "candidate_policy": "ALL_CANONICAL",
        }
        stages.update(("factor_explorer", "audit_factor_explorer"))
        config["stages"] = _ordered_stages(stages)
        config["factor_explorer"] = {
            "report_name": f"M4-UI-{tag}",
            "title": "Alpha Research OS · M4 Evidence Explorer",
            "output_root": "reports/factor_explorer",
        }
    if "m4_6" in selected:
        config["execution"] = {
            "window_start": request.window_start.isoformat(),
            "window_end": request.window_end.isoformat(),
            "holding_sessions": request.holding_sessions,
            "selection_quantile": request.selection_quantile,
            "capital_scenarios_cny": request.capital_scenarios_cny,
            "buy_commission_bps": request.buy_commission_bps,
            "sell_commission_bps": request.sell_commission_bps,
            "sell_stamp_duty_bps": request.sell_stamp_duty_bps,
            "base_slippage_bps": request.base_slippage_bps,
            "square_root_impact_bps": request.square_root_impact_bps,
            "maximum_slippage_bps": request.maximum_slippage_bps,
            "maximum_participation_rate": request.maximum_participation_rate,
        }
    return M4PipelineConfig.model_validate(config)


class JobManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.run_root = project_root / "reports" / "m4_runs"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.active_job_id: str | None = None

    def preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = M4RunRequest.model_validate(payload)
        config = build_pipeline_config(self.project_root, request, "PREFLIGHT")
        release = next(
            item for item in _factor_releases(self.project_root) if item["release_id"] == request.factor_release_id
        )
        variant_count = 1 + (len(request.processed_variants) if "m4_2" in request.stages else 0)
        entity_count = release["factor_count"] * variant_count
        warnings = [
            "M4.7 就是当前控制与结果界面，始终可用。",
            "当前历史区间已被研究人员看过，因此属于诊断样本，不是全新的盲测样本。",
        ]
        if "m4_5" in request.stages:
            pair_count = entity_count * (entity_count - 1) // 2
            warnings.append(f"M4.5 预计比较 {entity_count} 个因子版本、约 {pair_count} 对关系，因子多时会较慢。")
        if "m4_6" in request.stages:
            warnings.append("M4.6 使用日线成交代理做容量压力测试，不等同于逐笔订单簿撮合。")
        return {
            "status": "READY",
            "requested_stages": request.stages,
            "resolved_stages": config.stages,
            "config_id": config.config_id,
            "factor_release_id": request.factor_release_id,
            "holding_sessions": request.holding_sessions,
            "factor_count": release["factor_count"],
            "estimated_factor_variant_entities": entity_count,
            "estimated_pair_correlations": entity_count * (entity_count - 1) // 2,
            "warnings": warnings,
        }

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = M4RunRequest.model_validate(payload)
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError(f"job {self.active_job_id} is already running")
            job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            config = build_pipeline_config(self.project_root, request, job_id)
            config_path = self.run_root / f"{job_id}.config.json"
            config_path.write_bytes(canonical_json_bytes(config) + b"\n")
            log_path = self.run_root / f"{job_id}.log"
            log_stream = log_path.open("wb")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join((str(SRC_ROOT), str(PROJECT_ROOT)))
            self.process = subprocess.Popen(
                [sys.executable, "scripts/run_m4_pipeline.py", "--config", str(config_path)],
                cwd=self.project_root,
                env=environment,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
            )
            self.active_job_id = job_id
            threading.Thread(target=self._wait_and_close, args=(self.process, log_stream), daemon=True).start()
        return self.status(job_id)

    @staticmethod
    def _wait_and_close(process: subprocess.Popen[bytes], stream: Any) -> None:
        process.wait()
        stream.close()

    def stop(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if self.active_job_id != job_id or self.process is None or self.process.poll() is not None:
                raise ValueError("job is not running")
            self.process.terminate()
        return self.status(job_id)

    def delete(self, job_id: str) -> dict[str, Any]:
        """Delete one finished run record without touching immutable factor/evidence data."""
        if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", job_id):
            raise ValueError("invalid job id")
        with self.lock:
            if self.active_job_id == job_id and self.process is not None and self.process.poll() is None:
                raise RuntimeError("a running job cannot be deleted; stop it first")
            paths = [
                self.run_root / f"{job_id}.json",
                self.run_root / f"{job_id}.config.json",
                self.run_root / f"{job_id}.log",
            ]
            existing = [path for path in paths if path.exists()]
            if not existing:
                raise FileNotFoundError(job_id)
            for path in existing:
                path.unlink()
        return {
            "job_id": job_id,
            "deleted": True,
            "preserved": ["raw factor release", "immutable evidence cache", "other run records"],
        }

    def status(self, job_id: str) -> dict[str, Any]:
        report_path = self.run_root / f"{job_id}.json"
        config_path = self.run_root / f"{job_id}.config.json"
        log_path = self.run_root / f"{job_id}.log"
        if not config_path.exists() and not report_path.exists():
            raise FileNotFoundError(job_id)
        report = json.loads(report_path.read_bytes()) if report_path.exists() else None
        configured = json.loads(config_path.read_bytes())["stages"] if config_path.exists() else []
        is_active = self.active_job_id == job_id and self.process is not None and self.process.poll() is None
        if report is not None and report.get("status") in {"PASS", "FAIL"}:
            status = report["status"]
        elif is_active:
            status = "RUNNING"
        else:
            status = "STOPPED"
        stages = [] if report is None else list(report.get("stages", {}))
        explorer = None
        if report is not None:
            explorer = report.get("stages", {}).get("factor_explorer", {}).get("result", {}).get("index")
        log_tail = ""
        if log_path.exists():
            log_tail = log_path.read_bytes()[-12_000:].decode("utf-8", errors="replace")
        return {
            "job_id": job_id,
            "status": status,
            "configured_stages": configured,
            "completed_stages": stages,
            "current_stage": next((stage for stage in configured if stage not in stages), None) if is_active else None,
            "report_available": report is not None,
            "explorer_available": explorer is not None,
            "error": None if report is None else report.get("error"),
            "log_tail": log_tail,
        }

    def latest(self) -> dict[str, Any] | None:
        configs = sorted(self.run_root.glob("*.config.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        return None if not configs else self.status(configs[0].name.removesuffix(".config.json"))

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class FactorJobManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.run_root = project_root / "reports" / "factor_jobs"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.active_job_id: str | None = None

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = FactorComputeRequest.model_validate(payload)
        jqdata_account_factors: set[str] = set()
        if request.factor_id in jqdata_account_factors and (
            not os.environ.get("JQDATA_USERNAME", "").strip() or not os.environ.get("JQDATA_PASSWORD", "")
        ):
            raise ValueError(
                "JQData 尚未配置：请在启动后端前设置 JQDATA_USERNAME 和 JQDATA_PASSWORD 环境变量"
            )
        database = self.project_root / "data" / "warehouse" / "alpha_research.duckdb"
        with duckdb.connect(str(database), read_only=True) as connection:
            lower, upper = connection.execute(
                "SELECT min(trade_date), max(trade_date) FROM research.market_daily"
            ).fetchone()
        if lower is None or request.start < lower or request.end > upper:
            raise ValueError(f"calculation window must stay inside available universe data {lower}..{upper}")
        with self.lock:
            if self.running():
                raise RuntimeError(f"factor job {self.active_job_id} is already running")
            job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            request_path = self.run_root / f"{job_id}.request.json"
            request_path.write_bytes(canonical_json_bytes(request) + b"\n")
            result_path = self.run_root / f"{job_id}.result.json"
            log_path = self.run_root / f"{job_id}.log"
            stream = log_path.open("wb")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join((str(SRC_ROOT), str(PROJECT_ROOT)))
            current_factor_ids = {item.spec.factor_id for item in m4_2_factor_entries()}
            publisher = (
                "scripts/publish_factor_release.py" if request.factor_id in current_factor_ids
                else "scripts/publish_jqdata_factor.py" if request.factor_id.startswith("jqdata-")
                else "scripts/publish_alpha158_factor.py"
            )
            command = [
                sys.executable, publisher,
                "--factor-id", request.factor_id,
                "--start", request.start.isoformat(),
                "--end", request.end.isoformat(),
                "--result", str(result_path),
            ]
            if request.factor_id in current_factor_ids:
                command.extend(("--catalog-profile", "m4.2"))
            self.process = subprocess.Popen(
                command,
                cwd=self.project_root,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            self.active_job_id = job_id
            threading.Thread(target=JobManager._wait_and_close, args=(self.process, stream), daemon=True).start()
        return self.status(job_id)

    def stop(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if self.active_job_id != job_id or not self.running():
                raise ValueError("factor job is not running")
            assert self.process is not None
            self.process.terminate()
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        request_path = self.run_root / f"{job_id}.request.json"
        result_path = self.run_root / f"{job_id}.result.json"
        log_path = self.run_root / f"{job_id}.log"
        if not request_path.exists():
            raise FileNotFoundError(job_id)
        request = json.loads(request_path.read_bytes())
        active = self.active_job_id == job_id and self.running()
        result = json.loads(result_path.read_bytes()) if result_path.exists() else None
        exit_code = self.process.poll() if self.active_job_id == job_id and self.process is not None else None
        if result is not None:
            status = "PASS"
        elif active:
            status = "RUNNING"
        elif exit_code not in (None, 0):
            status = "FAIL"
        else:
            status = "STOPPED"
        log_tail = log_path.read_bytes()[-12_000:].decode("utf-8", errors="replace") if log_path.exists() else ""
        started_at = request_path.stat().st_mtime
        finished_at = (
            result_path.stat().st_mtime
            if result_path.exists()
            else log_path.stat().st_mtime if status != "RUNNING" and log_path.exists() else None
        )
        elapsed_seconds = max(0, round((finished_at or datetime.now().timestamp()) - started_at))
        total_years = date.fromisoformat(request["end"]).year - date.fromisoformat(request["start"]).year + 1
        completed_years = len(set(re.findall(r"year=(\d{4}) completed", log_tail)))
        last_log_line = next((line.strip() for line in reversed(log_tail.splitlines()) if line.strip()), "")
        verification: dict[str, Any] = {}
        if result and result.get("release_id"):
            release_dir = (
                self.project_root / "data" / "factor_store" / "releases"
                / result["release_id"].removeprefix("sha256:")
            )
            verification = accuracy_status(release_dir)
        if result is not None:
            if verification.get("status") == "FAIL":
                phase, progress, message = "准确性复核失败", 100, verification.get("error", "候选版本复核失败。")
            elif verification.get("status") == "PENDING":
                phase, progress, message = "计算完成，后台复核中", 100, "候选因子值已可使用，准确性复核正在后台运行。"
            else:
                calculation = result.get("calculation") or {}
                phase, progress, message = (
                    "发布完成", 100,
                    calculation.get("message") or "因子值已通过准确性复核并发布。",
                )
        elif status == "FAIL":
            phase, progress, message = "计算失败", 100, last_log_line or "任务异常退出，请查看运行日志。"
        elif status == "STOPPED":
            phase, progress, message = "已停止", 0, "计算任务已经停止。"
        elif "publishing metadata" in log_tail:
            phase, progress, message = "登记因子版本", 96, "正在登记不可变因子版本。"
        elif "quality checking" in log_tail:
            phase, progress, message = "检查数据质量", 90, "正在检查覆盖率、重复键和非有限值。"
        elif "accuracy checking" in log_tail:
            phase, progress, message = "准确性复核", 78, "正在与串行参考结果逐键核对。"
        elif "combining" in log_tail:
            phase, progress, message = "合并年度结果", 70, "年度分片已完成，正在确定性合并。"
        elif completed_years:
            progress = min(65, 10 + round(55 * completed_years / total_years))
            phase = f"年度并行计算 {completed_years}/{total_years}"
            message = f"已完成 {completed_years} 个年度，任务仍在运行。"
        elif "materializing" in log_tail:
            phase, progress, message = "计算因子值", 10, "计算进程正在运行，年度任务已经启动。"
        else:
            phase, progress, message = "准备任务", 3, "任务已接收，正在启动计算进程。"
        return {
            "job_id": job_id,
            "status": status,
            "factor_id": request["factor_id"],
            "factor_version": request["factor_version"],
            "start": request["start"],
            "end": request["end"],
            "release_id": result.get("release_id") if result else None,
            "result": result,
            "log_tail": log_tail,
            "phase": phase,
            "progress": progress,
            "message": message,
            "elapsed_seconds": elapsed_seconds,
            "accuracy_status": verification.get("status", result.get("accuracy_status") if result else None),
        }

    def latest(self) -> dict[str, Any] | None:
        requests = sorted(self.run_root.glob("*.request.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        return None if not requests else self.status(requests[0].name.removesuffix(".request.json"))


_BATCH_STAGE_NAMES = {
    "processed": "生成处理版本", "basic_evidence": "收益与分组证据",
    "audit_basic_evidence": "基础证据审计", "robustness": "稳健性检验",
    "audit_robustness": "稳健性审计", "walk_forward": "滚动时间检验",
    "audit_walk_forward": "滚动检验审计", "redundancy": "M4.5 去重与增量价值",
    "audit_redundancy": "M4.5 去重审计", "factor_explorer": "生成结果报告",
    "audit_factor_explorer": "结果报告审计", "execution": "成交与容量检验",
    "audit_execution": "成交检验审计",
}


def _batch_log_detail(path: Path) -> str | None:
    if not path.exists():
        return None
    tail = path.read_bytes()[-16_000:].decode("utf-8", errors="replace")
    lines = tail.splitlines()
    last_stage = next((index for index in range(len(lines) - 1, -1, -1)
                       if re.match(r"m4_stage=\w+ started$", lines[index].strip())), None)
    if last_stage is not None:
        lines = lines[last_stage + 1:]
    for line in reversed(lines):
        year = re.search(r"(?:conditional|daily|variant=\S+|factor=\S+) .*?year=(\d{4})", line)
        if year:
            variant = re.search(r"variant=(\S+)", line)
            subject = f" · {variant.group(1)}" if variant else ""
            return f"正在处理 {year.group(1)} 年{subject}"
        if "combining yearly partitions" in line:
            return "正在合并年度结果"
        if "quality checking" in line:
            return "正在检查数据质量"
        if "publishing metadata" in line:
            return "正在登记结果"
    return None


def _batch_m4_steps(project_root: Path, job_id: str | None, log_path: Path) -> dict[str, Any] | None:
    if not job_id:
        return None
    root = project_root / "reports" / "m4_runs"
    config_path = root / f"{job_id}.config.json"
    if not config_path.exists():
        return None
    config = json.loads(config_path.read_bytes())
    stages = config.get("stages") or []
    report_path = root / f"{job_id}.json"
    report = json.loads(report_path.read_bytes()) if report_path.exists() else {}
    completed = set(report.get("stages") or {})
    current = report.get("current_stage")
    if not current and report.get("status") == "RUNNING":
        current = next((stage for stage in stages if stage not in completed), None)
    steps = [{"id": stage, "label": _BATCH_STAGE_NAMES.get(stage, stage),
              "status": "PASS" if stage in completed else "RUNNING" if stage == current else "WAITING"}
             for stage in stages]
    started = report.get("current_stage_started_at")
    elapsed = max(0, int((datetime.now().astimezone() - datetime.fromisoformat(started)).total_seconds())) if started else None
    return {"completed": len(completed), "total": len(stages),
            "current": current, "current_label": _BATCH_STAGE_NAMES.get(current, current) if current else None,
            "detail": _batch_log_detail(log_path), "elapsed_seconds": elapsed,
            "steps": steps}


class FactorBatchManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.run_root = project_root / "reports" / "factor_batches"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.active_job_id: str | None = None

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = FactorBatchRequest.model_validate(payload)
        database = self.project_root / "data/warehouse/alpha_research.duckdb"
        with duckdb.connect(str(database), read_only=True) as connection:
            lower, upper = connection.execute(
                "SELECT min(trade_date), max(trade_date) FROM research.market_daily"
            ).fetchone()
        if lower is None or request.start < lower or request.end > upper:
            raise ValueError(f"计算日期必须在原始数据覆盖范围 {lower} 至 {upper} 内")
        catalog = {item["factor_id"]: item for item in build_factor_catalog_overview(self.project_root)}
        items = []
        for factor in request.factors:
            entry = catalog[factor["factor_id"]]
            coverage = entry.get("coverage") or {}
            items.append({
                "factor_id": factor["factor_id"], "name": entry["chinese_name"],
                "coverage": coverage, "action": "已覆盖，可复用" if coverage.get("start", "9999") <= request.start.isoformat()
                and coverage.get("end", "0000") >= request.end.isoformat() else "计算或补齐",
            })
        resolved = _batch_stage_closure(request.stages)
        entities = len(items) * (1 + (len(request.processed_variants) if "m4_2" in resolved else 0))
        pairs = entities * (entities - 1) // 2 if "m4_5" in resolved else 0
        warnings = ["M4.5 将对本批次通过前置检验的因子共同运行。"] if pairs else []
        if pairs > 10_000:
            warnings.append(f"M4.5 约需比较 {pairs:,} 对因子版本，预计耗时较长；建议缩小本批选择范围。")
        return {
            "status": "READY", "start": request.start.isoformat(), "end": request.end.isoformat(),
            "count": len(items), "items": items, "requested_stages": list(request.stages),
            "resolved_stages": resolved,
            "added_stages": [stage for stage in resolved if stage not in request.stages],
            "estimated_pair_correlations": pairs,
            "warnings": warnings,
        }

    def start(self, payload: dict[str, Any], reuse_items: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        plan = self.preflight(payload)
        request = FactorBatchRequest.model_validate(payload)
        with self.lock:
            if self.running():
                raise RuntimeError(f"factor batch {self.active_job_id} is already running")
            job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            request_path = self.run_root / f"{job_id}.request.json"
            saved = request.model_dump(mode="json")
            saved["resolved_stages"] = plan["resolved_stages"]
            saved["reuse_items"] = reuse_items or {}
            names = {item["factor_id"]: item["name"] for item in plan["items"]}
            saved["factors"] = [{**item, "name": names[item["factor_id"]]} for item in saved["factors"]]
            request_path.write_bytes(canonical_json_bytes(saved) + b"\n")
            log_stream = (self.run_root / f"{job_id}.log").open("wb")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join((str(SRC_ROOT), str(PROJECT_ROOT)))
            self.process = subprocess.Popen(
                [sys.executable, "scripts/run_factor_batch.py", "--request", str(request_path)],
                cwd=self.project_root, env=environment, stdout=log_stream, stderr=subprocess.STDOUT,
            )
            self.active_job_id = job_id
            threading.Thread(target=JobManager._wait_and_close, args=(self.process, log_stream), daemon=True).start()
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", job_id):
            raise ValueError("invalid batch id")
        request_path = self.run_root / f"{job_id}.request.json"
        if not request_path.exists():
            raise FileNotFoundError(job_id)
        request = json.loads(request_path.read_bytes())
        state_path = self.run_root / f"{job_id}.state.json"
        state = json.loads(state_path.read_bytes()) if state_path.exists() else {}
        active = self.active_job_id == job_id and self.running()
        status = state.get("status", "RUNNING")
        if status == "RUNNING" and not active and self.active_job_id == job_id:
            status = "FAIL"
        if not state:
            state = {"phase": "准备任务", "cohort_job_id": None, "cohort_status": "NOT_RUN", "error": None, "items": [
                {"factor_id": item["factor_id"], "name": item["name"], "status": "WAITING", "phase": "等待计算"}
                for item in request["factors"]]}
        completed = sum(item["status"] in {"PASS", "FAIL"} for item in state["items"])
        current = next((item for item in state["items"] if item["status"] == "RUNNING"), None)
        individual_stages = {stage for name in request["resolved_stages"] if name != "m4_5"
                             for stage in PIPELINE_STAGE_BY_UI[name]}
        factor_total = 1 + len(individual_stages)
        has_cohort = "m4_5" in request["resolved_stages"]
        cohort_pipeline = _batch_m4_steps(
            self.project_root, state.get("cohort_job_id"), self.run_root / f"{job_id}.cohort.log"
        ) if has_cohort else None
        cohort_total = (1 + (cohort_pipeline["total"] if cohort_pipeline else 7)) if has_cohort else 0
        total_steps = len(state["items"]) * factor_total + cohort_total
        completed_steps = 0
        progress_units = 0.0
        activity: dict[str, Any] | None = None
        total_years = date.fromisoformat(request["end"]).year - date.fromisoformat(request["start"]).year + 1
        for index, item in enumerate(state["items"]):
            pipeline = _batch_m4_steps(
                self.project_root, item.get("m4_job_id"), self.run_root / f"{job_id}.{index}.m4.log"
            )
            item["stage_progress"] = pipeline
            if item["status"] in {"PASS", "FAIL"}:
                units = float(factor_total)
                whole = factor_total
            elif item.get("release_id"):
                whole = 1 + (pipeline["completed"] if pipeline else 0)
                units = float(whole)
            elif item["status"] == "RUNNING":
                factor_log = self.run_root / f"{job_id}.{index}.factor.log"
                tail = factor_log.read_bytes()[-32_000:].decode("utf-8", errors="replace") if factor_log.exists() else ""
                finished_years = len(set(re.findall(r"year=(\d{4}) completed", tail)))
                whole = 0
                units = min(0.95, finished_years / total_years)
                item["factor_years"] = {"completed": finished_years, "total": total_years}
            else:
                whole = 0
                units = 0.0
            completed_steps += whole
            progress_units += units
            item["progress"] = round(100 * units / factor_total)
            if item is current:
                detail = pipeline["detail"] if pipeline else _batch_log_detail(
                    self.run_root / f"{job_id}.{index}.factor.log"
                )
                activity = {"title": item["name"],
                            "stage": pipeline["current_label"] if pipeline else item["phase"],
                            "detail": detail, "stage_progress": pipeline}
        if has_cohort:
            if state.get("cohort_status") in {"PASS", "FAIL", "SKIPPED"}:
                cohort_whole = cohort_total
            elif state.get("cohort_status") == "RUNNING":
                cohort_whole = (1 + (cohort_pipeline["completed"] if cohort_pipeline else 0)) if state.get("cohort_job_id") else 0
                activity = {"title": "本批联合 M4.5",
                            "stage": cohort_pipeline["current_label"] if cohort_pipeline else "合并因子数据",
                            "detail": cohort_pipeline["detail"] if cohort_pipeline else None,
                            "stage_progress": cohort_pipeline}
            else:
                cohort_whole = 0
            completed_steps += cohort_whole
            progress_units += cohort_whole
        progress = round(100 * progress_units / total_steps) if total_steps else 0
        if status == "RUNNING":
            progress = min(99, progress)
        elif status in {"PASS", "PARTIAL", "FAIL"}:
            progress = 100
        finished_at = state.get("completed_at") if status != "RUNNING" else None
        elapsed_seconds = 0
        if state.get("started_at"):
            end_time = datetime.fromisoformat(finished_at) if finished_at else datetime.now().astimezone()
            elapsed_seconds = max(0, int((end_time - datetime.fromisoformat(state["started_at"])).total_seconds()))
        return {**state, "status": status, "batch_id": job_id, "request": request,
                "completed": completed, "total": len(state["items"]), "progress": progress,
                "completed_steps": completed_steps, "total_steps": total_steps,
                "activity": activity, "cohort_stage_progress": cohort_pipeline,
                "elapsed_seconds": elapsed_seconds,
                "stop_requested": (self.run_root / f"{job_id}.stop").exists()}

    def stop(self, job_id: str) -> dict[str, Any]:
        current = self.status(job_id)
        if current["status"] != "RUNNING":
            raise ValueError("batch is not running")
        (self.run_root / f"{job_id}.stop").write_text("stop after current factor\n", encoding="utf-8")
        return self.status(job_id)

    def retry_failed(self, job_id: str) -> dict[str, Any]:
        current = self.status(job_id)
        if current["status"] == "RUNNING":
            raise RuntimeError("wait for the current batch before retrying")
        failures = {item["factor_id"] for item in current["items"]
                    if item["status"] == "FAIL" or current["status"] == "STOPPED" and item["status"] == "WAITING"}
        if "m4_5" in current["request"]["resolved_stages"]:
            failures = {item["factor_id"] for item in current["items"]}
        if not failures:
            raise ValueError("there are no failed factors to retry")
        payload = dict(current["request"])
        catalog = {item.factor_id: item for item in (*alpha158_catalog(), *jqdata_catalog())}
        catalog.update({item.spec.factor_id: item.spec for item in m4_2_factor_entries()})
        payload["factors"] = [
            {
                **item,
                "factor_version": catalog[item["factor_id"]].factor_version,
            }
            if item["factor_id"] in catalog else dict(item)
            for item in payload["factors"]
            if item["factor_id"] in failures
        ]
        if "m4_5" in payload["stages"] and len(payload["factors"]) < 2:
            payload["stages"] = [stage for stage in payload["stages"] if stage != "m4_5"]
        reuse_items = {
            item["factor_id"]: {
                "release_id": item["release_id"], "m4_job_id": item.get("m4_job_id"),
                "m4_retry": item["status"] == "FAIL",
            }
            for item in current["items"]
            if item.get("release_id") and (
                item["status"] == "PASS" or item["status"] == "FAIL" and item.get("m4_job_id")
            )
        }
        return self.start(payload, reuse_items=reuse_items)

    def latest(self) -> dict[str, Any] | None:
        requests = sorted(self.run_root.glob("*.request.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        return self.status(requests[0].name.removesuffix(".request.json")) if requests else None


def make_handler(
    project_root: Path,
    allowed_origins: set[str],
    manager: JobManager,
    factor_manager: FactorJobManager,
    batch_manager: FactorBatchManager,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/v1/health":
                self._json(HTTPStatus.OK, {"status": "ok", "service": "alpha-research-os-m4-control"})
                return
            if path == "/api/v1/m4/options":
                self._json(
                    HTTPStatus.OK,
                    {
                        "factor_releases": _factor_releases(project_root),
                        "stages": [
                            {"id": stage, "label": label, "optional": stage == "m4_6"}
                            for stage, label in STAGE_LABELS.items()
                        ],
                        "holding_sessions": [5, 10, 20, 30],
                        "processed_variants": ["WINSORIZED_ZSCORE", "SIZE_NEUTRALIZED"],
                    },
                )
                return
            if path == "/api/v1/factors/catalog":
                self._serve_factor_catalog(parsed.query)
                return
            if path == "/api/v1/factor-assets":
                self._serve_factor_assets(parsed.query)
                return
            if path == "/api/v1/factors/jobs/latest":
                self._json(HTTPStatus.OK, {"job": factor_manager.latest()})
                return
            if path == "/api/v1/factors/batches/latest":
                self._json(HTTPStatus.OK, {"batch": batch_manager.latest()})
                return
            if path == "/api/v1/factors/batches/options":
                database = project_root / "data/warehouse/alpha_research.duckdb"
                with duckdb.connect(str(database), read_only=True) as connection:
                    lower, upper = connection.execute(
                        "SELECT min(trade_date), max(trade_date) FROM research.market_daily"
                    ).fetchone()
                self._json(HTTPStatus.OK, {"start": lower.isoformat() if lower else None,
                                           "end": upper.isoformat() if upper else None})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[:4] == ["api", "v1", "factors", "batches"]:
                self._json(HTTPStatus.OK, batch_manager.status(parts[4]))
                return
            if len(parts) == 5 and parts[:4] == ["api", "v1", "factors", "jobs"]:
                self._handle_factor_status(parts[4])
                return
            if path == "/api/v1/m4/jobs/latest":
                self._json(HTTPStatus.OK, {"job": manager.latest()})
                return
            if len(parts) >= 5 and parts[:4] == ["api", "v1", "m4", "jobs"]:
                job_id = parts[4]
                if len(parts) == 5:
                    self._handle_status(job_id)
                    return
                if len(parts) == 6 and parts[5] == "report":
                    self._serve_report(job_id, project_root)
                    return
                if len(parts) >= 6 and parts[5] == "explorer":
                    self._serve_explorer(job_id, parts[6:], project_root)
                    return
            self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            parts = path.strip("/").split("/")
            try:
                payload = self._body()
                if path == "/api/v1/m4/preflight":
                    self._json(HTTPStatus.OK, manager.preflight(payload))
                    return
                if path == "/api/v1/factors/batches/preflight":
                    self._json(HTTPStatus.OK, batch_manager.preflight(payload))
                    return
                if path == "/api/v1/factors/batches":
                    if manager.running() or factor_manager.running():
                        raise RuntimeError("请等待当前因子或 M4 任务完成后再开始批次")
                    self._json(HTTPStatus.ACCEPTED, batch_manager.start(payload))
                    return
                if len(parts) == 6 and parts[:4] == ["api", "v1", "factors", "batches"] and parts[5] == "stop":
                    self._json(HTTPStatus.OK, batch_manager.stop(parts[4]))
                    return
                if len(parts) == 6 and parts[:4] == ["api", "v1", "factors", "batches"] and parts[5] == "retry":
                    if manager.running() or factor_manager.running():
                        raise RuntimeError("请等待当前因子或 M4 任务完成后再重试")
                    self._json(HTTPStatus.ACCEPTED, batch_manager.retry_failed(parts[4]))
                    return
                if path == "/api/v1/m4/jobs":
                    if factor_manager.running() or batch_manager.running():
                        raise RuntimeError("a factor calculation is running; wait for it before starting M4")
                    self._json(HTTPStatus.ACCEPTED, manager.start(payload))
                    return
                parts = path.strip("/").split("/")
                if path == "/api/v1/factors/jobs":
                    if manager.running() or batch_manager.running():
                        raise RuntimeError("an M4 job is running; wait for it before calculating a factor")
                    self._json(HTTPStatus.ACCEPTED, factor_manager.start(payload))
                    return
                if len(parts) == 6 and parts[:4] == ["api", "v1", "factors", "jobs"] and parts[5] == "stop":
                    self._json(HTTPStatus.OK, factor_manager.stop(parts[4]))
                    return
                if len(parts) == 6 and parts[:4] == ["api", "v1", "m4", "jobs"] and parts[5] == "stop":
                    self._json(HTTPStatus.OK, manager.stop(parts[4]))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            except ValidationError as error:
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": "INVALID_RUN_REQUEST", "detail": error.errors(include_context=False)},
                )
            except (ValueError, FileNotFoundError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": type(error).__name__, "detail": str(error)})
            except RuntimeError as error:
                self._json(HTTPStatus.CONFLICT, {"error": "JOB_ALREADY_RUNNING", "detail": str(error)})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                parts = path.strip("/").split("/")
                if len(parts) == 5 and parts[:4] == ["api", "v1", "m4", "jobs"]:
                    self._json(HTTPStatus.OK, manager.delete(parts[4]))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            except FileNotFoundError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "JOB_NOT_FOUND"})
            except ValueError as error:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "INVALID_JOB_ID", "detail": str(error)})
            except RuntimeError as error:
                self._json(HTTPStatus.CONFLICT, {"error": "JOB_RUNNING", "detail": str(error)})

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _handle_status(self, job_id: str) -> None:
            try:
                self._json(HTTPStatus.OK, manager.status(job_id))
            except FileNotFoundError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "JOB_NOT_FOUND"})

        def _handle_factor_status(self, job_id: str) -> None:
            try:
                self._json(HTTPStatus.OK, factor_manager.status(job_id))
            except FileNotFoundError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "FACTOR_JOB_NOT_FOUND"})

        def _serve_factor_catalog(self, query_string: str) -> None:
            try:
                parameters = parse_qs(query_string)

                def first(name: str, default: str) -> str:
                    return parameters.get(name, [default])[-1]

                source = first("source", "ALL").upper()
                status = first("status", "ALL").upper()
                sort_by = first("sortBy", "category")
                sort_order = first("sortOrder", "asc").lower()
                if source not in {"ALL", "CURRENT", "ALPHA158", "JQDATA"}:
                    raise ValueError("unknown factor source")
                if status not in {
                    "ALL", "M4_COMPLETE", "CALCULATED", "CALCULATED_VERIFYING",
                    "ACCURACY_FAILED", "NOT_CALCULATED",
                }:
                    raise ValueError("unknown calculation status")
                if sort_by not in {"category", "name", "status", "factor_id"}:
                    raise ValueError("unknown sort field")
                if sort_order not in {"asc", "desc"}:
                    raise ValueError("sortOrder must be asc or desc")
                result = query_factor_catalog(
                    build_factor_catalog_overview(project_root),
                    page=int(first("page", "1")),
                    page_size=int(first("pageSize", "36")),
                    query=first("query", ""),
                    category=first("category", "全部"),
                    source=source,  # type: ignore[arg-type]
                    status=status,  # type: ignore[arg-type]
                    sort_by=sort_by,  # type: ignore[arg-type]
                    sort_order=sort_order,  # type: ignore[arg-type]
                )
                self._json(HTTPStatus.OK, result)
            except (TypeError, ValueError) as error:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "INVALID_QUERY", "detail": str(error)})

        def _serve_factor_assets(self, query_string: str) -> None:
            try:
                parameters = parse_qs(query_string)

                def first(name: str, default: str) -> str:
                    return parameters.get(name, [default])[-1]

                status = first("status", "ALL").upper()
                source = first("source", "ALL").upper()
                sort_order = first("sortOrder", "desc").lower()
                horizon_text = first("horizon", "")
                result = query_factor_assets(
                    build_factor_asset_library(project_root),
                    page=int(first("page", "1")),
                    page_size=int(first("pageSize", "12")),
                    query=first("query", ""),
                    horizon=int(horizon_text) if horizon_text else None,
                    source=source,  # type: ignore[arg-type]
                    status=status,  # type: ignore[arg-type]
                    sort_order=sort_order,  # type: ignore[arg-type]
                )
                self._json(HTTPStatus.OK, result)
            except (TypeError, ValueError) as error:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "INVALID_QUERY", "detail": str(error)})

        def _serve_report(self, job_id: str, root: Path) -> None:
            path = root / "reports" / "m4_runs" / f"{job_id}.json"
            if not path.exists():
                self._json(HTTPStatus.NOT_FOUND, {"error": "REPORT_NOT_FOUND"})
                return
            payload = json.loads(path.read_bytes())
            body = (
                "<!doctype html><meta charset='utf-8'><title>M4 report</title>"
                "<style>body{background:#0b1020;color:#e8ecf5;font:14px ui-monospace,monospace;padding:32px}"
                "pre{white-space:pre-wrap;max-width:1200px;margin:auto}</style><pre>"
                + html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
                + "</pre>"
            ).encode()
            self._bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")

        def _serve_explorer(self, job_id: str, relative: list[str], root: Path) -> None:
            report_path = root / "reports" / "m4_runs" / f"{job_id}.json"
            if not report_path.exists():
                self._json(HTTPStatus.NOT_FOUND, {"error": "REPORT_NOT_FOUND"})
                return
            report = json.loads(report_path.read_bytes())
            index_value = report.get("stages", {}).get("factor_explorer", {}).get("result", {}).get("index")
            if not index_value:
                self._json(HTTPStatus.NOT_FOUND, {"error": "EXPLORER_NOT_AVAILABLE"})
                return
            index_path = Path(index_value)
            if not index_path.is_absolute():
                index_path = root / index_path
            explorer_root = index_path.resolve().parent
            target = (explorer_root / Path(*relative)).resolve() if relative else explorer_root / "index.html"
            if explorer_root not in target.parents and target != explorer_root:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_PATH"})
                return
            if target.is_dir():
                target = target / "index.html"
            if not target.exists():
                self._json(HTTPStatus.NOT_FOUND, {"error": "ASSET_NOT_FOUND"})
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._bytes(HTTPStatus.OK, target.read_bytes(), content_type)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("request body must be between 1 byte and 1 MB")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _json(self, status: HTTPStatus, payload: object) -> None:
            self._bytes(
                status,
                json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(),
                "application/json; charset=utf-8",
            )

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
            if origin in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--allow-origin", action="append")
    args = parser.parse_args()
    root = args.project_root.resolve()
    origins = set(args.allow_origin or ("http://127.0.0.1:8872", "http://localhost:8872"))
    manager = JobManager(root)
    factor_manager = FactorJobManager(root)
    batch_manager = FactorBatchManager(root)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(root, origins, manager, factor_manager, batch_manager))
    print(f"M4 control API: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
