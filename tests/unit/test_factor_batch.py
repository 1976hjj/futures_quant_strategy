from __future__ import annotations

import json
from datetime import date, datetime, timezone

import duckdb

from alpha_research_os.factors.assets import (
    DatasetLineage, FactorAssetRef, FactorAssetRequest, FactorReleaseManifest,
)
from alpha_research_os.kernel.canonical import canonical_json_bytes
from scripts.publish_factor_cohort import publish_cohort
from scripts.publish_factor_release import _sha256_file
from scripts.serve_m4_control_api import _batch_stage_closure
from scripts.serve_m4_control_api import FactorBatchManager


def test_m45_preflight_adds_its_dependencies() -> None:
    assert _batch_stage_closure(("m4_5",)) == ["m4_1", "m4_2", "m4_3", "m4_4", "m4_5"]


def test_retry_keeps_passed_factor_for_shared_m45_without_recomputing_it(tmp_path, monkeypatch) -> None:
    manager = FactorBatchManager(tmp_path)
    original = {
        "status": "PARTIAL", "cohort_status": "SKIPPED",
        "request": {
            "factors": [{"factor_id": "passed"}, {"factor_id": "failed"}],
            "stages": ["m4_5"], "resolved_stages": ["m4_1", "m4_2", "m4_3", "m4_4", "m4_5"],
        },
        "items": [
            {"factor_id": "passed", "status": "PASS", "release_id": "sha256:passed", "m4_job_id": "old"},
            {"factor_id": "failed", "status": "FAIL", "release_id": "sha256:failed", "m4_job_id": "failed-old"},
        ],
    }
    monkeypatch.setattr(manager, "status", lambda _job_id: original)
    captured = {}

    def fake_start(payload, reuse_items=None):
        captured.update(payload=payload, reuse_items=reuse_items)
        return captured

    monkeypatch.setattr(manager, "start", fake_start)
    manager.retry_failed("old-batch")

    assert [item["factor_id"] for item in captured["payload"]["factors"]] == ["passed", "failed"]
    assert captured["reuse_items"] == {
        "passed": {"release_id": "sha256:passed", "m4_job_id": "old", "m4_retry": False},
        "failed": {"release_id": "sha256:failed", "m4_job_id": "failed-old", "m4_retry": True},
    }


def test_batch_status_exposes_the_current_m4_step(tmp_path) -> None:
    manager = FactorBatchManager(tmp_path)
    batch_id = "20260922-180000-abcdef"
    m4_id = "20260922-180001-abcdef"
    request = {
        "start": "2020-01-01", "end": "2021-12-31", "resolved_stages": ["m4_1"],
        "factors": [{"factor_id": "factor-a", "name": "因子 A"}],
    }
    state = {
        "status": "RUNNING", "phase": "运行 M4 检验", "started_at": datetime.now(timezone.utc).isoformat(),
        "cohort_status": "NOT_RUN", "items": [{
            "factor_id": "factor-a", "name": "因子 A", "status": "RUNNING",
            "phase": "运行 M4 检验", "release_id": "sha256:factor-a", "m4_job_id": m4_id,
        }],
    }
    (manager.run_root / f"{batch_id}.request.json").write_text(json.dumps(request), encoding="utf-8")
    (manager.run_root / f"{batch_id}.state.json").write_text(json.dumps(state), encoding="utf-8")
    report_root = tmp_path / "reports/m4_runs"
    report_root.mkdir()
    (report_root / f"{m4_id}.config.json").write_text(
        json.dumps({"stages": ["basic_evidence", "audit_basic_evidence"]}), encoding="utf-8"
    )
    (report_root / f"{m4_id}.json").write_text(json.dumps({
        "status": "RUNNING", "current_stage": "audit_basic_evidence",
        "current_stage_started_at": datetime.now(timezone.utc).isoformat(),
        "stages": {"basic_evidence": {"completed_at": datetime.now(timezone.utc).isoformat()}},
    }), encoding="utf-8")
    (manager.run_root / f"{batch_id}.0.m4.log").write_text(
        "daily variant=RAW year=2020 materializing\n"
        "m4_stage=audit_basic_evidence started\n"
        "conditional year=2021 materializing\n", encoding="utf-8"
    )

    result = manager.status(batch_id)

    assert result["progress"] == 67
    assert (result["completed_steps"], result["total_steps"]) == (2, 3)
    assert result["activity"]["stage"] == "基础证据审计"
    assert result["activity"]["detail"] == "正在处理 2021 年"
    assert [step["status"] for step in result["items"][0]["stage_progress"]["steps"]] == ["PASS", "RUNNING"]


def test_shared_m45_asset_keeps_both_factors_and_source_identity(tmp_path) -> None:
    store = tmp_path / "data/factor_store"
    (tmp_path / "data/warehouse").mkdir(parents=True)
    duckdb.connect(str(tmp_path / "data/warehouse/alpha_research.duckdb")).close()
    source_ids = []
    for index, factor_id in enumerate(("factor-a", "factor-b"), start=1):
        digest = "sha256:" + str(index) * 64
        request = FactorAssetRequest(
            engine_version="1.0.0",
            factors=(FactorAssetRef(factor_id=factor_id, factor_version="1.0.0",
                                    spec_hash=digest, implementation_hash=digest,
                                    catalog_entry_hash=digest),),
            dataset_lineage=(DatasetLineage(manifest_table="metadata.test_source",
                                             checkpoint_hashes=(digest,)),),
            universe_id="ALL-A-PIT", universe_version="1.0.0",
            start=date(2020, 1, 6), end=date(2020, 1, 6), signal_clock_version="1.0.0",
        )
        release_id = request.computation_key
        directory = store / "releases" / release_id.removeprefix("sha256:")
        directory.mkdir(parents=True)
        parquet = directory / "raw_factor_values.parquet"
        with duckdb.connect() as connection:
            connection.execute(
                f"COPY (SELECT '{release_id}' AS release_id, DATE '2020-01-06' AS session, "
                f"'000001.SZ' AS instrument_id, '{factor_id}' AS factor_id, "
                "'1.0.0' AS factor_version, 'RAW' AS variant, "
                f"{index}.0 AS value, TIMESTAMP '2020-01-06 15:00:00' AS available_at, "
                f"'{digest}' AS implementation_hash) TO '{parquet.as_posix()}' (FORMAT PARQUET)"
            )
        manifest = FactorReleaseManifest(
            release_id=release_id, request=request, created_at=datetime.now(timezone.utc),
            parquet_relative_path=parquet.relative_to(store).as_posix(),
            parquet_hash=_sha256_file(parquet), row_count=1, session_count=1,
            instrument_count=1, factor_count=1, quality_status="PASS",
            quality_summary_hash=digest,
        )
        (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        source_ids.append(release_id)

    cohort_id = publish_cohort(tmp_path, source_ids, "2020-01-06", "2020-01-06")
    directory = store / "releases" / cohort_id.removeprefix("sha256:")
    with duckdb.connect() as connection:
        rows = connection.execute(
            "SELECT release_id, factor_id, value FROM read_parquet(?) ORDER BY factor_id",
            [str(directory / "raw_factor_values.parquet")],
        ).fetchall()
    assert rows == [(cohort_id, "factor-a", 1.0), (cohort_id, "factor-b", 2.0)]
    assert json.loads((directory / "cohort_sources.json").read_text(encoding="utf-8"))["source_release_ids"] == source_ids
    assert publish_cohort(tmp_path, source_ids, "2020-01-06", "2020-01-06") == cohort_id
