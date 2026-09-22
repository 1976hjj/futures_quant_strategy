from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from alpha_research_os.factors.assets import DatasetLineage, FactorReleaseManifest
from alpha_research_os.portfolio import strategy_backtest
from scripts import publish_factor_release
from scripts.serve_m4_control_api import FactorComputeRequest, FactorJobManager


def test_current_factor_can_be_requested_for_a_new_date_range() -> None:
    request = FactorComputeRequest(
        factor_id="roe-pit", factor_version="1.0.0", start=date(2020, 1, 2), end=date(2026, 9, 1)
    )
    assert request.end == date(2026, 9, 1)

    with pytest.raises(ValueError, match="factor version"):
        FactorComputeRequest(
            factor_id="roe-pit", factor_version="2.0.0", start=date(2020, 1, 2), end=date(2026, 9, 1)
        )


def test_current_factor_release_contains_only_selected_factor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        publish_factor_release,
        "_lineage",
        lambda _: (
            DatasetLineage(
                manifest_table="metadata.m2b_archive_manifest", checkpoint_hashes=("sha256:" + "a" * 64,)
            ),
        ),
    )

    request, catalog = publish_factor_release._request(
        None, date(2020, 1, 2), date(2026, 9, 1), "m4.2", "roe-pit"
    )
    assert [(item.factor_id, item.factor_version) for item in request.factors] == [("roe-pit", "1.0.0")]
    assert len(catalog.list()) == 1

    with pytest.raises(ValueError, match="not in the m4.2 catalog"):
        publish_factor_release._request(None, date(2020, 1, 2), date(2026, 9, 1), "m4.2", "unknown")


def _values(path: Path, release_id: str, values: list[tuple[str, float]]) -> None:
    with duckdb.connect() as connection:
        connection.execute(
            """CREATE TABLE values_for_test (
              release_id VARCHAR, session DATE, instrument_id VARCHAR, factor_id VARCHAR,
              factor_version VARCHAR, variant VARCHAR, value DOUBLE,
              available_at TIMESTAMPTZ, implementation_hash VARCHAR
            )"""
        )
        connection.executemany(
            """INSERT INTO values_for_test VALUES (?, ?, '000001.SZ', 'roe-pit', '1.0.0', 'RAW', ?,
               TIMESTAMPTZ '2024-01-01 15:00:00+08:00', 'sha256:implementation')""",
            [(release_id, session, value) for session, value in values],
        )
        connection.execute(f"COPY values_for_test TO '{path.as_posix()}' (FORMAT PARQUET)")


