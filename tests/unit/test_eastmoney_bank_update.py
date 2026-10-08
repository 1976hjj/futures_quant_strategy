from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from alpha_research_os.data.providers.eastmoney_bank import normalize_row
from scripts import data_update, sync_eastmoney_banks
from scripts.data_update import DataUpdateRequest


def bank_row(code="600036.SH", *, report="2026-06-30", notice="2026-08-29"):
    return {"SECUCODE": code, "ORG_TYPE": "银行", "REPORT_DATE": report,
            "NOTICE_DATE": notice, "UPDATE_DATE": notice, "REPORT_TYPE": "中报",
            "NONPERLOAN": 0.94, "BLDKBBL": 385.1, "HXYJBCZL": 14.07,
            "NEWCAPITALADER": 18.33, "FIRST_ADEQUACY_RATIO": 16.2,
            "NET_INTEREST_MARGIN": 1.83}


def test_normalizer_preserves_null_and_actual_acquisition_availability():
    raw = bank_row()
    raw["NET_INTEREST_MARGIN"] = None
    record = normalize_row(raw, {"code": "600036.SH", "name": "招商银行"},
                           retrieved_at="2026-10-08T00:00:00+00:00", requested_end=date(2026, 10, 8),
                           run_id="test", raw_sha256="abc")
    assert record["net_interest_margin"] is None
    assert record["available_at"] == "2026-10-08T00:00:00+00:00"
    assert record["provider_notice_date"] == "2026-08-29"
    assert record["verified_notice_date"] is None
    assert "HISTORY_UNVERIFIED" in record["pit_grade"]
    assert record["missing_core"] == '["net_interest_margin"]'
    assert normalize_row(raw, {"code": "600036.SH"}, retrieved_at="2026-10-08T00:00:00+00:00",
                         requested_end=date(2026, 8, 28), run_id="test", raw_sha256="abc") is None


def test_bank_identity_and_nonfinite_values_block():
    args = dict(retrieved_at="2026-10-08T00:00:00+00:00", requested_end=date(2026, 10, 8),
                run_id="test", raw_sha256="abc")
    with pytest.raises(ValueError, match="identity mismatch"):
        normalize_row(bank_row(), {"code": "601398.SH"}, **args)
    raw = bank_row()
    raw["NONPERLOAN"] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        normalize_row(raw, {"code": "600036.SH"}, **args)


def test_old_reports_without_notice_are_retained_but_flagged():
    raw = bank_row(report="2005-12-31")
    raw["NOTICE_DATE"] = None
    raw["UPDATE_DATE"] = None
    record = normalize_row(raw, {"code": "600036.SH"}, retrieved_at="2026-10-08T00:00:00+00:00",
                           requested_end=date(2026, 10, 8), run_id="test", raw_sha256="abc")
    assert record["provider_notice_date"] is None
    assert "missing_provider_notice_date" in json.loads(record["quality_flags"])
    assert record["available_at"] == "2026-10-08T00:00:00+00:00"


def _root(tmp_path):
    (tmp_path / "config").mkdir()
    banks = [{"code": "600036.SH", "name": "招商银行", "bank_type": "股份制银行"},
             {"code": "601398.SH", "name": "工商银行", "bank_type": "国有大型银行"}]
    (tmp_path / "config" / "eastmoney_banks.json").write_text(json.dumps({"banks": banks}), encoding="utf-8")
    return tmp_path


def test_publish_is_versioned_and_failure_keeps_previous_snapshot(monkeypatch, tmp_path):
    root = _root(tmp_path)
    fail = False

    def response(code, page):
        if fail and code == "601398.SH":
            raise ValueError("upstream identity changed")
        value = {"success": True, "result": {"pages": 1, "data": [bank_row(code)]}}
        return json.dumps(value).encode(), {"p": str(page)}

    monkeypatch.setattr(sync_eastmoney_banks, "fetch_page", response)
    first = sync_eastmoney_banks.sync(root, date.today(), min_free_gb=0)
    second = sync_eastmoney_banks.sync(root, date.today(), min_free_gb=0)
    assert first["run_id"] != second["run_id"]
    with duckdb.connect(str(root / "data/warehouse/eastmoney_bank.duckdb"), read_only=True) as connection:
        assert connection.execute("select count(*) from bank_financial_snapshots").fetchone()[0] == 4
        assert connection.execute("select count(*) from bank_financial_current").fetchone()[0] == 2
        current_run = connection.execute("select distinct run_id from bank_financial_current").fetchone()[0]
        assert current_run == second["run_id"]
    checkpoint = (root / "data/eastmoney_bank_archive/checkpoint.json").read_bytes()
    fail = True
    with pytest.raises(RuntimeError, match="previous published snapshot retained"):
        sync_eastmoney_banks.sync(root, date.today(), min_free_gb=0)
    assert (root / "data/eastmoney_bank_archive/checkpoint.json").read_bytes() == checkpoint
    assert len(list((root / "data/eastmoney_bank_archive/runs").glob("*/summary.json"))) == 3
    assert all((Path(r["run_directory"]) / "normalized.parquet").exists() for r in [first, second])


