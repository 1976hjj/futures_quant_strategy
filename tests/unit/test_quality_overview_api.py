# ruff: noqa: E501
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from alpha_research_os.reporting.quality_overview import build_quality_overview
from scripts.serve_research_api import make_handler


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(root: Path) -> None:
    report_hash = "a" * 64
    reports = root / "reports"
    _write(
        reports / "warehouse_audit.json",
        {
            "status": "PASSED_WITH_WARNINGS",
            "audited_at": "2026-09-02T20:56:57+08:00",
            "tables": {
                "daily": {"duplicate_keys": 0, "null_keys": 0, "invalid_ohlc": 7, "negative_volume": 0, "negative_amount": 0, "tradable_view_rows": 100, "min_trade_date": "2025-01-01", "max_trade_date": "2025-12-31"},
                "adj_factor": {"duplicate_keys": 0, "null_keys": 0},
            },
            "warnings": ["isolated"],
            "failures": [],
        },
    )
    _write(reports / "factor_explorer" / "latest.json", {"report_id": f"sha256:{report_hash}"})
    _write(
        reports / "factor_explorer" / report_hash / "evidence-summary.json",
        {
            "report": {"report_id": f"sha256:{report_hash}", "window": {"start": "2025-01-01", "end": "2025-12-31"}},
            "factors": [
                {"entity_id": "RAW|factor|1.0.0", "factor_id": "factor", "factor_version": "1.0.0", "variant": "RAW", "basic_evidence": {"mean_coverage": 0.97, "window": {"start": "2025-01-01", "end": "2025-03-31"}}}
            ],
        },
    )
    _write(reports / "m2b_reference_audit.json", {"status": "PASS", "calendar": {"min_date": "2025-01-01", "max_date": "2025-12-31"}, "security_session_state": {"duplicate_keys": 0, "unknown_st_rows": 2, "after_delisting_rows": 0, "current_name_fallback_rows": 3}, "warnings": [], "failures": []})
    _write(reports / "m2c_corporate_action_audit.json", {"status": "PASS", "approval_gate": {"matched_but_quarantined": 4, "approved_dividend_adjustments": 5}, "diagnostic_statuses": {"UNEXPLAINED_PRICE_ADJUSTMENT": 6}, "warnings": [], "failures": []})
    _write(reports / "m2d_financial_audit.json", {"status": "PASS", "canonical_count": 9, "pit_exception_counts": {"MISSING_AVAILABLE_DATE": 2}, "failures": []})
    _write(reports / "m3_2_factor_release_audit.json", {"status": "PASS", "duplicate_key_count": 0, "outside_universe_count": 0, "clock_error_count": 0, "quality_summary": {"nonfinite_count": 0}, "failures": []})
    _write(root / "data" / "tushare_financial_archive" / "run_status.json", {"summary": {"coverage": {"start": "2025-01-01", "end": "2025-12-31"}}})


def test_quality_overview_is_built_from_current_artifacts(tmp_path: Path) -> None:
    _fixture(tmp_path)
    first = build_quality_overview(tmp_path)
    assert first["summary"]["isolated_or_exception_count"] == 13
    assert first["summary"]["low_coverage_entity_count"] == 1
    assert first["summary"]["integrity_error_count"] == 0
    warehouse = tmp_path / "reports" / "warehouse_audit.json"
    payload = json.loads(warehouse.read_text(encoding="utf-8"))
    payload["tables"]["daily"]["invalid_ohlc"] = 8
    _write(warehouse, payload)
    second = build_quality_overview(tmp_path)
    assert second["summary"]["invalid_ohlc_count"] == 8
    assert second["generated_at"] >= first["generated_at"]


def test_http_endpoint_refreshes_and_limits_cors(tmp_path: Path) -> None:
    _fixture(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path, "http://127.0.0.1:8871"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/quality/overview"
        request = Request(url, headers={"Origin": "http://127.0.0.1:8871"})
        with urlopen(request) as response:
            payload = json.load(response)
            assert payload["summary"]["invalid_ohlc_count"] == 7
            assert response.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:8871"
        with urlopen(url) as response:
            assert response.headers.get("Access-Control-Allow-Origin") is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
