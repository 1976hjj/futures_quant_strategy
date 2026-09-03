"""Publish immutable M4.2 cross-sectionally processed factor releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from alpha_research_os.factors import (
    FactorReleaseManifest,
    PreprocessingSpec,
    ProcessedFactorAssetRequest,
    ProcessedFactorReleaseManifest,
)
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash

ENGINE_VERSION = "duckdb-cross-sectional-preprocessing-1.0.0"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _parent_release(store: Path, release_id: str) -> tuple[FactorReleaseManifest, Path]:
    release_dir = store / "releases" / release_id.removeprefix("sha256:")
    manifest_path = release_dir / "manifest.json"
    manifest = FactorReleaseManifest.model_validate_json(manifest_path.read_bytes())
    parquet = store / manifest.parquet_relative_path
    if manifest.release_id != release_id or _sha256_file(parquet) != manifest.parquet_hash:
        raise ValueError("parent RAW factor release failed immutable verification")
    return manifest, parquet


def _preprocessing_spec(variant: str) -> PreprocessingSpec:
    if variant == "WINSORIZED_ZSCORE":
        return PreprocessingSpec(
            preprocessing_id="cross-section-mad-zscore",
            preprocessing_version="1.0.0",
            neutralize_log_size=False,
        )
    if variant == "SIZE_NEUTRALIZED":
        return PreprocessingSpec(
            preprocessing_id="cross-section-mad-zscore-size-residual",
            preprocessing_version="1.0.0",
            neutralize_log_size=True,
        )
    raise ValueError(f"unsupported processed factor variant: {variant}")


def _request(parent: FactorReleaseManifest, variant: str) -> ProcessedFactorAssetRequest:
    return ProcessedFactorAssetRequest(
        engine_version=ENGINE_VERSION,
        parent_release_id=parent.release_id,
        parent_parquet_hash=parent.parquet_hash,
        dataset_lineage=parent.request.dataset_lineage,
        universe_id=parent.request.universe_id,
        universe_version=parent.request.universe_version,
        start=parent.request.start,
        end=parent.request.end,
        variant=variant,
        preprocessing=_preprocessing_spec(variant),
    )


def _common_ctes(
    parent: Path,
    request: ProcessedFactorAssetRequest,
    output_start: date | None = None,
    output_end: date | None = None,
) -> str:
    spec = request.preprocessing
    lower = output_start or request.start
    upper = output_end or request.end
    return f"""
      raw_input AS (
        SELECT * FROM read_parquet('{_sql_path(parent)}')
        WHERE session BETWEEN DATE {_sql_string(lower.isoformat())} AND DATE {_sql_string(upper.isoformat())}
      ), centers AS (
        SELECT session, factor_id, factor_version, median(value) AS center
        FROM raw_input WHERE value IS NOT NULL
        GROUP BY 1,2,3
      ), dispersions AS (
        SELECT r.session, r.factor_id, r.factor_version,
               median(abs(r.value-c.center)) AS mad
        FROM raw_input r JOIN centers c USING (session, factor_id, factor_version)
        WHERE r.value IS NOT NULL
        GROUP BY 1,2,3
      ), winsorized AS (
        SELECT r.*,
          CASE WHEN r.value IS NULL THEN NULL
               WHEN d.mad IS NULL OR d.mad=0 THEN r.value
               ELSE greatest(c.center-{spec.mad_multiplier}*{spec.mad_consistency_scale}*d.mad,
                    least(c.center+{spec.mad_multiplier}*{spec.mad_consistency_scale}*d.mad, r.value))
          END::DOUBLE AS winsorized_value
        FROM raw_input r
        LEFT JOIN centers c USING (session, factor_id, factor_version)
        LEFT JOIN dispersions d USING (session, factor_id, factor_version)
      ), standardization_stats AS (
        SELECT session, factor_id, factor_version, count(winsorized_value) AS present_n,
               avg(winsorized_value) AS value_mean, stddev_pop(winsorized_value) AS value_std
        FROM winsorized GROUP BY 1,2,3
      ), standardized AS (
        SELECT w.*,
          CASE WHEN s.present_n>={spec.minimum_cross_section} AND s.value_std>0
               THEN (w.winsorized_value-s.value_mean)/s.value_std END::DOUBLE AS standardized_value
        FROM winsorized w JOIN standardization_stats s USING (session, factor_id, factor_version)
      )
    """


def _materialization_sql(
    parent: Path,
    target: Path,
    request: ProcessedFactorAssetRequest,
    output_start: date | None = None,
    output_end: date | None = None,
) -> str:
    common = _common_ctes(parent, request, output_start, output_end)
    release = _sql_string(request.computation_key)
    parent_id = _sql_string(request.parent_release_id)
    variant = _sql_string(request.variant)
    preprocessing_hash = _sql_string(request.preprocessing.spec_hash)
    if request.variant == "WINSORIZED_ZSCORE":
        final_ctes = ""
        value_expression = "standardized_value"
        final_source = "standardized"
    else:
        minimum = request.preprocessing.minimum_cross_section
        final_ctes = f""", exposed AS (
          SELECT s.*, CASE WHEN b.total_mv>0 THEN ln(b.total_mv) END AS log_size
          FROM standardized s
          LEFT JOIN research.daily_basic b
            ON b.trade_date=s.session AND b.ts_code=s.instrument_id
        ), regression_stats AS (
          SELECT session, factor_id, factor_version,
                 count(*) FILTER (WHERE standardized_value IS NOT NULL AND log_size IS NOT NULL) AS regression_n,
                 avg(standardized_value) FILTER
                   (WHERE standardized_value IS NOT NULL AND log_size IS NOT NULL) AS y_mean,
                 avg(log_size) FILTER
                   (WHERE standardized_value IS NOT NULL AND log_size IS NOT NULL) AS x_mean,
                 regr_slope(standardized_value, log_size) FILTER
                   (WHERE standardized_value IS NOT NULL AND log_size IS NOT NULL) AS size_beta
          FROM exposed GROUP BY 1,2,3
        ), residualized AS (
          SELECT e.*,
            CASE WHEN r.regression_n>={minimum} AND isfinite(r.size_beta)
                 THEN e.standardized_value-(r.y_mean+r.size_beta*(e.log_size-r.x_mean)) END::DOUBLE
              AS residual_value
          FROM exposed e JOIN regression_stats r USING (session, factor_id, factor_version)
        ), residual_stats AS (
          SELECT session, factor_id, factor_version, count(residual_value) AS residual_n,
                 avg(residual_value) AS residual_mean, stddev_pop(residual_value) AS residual_std
          FROM residualized GROUP BY 1,2,3
        ), final_values AS (
          SELECT r.*,
            CASE WHEN s.residual_n>={minimum} AND s.residual_std>0
                 THEN (r.residual_value-s.residual_mean)/s.residual_std END::DOUBLE AS final_value
          FROM residualized r JOIN residual_stats s USING (session, factor_id, factor_version)
        )"""
        value_expression = "final_value"
        final_source = "final_values"
    return f"""
      COPY (WITH {common}{final_ctes}
      SELECT {release} AS release_id, session, instrument_id, factor_id, factor_version,
             {variant} AS variant, {value_expression} AS value, available_at,
             implementation_hash, {parent_id} AS parent_release_id,
             {preprocessing_hash} AS preprocessing_hash
      FROM {final_source}
      ORDER BY session, instrument_id, factor_id, factor_version)
      TO '{_sql_path(target)}'
      (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 122880)
    """


def _configure_bounded_connection(connection: duckdb.DuckDBPyConnection, temporary_directory: Path) -> None:
    temporary_directory.mkdir(parents=True, exist_ok=True)
    connection.execute("SET TimeZone='Asia/Shanghai'")
    connection.execute("SET memory_limit='10GB'")
    connection.execute(f"SET temp_directory='{_sql_path(temporary_directory)}'")


def _materialize_yearly(
    database: Path,
    store: Path,
    parent: Path,
    request: ProcessedFactorAssetRequest,
    target: Path,
) -> None:
    staging = target.parent / "yearly_staging"
    staging.mkdir(parents=True, exist_ok=True)
    partitions: list[Path] = []
    for year in range(request.start.year, request.end.year + 1):
        lower = max(request.start, date(year, 1, 1))
        upper = min(request.end, date(year, 12, 31))
        partition = staging / f"year={year}.parquet"
        partitions.append(partition)
        if partition.exists():
            with duckdb.connect() as connection:
                identity, first_session, last_session = connection.execute(
                    f"SELECT min(release_id),min(session),max(session) FROM read_parquet('{_sql_path(partition)}')"
                ).fetchone()
            if identity == request.computation_key and lower <= first_session <= last_session <= upper:
                print(f"variant={request.variant} year={year} cache_hit", flush=True)
                continue
            raise ValueError(f"invalid processed yearly staging partition: {partition}")
        temporary = partition.with_name(f".{partition.stem}.{uuid.uuid4().hex}.tmp.parquet")
        print(f"variant={request.variant} year={year} materializing", flush=True)
        with duckdb.connect(str(database), read_only=True) as connection:
            _configure_bounded_connection(connection, store / "duckdb_tmp")
            connection.execute(_materialization_sql(parent, temporary, request, lower, upper))
        os.replace(temporary, partition)
    parquet_list = ",".join(_sql_string(_sql_path(path)) for path in partitions)
    print(f"variant={request.variant} combining yearly partitions", flush=True)
    with duckdb.connect() as connection:
        _configure_bounded_connection(connection, store / "duckdb_tmp")
        connection.execute(
            f"""COPY (SELECT * FROM read_parquet([{parquet_list}])) TO '{_sql_path(target)}'
            (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 122880)"""
        )
    for partition in partitions:
        partition.unlink()
    staging.rmdir()


def _quality(path: Path, factor_count: int, minimum: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = f"read_parquet('{_sql_path(path)}')"
    with duckdb.connect() as connection:
        row = connection.execute(
            f"""SELECT count(*), count(value), count(DISTINCT session), count(DISTINCT instrument_id),
                count(DISTINCT factor_id), count(*) FILTER (WHERE value IS NOT NULL AND NOT isfinite(value))
                FROM {source}"""
        ).fetchone()
        duplicate_count = connection.execute(
            f"""SELECT count(*) FROM (SELECT session,instrument_id,factor_id,factor_version,variant,count(*) n
            FROM {source} GROUP BY 1,2,3,4,5 HAVING n>1)"""
        ).fetchone()[0]
        details = [
            {
                "factor_id": item[0],
                "factor_version": item[1],
                "row_count": item[2],
                "present_count": item[3],
                "coverage": item[3] / item[2] if item[2] else 0.0,
                "cross_section_mean_abs_max": item[4],
                "cross_section_std_error_max": item[5],
            }
            for item in connection.execute(
                f"""WITH per_day AS (
                  SELECT session,factor_id,factor_version,count(value) n,
                         avg(value) value_mean,stddev_pop(value) value_std
                  FROM {source} GROUP BY 1,2,3), totals AS (
                  SELECT factor_id,factor_version,count(*) row_count,count(value) present_count
                  FROM {source} GROUP BY 1,2)
                SELECT t.factor_id,t.factor_version,t.row_count,t.present_count,
                       max(abs(p.value_mean)) FILTER (WHERE p.n>={minimum}),
                       max(abs(p.value_std-1)) FILTER (WHERE p.n>={minimum})
                FROM totals t JOIN per_day p USING (factor_id,factor_version)
                GROUP BY 1,2,3,4 ORDER BY 1,2"""
            ).fetchall()
        ]
    row_count, present_count, session_count, instrument_count, actual_factors, nonfinite = row
    if duplicate_count or nonfinite or actual_factors != factor_count:
        raise ValueError(
            f"processed quality gate failed duplicates={duplicate_count} nonfinite={nonfinite} "
            f"factors={actual_factors}/{factor_count}"
        )
    return (
        {
            "status": "PASS",
            "row_count": row_count,
            "present_count": present_count,
            "session_count": session_count,
            "instrument_count": instrument_count,
            "factor_count": actual_factors,
            "duplicate_key_count": duplicate_count,
            "nonfinite_count": nonfinite,
            "factors": details,
        },
        details,
    )


def _register(
    database: Path,
    store: Path,
    manifest: ProcessedFactorReleaseManifest,
    quality_details: list[dict[str, Any]],
) -> None:
    connection = duckdb.connect(str(database))
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute("CREATE SCHEMA IF NOT EXISTS metadata")
        connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
        connection.execute("CREATE SCHEMA IF NOT EXISTS research")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.processed_factor_release_manifest (
            release_id VARCHAR PRIMARY KEY, manifest_hash VARCHAR, parent_release_id VARCHAR,
            variant VARCHAR, preprocessing_hash VARCHAR, parquet_path VARCHAR, parquet_hash VARCHAR,
            start_date DATE, end_date DATE, factor_count BIGINT, row_count BIGINT, present_count BIGINT,
            quality_status VARCHAR, request_json JSON, created_at TIMESTAMPTZ)"""
        )
        manifest_hash = content_hash(manifest)
        existing = connection.execute(
            "SELECT manifest_hash,parquet_hash FROM metadata.processed_factor_release_manifest WHERE release_id=?",
            [manifest.release_id],
        ).fetchone()
        if existing and existing != (manifest_hash, manifest.parquet_hash):
            raise ValueError("immutable processed factor release registry conflict")
        connection.execute(
            """INSERT OR IGNORE INTO metadata.processed_factor_release_manifest VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                manifest.release_id,
                manifest_hash,
                manifest.request.parent_release_id,
                manifest.request.variant,
                manifest.request.preprocessing.spec_hash,
                manifest.parquet_relative_path,
                manifest.parquet_hash,
                manifest.request.start,
                manifest.request.end,
                manifest.factor_count,
                manifest.row_count,
                manifest.present_count,
                manifest.quality_status,
                json.dumps(manifest.request.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                manifest.created_at,
            ],
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.processed_factor_quality_summary (
            release_id VARCHAR, factor_id VARCHAR, factor_version VARCHAR, row_count BIGINT,
            present_count BIGINT, coverage DOUBLE, cross_section_mean_abs_max DOUBLE,
            cross_section_std_error_max DOUBLE, PRIMARY KEY (release_id,factor_id,factor_version))"""
        )
        connection.executemany(
            "INSERT OR IGNORE INTO metadata.processed_factor_quality_summary VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    manifest.release_id,
                    item["factor_id"],
                    item["factor_version"],
                    item["row_count"],
                    item["present_count"],
                    item["coverage"],
                    item["cross_section_mean_abs_max"],
                    item["cross_section_std_error_max"],
                )
                for item in quality_details
            ],
        )
        processed_glob = _sql_path(store / "processed_releases" / "*" / "processed_factor_values.parquet")
        raw_glob = _sql_path(store / "releases" / "*" / "raw_factor_values.parquet")
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.factor_values_processed AS
            SELECT * FROM read_parquet('{processed_glob}', union_by_name=true)"""
        )
        connection.execute(
            """CREATE OR REPLACE VIEW research.factor_values_processed AS
            SELECT *, value IS NOT NULL AS is_present FROM raw.factor_values_processed"""
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW research.factor_values_all AS
            SELECT * FROM read_parquet(['{raw_glob}','{processed_glob}'], union_by_name=true)"""
        )
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def publish(database: Path, store: Path, parent_release_id: str, variant: str) -> dict[str, Any]:
    parent, parent_parquet = _parent_release(store, parent_release_id)
    request = _request(parent, variant)
    release_dir = store / "processed_releases" / request.computation_key.removeprefix("sha256:")
    parquet = release_dir / "processed_factor_values.parquet"
    quality_path = release_dir / "quality_summary.json"
    manifest_path = release_dir / "manifest.json"
    if manifest_path.exists() and parquet.exists() and quality_path.exists():
        manifest = ProcessedFactorReleaseManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.request != request or _sha256_file(parquet) != manifest.parquet_hash:
            raise ValueError("cached processed release failed immutable verification")
        quality = json.loads(quality_path.read_bytes())
        _register(database, store, manifest, quality["factors"])
        return {"cache_hit": True, "release_id": manifest.release_id, "manifest": str(manifest_path.resolve())}
    release_dir.mkdir(parents=True, exist_ok=True)
    temporary = release_dir / f".processed_factor_values.{uuid.uuid4().hex}.tmp.parquet"
    if (request.end - request.start).days > 370:
        _materialize_yearly(database, store, parent_parquet, request, temporary)
    else:
        with duckdb.connect(str(database), read_only=True) as connection:
            _configure_bounded_connection(connection, store / "duckdb_tmp")
            connection.execute(_materialization_sql(parent_parquet, temporary, request))
    quality, details = _quality(temporary, parent.factor_count, request.preprocessing.minimum_cross_section)
    os.replace(temporary, parquet)
    _atomic_write(quality_path, canonical_json_bytes(quality))
    manifest = ProcessedFactorReleaseManifest(
        release_id=request.computation_key,
        request=request,
        created_at=datetime.now().astimezone(),
        parquet_relative_path=parquet.relative_to(store).as_posix(),
        parquet_hash=_sha256_file(parquet),
        row_count=quality["row_count"],
        present_count=quality["present_count"],
        session_count=quality["session_count"],
        instrument_count=quality["instrument_count"],
        factor_count=quality["factor_count"],
        quality_status="PASS",
        quality_summary_hash=_sha256_file(quality_path),
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    _register(database, store, manifest, details)
    return {
        "cache_hit": False,
        "release_id": manifest.release_id,
        "parent_release_id": parent_release_id,
        "variant": variant,
        "row_count": manifest.row_count,
        "present_count": manifest.present_count,
        "manifest": str(manifest_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--parent-release-id", required=True)
    parser.add_argument("--variant", choices=("WINSORIZED_ZSCORE", "SIZE_NEUTRALIZED"), required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            publish(args.database, args.store, args.parent_release_id, args.variant),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
