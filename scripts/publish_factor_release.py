"""Materialize and register an immutable experiment-scoped M3.2 RAW factor release."""

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

from alpha_research_os.factors import build_initial_catalog
from alpha_research_os.factors.assets import (
    DatasetLineage,
    FactorAssetRef,
    FactorAssetRequest,
    FactorReleaseManifest,
)
from alpha_research_os.factors.sql import expression_manifest_to_duckdb
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash

ENGINE_VERSION = "duckdb-feature-sql-1.0.0"
SIGNAL_CLOCK_VERSION = "cn-close-postclose-v1"


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


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _lineage(connection: duckdb.DuckDBPyConnection) -> tuple[DatasetLineage, ...]:
    tables = (
        "metadata.archive_manifest",
        "metadata.m2b_archive_manifest",
        "metadata.m2c_archive_manifest",
        "metadata.m2d_archive_manifest",
    )
    result = []
    for table in tables:
        hashes = tuple(
            sorted({row[0] for row in connection.execute(f"SELECT checkpoint_hash FROM {table}").fetchall()})
        )
        result.append(DatasetLineage(manifest_table=table, checkpoint_hashes=hashes))
    return tuple(sorted(result, key=lambda item: item.manifest_table))


def _request(connection: duckdb.DuckDBPyConnection, start: date, end: date) -> tuple[FactorAssetRequest, Any]:
    catalog = build_initial_catalog()
    references = tuple(
        sorted(
            (
                FactorAssetRef(
                    factor_id=item.entry.spec.factor_id,
                    factor_version=item.entry.spec.factor_version,
                    spec_hash=item.spec_hash,
                    implementation_hash=item.entry.spec.implementation_hash,
                    catalog_entry_hash=item.entry_hash,
                )
                for item in catalog.list()
            ),
            key=lambda item: (item.factor_id, item.factor_version),
        )
    )
    lineage = _lineage(connection)
    m2b_hash = next(
        item.checkpoint_hashes[0] for item in lineage if item.manifest_table == "metadata.m2b_archive_manifest"
    )
    request = FactorAssetRequest(
        engine_version=ENGINE_VERSION,
        factors=references,
        dataset_lineage=lineage,
        universe_id="ALL-A-PIT",
        universe_version=f"m2b-{m2b_hash.removeprefix('sha256:')[:16]}",
        start=start,
        end=end,
        signal_clock_version=SIGNAL_CLOCK_VERSION,
    )
    return request, catalog


def _warmup_start(connection: duckdb.DuckDBPyConnection, start: date, history: int) -> date:
    if history == 0:
        return start
    rows = connection.execute(
        """SELECT cal_date FROM research.trading_calendar
        WHERE exchange = 'SSE' AND is_open AND cal_date < ?
        ORDER BY cal_date DESC LIMIT ?""",
        [start, history],
    ).fetchall()
    return min((row[0] for row in rows), default=start)


