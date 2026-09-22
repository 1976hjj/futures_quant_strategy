from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from scripts import data_update
from scripts.data_update import DataUpdateRequest, plan
from scripts.data_update_api import DataUpdateManager


def test_data_update_rejects_future_and_duplicate_groups() -> None:
    with pytest.raises(ValidationError, match="not yet complete"):
        DataUpdateRequest(end=date.today() + timedelta(days=1))
    with pytest.raises(ValidationError, match="duplicates"):
        DataUpdateRequest(groups=["market", "market"])


def test_latest_complete_day_waits_for_market_data_publication() -> None:
    assert data_update.latest_complete_day(datetime(2026, 9, 22, 6)) == date(2026, 9, 21)
    assert data_update.latest_complete_day(datetime(2026, 9, 22, 18)) == date(2026, 9, 22)


def test_plan_requires_unselected_stale_dependencies(monkeypatch, tmp_path) -> None:
    target = data_update.latest_complete_day()
    end = target.isoformat()
    groups = [
        {"id": group, "name": group, "end": "2020-01-01" if group == "market" else end,
         "published": True}
        for group in data_update.GROUPS
    ]
    monkeypatch.setattr(data_update, "inventory", lambda _: {
        "groups": groups, "factors": [{"id": "jqdata-test", "start": "2020-01-01", "end": end}],
    })
    with pytest.raises(ValueError, match="requires market"):
        plan(tmp_path, DataUpdateRequest(end=target, groups=["corporate"], factor_ids=[]))
    prepared = plan(tmp_path, DataUpdateRequest(end=target, groups=["market", "corporate"], factor_ids=[]))
    assert [stage["id"] for stage in prepared["stages"]] == ["market", "corporate"]
    assert prepared["required_unselected"] == []


def test_data_job_status_survives_manager_recreation(tmp_path) -> None:
    first = DataUpdateManager(tmp_path)
    job_id = "20260921-200000-abcdef"
    paths = first._paths(job_id)
    paths["request.json"].write_text(json.dumps({"end": "2026-09-21"}), encoding="utf-8")
    paths["progress.json"].write_text(
        json.dumps({"status": "PASS", "phase": "done", "progress": 100}), encoding="utf-8"
    )
    paths["log"].write_text("updated market", encoding="utf-8")
    restored = DataUpdateManager(tmp_path).latest()
    assert restored is not None
    assert restored["job_id"] == job_id
    assert restored["status"] == "PASS"
    assert restored["log_tail"] == "updated market"


def test_data_update_uses_existing_archive_endpoint(tmp_path) -> None:
    archive = tmp_path / "data" / "tushare_reference_archive"
    archive.mkdir(parents=True)
    (archive / "checkpoint.json").write_text(
        json.dumps({"api_base_url": "https://existing.example/"}), encoding="utf-8"
    )
    assert data_update._archive_endpoint(tmp_path, "reference") == "https://existing.example/"
    with pytest.raises(ValueError, match="valid HTTPS"):
        data_update._archive_endpoint(tmp_path, "market")


def test_data_update_retries_transient_stage_without_restarting_completed_work(monkeypatch, tmp_path) -> None:
    target = data_update.latest_complete_day()
    monkeypatch.setattr(data_update, "plan", lambda *_: {"stages": [
        {"id": "benchmark", "name": "benchmark", "needs_update": True},
    ]})
    monkeypatch.setattr(data_update, "_archive_endpoint", lambda *_: "https://source.example/")
    attempts = []

    def fake_run(command, *, cwd, env, stdout, stderr, check):
        attempts.append(command)
        if len(attempts) == 1:
            stdout.write("RemoteDisconnected: remote end closed connection\n")
            return subprocess.CompletedProcess(command, 1)
        stdout.write("benchmark updated\n")
        return subprocess.CompletedProcess(command, 0)

    delays = []
    monkeypatch.setattr(data_update.subprocess, "run", fake_run)
    monkeypatch.setattr(data_update.time, "sleep", delays.append)
    progress_path = tmp_path / "progress.json"
    data_update.run(
        tmp_path, DataUpdateRequest(end=target, groups=["benchmark"], factor_ids=[]),
        progress_path, tmp_path / "run.log",
    )
    assert len(attempts) == 2
    assert delays == [5]
    assert json.loads(progress_path.read_text(encoding="utf-8"))["status"] == "PASS"


def test_data_update_does_not_retry_nontransient_stage_failure(monkeypatch, tmp_path) -> None:
    target = data_update.latest_complete_day()
    monkeypatch.setattr(data_update, "plan", lambda *_: {"stages": [
        {"id": "benchmark", "name": "benchmark", "needs_update": True},
    ]})
    monkeypatch.setattr(data_update, "_archive_endpoint", lambda *_: "https://source.example/")
    attempts = []

    def fake_run(command, *, cwd, env, stdout, stderr, check):
        attempts.append(command)
        stdout.write("ValueError: invalid data schema\n")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(data_update.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="failed during"):
        data_update.run(
            tmp_path, DataUpdateRequest(end=target, groups=["benchmark"], factor_ids=[]),
            tmp_path / "progress.json", tmp_path / "run.log",
        )
    assert len(attempts) == 1


def test_data_update_failure_keeps_completed_stage_progress(monkeypatch, tmp_path) -> None:
    request_path = tmp_path / "request.json"
    progress_path = tmp_path / "progress.json"
    log_path = tmp_path / "run.log"
    request_path.write_text(DataUpdateRequest(groups=["benchmark"], factor_ids=[]).model_dump_json())
    progress_path.write_text(json.dumps({"status": "RUNNING", "progress": 75, "completed_groups": 1,
                                         "total_groups": 2}), encoding="utf-8")

    def fail_run(*_):
        raise RuntimeError("invalid data schema")

    monkeypatch.setattr(data_update, "run", fail_run)
    monkeypatch.setattr(sys, "argv", ["data_update.py", "--project-root", str(tmp_path),
                                      "--request", str(request_path), "--progress", str(progress_path),
                                      "--log", str(log_path)])
    assert data_update.main() == 1
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["status"] == "FAIL"
    assert progress["progress"] == 75
    assert progress["completed_groups"] == 1
