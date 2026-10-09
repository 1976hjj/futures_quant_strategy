from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from alpha_research_os.evaluation.assets import LabelAssetRequest, LabelReleaseManifest
from alpha_research_os.evaluation.labels import ExecutionConstraintLevel
from alpha_research_os.factors.assets import DatasetLineage
from alpha_research_os.kernel.canonical import canonical_json_bytes
from scripts.audit_m4_1_evidence import audit
from scripts.run_m4_1_evidence import _publish_evidence, _register, _sha256_file


@pytest.mark.parametrize("groups", [3, 7])
def test_audit_recomputes_declared_groups_without_weakening_checks(tmp_path, groups):
    database = tmp_path / "warehouse.duckdb"
    factor_path = tmp_path / "factor.parquet"
    store = tmp_path / "evidence"
    factor_id = "sha256:" + "a" * 64
    signal = date(2024, 1, 2)
    exit_day = signal + timedelta(days=6)

    def clock(d):
        return datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=7)

    factors = []
    labels = []
    for i in range(14):
        code = f"BANK{i:02}"
        factors.append(
            dict(
                session=signal,
                instrument_id=code,
                factor_id="test-factor",
                factor_version="1.0.0",
                value=float(i),
                available_at=clock(signal),
                implementation_hash=factor_id,
            )
        )
        labels.append(
            dict(
                signal_session=signal,
                instrument_id=code,
                label_id="next-open-to-5d-close-total-return",
                label_version="1.0.0",
                value=((i * 5) % 14) / 100,
                entry_session=signal + timedelta(days=1),
                exit_session=exit_day,
                entry_adjusted_price=10.0,
                exit_adjusted_price=10 * (1 + ((i * 5) % 14) / 100),
                available_at=clock(exit_day),
                is_valid=True,
                invalid_reason=None,
                constraint_level=ExecutionConstraintLevel.BAR_AND_SUSPENSION_ONLY.value,
            )
        )
    pd.DataFrame(factors).to_parquet(factor_path)
    request = LabelAssetRequest(
        engine_version="1.0.0",
        label_id="next-open-to-5d-close-total-return",
        label_version="1.0.0",
        label_spec_hash=factor_id,
        source_factor_release_id=factor_id,
        dataset_lineage=(DatasetLineage(manifest_table="test", checkpoint_hashes=(factor_id,)),),
        universe_id="ALL-A-PIT",
        universe_version="1.0.0",
        start=signal,
        end=signal,
        constraint_level=ExecutionConstraintLevel.BAR_AND_SUSPENSION_ONLY,
    )
    folder = store / "labels" / request.computation_key.removeprefix("sha256:")
    folder.mkdir(parents=True)
    label_path = folder / "forward_return_labels.parquet"
    pd.DataFrame(labels).to_parquet(label_path)
    manifest = LabelReleaseManifest(
        release_id=request.computation_key,
        request=request,
        created_at=datetime.now(UTC),
        parquet_relative_path=label_path.relative_to(store).as_posix(),
        parquet_hash=_sha256_file(label_path),
        row_count=14,
        valid_count=14,
        invalid_count=0,
        quality_status="PASS",
    )
    (folder / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    with duckdb.connect(str(database)) as c:
        c.execute("CREATE SCHEMA research")
        c.execute("CREATE TABLE research.trading_calendar(cal_date DATE,exchange VARCHAR,is_open BOOLEAN)")
        c.executemany(
            "INSERT INTO research.trading_calendar VALUES (?,'SSE',true)",
            [(signal + timedelta(days=i),) for i in range(7)],
        )
        c.execute("CREATE SCHEMA metadata")
        c.execute("CREATE TABLE metadata.factor_release_manifest(release_id VARCHAR,parquet_path VARCHAR)")
        c.execute("CREATE TABLE metadata.processed_factor_release_manifest(release_id VARCHAR,parquet_path VARCHAR)")
        c.execute("INSERT INTO metadata.factor_release_manifest VALUES (?, 'factor.parquet')", [factor_id])
    source = SimpleNamespace(release_id=factor_id, request=SimpleNamespace(variant="RAW"), factor_count=1)
    bundle, _ = _publish_evidence(
        database, store, source, factor_path, manifest, label_path, quantile_count=groups, minimum_pairs=10
    )
    _register(database, store, manifest, bundle)
    result = audit(database, store, bundle.evidence_id, tmp_path)
    assert result["status"] == "PASS", result["failures"]
    assert result["failures"] == []
    assert result["evidence_rows"]["quantile_returns"] == groups