def test_free_plan_ignores_broken_token_and_uses_snapshot_day(monkeypatch, tmp_path):
    def broken_token(_):
        raise ValueError("expired or malformed paid credential")

    monkeypatch.setattr(data_update, "tushare_token", broken_token)
    today = date.today().isoformat()
    groups = [{"id": group, "name": group, "end": "2026-06-30", "published": True,
               "snapshot_day": today, "snapshot_through": today} for group in data_update.GROUPS]
    monkeypatch.setattr(data_update, "inventory", lambda _: {"groups": groups, "factors": []})
    request = DataUpdateRequest(groups=["bank_free"], end=date.today())
    prepared = data_update.plan(tmp_path, request)
    assert prepared["can_start"] and not prepared["token_required"]
    assert not prepared["strategy_ready_if_complete"]
    assert prepared["required_unselected"] == []
    assert not prepared["stages"][0]["needs_update"]
    groups[-1]["snapshot_day"] = (date.today() - timedelta(days=1)).isoformat()
    assert data_update.plan(tmp_path, request)["stages"][0]["needs_update"]
    with pytest.raises(ValueError, match="one source"):
        DataUpdateRequest(source_id="eastmoney", groups=["market", "bank_free"])


def test_china_day_boundary_and_future_update_validation():
    # China 00:30 is still the previous UTC day; source day must use China time.
    raw = bank_row(notice="2026-08-29")
    raw["UPDATE_DATE"] = "2026-10-08"
    record = normalize_row(raw, {"code": "600036.SH"}, retrieved_at="2026-10-07T16:30:00+00:00",
                           requested_end=date(2026, 10, 8), run_id="test", raw_sha256="abc")
    assert record is not None
    raw["UPDATE_DATE"] = "2026-10-09"
    with pytest.raises(ValueError, match="future"):
        normalize_row(raw, {"code": "600036.SH"}, retrieved_at="2026-10-07T16:30:00+00:00",
                      requested_end=date(2026, 10, 8), run_id="test", raw_sha256="abc")


def test_free_job_strips_paid_token_and_never_reads_credentials(monkeypatch, tmp_path):
    from scripts import data_update_api

    captured = {}

    class Process:
        def poll(self):
            return None

    def popen(command, **kwargs):
        captured.update(kwargs)
        return Process()

    def forbidden(_):
        raise AssertionError("free job must not read paid credentials")

    monkeypatch.setenv("TUSHARE_TOKEN", "expired-test-value")
    monkeypatch.setattr(data_update_api, "tushare_token", forbidden)
    monkeypatch.setattr(data_update_api, "plan", lambda *_: {
        "token_available": False, "stages": [{"id": "bank_free", "needs_update": True}],
    })
    monkeypatch.setattr(data_update_api.subprocess, "Popen", popen)
    manager = data_update_api.DataUpdateManager(tmp_path)
    job = manager.start({"groups": ["bank_free"], "end": date.today().isoformat()})
    assert job["status"] == "RUNNING"
    assert job["request"]["source_id"] == "eastmoney"
    assert "TUSHARE_TOKEN" not in captured["env"]


def test_atomic_progress_retries_transient_windows_reader(monkeypatch, tmp_path):
    original = Path.replace
    calls = []

    def replace(path, destination):
        calls.append(destination)
        if len(calls) == 1:
            raise PermissionError("temporary Windows read handle")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(sync_eastmoney_banks.time, "sleep", lambda _: None)
    target = tmp_path / "progress.json"
    sync_eastmoney_banks.atomic_json(target, {"progress": 42})
    assert len(calls) == 2
    assert json.loads(target.read_text())["progress"] == 42


def test_bank_token_subset_does_not_advance_market_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(data_update, "bank_inventory", lambda _: {"published": False})
    monkeypatch.setattr(data_update, "_published", lambda *_: True)
    folder = tmp_path / "data/tushare_archive"
    folder.mkdir(parents=True)
    (folder / "checkpoint.json").write_text(json.dumps({
        "completed": {api: {"20260922": {}} for api in ("daily", "adj_factor", "daily_basic")},
    }), encoding="utf-8")
    warehouse = tmp_path / "data/warehouse"
    warehouse.mkdir()
    summary = {"published": True, "groups": {"market": {
        "banks": 42, "expected_banks": 42, "end": "2026-09-30",
    }}}
    (warehouse / "bank_token_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    market = next(g for g in data_update.inventory(tmp_path)["groups"] if g["id"] == "market")
    assert market["end"] == "2026-09-22"
    assert "bank_research" not in market
    industry = data_update.inventory(tmp_path)["industries"][0]
    assert industry["datasets"][0]["end"] == "2026-09-30"
    summary["published"] = False
    (warehouse / "bank_token_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    market = next(g for g in data_update.inventory(tmp_path)["groups"] if g["id"] == "market")
    assert "bank_research" not in market
