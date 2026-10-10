"""Sector factors share calculation routes and stay outside stock-selection M4."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from alpha_research_os.factors.bank_timing import bank_timing_catalog, bank_timing_overview
from alpha_research_os.reporting.bank_timing_readiness import attach_bank_timing_readiness
from alpha_research_os.reporting.factor_catalog_overview import build_factor_catalog_overview, query_factor_catalog
from scripts.serve_m4_control_api import FactorBatchRequest, FactorComputeRequest
from scripts.serve_strategy_backtest_api import rotation_options, strategy_options


def test_definitions_are_visible_searchable_and_paginated_without_values(tmp_path):
    items = build_factor_catalog_overview(tmp_path)
    result = query_factor_catalog(items, source="BANK_TIMING", page_size=4, page=2)
    assert result["totalItems"] == 10
    assert result["totalPages"] == 3
    assert len(result["items"]) == 4
    assert result["counts"]["bank_timing"] == 10
    assert result["counts"]["bank"] == 16
    definitions = query_factor_catalog(items, source="BANK_TIMING", page_size=100)["items"]
    assert sum(item["research_batch"] == 1 for item in definitions) == 7
    assert sum(item["research_batch"] == 2 for item in definitions) == 3
    for item in definitions:
        assert item["status"] == "NOT_CALCULATED"
        assert item["calculated"] is item["m4_completed"] is False
        assert item["compute_supported"] is True and item["m4_supported"] is False
        assert item["latest_release_id"] is item["result"] is item["coverage"] is None
        assert item["observation_level"] == "SECTOR"
    search = query_factor_catalog(items, source="BANK_TIMING", query="年度盈利", category="估值")
    assert [item["factor_id"] for item in search["items"]] == ["bank-sector-earnings-yield-history-percentile"]
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("indicator", bank_timing_catalog(), ids=lambda item: item.factor_id)
def test_sector_factors_share_compute_routes_but_reject_stock_m4(indicator):
    reference = dict(factor_id=indicator.factor_id, factor_version=indicator.factor_version)
    FactorComputeRequest(**reference, start="2020-01-02", end="2025-12-31")
    FactorBatchRequest(factors=[reference], start="2020-01-02", end="2025-12-31")
    with pytest.raises(ValidationError, match="个股横截面 M4"):
        FactorBatchRequest(factors=[reference], start="2020-01-02", end="2025-12-31", stages=["m4_1"])


def test_mixed_stock_and_sector_batch_is_supported_without_sector_cohort_members():
    request = FactorBatchRequest(
            factors=[
                dict(factor_id="bank-cash-dividend-yield-365", factor_version="1.0.0"),
                dict(factor_id="bank-sector-trend-breadth", factor_version="1.0.0"),
            ],
            start="2020-01-02", end="2025-12-31", stages=["m4_1"],
        )
    assert len(request.factors) == 2
    with pytest.raises(ValidationError, match="stock factors"):
        FactorBatchRequest(**request.model_dump(exclude={"stages"}), stages=["m4_5"])


def test_even_a_materialized_sector_is_excluded_from_stock_strategy_choices(monkeypatch, tmp_path):
    sector = bank_timing_overview()[0]
    sector.update(calculated=True, latest_release_id="fake-sector-release")
    stock = dict(sector, factor_id="stock", observation_level="STOCK", source_collection="BANK")
    monkeypatch.setattr("scripts.serve_strategy_backtest_api.build_factor_catalog_overview", lambda _: [sector, stock])
    assert [item["factor_id"] for item in strategy_options(tmp_path)["factors"]] == ["stock"]
    assert [item["factor_id"] for item in rotation_options(tmp_path)["factors"]] == ["stock"]


def test_input_pack_readiness_is_separate_from_computed_status_and_hash_checked(tmp_path):
    folder = tmp_path / "data/bank_timing_store/packs/example"
    folder.mkdir(parents=True)
    audit = {"status": "PASS_WITH_EXPLICIT_LIMITATIONS", "benchmark_status": "PARTIAL",
             "benchmark_missing_sessions": ["2020-06-18"], "indicator_inputs": [
                 {"factor_id": bank_timing_catalog()[0].factor_id, "ready_sessions": 300,
                  "session_count": 1000, "latest_ready": True, "latest_coverage": .8}]}
    audit_bytes = json.dumps(audit).encode()
    (folder / "audit.json").write_bytes(audit_bytes)
    manifest = {"pack_id": "sha256:example", "created_at": "2026-10-10", "start": "2020-01-02",
                "end": "2025-12-31", "historical_grade": "RESEARCH_ONLY", "future_labels": False,
                "files": {"audit.json": hashlib.sha256(audit_bytes).hexdigest()}}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    items = attach_bank_timing_readiness(tmp_path, bank_timing_overview())
    assert items[0]["data_readiness"]["indicator"]["ready_sessions"] == 300
    assert "2020-06-18" in items[0]["dependency_note"]
    assert items[0]["status"] == "NOT_CALCULATED" and items[0]["result"] is None
    assert items[0]["compute_supported"] is True
    (folder / "audit.json").write_bytes(b"tampered")
    invalid = attach_bank_timing_readiness(tmp_path, bank_timing_overview())
    assert invalid[0]["data_readiness"]["status"] == "INVALID_PACK"
    assert invalid[0]["data_readiness"]["indicator"] is None
