import json
from datetime import datetime

import pytest

from scripts import data_update
from scripts.data_maintenance import CHINA, maintenance_inventory, update_history


def job(root, name, status, groups, updated_groups, updated_at):
    folder = root / "reports/data_updates"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.request.json").write_text(json.dumps({"groups": groups, "end": "2026-10-01"}))
    (folder / f"{name}.progress.json").write_text(json.dumps({
        "status": status, "updated_groups": updated_groups, "updated_at": updated_at,
    }))


def test_failed_and_empty_jobs_do_not_reset_weekly_reminder(tmp_path):
    job(tmp_path, "a", "PASS", ["market"], ["market"], "2026-10-01T20:00:00+08:00")
    job(tmp_path, "b", "FAIL", ["market"], ["market"], "2026-10-12T20:00:00+08:00")
    job(tmp_path, "c", "PASS", ["market"], [], "2026-10-12T20:00:00+08:00")
    result = maintenance_inventory(tmp_path, {"groups": [
        {"id": "market", "name": "行情", "published": True, "end": "2026-10-01"},
    ]}, datetime(2026, 10, 12, 21, tzinfo=CHINA))
    row = result["ledger"][0]
    assert row["days_since_success"] == 11
    assert row["state"] == "due"
    assert result["next_weekly_at"] == "2026-10-19T20:30:00+08:00"


def test_china_time_long_unused_missing_and_unpublished(tmp_path):
    result = maintenance_inventory(tmp_path, {"groups": [
        {"id": "market", "name": "行情", "published": True, "last_collected_at": "2026-09-12T13:00:00Z"},
        {"id": "financial", "name": "财务", "published": False, "last_collected_at": "2026-10-12T09:00:00Z"},
        {"id": "corporate", "name": "分红", "published": True},
    ]}, datetime(2026, 10, 12, 20, tzinfo=CHINA))
    assert [row["state"] for row in result["ledger"]] == ["long_unused", "missing", "missing"]
    assert result["ledger"][0]["days_since_success"] == 30
    assert result["next_weekly_at"] == "2026-10-12T20:30:00+08:00"


def test_ledger_ignores_future_timestamps_and_newer_failed_job(tmp_path):
    job(tmp_path, "a", "PASS", ["market"], ["market"], "2027-01-01T20:00:00+08:00")
    result = maintenance_inventory(tmp_path, {"groups": [
        {"id": "market", "name": "行情", "published": True, "last_collected_at": "bad-timestamp"},
    ]}, datetime(2026, 10, 12, 20, tzinfo=CHINA))
    assert result["ledger"][0]["state"] == "missing"


def test_history_is_paginated_bounded_and_keeps_failure(tmp_path):
    job(tmp_path, "a", "PASS", ["market"], ["market"], "2026-10-01T20:00:00+08:00")
    job(tmp_path, "b", "FAIL", ["financial"], [], "2026-10-02T20:00:00+08:00")
    first = update_history(tmp_path, 1, 1)
    assert first["items"][0]["status"] == "FAIL"
    assert first["total_items"] == first["total_pages"] == 2
    assert not update_history(tmp_path, 3, 1)["items"]
    for page, size in ((0, 10), (1, 101)):
        with pytest.raises(ValueError):
            update_history(tmp_path, page, size)


def test_legacy_job_with_skipped_stages_is_not_assumed_to_refresh_all_sources(tmp_path):
    folder = tmp_path / "reports/data_updates"
    folder.mkdir(parents=True)
    (folder / "legacy.request.json").write_text(json.dumps({"groups": ["market", "financial"]}))
    (folder / "legacy.progress.json").write_text(json.dumps({
        "status": "PASS", "total_groups": 1, "completed_groups": 1,
        "updated_at": "2026-10-12T19:00:00+08:00",
    }))
    result = maintenance_inventory(tmp_path, {"groups": [
        {"id": "market", "name": "market", "published": True},
    ]}, datetime(2026, 10, 12, 20, tzinfo=CHINA))
    assert result["ledger"][0]["last_success_at"] is None


def test_financial_refresh_is_not_skipped_when_coverage_date_unchanged(monkeypatch, tmp_path):
    end = data_update.latest_complete_day()
    monkeypatch.setattr(data_update, "inventory", lambda _: {"groups": [
        {"id": group, "name": group, "published": True, "end": str(end)}
        for group in data_update.LEGACY_GROUPS
    ], "factors": []})
    monkeypatch.setattr(data_update, "tushare_token", lambda _: "configured")
    refreshed = data_update.plan(tmp_path, data_update.DataUpdateRequest(end=end, groups=["financial"]))
    assert refreshed["stages"][0]["needs_update"]
    unchanged = data_update.plan(tmp_path, data_update.DataUpdateRequest(
        end=end, groups=["financial"], refresh_recent_periods=0))
    assert not unchanged["stages"][0]["needs_update"]