def test_incremental_parent_accepts_older_multi_factor_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        publish_factor_release, "_lineage",
        lambda _: (
            DatasetLineage(
                manifest_table="metadata.m2b_archive_manifest", checkpoint_hashes=("sha256:" + "a" * 64,)
            ),
        ),
    )
    start, old_end, end = date(2024, 1, 1), date(2024, 1, 3), date(2024, 1, 4)
    old_request, _ = publish_factor_release._request(None, start, old_end, "m4.2")
    new_request, _ = publish_factor_release._request(None, start, end, "m4.2", "roe-pit")
    store = tmp_path / "store"
    folder = store / "releases" / old_request.computation_key.removeprefix("sha256:")
    folder.mkdir(parents=True)
    parquet = folder / "raw_factor_values.parquet"
    _values(parquet, old_request.computation_key, [("2024-01-01", 1), ("2024-01-02", 2)])
    manifest = FactorReleaseManifest(
        release_id=old_request.computation_key, request=old_request,
        created_at=datetime.now().astimezone(), parquet_relative_path=parquet.relative_to(store).as_posix(),
        parquet_hash=publish_factor_release._sha256_file(parquet),
        row_count=2, session_count=2, instrument_count=1, factor_count=len(old_request.factors),
        quality_status="PASS", quality_summary_hash="sha256:" + "b" * 64,
    )
    (folder / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")

    parent = publish_factor_release._incremental_parent(store, new_request)
    assert parent is not None
    assert parent[0].release_id == old_request.computation_key


@pytest.mark.parametrize("mismatch", [False, True])
def test_incremental_release_checks_overlap_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: bool
) -> None:
    lineage = (
        DatasetLineage(
            manifest_table="metadata.m2b_archive_manifest", checkpoint_hashes=("sha256:" + "a" * 64,)
        ),
    )
    monkeypatch.setattr(publish_factor_release, "_lineage", lambda _: lineage)
    start, old_end, end = date(2024, 1, 1), date(2024, 1, 3), date(2024, 1, 4)
    current_request, catalog = publish_factor_release._request(None, start, end, "m4.2", "roe-pit")
    old_request, _ = publish_factor_release._request(None, start, old_end, "m4.2")
    store = tmp_path / "store"
    store.mkdir()
    old_parquet = tmp_path / "old.parquet"
    partial_parquet = tmp_path / "partial.parquet"
    full_parquet = tmp_path / "full.parquet"
    _values(old_parquet, old_request.computation_key, [("2024-01-01", 1), ("2024-01-02", 2), ("2024-01-03", 3)])
    _values(partial_parquet, current_request.computation_key, [
        ("2024-01-02", 2), ("2024-01-03", 9 if mismatch else 3), ("2024-01-04", 4)
    ])
    _values(full_parquet, current_request.computation_key, [
        ("2024-01-01", 1), ("2024-01-02", 2), ("2024-01-03", 9), ("2024-01-04", 4)
    ])
    old_manifest = FactorReleaseManifest(
        release_id=old_request.computation_key, request=old_request,
        created_at=datetime.now().astimezone(), parquet_relative_path="old.parquet",
        parquet_hash=publish_factor_release._sha256_file(old_parquet),
        row_count=3, session_count=3, instrument_count=1, factor_count=len(old_request.factors),
        quality_status="PASS", quality_summary_hash="sha256:" + "b" * 64,
    )
    monkeypatch.setattr(publish_factor_release, "_request", lambda *_: (current_request, catalog))
    monkeypatch.setattr(publish_factor_release, "_incremental_parent", lambda *_: (old_manifest, old_parquet))
    monkeypatch.setattr(publish_factor_release, "_warmup_start", lambda *_: date(2024, 1, 2))
    monkeypatch.setattr(publish_factor_release, "_register", lambda *_: None)
    materializations: list[date] = []

    def materialize(_database: Path, _store: Path, _catalog: object, _request: object,
                    target: Path, _history: int, output_start: date, _output_end: date) -> None:
        import shutil
        materializations.append(output_start)
        shutil.copyfile(full_parquet if output_start == start else partial_parquet, target)

    monkeypatch.setattr(publish_factor_release, "_materialize_range", materialize)
    database = tmp_path / "empty.duckdb"
    duckdb.connect(str(database)).close()
    result = publish_factor_release.publish(database, store, start, end, "m4.2", "roe-pit")

    assert result["calculation"]["mode"] == ("FULL_AFTER_MISMATCH" if mismatch else "INCREMENTAL")
    assert result["calculation"]["overlap"]["different_rows"] == (1 if mismatch else 0)
    assert materializations == ([date(2024, 1, 2), start] if mismatch else [date(2024, 1, 2)])
    with duckdb.connect() as connection:
        published = Path(result["manifest"]).parent / "raw_factor_values.parquet"
        values = connection.execute(
            "SELECT value FROM read_parquet(?) ORDER BY session", [str(published)]
        ).fetchall()
    assert [row[0] for row in values] == [1, 2, 9 if mismatch else 3, 4]


def test_backtest_uses_newest_release_covering_selected_dates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    old_id = "sha256:" + "a" * 64
    new_id = "sha256:" + "b" * 64
    monkeypatch.setattr(
        strategy_backtest,
        "_factor_release_candidates",
        lambda *_: [
            {
                "factor_id": "roe-pit", "release_id": old_id,
                "start": date(2016, 1, 4), "end": date(2025, 12, 31),
                "created_at": "2025-01-01T00:00:00+08:00", "parquet": tmp_path / "old.parquet",
            },
            {
                "factor_id": "roe-pit", "release_id": new_id,
                "start": date(2020, 1, 2), "end": date(2025, 12, 31),
                "created_at": "2026-01-01T00:00:00+08:00", "parquet": tmp_path / "new.parquet",
            },
        ],
    )
    request = strategy_backtest.StrategyBacktestRequest.model_validate({
        "name": "latest release", "start": "2020-01-02", "end": "2025-12-31",
        "score_rules": [{"factor_id": "roe-pit", "release_id": old_id, "weight": 1}],
    })
    assert strategy_backtest._resolve_inputs(tmp_path, request)[0]["release_id"] == new_id


def test_factor_job_reports_overlap_mismatch_to_page(tmp_path: Path) -> None:
    manager = FactorJobManager(tmp_path)
    job_id = "overlap-mismatch"
    (manager.run_root / f"{job_id}.request.json").write_text(
        json.dumps({
            "factor_id": "roe-pit", "factor_version": "1.0.0",
            "start": "2024-01-01", "end": "2024-01-04",
        }), encoding="utf-8",
    )
    (manager.run_root / f"{job_id}.result.json").write_text(
        json.dumps({
            "release_id": "sha256:" + "a" * 64,
            "calculation": {"mode": "FULL_AFTER_MISMATCH", "message": "重叠区间有 1 条差异，已全区间重算。"},
        }), encoding="utf-8",
    )
    status = manager.status(job_id)
    assert status["status"] == "PASS"
    assert status["message"] == "重叠区间有 1 条差异，已全区间重算。"
