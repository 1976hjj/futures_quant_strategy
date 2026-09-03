"""Publish the completed M2-D financial archive into DuckDB and Parquet."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

RAW_NAMES = {
    "income_vip": "income_statement_versions",
    "balancesheet_vip": "balance_sheet_versions",
    "cashflow_vip": "cashflow_statement_versions",
    "fina_indicator_vip": "financial_indicator_versions",
}
PROVENANCE = (
    "source_api",
    "source_partition",
    "source_row_number",
    "source_snapshot_id",
    "source_payload_artifact_id",
    "source_retrieved_at",
    "row_payload_hash",
)


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _artifact_path(root: Path, artifact_id: str) -> Path:
    algorithm, separator, digest = artifact_id.partition(":")
    if algorithm != "sha256" or separator != ":" or len(digest) != 64:
        raise ValueError(f"invalid artifact id: {artifact_id}")
    return root / "objects" / "sha256" / digest[:2] / digest


def _verified(path: Path, expected_hash: str) -> bytes:
    payload = path.read_bytes()
    if _sha256(payload) != expected_hash:
        raise ValueError(f"artifact hash mismatch: {path}")
    return payload


def _read_entry(root: Path, api: str, partition: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot_id = str(entry["snapshot_id"])
    snapshot_doc = json.loads(_verified(_artifact_path(root, snapshot_id), snapshot_id))
    snapshot = snapshot_doc["payload"]
    payload_id = str(snapshot["payload_artifact_id"])
    if payload_id != entry["payload_artifact_id"]:
        raise ValueError(f"checkpoint lineage mismatch: {api}/{partition}")
    stored = _verified(_artifact_path(root, payload_id), payload_id)
    encoding = snapshot["payload_encoding"]
    payload = gzip.decompress(stored) if encoding == "gzip" else stored
    if encoding not in {"gzip", "identity"}:
        raise ValueError(f"unsupported payload encoding: {encoding}")
    if len(payload) != snapshot["uncompressed_byte_size"] or _sha256(payload) != snapshot["uncompressed_payload_hash"]:
        raise ValueError(f"uncompressed payload mismatch: {api}/{partition}")
    data = json.loads(payload).get("data", {})
    fields, items = data.get("fields"), data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list) or len(items) != int(entry["rows"]):
        raise ValueError(f"invalid tabular payload or row count: {api}/{partition}")
    retrieved_at = str(entry["retrieved_at"])
    rows = []
    for index, values in enumerate(items):
        row = dict(zip(fields, values, strict=True))
        row.update(
            source_api=api,
            source_partition=partition,
            source_row_number=index,
            source_snapshot_id=snapshot_id,
            source_payload_artifact_id=payload_id,
            source_retrieved_at=retrieved_at,
            row_payload_hash=_sha256(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ),
        )
        rows.append(row)
    return rows


def _string(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _write_api(archive: Path, api: str, entries: dict[str, Any], target: Path) -> int:
    fields = sorted({str(field) for entry in entries.values() for field in entry.get("fields", [])})
    schema = pa.schema(
        [(field, pa.string()) for field in fields]
        + [
            ("source_api", pa.string()),
            ("source_partition", pa.string()),
            ("source_row_number", pa.int64()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
            ("source_retrieved_at", pa.string()),
            ("row_payload_hash", pa.string()),
        ]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    writer = pq.ParquetWriter(temporary, schema, compression="zstd", compression_level=6, use_dictionary=True)
    total = 0
    try:
        for partition, entry in sorted(entries.items()):
            raw_rows = _read_entry(archive / "artifacts", api, partition, entry)
            normalized = []
            for row in raw_rows:
                normalized.append(
                    {
                        name: row[name] if name == "source_row_number" else _string(row.get(name))
                        for name in schema.names
                    }
                )
            if normalized:
                writer.write_table(pa.Table.from_pylist(normalized, schema=schema))
            total += len(normalized)
    finally:
        writer.close()
    os.replace(temporary, target)
    return total


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _has(columns: set[str], name: str, cast: str = "VARCHAR") -> str:
    if name not in columns:
        return f"NULL::{cast}"
    if cast == "DATE":
        return f"try_strptime({name}, '%Y%m%d')::DATE"
    return f"try_cast({name} AS {cast})"


def _build_catalog(database: Path, paths: dict[str, Path], counts: dict[str, int], checkpoint_hash: str) -> None:
    connection = duckdb.connect(str(database))
    try:
        for schema in ("raw", "research", "metadata"):
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        union_parts = []
        for api, raw_name in RAW_NAMES.items():
            path = _sql_path(paths[api])
            connection.execute(f"CREATE OR REPLACE VIEW raw.{raw_name} AS SELECT * FROM read_parquet('{path}')")
            columns = {row[0] for row in connection.execute(f"DESCRIBE raw.{raw_name}").fetchall()}
            report_type = "report_type" if "report_type" in columns else "comp_type" if "comp_type" in columns else None
            union_parts.append(
                f"""SELECT source_api, ts_code,
                {_has(columns, "ann_date", "DATE")} AS ann_date,
                {_has(columns, "f_ann_date", "DATE")} AS f_ann_date,
                coalesce({_has(columns, "f_ann_date", "DATE")}, {_has(columns, "ann_date", "DATE")}) AS available_date,
                {_has(columns, "end_date", "DATE")} AS end_date,
                {report_type if report_type else "NULL::VARCHAR"} AS report_type,
                {_has(columns, "update_flag")} AS update_flag,
                {_has(columns, "revenue", "DOUBLE")} AS revenue,
                {_has(columns, "total_revenue", "DOUBLE")} AS total_revenue,
                {_has(columns, "operate_profit", "DOUBLE")} AS operate_profit,
                {_has(columns, "total_profit", "DOUBLE")} AS total_profit,
                {_has(columns, "n_income_attr_p", "DOUBLE")} AS net_income_parent,
                {_has(columns, "total_assets", "DOUBLE")} AS total_assets,
                {_has(columns, "total_liab", "DOUBLE")} AS total_liabilities,
                {_has(columns, "total_hldr_eqy_exc_min_int", "DOUBLE")} AS equity_parent,
                {_has(columns, "n_cashflow_act", "DOUBLE")} AS operating_cashflow,
                {_has(columns, "basic_eps", "DOUBLE")} AS basic_eps,
                {_has(columns, "roe", "DOUBLE")} AS roe,
                {_has(columns, "roa", "DOUBLE")} AS roa,
                {_has(columns, "grossprofit_margin", "DOUBLE")} AS grossprofit_margin,
                {_has(columns, "debt_to_assets", "DOUBLE")} AS debt_to_assets,
                row_payload_hash, source_partition, source_row_number, source_snapshot_id,
                source_payload_artifact_id, try_cast(source_retrieved_at AS TIMESTAMPTZ) AS source_retrieved_at
                FROM raw.{raw_name}"""
            )
        connection.execute(
            "CREATE OR REPLACE VIEW research.financial_versions_canonical AS " + " UNION ALL ".join(union_parts)
        )
        connection.execute(
            """CREATE OR REPLACE VIEW research.financial_pit_exceptions AS
            SELECT *, CASE
              WHEN ts_code IS NULL THEN 'MISSING_TS_CODE'
              WHEN end_date IS NULL THEN 'MISSING_END_DATE'
              WHEN available_date IS NULL THEN 'MISSING_AVAILABLE_DATE'
              WHEN available_date < end_date THEN 'AVAILABLE_BEFORE_PERIOD_END'
            END AS exception_reason
            FROM research.financial_versions_canonical
            WHERE ts_code IS NULL OR end_date IS NULL OR available_date IS NULL OR available_date < end_date"""
        )
        connection.execute(
            """CREATE OR REPLACE VIEW research.financial_revision_events AS
            WITH distinct_versions AS (
              SELECT *, row_number() OVER (
                PARTITION BY source_api, ts_code, end_date, coalesce(report_type, ''), available_date, row_payload_hash
                ORDER BY source_retrieved_at, source_partition, source_row_number
              ) AS duplicate_rank
              FROM research.financial_versions_canonical
              WHERE ts_code IS NOT NULL AND end_date IS NOT NULL AND available_date IS NOT NULL
            ), sequenced AS (
              SELECT *, row_number() OVER (
                PARTITION BY source_api, ts_code, end_date, coalesce(report_type, '')
                ORDER BY available_date, source_retrieved_at, source_partition, source_row_number
              ) AS revision_number,
              lead(available_date) OVER (
                PARTITION BY source_api, ts_code, end_date, coalesce(report_type, '')
                ORDER BY available_date, source_retrieved_at, source_partition, source_row_number
              ) AS next_available_date
              FROM distinct_versions WHERE duplicate_rank = 1
            ) SELECT * EXCLUDE(duplicate_rank) FROM sequenced"""
        )
        connection.execute(
            """CREATE OR REPLACE VIEW research.financial_pit_asof AS
            SELECT *, available_date AS valid_from, next_available_date AS valid_to
            FROM research.financial_revision_events"""
        )
        connection.execute("DROP TABLE IF EXISTS metadata.m2d_archive_manifest")
        connection.execute("""CREATE TABLE metadata.m2d_archive_manifest (
            dataset VARCHAR PRIMARY KEY, row_count BIGINT NOT NULL, checkpoint_hash VARCHAR NOT NULL,
            built_at TIMESTAMPTZ NOT NULL, pit_grade VARCHAR NOT NULL)""")
        built_at = datetime.now().astimezone()
        connection.executemany(
            "INSERT INTO metadata.m2d_archive_manifest VALUES (?, ?, ?, ?, ?)",
            [(RAW_NAMES[api], counts[api], checkpoint_hash, built_at, "ANNOUNCEMENT_DATE_PIT") for api in RAW_NAMES],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def build(archive: Path, warehouse: Path) -> dict[str, Any]:
    checkpoint_bytes = (archive / "checkpoint.json").read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    if checkpoint.get("schema") != "tushare-financial-backfill-v1":
        raise ValueError("unsupported M2-D checkpoint")
    if set(checkpoint.get("completed", {})) != set(RAW_NAMES):
        raise ValueError("M2-D archive does not contain all required APIs")
    periods = set(checkpoint["periods"])
    for api in RAW_NAMES:
        if set(checkpoint["terminal_offsets"].get(api, {})) != periods:
            raise ValueError(f"M2-D pagination is incomplete: {api}")
    paths, counts = {}, {}
    for api in RAW_NAMES:
        target = warehouse / "parquet" / "financial" / api / "data.parquet"
        paths[api] = target
        counts[api] = _write_api(archive, api, checkpoint["completed"][api], target)
        expected = sum(int(entry["rows"]) for entry in checkpoint["completed"][api].values())
        if counts[api] != expected:
            raise ValueError(f"M2-D published row count differs: {api}")
        print(f"published {api}: {counts[api]} rows", flush=True)
    checkpoint_hash = _sha256(checkpoint_bytes)
    _build_catalog(warehouse / "alpha_research.duckdb", paths, counts, checkpoint_hash)
    summary = {
        "built_at": datetime.now().astimezone().isoformat(),
        "checkpoint_hash": checkpoint_hash,
        "counts": counts,
        "total_rows": sum(counts.values()),
        "database": str((warehouse / "alpha_research.duckdb").resolve()),
    }
    (warehouse / "financial_build_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/tushare_financial_archive"))
    parser.add_argument("--warehouse", type=Path, default=Path("data/warehouse"))
    args = parser.parse_args()
    print(json.dumps(build(args.archive, args.warehouse), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