def _materialization_sql(catalog: Any, request: FactorAssetRequest, target: Path, warmup_start: date) -> str:
    window = "PARTITION BY instrument_id ORDER BY session"
    expressions = []
    factor_ids = []
    factor_versions = []
    implementation_hashes = []
    for index, item in enumerate(catalog.list()):
        registered = catalog.registry.get(item.entry.spec.factor_id, item.entry.spec.factor_version)
        assert registered.compiled_expression is not None
        root = registered.compiled_expression.manifest()["root"]
        sql = expression_manifest_to_duckdb(root, window=window)
        expressions.append(f"try_cast(({sql}) AS DOUBLE) AS factor_{index}")
        factor_ids.append(_sql_string(item.entry.spec.factor_id))
        factor_versions.append(_sql_string(item.entry.spec.factor_version))
        implementation_hashes.append(_sql_string(item.entry.spec.implementation_hash))
    value_columns = ", ".join(f"factor_{index}" for index in range(len(expressions)))
    path = _sql_path(target)
    return f"""
        COPY (
          WITH feature_input AS (
            SELECT
              u.trade_date AS session,
              u.ts_code AS instrument_id,
              u.eligible_for_signal,
              m.open, m.high, m.low, m.close, m.volume_shares,
              CASE WHEN m.pre_close > 0 THEN m.close / m.pre_close - 1 END AS return_1d,
              CASE WHEN m.pre_close > 0 AND m.amount_cny > 0
                   THEN abs(m.close / m.pre_close - 1) / m.amount_cny END AS illiquidity_1d,
              CASE WHEN b.pb > 0 THEN b.pb END AS pb,
              CASE WHEN b.pe_ttm > 0 THEN b.pe_ttm END AS pe_ttm,
              CASE WHEN b.total_mv > 0 THEN b.total_mv END AS total_mv,
              f.roe, f.debt_to_assets
            FROM research.universe_daily u
            LEFT JOIN research.market_daily m USING (trade_date, ts_code)
            LEFT JOIN research.daily_basic b USING (trade_date, ts_code)
            LEFT JOIN LATERAL (
              SELECT p.roe, p.debt_to_assets
              FROM research.financial_pit_asof p
              WHERE p.source_api = 'fina_indicator_vip'
                AND p.ts_code = u.ts_code
                AND p.valid_from < u.trade_date
                AND (p.valid_to IS NULL OR u.trade_date < p.valid_to)
              ORDER BY p.valid_from DESC, p.revision_number DESC
              LIMIT 1
            ) f ON true
            WHERE u.trade_date BETWEEN DATE {_sql_string(warmup_start.isoformat())}
                                   AND DATE {_sql_string(request.end.isoformat())}
          ), computed AS (
            SELECT *, {", ".join(expressions)} FROM feature_input
          ), long_values AS (
            SELECT
              {_sql_string(request.computation_key)} AS release_id,
              session,
              instrument_id,
              unnest([{", ".join(factor_ids)}]) AS factor_id,
              unnest([{", ".join(factor_versions)}]) AS factor_version,
              'RAW' AS variant,
              unnest([{value_columns}]) AS candidate_value,
              unnest([{", ".join(implementation_hashes)}]) AS implementation_hash
            FROM computed
            WHERE session BETWEEN DATE {_sql_string(request.start.isoformat())}
                              AND DATE {_sql_string(request.end.isoformat())}
              AND eligible_for_signal
          )
          SELECT
            release_id, session, instrument_id, factor_id, factor_version, variant,
            CASE WHEN isfinite(candidate_value) THEN candidate_value END AS value,
            session::TIMESTAMP AT TIME ZONE 'Asia/Shanghai' + INTERVAL 15 HOURS AS available_at,
            implementation_hash
          FROM long_values
          ORDER BY session, instrument_id, factor_id, factor_version
        ) TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 122880)
    """


