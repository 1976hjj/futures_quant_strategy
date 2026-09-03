"""Independently audit M4.2 corrected RAW and processed factor releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import median, pstdev
from typing import Any

import duckdb

from alpha_research_os.evaluation import EvidenceBundleManifest
from alpha_research_os.factors import FactorReleaseManifest, ProcessedFactorReleaseManifest

CORRECTED_RAW_ID = "sha256:85655b0bb661e845df51c0e20aab0223afadac4cee45c9f2e5a6d4c8d42c2aa9"
WINSORIZED_ID = "sha256:6e9bc1fd807f5f20fcd24fdc9846d1a4554570d8e92823636f33313d6fccd73b"
SIZE_NEUTRALIZED_ID = "sha256:68f0c0a8a39921382325792e653b20daff8771d4294cd68be36b3bbc3afa2de2"
EVIDENCE_IDS = {
    "RAW": "sha256:818115f4bfd6e8b7bc5e6f09b02dfaeb8638778348399429da695a1cd7383766",
    "WINSORIZED_ZSCORE": "sha256:84e278c977fe28e3277f4cd910cec9ca303844148a60c2da3edf3569e65d2528",
    "SIZE_NEUTRALIZED": "sha256:8b0aa51eed07843f79860f7f7e9098220f70caab8e85629f889b72a58b398cfc",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _processed_manifest(store: Path, release_id: str) -> tuple[ProcessedFactorReleaseManifest, Path]:
    directory = store / "processed_releases" / release_id.removeprefix("sha256:")
    manifest = ProcessedFactorReleaseManifest.model_validate_json((directory / "manifest.json").read_bytes())
    parquet = store / manifest.parquet_relative_path
    assert manifest.release_id == release_id
    assert _sha256_file(parquet) == manifest.parquet_hash
    return manifest, parquet


def _zscore(values: list[float]) -> list[float]:
    center = sum(values) / len(values)
    scale = pstdev(values)
    return [(value - center) / scale for value in values]


def _independent_cross_section(
    connection: duckdb.DuckDBPyConnection,
    raw_path: Path,
    processed_path: Path,
    *,
    size_neutralized: bool,
) -> float:
    rows = connection.execute(
        f"""SELECT r.instrument_id,r.value,b.total_mv,p.value
        FROM read_parquet('{_sql_path(raw_path)}') r
        JOIN read_parquet('{_sql_path(processed_path)}') p
          USING (session,instrument_id,factor_id,factor_version)
        LEFT JOIN research.daily_basic b ON b.trade_date=r.session AND b.ts_code=r.instrument_id
        WHERE r.session=DATE '2024-03-29' AND r.factor_id='price-momentum-20'
          AND r.value IS NOT NULL ORDER BY r.instrument_id"""
    ).fetchall()
    raw_values = [float(row[1]) for row in rows]
    center = median(raw_values)
    mad = median(abs(value - center) for value in raw_values)
    boundary = 5.0 * 1.4826 * mad
    winsorized = [min(center + boundary, max(center - boundary, value)) if mad > 0 else value for value in raw_values]
    standardized = _zscore(winsorized)
    expected: list[float | None]
    if not size_neutralized:
        expected = standardized
    else:
        pairs = [
            (value, math.log(float(row[2])))
            for value, row in zip(standardized, rows, strict=True)
            if row[2] is not None and row[2] > 0
        ]
        y_mean = sum(pair[0] for pair in pairs) / len(pairs)
        x_mean = sum(pair[1] for pair in pairs) / len(pairs)
        denominator = sum((x - x_mean) ** 2 for _, x in pairs)
        beta = sum((x - x_mean) * (y - y_mean) for y, x in pairs) / denominator
        residuals = [
            value - (y_mean + beta * (math.log(float(row[2])) - x_mean))
            for value, row in zip(standardized, rows, strict=True)
        ]
        expected = _zscore(residuals)
    differences = [
        abs(target - float(row[3]))
        for target, row in zip(expected, rows, strict=True)
        if row[3] is not None
    ]
    return max(differences, default=0.0)


def audit(database: Path, factor_store: Path, evidence_store: Path) -> dict[str, Any]:
    raw_dir = factor_store / "releases" / CORRECTED_RAW_ID.removeprefix("sha256:")
    raw_manifest = FactorReleaseManifest.model_validate_json((raw_dir / "manifest.json").read_bytes())
    raw_path = factor_store / raw_manifest.parquet_relative_path
    assert raw_manifest.release_id == CORRECTED_RAW_ID
    assert _sha256_file(raw_path) == raw_manifest.parquet_hash
    winsorized_manifest, winsorized_path = _processed_manifest(factor_store, WINSORIZED_ID)
    size_manifest, size_path = _processed_manifest(factor_store, SIZE_NEUTRALIZED_ID)
    assert winsorized_manifest.request.parent_release_id == CORRECTED_RAW_ID
    assert size_manifest.request.parent_release_id == CORRECTED_RAW_ID
    assert not winsorized_manifest.request.preprocessing.neutralize_industry
    assert not size_manifest.request.preprocessing.neutralize_industry

    with duckdb.connect(str(database), read_only=True) as connection:
        versions = dict(
            connection.execute(
                """SELECT factor_id,factor_version FROM metadata.factor_registry
                WHERE (factor_id,factor_version) IN (
                  ('price-momentum-20','2.0.0'),('short-reversal-5','2.0.0'),('overnight-gap-1','2.0.0'))"""
            ).fetchall()
        )
        assert versions == {
            "overnight-gap-1": "2.0.0",
            "price-momentum-20": "2.0.0",
            "short-reversal-5": "2.0.0",
        }
        disposition_count = connection.execute(
            """SELECT count(*) FROM metadata.factor_version_disposition
            WHERE factor_version='1.0.0' AND successor_version='2.0.0'
              AND disposition='SUPERSEDED_DIAGNOSTIC'"""
        ).fetchone()[0]
        assert disposition_count == 3
        jump_count = connection.execute(
            """WITH x AS (SELECT ts_code,trade_date,adj_factor,
              lag(adj_factor) OVER (PARTITION BY ts_code ORDER BY trade_date) previous
              FROM research.adj_factor)
            SELECT count(*) FROM x WHERE trade_date BETWEEN DATE '2024-01-02' AND DATE '2024-03-29'
              AND previous IS NOT NULL AND abs(adj_factor/previous-1)>1e-8"""
        ).fetchone()[0]
        assert jump_count > 0
        old_path = factor_store / "releases" / (
            "2faab50ecfd39544d40c7c451b9f9325cdab57f075d12b6a52a50593fe18778e/raw_factor_values.parquet"
        )
        changed = dict(
            connection.execute(
                f"""SELECT n.factor_id,count(*) FILTER
                  (WHERE o.value IS NOT NULL AND n.value IS NOT NULL AND abs(o.value-n.value)>1e-12)
                FROM read_parquet('{_sql_path(raw_path)}') n
                JOIN read_parquet('{_sql_path(old_path)}') o
                  ON o.session=n.session AND o.instrument_id=n.instrument_id AND o.factor_id=n.factor_id
                WHERE n.factor_id IN ('price-momentum-20','short-reversal-5','overnight-gap-1')
                GROUP BY 1"""
            ).fetchall()
        )
        assert all(changed.get(factor_id, 0) > 0 for factor_id in versions)
        normalization_error = connection.execute(
            f"""WITH d AS (SELECT session,factor_id,avg(value) mean,stddev_pop(value) std,count(value) n
              FROM read_parquet('{_sql_path(winsorized_path)}') GROUP BY 1,2)
            SELECT max(abs(mean)),max(abs(std-1)) FROM d WHERE n>=20"""
        ).fetchone()
        size_correlation = connection.execute(
            f"""WITH d AS (SELECT p.session,p.factor_id,corr(p.value,ln(b.total_mv)) correlation
              FROM read_parquet('{_sql_path(size_path)}') p JOIN research.daily_basic b
                ON b.trade_date=p.session AND b.ts_code=p.instrument_id
              WHERE p.value IS NOT NULL AND b.total_mv>0 GROUP BY 1,2)
            SELECT max(abs(correlation)) FROM d"""
        ).fetchone()[0]
        assert normalization_error[0] < 1e-10 and normalization_error[1] < 1e-10
        assert size_correlation < 1e-10
        winsorized_crosscheck = _independent_cross_section(
            connection, raw_path, winsorized_path, size_neutralized=False
        )
        size_crosscheck = _independent_cross_section(connection, raw_path, size_path, size_neutralized=True)
        assert winsorized_crosscheck < 1e-10 and size_crosscheck < 1e-10

    for variant, evidence_id in EVIDENCE_IDS.items():
        directory = evidence_store / "bundles" / evidence_id.removeprefix("sha256:")
        manifest = EvidenceBundleManifest.model_validate_json((directory / "manifest.json").read_bytes())
        assert manifest.request.factor_variant == variant
        assert manifest.quality_status == "PASS"
        for item in manifest.files:
            assert _sha256_file(evidence_store / item.relative_path) == item.artifact_hash
    return {
        "status": "PASS",
        "corporate_action_jump_count": jump_count,
        "changed_value_counts": changed,
        "winsorized_mean_abs_max": normalization_error[0],
        "winsorized_std_error_max": normalization_error[1],
        "size_exposure_correlation_abs_max": size_correlation,
        "independent_winsorized_value_error_max": winsorized_crosscheck,
        "independent_size_neutralized_value_error_max": size_crosscheck,
        "evidence_ids": EVIDENCE_IDS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--factor-store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    args = parser.parse_args()
    print(json.dumps(audit(args.database, args.factor_store, args.evidence_store), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
