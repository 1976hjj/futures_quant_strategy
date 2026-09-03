"""Independently audit M4.1 label alignment, artifacts, and basic metrics."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import duckdb

from alpha_research_os.evaluation import EvidenceBundleManifest, ForwardReturnLabel, LabelReleaseManifest
from alpha_research_os.evaluation.metrics import evaluate_basic_factor
from alpha_research_os.factors import RawFactorValue
from alpha_research_os.kernel.canonical import content_hash
from scripts.run_m4_1_evidence import _sha256_file, _sql_path


def _close(left: float | None, right: float | None, tolerance: float = 1e-12) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def audit(database: Path, evidence_store: Path, evidence_id: str) -> dict[str, object]:
    failures: list[str] = []
    bundle_dir = evidence_store / "bundles" / evidence_id.removeprefix("sha256:")
    evidence_manifest = EvidenceBundleManifest.model_validate_json((bundle_dir / "manifest.json").read_bytes())
    if evidence_manifest.evidence_id != evidence_id:
        failures.append("requested evidence identity differs from its manifest")
    label_id = evidence_manifest.request.label_release_id
    label_dir = evidence_store / "labels" / label_id.removeprefix("sha256:")
    label_manifest = LabelReleaseManifest.model_validate_json((label_dir / "manifest.json").read_bytes())
    label_path = evidence_store / label_manifest.parquet_relative_path
    if _sha256_file(label_path) != label_manifest.parquet_hash:
        failures.append("label Parquet hash differs from its manifest")
    file_by_name = {item.name: item for item in evidence_manifest.files}
    evidence_paths = {name: evidence_store / item.relative_path for name, item in file_by_name.items()}
    for name, path in evidence_paths.items():
        if _sha256_file(path) != file_by_name[name].artifact_hash:
            failures.append(f"evidence file hash differs: {name}")

    factor_release_id = evidence_manifest.request.factor_release_id
    with duckdb.connect(str(database), read_only=True) as connection:
        factor_path_value = connection.execute(
            "SELECT parquet_path FROM metadata.factor_release_manifest WHERE release_id=?",
            [factor_release_id],
        ).fetchone()
        if factor_path_value is None:
            raise ValueError("source factor release is not registered")
        factor_path = Path("data/factor_store") / factor_path_value[0]
        registered_label = connection.execute(
            "SELECT manifest_hash, parquet_hash FROM metadata.label_release_manifest WHERE release_id=?",
            [label_id],
        ).fetchone()
        if registered_label != (content_hash(label_manifest), label_manifest.parquet_hash):
            failures.append("DuckDB label registry differs from its manifest")
        registered_evidence = connection.execute(
            "SELECT manifest_hash FROM metadata.evidence_bundle_manifest WHERE evidence_id=?",
            [evidence_id],
        ).fetchone()
        if registered_evidence != (content_hash(evidence_manifest),):
            failures.append("DuckDB evidence registry differs from its manifest")

        label_source = f"read_parquet('{_sql_path(label_path)}')"
        label_stats = connection.execute(
            f"""SELECT count(*), count(*) FILTER (WHERE is_valid),
            count(*) FILTER (WHERE NOT is_valid),
            count(*)-count(DISTINCT (signal_session, instrument_id)),
            count(*) FILTER (WHERE value IS NOT NULL AND NOT isfinite(value)),
            count(DISTINCT constraint_level) FROM {label_source}"""
        ).fetchone()
        if label_stats[:3] != (
            label_manifest.row_count,
            label_manifest.valid_count,
            label_manifest.invalid_count,
        ):
            failures.append("label dimensions differ from manifest")
        if label_stats[3] or label_stats[4] or label_stats[5] != 1:
            failures.append("label uniqueness, finite-value, or constraint-level gate failed")
        factor_source = f"read_parquet('{_sql_path(factor_path)}')"
        key_mismatch = connection.execute(
            f"""WITH factor_keys AS (
              SELECT DISTINCT session, instrument_id FROM {factor_source}
            ), label_keys AS (
              SELECT signal_session AS session, instrument_id FROM {label_source}
            )
            SELECT
              (SELECT count(*) FROM factor_keys ANTI JOIN label_keys USING (session,instrument_id))+
              (SELECT count(*) FROM label_keys ANTI JOIN factor_keys USING (session,instrument_id))"""
        ).fetchone()[0]
        if key_mismatch:
            failures.append("label keys differ from source factor signal keys")
        alignment_errors = connection.execute(
            f"""WITH calendar AS (
              SELECT cal_date, row_number() OVER (ORDER BY cal_date) AS n
              FROM research.trading_calendar WHERE exchange='SSE' AND is_open
            )
            SELECT count(*) FROM {label_source} l
            JOIN calendar signal ON signal.cal_date=l.signal_session
            LEFT JOIN calendar entry ON entry.cal_date=l.entry_session
            LEFT JOIN calendar exit ON exit.cal_date=l.exit_session
            WHERE l.is_valid AND (entry.n-signal.n<>1 OR exit.n-signal.n<>6)"""
        ).fetchone()[0]
        if alignment_errors:
            failures.append("valid labels violate fixed T+1/T+6 session alignment")
        return_errors = connection.execute(
            f"""SELECT count(*) FROM {label_source}
            WHERE is_valid AND abs(value-(exit_adjusted_price/entry_adjusted_price-1))>1e-12"""
        ).fetchone()[0]
        if return_errors:
            failures.append("valid labels do not reconcile to adjusted prices")
        clock_errors = connection.execute(
            f"""SELECT count(*) FROM {label_source}
            WHERE is_valid AND (
              available_at IS NULL OR
              CAST(available_at AT TIME ZONE 'Asia/Shanghai' AS DATE)<>exit_session OR
              available_at<=signal_session::TIMESTAMP AT TIME ZONE 'Asia/Shanghai'+INTERVAL 15 HOURS)"""
        ).fetchone()[0]
        if clock_errors:
            failures.append("label availability clock is not strictly after the signal")

        daily_path = evidence_paths["daily_metrics"]
        quantile_path = evidence_paths["quantile_returns"]
        summary_path = evidence_paths["factor_summary"]
        evidence_stats = connection.execute(
            f"""SELECT
              (SELECT count(*) FROM read_parquet('{_sql_path(summary_path)}')),
              (SELECT count(*) FROM read_parquet('{_sql_path(daily_path)}')),
              (SELECT count(*) FROM read_parquet('{_sql_path(quantile_path)}'))"""
        ).fetchone()
        if evidence_stats[0] != evidence_manifest.factor_count:
            failures.append("factor summary count differs from evidence manifest")
        daily_invalid = connection.execute(
            f"""SELECT count(*) FROM read_parquet('{_sql_path(daily_path)}')
            WHERE paired_count>factor_present_count OR factor_present_count>universe_count
               OR valid_label_count>universe_count OR coverage<0 OR coverage>1
               OR (pearson_ic IS NOT NULL AND (NOT isfinite(pearson_ic) OR abs(pearson_ic)>1))
               OR (rank_ic IS NOT NULL AND (NOT isfinite(rank_ic) OR abs(rank_ic)>1))"""
        ).fetchone()[0]
        if daily_invalid:
            failures.append("daily evidence violates count, coverage, finite-value, or correlation bounds")

        crosscheck_factor = "price-momentum-20"
        crosscheck_session = "2024-02-01"
        factor_rows = [
            RawFactorValue(
                session=row[0],
                instrument_id=row[1],
                factor_id=row[2],
                factor_version=row[3],
                value=row[4],
                available_at=row[5],
                implementation_hash=row[6],
            )
            for row in connection.execute(
                f"""SELECT session,instrument_id,factor_id,factor_version,value,available_at,implementation_hash
                FROM {factor_source} WHERE factor_id=? AND session=?""",
                [crosscheck_factor, crosscheck_session],
            ).fetchall()
        ]
        label_rows = [
            ForwardReturnLabel(
                signal_session=row[0],
                instrument_id=row[1],
                label_id=row[2],
                label_version=row[3],
                value=row[4],
                entry_session=row[5],
                exit_session=row[6],
                entry_adjusted_price=row[7],
                exit_adjusted_price=row[8],
                available_at=row[9],
                is_valid=row[10],
                invalid_reason=row[11],
                constraint_level=row[12],
            )
            for row in connection.execute(
                f"""SELECT signal_session,instrument_id,label_id,label_version,value,entry_session,exit_session,
                entry_adjusted_price,exit_adjusted_price,available_at,is_valid,invalid_reason,constraint_level
                FROM {label_source} WHERE signal_session=?""",
                [crosscheck_session],
            ).fetchall()
        ]
        independent = evaluate_basic_factor(factor_rows, label_rows)
        stored_daily = connection.execute(
            f"""SELECT coverage,pearson_ic,rank_ic,paired_count
            FROM read_parquet('{_sql_path(daily_path)}') WHERE factor_id=? AND session=?""",
            [crosscheck_factor, crosscheck_session],
        ).fetchone()
        if stored_daily is None or not all(
            (
                _close(independent.mean_coverage, stored_daily[0]),
                _close(independent.mean_pearson_ic, stored_daily[1]),
                _close(independent.mean_rank_ic, stored_daily[2]),
                independent.paired_observations == stored_daily[3],
            )
        ):
            failures.append("independent Python IC cross-check differs from DuckDB evidence")
        independent_quantiles = {
            item.quantile: (item.count, item.mean_return) for item in independent.daily[0].quantile_returns
        }
        stored_quantiles = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                f"""SELECT quantile,observation_count,mean_return
                FROM read_parquet('{_sql_path(quantile_path)}') WHERE factor_id=? AND session=?""",
                [crosscheck_factor, crosscheck_session],
            ).fetchall()
        }
        if independent_quantiles.keys() != stored_quantiles.keys() or any(
            independent_quantiles[key][0] != stored_quantiles[key][0]
            or not _close(independent_quantiles[key][1], stored_quantiles[key][1])
            for key in independent_quantiles
        ):
            failures.append("independent Python quantile cross-check differs from DuckDB evidence")

    required_limitations = ("price-limit", "descriptive", "transaction-cost")
    limitations_text = " ".join(evidence_manifest.limitations).lower()
    if any(term not in limitations_text for term in required_limitations):
        failures.append("evidence manifest does not disclose every provisional limitation")
    return {
        "audited_at": datetime.now().astimezone().isoformat(),
        "crosscheck_factor": crosscheck_factor,
        "crosscheck_session": crosscheck_session,
        "evidence_id": evidence_id,
        "evidence_rows": {
            "daily_metrics": evidence_stats[1],
            "factor_summary": evidence_stats[0],
            "quantile_returns": evidence_stats[2],
        },
        "failures": failures,
        "label_invalid_count": label_manifest.invalid_count,
        "label_release_id": label_id,
        "label_valid_count": label_manifest.valid_count,
        "status": "PASS" if not failures else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument(
        "--evidence-id", default="sha256:4c2bc48115894c0618149004c84bc1c820b1f1ec7c799b1f07160c370ef6faf3"
    )
    parser.add_argument("--output", type=Path, default=Path("reports/m4_1_evidence_audit.json"))
    args = parser.parse_args()
    report = audit(args.database, args.evidence_store, args.evidence_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