def _quality(path: Path, factor_count: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with duckdb.connect() as connection:
        source = f"read_parquet('{_sql_path(path)}')"
        duplicate_count = connection.execute(
            f"""SELECT count(*) FROM (
            SELECT session, instrument_id, factor_id, factor_version, variant, count(*) AS n
            FROM {source} GROUP BY 1,2,3,4,5 HAVING n > 1)"""
        ).fetchone()[0]
        nonfinite_count = connection.execute(
            f"SELECT count(*) FROM {source} WHERE value IS NOT NULL AND NOT isfinite(value)"
        ).fetchone()[0]
        count_sql = (
            "SELECT count(*), count(DISTINCT session), "
            "count(DISTINCT instrument_id), count(DISTINCT factor_id) "
            f"FROM {source}"
        )
        row_count, session_count, instrument_count, actual_factor_count = connection.execute(count_sql).fetchone()
        details = [
            {
                "factor_id": row[0],
                "factor_version": row[1],
                "row_count": row[2],
                "present_count": row[3],
                "coverage": row[3] / row[2] if row[2] else 0.0,
                "minimum": row[4],
                "maximum": row[5],
            }
            for row in connection.execute(
                f"""SELECT factor_id, factor_version, count(*), count(value), min(value), max(value)
                FROM {source} GROUP BY 1,2 ORDER BY 1,2"""
            ).fetchall()
        ]
    if duplicate_count or nonfinite_count or actual_factor_count != factor_count:
        raise ValueError(
            f"factor quality gate failed: duplicates={duplicate_count} nonfinite={nonfinite_count} "
            f"factors={actual_factor_count}/{factor_count}"
        )
    summary = {
        "status": "PASS",
        "row_count": row_count,
        "session_count": session_count,
        "instrument_count": instrument_count,
        "factor_count": actual_factor_count,
        "duplicate_key_count": duplicate_count,
        "nonfinite_count": nonfinite_count,
        "factors": details,
    }
    return summary, details


def _register(
    database: Path,
    store: Path,
    manifest: FactorReleaseManifest,
    manifest_hash: str,
    catalog: Any,
    quality_details: list[dict[str, Any]],
) -> None:
    connection = duckdb.connect(str(database))
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
        connection.execute("CREATE SCHEMA IF NOT EXISTS research")
        connection.execute("CREATE SCHEMA IF NOT EXISTS metadata")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.factor_registry (
            factor_id VARCHAR, factor_version VARCHAR, spec_hash VARCHAR, implementation_hash VARCHAR,
            catalog_entry_hash VARCHAR, family VARCHAR, lifecycle VARCHAR, source_id VARCHAR,
            spec_json JSON, registered_at TIMESTAMPTZ,
            PRIMARY KEY (factor_id, factor_version))"""
        )
        for item in catalog.list():
            spec = item.entry.spec
            existing = connection.execute(
                """SELECT spec_hash, catalog_entry_hash FROM metadata.factor_registry
                WHERE factor_id=? AND factor_version=?""",
                [spec.factor_id, spec.factor_version],
            ).fetchone()
            if existing and existing != (item.spec_hash, item.entry_hash):
                raise ValueError(f"immutable factor registry conflict: {spec.factor_id}@{spec.factor_version}")
            connection.execute(
                """INSERT OR IGNORE INTO metadata.factor_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    spec.factor_id,
                    spec.factor_version,
                    item.spec_hash,
                    spec.implementation_hash,
                    item.entry_hash,
                    item.entry.family,
                    item.entry.lifecycle.value,
                    item.entry.source_reference.source_id,
                    json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                    manifest.created_at,
                ],
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.factor_release_manifest (
            release_id VARCHAR PRIMARY KEY, manifest_hash VARCHAR, parquet_path VARCHAR, parquet_hash VARCHAR,
            start_date DATE, end_date DATE, universe_id VARCHAR, universe_version VARCHAR,
            factor_count BIGINT, row_count BIGINT, quality_status VARCHAR, request_json JSON,
            created_at TIMESTAMPTZ)"""
        )
        relative_path = manifest.parquet_relative_path
        existing_release = connection.execute(
            "SELECT manifest_hash, parquet_hash FROM metadata.factor_release_manifest WHERE release_id=?",
            [manifest.release_id],
        ).fetchone()
        if existing_release and existing_release != (manifest_hash, manifest.parquet_hash):
            raise ValueError("immutable factor release registry conflict")
        connection.execute(
            """INSERT OR IGNORE INTO metadata.factor_release_manifest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                manifest.release_id,
                manifest_hash,
                relative_path,
                manifest.parquet_hash,
                manifest.request.start,
                manifest.request.end,
                manifest.request.universe_id,
                manifest.request.universe_version,
                manifest.factor_count,
                manifest.row_count,
                manifest.quality_status,
                json.dumps(manifest.request.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                manifest.created_at,
            ],
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.factor_quality_summary (
            release_id VARCHAR, factor_id VARCHAR, factor_version VARCHAR, row_count BIGINT,
            present_count BIGINT, coverage DOUBLE, minimum DOUBLE, maximum DOUBLE,
            PRIMARY KEY (release_id, factor_id, factor_version))"""
        )
        connection.executemany(
            "INSERT OR IGNORE INTO metadata.factor_quality_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    manifest.release_id,
                    item["factor_id"],
                    item["factor_version"],
                    item["row_count"],
                    item["present_count"],
                    item["coverage"],
                    item["minimum"],
                    item["maximum"],
                )
                for item in quality_details
            ],
        )
        release_glob = _sql_path(store / "releases" / "*" / "raw_factor_values.parquet")
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.factor_values AS
            SELECT * FROM read_parquet('{release_glob}', union_by_name=true)"""
        )
        connection.execute(
            """CREATE OR REPLACE VIEW research.factor_values_raw AS
            SELECT *, value IS NOT NULL AS is_present
            FROM raw.factor_values"""
        )
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def publish(database: Path, store: Path, start: date, end: date) -> dict[str, Any]:
    with duckdb.connect(str(database), read_only=True) as connection:
        request, catalog = _request(connection, start, end)
        max_history = max(
            item.compiled_expression.required_history
            for item in catalog.registry.list()
            if item.compiled_expression is not None
        )
        warmup_start = _warmup_start(connection, start, max_history)
    release_dir = store / "releases" / request.computation_key.removeprefix("sha256:")
    parquet = release_dir / "raw_factor_values.parquet"
    quality_path = release_dir / "quality_summary.json"
    manifest_path = release_dir / "manifest.json"
    if manifest_path.exists() and parquet.exists() and quality_path.exists():
        manifest = FactorReleaseManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.request != request or _sha256_file(parquet) != manifest.parquet_hash:
            raise ValueError("cached factor release failed immutable identity verification")
        quality = json.loads(quality_path.read_bytes())
        _register(database, store, manifest, content_hash(manifest), catalog, quality["factors"])
        return {"cache_hit": True, "release_id": manifest.release_id, "manifest": str(manifest_path.resolve())}

    release_dir.mkdir(parents=True, exist_ok=True)
    temporary = release_dir / f".raw_factor_values.{uuid.uuid4().hex}.tmp.parquet"
    with duckdb.connect(str(database), read_only=True) as connection:
        connection.execute("SET TimeZone='Asia/Shanghai'")
        connection.execute(_materialization_sql(catalog, request, temporary, warmup_start))
    quality, quality_details = _quality(temporary, len(request.factors))
    os.replace(temporary, parquet)
    _atomic_write(quality_path, canonical_json_bytes(quality))
    manifest = FactorReleaseManifest(
        release_id=request.computation_key,
        request=request,
        created_at=datetime.now().astimezone(),
        parquet_relative_path=parquet.relative_to(store).as_posix(),
        parquet_hash=_sha256_file(parquet),
        row_count=quality["row_count"],
        session_count=quality["session_count"],
        instrument_count=quality["instrument_count"],
        factor_count=quality["factor_count"],
        quality_status="PASS",
        quality_summary_hash=_sha256_file(quality_path),
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    manifest_hash = content_hash(manifest)
    _register(database, store, manifest, manifest_hash, catalog, quality_details)
    return {
        "cache_hit": False,
        "release_id": manifest.release_id,
        "manifest_hash": manifest_hash,
        "parquet_hash": manifest.parquet_hash,
        "row_count": manifest.row_count,
        "session_count": manifest.session_count,
        "instrument_count": manifest.instrument_count,
        "factor_count": manifest.factor_count,
        "manifest": str(manifest_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 2))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 3, 29))
    args = parser.parse_args()
    print(json.dumps(publish(args.database, args.store, args.start, args.end), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
