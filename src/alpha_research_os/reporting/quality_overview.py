# ruff: noqa: E501
"""Read-only quality overview assembled from the latest published audit artifacts."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPORT_ID = re.compile(r"^sha256:([a-f0-9]{64})$")


def _read_json(path: Path, *, required: bool = True) -> tuple[dict[str, Any] | None, str | None]:
    for attempt in range(3):
        try:
            value = json.loads(path.read_bytes())
            if not isinstance(value, dict):
                raise ValueError("JSON root must be an object")
            return value, None
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as error:
            if attempt < 2:
                time.sleep(0.05)
                continue
            if required:
                raise ValueError(f"cannot read required quality artifact {path}: {error}") from error
            return None, f"{path.name}: {error}"
    raise AssertionError("unreachable")


def _sum_values(values: dict[str, Any] | None) -> int:
    return sum(int(value or 0) for value in (values or {}).values())


def _status(ok: bool, findings: bool = False) -> str:
    if not ok:
        return "FAIL"
    return "PASS_WITH_FINDINGS" if findings else "PASS"


def build_quality_overview(project_root: Path) -> dict[str, Any]:
    """Build a fresh response on every call without opening or mutating DuckDB."""
    root = project_root.resolve()
    reports = root / "reports"
    warnings: list[str] = []

    warehouse, _ = _read_json(reports / "warehouse_audit.json")
    latest, _ = _read_json(reports / "factor_explorer" / "latest.json")
    assert warehouse is not None and latest is not None
    match = _REPORT_ID.fullmatch(str(latest.get("report_id", "")))
    if match is None:
        raise ValueError("latest factor explorer report_id is invalid")
    evidence_path = reports / "factor_explorer" / match.group(1) / "evidence-summary.json"
    evidence, _ = _read_json(evidence_path)
    assert evidence is not None
    if evidence.get("report", {}).get("report_id") != latest["report_id"]:
        raise ValueError("latest factor explorer pointer and evidence report do not match")

    optional: dict[str, dict[str, Any]] = {}
    for key, name in {
        "reference": "m2b_reference_audit.json",
        "corporate_actions": "m2c_corporate_action_audit.json",
        "financial": "m2d_financial_audit.json",
        "factor_release": "m3_2_factor_release_audit.json",
    }.items():
        value, warning = _read_json(reports / name, required=False)
        optional[key] = value or {}
        if warning:
            warnings.append(warning)

    tables = warehouse.get("tables", {})
    daily = tables.get("daily", {})
    table_rows = list(tables.values())
    duplicate_keys = sum(int(item.get("duplicate_keys") or 0) for item in table_rows)
    null_keys = sum(int(item.get("null_keys") or 0) for item in table_rows)
    invalid_ohlc = int(daily.get("invalid_ohlc") or 0)

    reference = optional["reference"]
    security_state = reference.get("security_session_state", {})
    corporate = optional["corporate_actions"]
    approval_gate = corporate.get("approval_gate", {})
    quarantined = int(approval_gate.get("matched_but_quarantined") or 0)
    financial = optional["financial"]
    pit_exceptions = _sum_values(financial.get("pit_exception_counts"))
    factor_release = optional["factor_release"]
    factor_quality = factor_release.get("quality_summary", {})
    factor_integrity_errors = sum(
        int(factor_release.get(key) or factor_quality.get(key) or 0)
        for key in ("duplicate_key_count", "outside_universe_count", "clock_error_count", "nonfinite_count")
    )

    factor_entities = evidence.get("factors", [])
    low_coverage = []
    for factor in factor_entities:
        coverage = factor.get("basic_evidence", {}).get("mean_coverage")
        if isinstance(coverage, (int, float)) and coverage < 0.98:
            low_coverage.append(
                {
                    "entity_id": factor.get("entity_id"),
                    "factor_id": factor.get("factor_id"),
                    "factor_version": factor.get("factor_version"),
                    "variant": factor.get("variant"),
                    "coverage": coverage,
                    "window": factor.get("basic_evidence", {}).get("window"),
                }
            )

    checks = [
        {"id": "market_keys", "name": "行情主键", "scope": "daily / adj_factor / daily_basic", "status": _status(duplicate_keys == 0 and null_keys == 0), "facts": {"duplicate_keys": duplicate_keys, "null_keys": null_keys}},
        {"id": "market_prices", "name": "行情价格关系", "scope": "research.market_daily_anomalies", "status": _status(True, invalid_ohlc > 0), "facts": {"isolated_invalid_ohlc": invalid_ohlc, "tradable_rows": daily.get("tradable_view_rows")}},
        {"id": "market_volume", "name": "成交量与成交额", "scope": "daily", "status": _status(int(daily.get("negative_volume") or 0) == 0 and int(daily.get("negative_amount") or 0) == 0), "facts": {"negative_volume": daily.get("negative_volume", 0), "negative_amount": daily.get("negative_amount", 0)}},
        {"id": "security_state", "name": "历史证券状态", "scope": "security_session_state", "status": _status(int(security_state.get("duplicate_keys") or 0) == 0, int(security_state.get("unknown_st_rows") or 0) > 0), "facts": {"unknown_st_rows": security_state.get("unknown_st_rows"), "after_delisting_rows": security_state.get("after_delisting_rows"), "current_name_fallback_rows": security_state.get("current_name_fallback_rows")}},
        {"id": "financial_pit", "name": "财务可知时间", "scope": "四类财务版本表", "status": _status(not financial.get("failures"), pit_exceptions > 0), "facts": {"pit_exceptions": pit_exceptions, "canonical_versions": financial.get("canonical_count")}},
        {"id": "corporate_actions", "name": "公司行动对账", "scope": "股息事件 × 复权因子", "status": _status(not corporate.get("failures"), quarantined > 0), "facts": {"quarantined_matches": quarantined, "approved_adjustments": approval_gate.get("approved_dividend_adjustments"), "unexplained_price_adjustments": corporate.get("diagnostic_statuses", {}).get("UNEXPLAINED_PRICE_ADJUSTMENT")}},
        {"id": "factor_integrity", "name": "因子发布完整性", "scope": "主键 / 股票池 / 时钟 / 非有限值", "status": _status(factor_integrity_errors == 0), "facts": {"integrity_errors": factor_integrity_errors, "audit_status": factor_release.get("status")}},
        {"id": "factor_coverage", "name": "因子基础覆盖率", "scope": "各实体 basic_evidence 独立窗口", "status": _status(True, bool(low_coverage)), "facts": {"below_98_percent": len(low_coverage), "entity_count": len(factor_entities)}},
    ]

    ingestion_status: dict[str, dict[str, Any]] = {}
    for directory in ("tushare_archive", "tushare_reference_archive", "tushare_financial_archive"):
        value, warning = _read_json(root / "data" / directory / "run_status.json", required=False)
        if value:
            ingestion_status[directory] = value
        if warning:
            warnings.append(warning)

    report = evidence.get("report", {})
    coverage = [
        {"id": "market", "name": "日行情", "start": daily.get("min_trade_date"), "end": daily.get("max_trade_date"), "meaning": "AUDIT_RANGE"},
        {"id": "reference", "name": "参考数据", "start": reference.get("calendar", {}).get("min_date"), "end": reference.get("calendar", {}).get("max_date"), "meaning": "AUDIT_RANGE"},
        {"id": "financial", "name": "财务归档", **ingestion_status.get("tushare_financial_archive", {}).get("summary", {}).get("coverage", {}), "meaning": "ARCHIVE_RANGE"},
        {"id": "factor_evidence", "name": "因子证据", **report.get("window", {}), "meaning": "REPORT_RANGE"},
    ]
    audit_batches = [
        {"id": "market", "name": "行情审计", "status": warehouse.get("status"), "audited_at": warehouse.get("audited_at"), "finding_count": len(warehouse.get("warnings", [])), "failure_count": len(warehouse.get("failures", []))},
        {"id": "reference", "name": "参考与股票池", "status": reference.get("status"), "audited_at": reference.get("audited_at"), "finding_count": len(reference.get("warnings", [])), "failure_count": len(reference.get("failures", []))},
        {"id": "corporate_actions", "name": "公司行动", "status": corporate.get("status"), "audited_at": corporate.get("audited_at"), "finding_count": len(corporate.get("warnings", [])), "failure_count": len(corporate.get("failures", []))},
        {"id": "financial", "name": "财务 PIT", "status": financial.get("status"), "audited_at": financial.get("audited_at"), "finding_count": 0, "failure_count": len(financial.get("failures", []))},
        {"id": "factor_release", "name": "因子发布", "status": factor_release.get("status"), "audited_at": factor_release.get("audited_at"), "finding_count": 0, "failure_count": len(factor_release.get("failures", []))},
    ]

    source_paths = [reports / "warehouse_audit.json", reports / "m2b_reference_audit.json", reports / "m2c_corporate_action_audit.json", reports / "m2d_financial_audit.json", reports / "m3_2_factor_release_audit.json", reports / "factor_explorer" / "latest.json", evidence_path]
    source_versions = [
        {"path": path.relative_to(root).as_posix(), "modified_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).astimezone().isoformat()}
        for path in source_paths
        if path.exists()
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).astimezone().isoformat(),
        "report_id": report.get("report_id"),
        "report_window": report.get("window"),
        "summary": {
            "audit_domain_count": len(audit_batches),
            "integrity_error_count": factor_integrity_errors + duplicate_keys + null_keys,
            "isolated_or_exception_count": invalid_ohlc + quarantined + pit_exceptions,
            "invalid_ohlc_count": invalid_ohlc,
            "quarantined_corporate_action_count": quarantined,
            "pit_exception_count": pit_exceptions,
            "low_coverage_entity_count": len(low_coverage),
            "factor_entity_count": len(factor_entities),
        },
        "checks": checks,
        "coverage": coverage,
        "audit_batches": audit_batches,
        "low_coverage_entities": low_coverage,
        "source_versions": source_versions,
        "warnings": warnings,
    }
