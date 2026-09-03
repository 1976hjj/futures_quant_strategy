"""Build a queryable DuckDB + Parquet warehouse from immutable Tushare snapshots."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMAS = {
    "daily": pa.schema(
        [
            ("ts_code", pa.string()),
            ("trade_date", pa.date32()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("pre_close", pa.float64()),
            ("vol", pa.float64()),
            ("amount", pa.float64()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
    "adj_factor": pa.schema(
        [
            ("ts_code", pa.string()),
            ("trade_date", pa.date32()),
            ("adj_factor", pa.float64()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
    "daily_basic": pa.schema(
        [
            ("ts_code", pa.string()),
            ("trade_date", pa.date32()),
            ("close", pa.float64()),
            ("turnover_rate", pa.float64()),
            ("turnover_rate_f", pa.float64()),
            ("volume_ratio", pa.float64()),
            ("pe", pa.float64()),
            ("pe_ttm", pa.float64()),
            ("pb", pa.float64()),
            ("ps", pa.float64()),
            ("ps_ttm", pa.float64()),
            ("dv_ratio", pa.float64()),
            ("dv_ttm", pa.float64()),
            ("total_share", pa.float64()),
            ("float_share", pa.float64()),
            ("free_share", pa.float64()),
            ("total_mv", pa.float64()),
            ("circ_mv", pa.float64()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
}


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _artifact_path(artifact_root: Path, artifact_id: str) -> Path:
    algorithm, separator, digest = artifact_id.partition(":")
    if algorithm != "sha256" or separator != ":" or len(digest) != 64:
        raise ValueError(f"invalid artifact id: {artifact_id}")
    return artifact_root / "objects" / "sha256" / digest[:2] / digest


def _read_verified(path: Path, expected_hash: str) -> bytes:
    payload = path.read_bytes()
    if _sha256(payload) != expected_hash:
        raise ValueError(f"artifact hash mismatch: {path}")
    return payload


def _read_partition(artifact_root: Path, partition_date: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot_id = str(entry["snapshot_id"])
    snapshot_bytes = _read_verified(_artifact_path(artifact_root, snapshot_id), snapshot_id)
    snapshot_document = json.loads(snapshot_bytes)
    snapshot = snapshot_document["payload"]
    payload_artifact_id = str(snapshot["payload_artifact_id"])
    if payload_artifact_id != entry["payload_artifact_id"]:
        raise ValueError(f"checkpoint lineage mismatch: {partition_date}")
    stored = _read_verified(_artifact_path(artifact_root, payload_artifact_id), payload_artifact_id)
    encoding = snapshot["payload_encoding"]
    if encoding == "gzip":
        payload = gzip.decompress(stored)
    elif encoding == "identity":
        payload = stored
    else:
        raise ValueError(f"unsupported payload encoding: {encoding}")
    if len(payload) != snapshot["uncompressed_byte_size"]:
        raise ValueError(f"uncompressed byte-size mismatch: {partition_date}")
    if _sha256(payload) != snapshot["uncompressed_payload_hash"]:
        raise ValueError(f"uncompressed payload hash mismatch: {partition_date}")
    document = json.loads(payload)
    data = document.get("data", {})
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError(f"invalid tabular response: {partition_date}")
    if len(items) != entry["rows"]:
        raise ValueError(f"checkpoint row-count mismatch: {partition_date}")
    return [dict(zip(fields, item, strict=True)) for item in items]


def _normalize_row(
    api_name: str,
    partition_date: str,
    row: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any]:
    if str(row.get("trade_date")) != partition_date:
        raise ValueError(f"row trade_date differs from partition: {api_name}/{partition_date}")
    output: dict[str, Any] = {}
    for field in SCHEMAS[api_name]:
        name = field.name
        if name == "source_snapshot_id":
            output[name] = entry["snapshot_id"]
        elif name == "source_payload_artifact_id":
            output[name] = entry["payload_artifact_id"]
        elif name == "trade_date":
            output[name] = datetime.strptime(partition_date, "%Y%m%d").date()
        elif name == "ts_code":
            output[name] = str(row[name])
        else:
            value = row.get(name)
            output[name] = None if value is None or value == "" else float(value)
    return output


def _write_month(
    api_name: str,
    partitions: list[tuple[str, dict[str, Any]]],
    artifact_root: Path,
    target: Path,
) -> int:
    expected_rows = sum(int(entry["rows"]) for _, entry in partitions)
    if target.exists():
        metadata = pq.read_metadata(target)
        if metadata.num_rows == expected_rows and metadata.schema.to_arrow_schema() == SCHEMAS[api_name]:
            return expected_rows
    rows: list[dict[str, Any]] = []
    for partition_date, entry in partitions:
        raw_rows = _read_partition(artifact_root, partition_date, entry)
        rows.extend(_normalize_row(api_name, partition_date, row, entry) for row in raw_rows)
    table = pa.Table.from_pylist(rows, schema=SCHEMAS[api_name])
    key_count = len(set(zip(table["trade_date"].to_pylist(), table["ts_code"].to_pylist(), strict=True)))
    if key_count != table.num_rows:
        raise ValueError(f"duplicate (trade_date, ts_code) key in {api_name}/{target.parent}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    pq.write_table(
        table,
        temporary,
        compression="zstd",
        compression_level=6,
        use_dictionary=True,
        write_statistics=True,
        row_group_size=131_072,
    )
    os.replace(temporary, target)
    return table.num_rows


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _build_catalog(database: Path, parquet_root: Path, checkpoint_hash: str, counts: dict[str, int]) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database))
    try:
        connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
        connection.execute("CREATE SCHEMA IF NOT EXISTS research")
        connection.execute("CREATE SCHEMA IF NOT EXISTS metadata")
        for api_name in SCHEMAS:
            pattern = _sql_path(parquet_root / api_name / "year=*" / "month=*" / "data.parquet")
            connection.execute(
                f"CREATE OR REPLACE VIEW raw.{api_name} AS "
                f"SELECT * EXCLUDE (year, month) FROM read_parquet('{pattern}', hive_partitioning=true)"
            )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.market_daily AS
            SELECT
                ts_code, trade_date, open, high, low, close, pre_close,
                vol * 100.0 AS volume_shares,
                amount * 1000.0 AS amount_cny,
                high >= greatest(open, low, close)
                    AND low <= least(open, high, close) AS is_valid_ohlc,
                open = 0 AND high = 0 AND low = 0 AND close > 0
                    AND vol = 0 AND amount = 0 AS is_provider_zero_quote_row,
                open > 0 AND high > 0 AND low > 0 AND close > 0 AND vol > 0
                    AND high >= greatest(open, low, close)
                    AND low <= least(open, high, close) AS is_tradeable_bar,
                source_snapshot_id, source_payload_artifact_id
            FROM raw.daily
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.market_daily_tradable AS
            SELECT *
            FROM research.market_daily
            WHERE is_tradeable_bar
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.market_daily_anomalies AS
            SELECT *
            FROM research.market_daily
            WHERE NOT is_valid_ohlc OR is_provider_zero_quote_row
            """
        )
        connection.execute("CREATE OR REPLACE VIEW research.adj_factor AS SELECT * FROM raw.adj_factor")
        connection.execute("CREATE OR REPLACE VIEW research.daily_basic AS SELECT * FROM raw.daily_basic")
        connection.execute("DROP TABLE IF EXISTS metadata.archive_manifest")
        connection.execute(
            """
            CREATE TABLE metadata.archive_manifest (
                api_name VARCHAR PRIMARY KEY,
                row_count BIGINT NOT NULL,
                checkpoint_hash VARCHAR NOT NULL,
                built_at TIMESTAMPTZ NOT NULL,
                source VARCHAR NOT NULL
            )
            """
        )
        built_at = datetime.now().astimezone()
        connection.executemany(
            "INSERT INTO metadata.archive_manifest VALUES (?, ?, ?, ?, ?)",
            [
                (api_name, count, checkpoint_hash, built_at, "tushare-compatible-raw-archive")
                for api_name, count in counts.items()
            ],
        )
        connection.execute("DROP TABLE IF EXISTS metadata.column_dictionary")
        connection.execute(
            """
            CREATE TABLE metadata.column_dictionary AS
            SELECT * FROM (VALUES
                ('raw.daily', 'vol', 'hand', 'Tushare原始成交量，1手=100股'),
                ('raw.daily', 'amount', 'thousand CNY', 'Tushare原始成交额，单位千元'),
                ('research.market_daily', 'volume_shares', 'share', '换算后的成交股数'),
                ('research.market_daily', 'amount_cny', 'CNY', '换算后的成交金额'),
                ('research.market_daily', 'is_valid_ohlc', 'boolean', '高低价是否包住开盘价与收盘价'),
                ('research.market_daily', 'is_tradeable_bar', 'boolean', '通过价格、成交量和OHLC可交易性检查'),
                ('raw.daily_basic', 'total_share', '10k shares', '总股本，Tushare原始单位万股'),
                ('raw.daily_basic', 'total_mv', '10k CNY', '总市值，Tushare原始单位万元')
            ) AS t(table_name, column_name, unit, description)
            """
        )
        connection.execute("DROP TABLE IF EXISTS metadata.data_quality_summary")
        connection.execute(
            """
            CREATE TABLE metadata.data_quality_summary (
                issue_code VARCHAR PRIMARY KEY,
                severity VARCHAR NOT NULL,
                row_count BIGINT NOT NULL,
                description VARCHAR NOT NULL,
                detected_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        quality_counts = connection.execute(
            """
            SELECT
                count(*) FILTER (WHERE NOT is_valid_ohlc),
                count(*) FILTER (WHERE is_provider_zero_quote_row),
                count(*) FILTER (WHERE amount_cny IS NULL),
                count(*) FILTER (WHERE pre_close IS NULL)
            FROM research.market_daily
            """
        ).fetchone()
        connection.executemany(
            "INSERT INTO metadata.data_quality_summary VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "INVALID_OHLC",
                    "WARNING",
                    quality_counts[0],
                    "Raw provider row violates high/low containment; excluded from tradable view",
                    built_at,
                ),
                (
                    "PROVIDER_ZERO_QUOTE_ROW",
                    "WARNING",
                    quality_counts[1],
                    "Provider encoded a non-trading row with zero OHLC/volume/amount and carried close",
                    built_at,
                ),
                (
                    "NULL_AMOUNT",
                    "INFO",
                    quality_counts[2],
                    "Raw provider row has no transaction amount",
                    built_at,
                ),
                (
                    "NULL_PRE_CLOSE",
                    "INFO",
                    quality_counts[3],
                    "Raw provider row has no previous close",
                    built_at,
                ),
            ],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def build(archive: Path, warehouse: Path) -> dict[str, Any]:
    checkpoint_path = archive / "checkpoint.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    if checkpoint.get("schema") != "tushare-daily-backfill-v1":
        raise ValueError("unsupported checkpoint schema")
    completed = checkpoint["completed"]
    artifact_root = archive / "artifacts"
    parquet_root = warehouse / "parquet"
    expected_counts = {
        api_name: sum(int(item["rows"]) for item in completed[api_name].values())
        for api_name in SCHEMAS
    }
    written_counts: dict[str, int] = {}
    for api_name in SCHEMAS:
        groups: defaultdict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for partition_date, entry in completed[api_name].items():
            groups[(partition_date[:4], partition_date[4:6])].append((partition_date, entry))
        total = 0
        for index, ((year, month), partitions) in enumerate(sorted(groups.items()), start=1):
            target = parquet_root / api_name / f"year={year}" / f"month={month}" / "data.parquet"
            total += _write_month(api_name, sorted(partitions), artifact_root, target)
            print(f"{api_name} {year}-{month}: {index}/{len(groups)} rows={total}", flush=True)
        if total != expected_counts[api_name]:
            raise ValueError(f"warehouse row count differs from checkpoint: {api_name}")
        written_counts[api_name] = total
    database = warehouse / "alpha_research.duckdb"
    checkpoint_hash = _sha256(checkpoint_bytes)
    _build_catalog(database, parquet_root, checkpoint_hash, written_counts)
    return {
        "built_at": datetime.now().astimezone().isoformat(),
        "checkpoint_hash": checkpoint_hash,
        "database": str(database.resolve()),
        "parquet_root": str(parquet_root.resolve()),
        "row_counts": written_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=Path("data/tushare_archive"))
    parser.add_argument("--warehouse", type=Path, default=Path("data/warehouse"))
    args = parser.parse_args()
    summary = build(args.archive.resolve(), args.warehouse.resolve())
    summary_path = args.warehouse / "build_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
