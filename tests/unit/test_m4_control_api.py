from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts import serve_m4_control_api as control

DIGEST = "sha256:" + "a" * 64


def _request(**updates: object) -> control.M4RunRequest:
    payload: dict[str, object] = {
        "factor_release_id": DIGEST,
        "stages": ["m4_1", "m4_6"],
        "window_start": "2020-01-02",
        "window_end": "2025-12-31",
        "holding_sessions": 10,
    }
    payload.update(updates)
    return control.M4RunRequest.model_validate(payload)


def test_ui_request_rejects_unknown_stage_or_holding_period() -> None:
    with pytest.raises(ValidationError, match="M4.1-M4.6"):
        _request(stages=["m4_8"])
    with pytest.raises(ValidationError, match="5, 10, 20, or 30"):
        _request(holding_sessions=7)


def test_config_builder_is_generic_and_orders_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    release = {
        "release_id": DIGEST,
        "factor_count": 137,
        "start": "2020-01-02",
        "end": "2025-12-31",
    }
    monkeypatch.setattr(control, "_factor_releases", lambda _root: [release])

    config = control.build_pipeline_config(Path("unused"), _request(), "TEST-JOB")

    assert config.stages == (
        "basic_evidence",
        "audit_basic_evidence",
        "execution",
        "audit_execution",
    )
    assert config.basic_evidence.holding_sessions == 10
    assert config.execution is not None
    assert config.execution.holding_sessions == 10


def test_delete_finished_m4_run_removes_only_its_run_files(tmp_path) -> None:
    manager = control.JobManager(tmp_path)
    job_id = "20260908-120000-abcdef"
    report = manager.run_root / f"{job_id}.json"
    config = manager.run_root / f"{job_id}.config.json"
    log = manager.run_root / f"{job_id}.log"
    report.write_text('{"status":"PASS"}', encoding="utf-8")
    config.write_text('{"stages":[]}', encoding="utf-8")
    log.write_text("finished", encoding="utf-8")
    untouched = manager.run_root / "20260908-120001-fedcba.json"
    untouched.write_text('{"status":"PASS"}', encoding="utf-8")

    result = manager.delete(job_id)

    assert result["deleted"] is True
    assert not report.exists()
    assert not config.exists()
    assert not log.exists()
    assert untouched.exists()
    with pytest.raises(FileNotFoundError):
        manager.status(job_id)


def test_delete_rejects_an_invalid_job_id(tmp_path) -> None:
    with pytest.raises(ValueError, match="invalid job id"):
        control.JobManager(tmp_path).delete("../../not-a-job")
