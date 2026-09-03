"""Audit an immutable M3.2 factor release against its files, registry, and M2 lineage."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import duckdb

from alpha_research_os.factors.assets import FactorReleaseManifest
from alpha_research_os.kernel.canonical import content_hash


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def audit(database: Path, store: Path, release_id: str) -> dict[str, object]:
    release_dir = store / "releases" / release_id.removeprefix("sha256:")
    manifest_path = release_dir / "manifest.json"
    quality_path = release_dir / "quality_summary.json"
    parquet_path = release_dir / "raw_factor_values.parquet"
    manifest = FactorReleaseManifest.model_validate_json(manifest_path.read_bytes())
    failures: list[str] = []
    if manifest.release_id != release_id:
        failures.append("release id differs from requested audit identity")
    if _sha256_file(parquet_path) != manifest.parquet_hash:
        failures.append("Parquet hash differs from release manifest")
    if _sha256_file(quality_path) != manifest.quality_summary_hash:
        failures.append("quality summary hash differs from release manifest")
    quality = json.loads(quality_path.read_bytes())
    path = parquet_path.resolve().as_posix().replace("'", "''")
    with duckdb.connect(str(database), read_only=True) as connection:
        registered = connection.execute(
            """SELECT manifest_hash, parquet_hash, row_count, factor_count, quality_status
            FROM metadata.factor_release_manifest WHERE release_id=?""",
            [release_id],
        ).fetchone()
        expected_registered = (
            content_hash(manifest),
            manifest.parquet_hash,
            manifest.row_count,
            manifest.factor_count,
            manifest.quality_status,
        )
        if registered != expected_registered:
            failures.append("DuckDB release registry differs from immutable manifest")
        source = f"read_parquet('{path}')"
        stats = connection.execute(
            f"""SELECT count(*), count(DISTINCT session), count(DISTINCT instrument_id),
            count(DISTINCT factor_id),
            count(*) FILTER (WHERE value IS NOT NULL AND NOT isfinite(value)),
            min(session), max(session)
            FROM {source}"""
        ).fetchone()
        if stats[:4] != (
            manifest.row_count,
            manifest.session_count,
            manifest.instrument_count,
            manifest.factor_count,
        ):
            failures.append("Parquet dimensions differ from manifest")
        if stats[4]:
            failures.append("factor release contains non-finite values")
        duplicates = connection.execute(
            f"""SELECT count(*) FROM (
            SELECT session, instrument_id, factor_id, factor_version, variant, count(*) AS n
            FROM {source} GROUP BY 1,2,3,4,5 HAVING n > 1)"""
        ).fetchone()[0]
        if duplicates:
            failures.append("factor release contains duplicate logical keys")
        outside_universe = connection.execute(
            f"""SELECT count(*) FROM {source} f
            LEFT JOIN research.universe_daily u
              ON u.trade_date=f.session AND u.ts_code=f.instrument_id
            WHERE coalesce(u.eligible_for_signal, false) = false"""
        ).fetchone()[0]
        if outside_universe:
            failures.append("factor release contains rows outside ALL-A-PIT eligibility")
        clock_errors = connection.execute(
            f"""SELECT count(*) FROM {source}
            WHERE CAST(available_at AT TIME ZONE 'Asia/Shanghai' AS DATE) != session"""
        ).fetchone()[0]
        if clock_errors:
            failures.append("factor available_at does not map to its signal session")
        current_lineage = {}
        for item in manifest.request.dataset_lineage:
            hashes = tuple(
                sorted(
                    {
                        row[0]
                        for row in connection.execute(f"SELECT checkpoint_hash FROM {item.manifest_table}").fetchall()
                    }
                )
            )
            current_lineage[item.manifest_table] = hashes
            if hashes != item.checkpoint_hashes:
                failures.append(f"current M2 lineage differs: {item.manifest_table}")
        registry_count = connection.execute(
            """SELECT count(*) FROM metadata.factor_registry r
            JOIN (SELECT unnest(?) AS factor_id) requested USING (factor_id)""",
            [[item.factor_id for item in manifest.request.factors]],
        ).fetchone()[0]
        if registry_count != manifest.factor_count:
            failures.append("not every release factor is present in the registry")
    return {
        "audited_at": datetime.now().astimezone().isoformat(),
        "release_id": release_id,
        "manifest_hash": content_hash(manifest),
        "parquet_hash": manifest.parquet_hash,
        "quality_summary": quality,
        "duplicate_key_count": duplicates,
        "outside_universe_count": outside_universe,
        "clock_error_count": clock_errors,
        "current_lineage": current_lineage,
        "failures": failures,
        "status": "PASS" if not failures else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/m3_2_factor_release_audit.json"))
    args = parser.parse_args()
    report = audit(args.database, args.store, args.release_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
