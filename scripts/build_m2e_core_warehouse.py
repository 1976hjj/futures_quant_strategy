"""Publish the completed priority subset of M2-E into the research warehouse."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMAS = {
    "stk_limit": pa.schema(
        [
            ("trade_date", pa.date32()),
            ("ts_code", pa.string()),
            ("up_limit", pa.float64()),
            ("down_limit", pa.float64()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
    "index_classify": pa.schema(
        [
            ("index_code", pa.string()),
            ("industry_name", pa.string()),
            ("level", pa.string()),
            ("industry_code", pa.string()),
            ("is_pub", pa.string()),
            ("parent_code", pa.string()),
            ("src", pa.string()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
    "index_member_all": pa.schema(
        [
            ("l1_code", pa.string()),
            ("l1_name", pa.string()),
            ("l2_code", pa.string()),
            ("l2_name", pa.string()),
            ("l3_code", pa.string()),
            ("l3_name", pa.string()),
            ("ts_code", pa.string()),
            ("name", pa.string()),
            ("in_date", pa.date32()),
            ("out_date", pa.date32()),
            ("is_new", pa.string()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
}

PAGE_SIZES = {"stk_limit": 5800, "index_classify": 2000, "index_member_all": 2000}


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _artifact_path(root: Path, artifact_id: str) -> Path:
    algorithm, separator, digest = artifact_id.partition(":")
    if algorithm != "sha256" or separator != ":" or len(digest) != 64:
        raise ValueError(f"invalid artifact id: {artifact_id}")
    return root / "objects" / "sha256" / digest[:2] / digest


def _read_verified(path: Path, expected_hash: str) -> bytes:
    payload = path.read_bytes()
    if _sha256(payload) != expected_hash:
        raise ValueError(f"artifact hash mismatch: {path}")
    return payload


def _read_entry(root: Path, entry: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    snapshot_id = str(entry["snapshot_id"])
    snapshot = json.loads(_read_verified(_artifact_path(root, snapshot_id), snapshot_id))["payload"]
    payload_id = str(snapshot["payload_artifact_id"])
    if payload_id != entry["payload_artifact_id"]:
        raise ValueError("checkpoint and snapshot payload lineage differ")
    stored = _read_verified(_artifact_path(root, payload_id), payload_id)
    payload = gzip.decompress(stored) if snapshot["payload_encoding"] == "gzip" else stored
    if _sha256(payload) != snapshot["uncompressed_payload_hash"]:
        raise ValueError("uncompressed payload hash mismatch")
    data = json.loads(payload).get("data", {})
    fields, items = data.get("fields"), data.get("items")
    if fields in (None, []) and items in (None, []):
        return [], snapshot_id, payload_id
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError("invalid tabular payload")
    if len(items) != int(entry["rows"]):
        raise ValueError("checkpoint row count differs from payload")
    return [dict(zip(fields, item, strict=True)) for item in items], snapshot_id, payload_id


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return datetime.strptime(str(value), "%Y%m%d").date()


def _as_text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _as_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _normalize(api: str, raw: dict[str, Any], snapshot_id: str, payload_id: str) -> dict[str, Any]:
    if api == "stk_limit":
        row = {
            "trade_date": _as_date(raw.get("trade_date")),
            "ts_code": _as_text(raw.get("ts_code")),
            "up_limit": _as_float(raw.get("up_limit")),
            "down_limit": _as_float(raw.get("down_limit")),
        }
    elif api == "index_classify":
        row = {name: _as_text(raw.get(name)) for name in SCHEMAS[api].names[:-2]}
    elif api == "index_member_all":
        row = {name: _as_text(raw.get(name)) for name in SCHEMAS[api].names[:-4]}
        row.update(
            {
                "in_date": _as_date(raw.get("in_date")),
                "out_date": _as_date(raw.get("out_date")),
                "is_new": _as_text(raw.get("is_new")),
            }
        )
    else:
        raise ValueError(f"unsupported priority API: {api}")
    return {
        **row,
        "source_snapshot_id": snapshot_id,
        "source_payload_artifact_id": payload_id,
    }


def _terminal_partition_exists(entries: dict[str, Any], key: str, page_size: int) -> bool:
    pages = [entry for partition, entry in entries.items() if partition.startswith(f"{key}:offset=")]
    return bool(pages) and any(int(page["rows"]) < int(page.get("page_size", page_size)) for page in pages)


def _assert_priority_complete(checkpoint: dict[str, Any], reference: Path) -> None:
    completed = checkpoint.get("completed", {})
    required = set(SCHEMAS)
    if not required.issubset(completed):
        raise ValueError("M2-E checkpoint lacks priority APIs")
    reference_checkpoint = json.loads((reference / "checkpoint.json").read_bytes())
    coverage = checkpoint["coverage"]
    sessions = {
        value
        for value in reference_checkpoint["open_sessions"]
        if coverage["start"].replace("-", "") <= value <= coverage["end"].replace("-", "")
    }
    missing = {
        value
        for value in sessions
        if not _terminal_partition_exists(completed["stk_limit"], value, PAGE_SIZES["stk_limit"])
    }
    if missing:
        raise ValueError(f"stk_limit is incomplete for {len(missing)} trading sessions")
    for level in ("L1", "L2", "L3"):
        if not _terminal_partition_exists(
            completed["index_classify"], f"SW2021:{level}", PAGE_SIZES["index_classify"]
        ):
            raise ValueError(f"index_classify is incomplete: {level}")
    for membership_scope in ("SW2021:current", "SW2021:all-history"):
        if not _terminal_partition_exists(
            completed["index_member_all"], membership_scope, PAGE_SIZES["index_member_all"]
        ):
            raise ValueError(f"index_member_all is incomplete: {membership_scope}")


def _projection(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "m2e-priority-core-projection-v1",
        "source_schema": checkpoint["schema"],
        "api_base_url": checkpoint["api_base_url"],
        "coverage": checkpoint["coverage"],
        "completed": {api: checkpoint["completed"][api] for api in sorted(SCHEMAS)},
    }


def _write_api(archive: Path, api: str, entries: dict[str, Any], target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    writer = pq.ParquetWriter(temporary, SCHEMAS[api], compression="zstd", compression_level=6)
    count = 0
    try:
        for partition in sorted(entries):
            rows, snapshot_id, payload_id = _read_entry(archive / "artifacts", entries[partition])
            normalized = [_normalize(api, row, snapshot_id, payload_id) for row in rows]
            if normalized:
                table = pa.Table.from_pylist(normalized, schema=SCHEMAS[api])
                writer.write_table(table)
                count += table.num_rows
    finally:
        writer.close()
    os.replace(temporary, target)
    return count


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _build_catalog(database: Path, paths: dict[str, Path], counts: dict[str, int], core_hash: str) -> None:
    connection = duckdb.connect(str(database))
    try:
        for schema in ("raw", "research", "metadata"):
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for api, path in paths.items():
            connection.execute(
                f"CREATE OR REPLACE VIEW raw.m2e_{api} AS SELECT * FROM read_parquet('{_sql_path(path)}')"
            )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.price_limits AS
            SELECT * FROM raw.m2e_stk_limit
            QUALIFY row_number() OVER (
                PARTITION BY trade_date, ts_code ORDER BY source_snapshot_id
            ) = 1
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.sw_industry_classification AS
            SELECT * FROM raw.m2e_index_classify
            QUALIFY row_number() OVER (
                PARTITION BY index_code ORDER BY source_snapshot_id
            ) = 1
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.sw_industry_membership AS
            SELECT * FROM raw.m2e_index_member_all
            QUALIFY row_number() OVER (
                PARTITION BY ts_code, l1_code, l2_code, l3_code, in_date, out_date
                ORDER BY source_snapshot_id
            ) = 1
            """
        )
        connection.execute("DROP TABLE IF EXISTS metadata.m2e_core_archive_manifest")
        connection.execute(
            """
            CREATE TABLE metadata.m2e_core_archive_manifest (
                dataset VARCHAR PRIMARY KEY,
                row_count BIGINT NOT NULL,
                core_checkpoint_hash VARCHAR NOT NULL,
                built_at TIMESTAMPTZ NOT NULL,
                pit_grade VARCHAR NOT NULL
            )
            """
        )
        built_at = datetime.now().astimezone()
        connection.executemany(
            "INSERT INTO metadata.m2e_core_archive_manifest VALUES (?, ?, ?, ?, ?)",
            [(api, counts[api], core_hash, built_at, "HISTORICAL_OR_DAILY_PIT") for api in sorted(counts)],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def build(archive: Path, reference: Path, warehouse: Path) -> dict[str, Any]:
    checkpoint = json.loads((archive / "checkpoint.json").read_bytes())
    if checkpoint.get("schema") != "tushare-m2e-backfill-v1":
        raise ValueError("unsupported M2-E checkpoint")
    _assert_priority_complete(checkpoint, reference)
    projection = _projection(checkpoint)
    core_hash = _sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode())
    paths: dict[str, Path] = {}
    counts: dict[str, int] = {}
    for api in sorted(SCHEMAS):
        target = warehouse / "parquet" / "m2e_core" / api / "data.parquet"
        paths[api] = target
        counts[api] = _write_api(archive, api, checkpoint["completed"][api], target)
        expected = sum(int(entry["rows"]) for entry in checkpoint["completed"][api].values())
        if counts[api] != expected:
            raise ValueError(f"published row count differs for {api}")
        print(f"published {api}: {counts[api]} rows", flush=True)
    _build_catalog(warehouse / "alpha_research.duckdb", paths, counts, core_hash)
    return {"status": "PASS", "core_checkpoint_hash": core_hash, "counts": counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/tushare_m2e_archive"))
    parser.add_argument("--reference", type=Path, default=Path("data/tushare_reference_archive"))
    parser.add_argument("--warehouse", type=Path, default=Path("data/warehouse"))
    args = parser.parse_args()
    print(json.dumps(build(args.archive, args.reference, args.warehouse), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
