"""Publish the M2-B reference archive into the DuckDB/Parquet warehouse."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import uuid
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMAS = {
    "trading_calendar": pa.schema(
        [
            ("exchange", pa.string()),
            ("cal_date", pa.date32()),
            ("is_open", pa.bool_()),
            ("pretrade_date", pa.date32()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
    "security_master": pa.schema(
        [
            ("ts_code", pa.string()),
            ("symbol", pa.string()),
            ("name", pa.string()),
            ("area", pa.string()),
            ("industry", pa.string()),
            ("fullname", pa.string()),
            ("enname", pa.string()),
            ("cnspell", pa.string()),
            ("market", pa.string()),
            ("exchange", pa.string()),
            ("curr_type", pa.string()),
            ("list_status", pa.string()),
            ("list_date", pa.date32()),
            ("delist_date", pa.date32()),
            ("is_hs", pa.string()),
            ("act_name", pa.string()),
            ("act_ent_type", pa.string()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
    "name_history": pa.schema(
        [
            ("ts_code", pa.string()),
            ("name", pa.string()),
            ("start_date", pa.date32()),
            ("end_date", pa.date32()),
            ("ann_date", pa.date32()),
            ("change_reason", pa.string()),
            ("source_row_number", pa.int64()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
    "stock_st": pa.schema(
        [
            ("ts_code", pa.string()),
            ("trade_date", pa.date32()),
            ("name", pa.string()),
            ("type", pa.string()),
            ("type_name", pa.string()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
    "suspensions": pa.schema(
        [
            ("ts_code", pa.string()),
            ("trade_date", pa.date32()),
            ("suspend_type", pa.string()),
            ("suspend_timing", pa.string()),
            ("source_snapshot_id", pa.string()),
            ("source_payload_artifact_id", pa.string()),
        ]
    ),
}

API_TO_DATASET = {
    "trade_cal": "trading_calendar",
    "stock_basic": "security_master",
    "namechange": "name_history",
    "stock_st": "stock_st",
    "suspend_d": "suspensions",
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


def _read_entry(artifact_root: Path, entry: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    snapshot_id = str(entry["snapshot_id"])
    snapshot_document = json.loads(_read_verified(_artifact_path(artifact_root, snapshot_id), snapshot_id))
    snapshot = snapshot_document["payload"]
    payload_artifact_id = str(snapshot["payload_artifact_id"])
    if payload_artifact_id != entry["payload_artifact_id"]:
        raise ValueError("checkpoint and snapshot payload lineage differ")
    stored = _read_verified(_artifact_path(artifact_root, payload_artifact_id), payload_artifact_id)
    if snapshot["payload_encoding"] == "gzip":
        payload = gzip.decompress(stored)
    elif snapshot["payload_encoding"] == "identity":
        payload = stored
    else:
        raise ValueError(f"unsupported payload encoding: {snapshot['payload_encoding']}")
    if len(payload) != snapshot["uncompressed_byte_size"] or _sha256(payload) != snapshot["uncompressed_payload_hash"]:
        raise ValueError("uncompressed payload does not match its snapshot manifest")
    document = json.loads(payload)
    data = document.get("data", {})
    fields, items = data.get("fields"), data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError("invalid tabular payload")
    if len(items) != entry["rows"]:
        raise ValueError("checkpoint row count differs from payload")
    return [dict(zip(fields, item, strict=True)) for item in items], snapshot_id, payload_artifact_id


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return datetime.strptime(str(value), "%Y%m%d").date()


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _lineage(row: dict[str, Any], snapshot_id: str, payload_id: str) -> dict[str, Any]:
    return {
        **row,
        "source_snapshot_id": snapshot_id,
        "source_payload_artifact_id": payload_id,
    }


def _normalize(
    dataset: str,
    raw: dict[str, Any],
    snapshot_id: str,
    payload_id: str,
    source_row_number: int,
) -> dict[str, Any]:
    if dataset == "trading_calendar":
        row = {
            "exchange": str(raw["exchange"]),
            "cal_date": _date(raw["cal_date"]),
            "is_open": str(raw["is_open"]) == "1",
            "pretrade_date": _date(raw.get("pretrade_date")),
        }
    elif dataset == "security_master":
        row = {
            name: _text(raw.get(name))
            for name in (
                "ts_code",
                "symbol",
                "name",
                "area",
                "industry",
                "fullname",
                "enname",
                "cnspell",
                "market",
                "exchange",
                "curr_type",
                "list_status",
                "is_hs",
                "act_name",
                "act_ent_type",
            )
        }
        row.update({"list_date": _date(raw.get("list_date")), "delist_date": _date(raw.get("delist_date"))})
    elif dataset == "name_history":
        row = {
            "ts_code": _text(raw.get("ts_code")),
            "name": _text(raw.get("name")),
            "start_date": _date(raw.get("start_date")),
            "end_date": _date(raw.get("end_date")),
            "ann_date": _date(raw.get("ann_date")),
            "change_reason": _text(raw.get("change_reason")),
            "source_row_number": source_row_number,
        }
    elif dataset == "stock_st":
        row = {
            "ts_code": _text(raw.get("ts_code")),
            "trade_date": _date(raw.get("trade_date")),
            "name": _text(raw.get("name")),
            "type": _text(raw.get("type")),
            "type_name": _text(raw.get("type_name")),
        }
    elif dataset == "suspensions":
        row = {
            "ts_code": _text(raw.get("ts_code")),
            "trade_date": _date(raw.get("trade_date")),
            "suspend_type": _text(raw.get("suspend_type")),
            "suspend_timing": _text(raw.get("suspend_timing")),
        }
    else:
        raise ValueError(f"unknown dataset: {dataset}")
    return _lineage(row, snapshot_id, payload_id)


def _write_table(rows: list[dict[str, Any]], schema: pa.Schema, target: Path) -> int:
    table = pa.Table.from_pylist(rows, schema=schema)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    pq.write_table(table, temporary, compression="zstd", compression_level=6, use_dictionary=True)
    os.replace(temporary, target)
    return table.num_rows


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _assert_complete(checkpoint: dict[str, Any]) -> None:
    completed = checkpoint["completed"]
    sessions = [str(value) for value in checkpoint["open_sessions"]]
    if len(completed["trade_cal"]) != 1:
        raise ValueError("M2-B archive must contain one canonical trading calendar")
    coverage = checkpoint["coverage"]
    expected_name_ranges = {
        f"range:{year:04d}"
        for year in range(int(coverage["start"][:4]), int(coverage["end"][:4]) + 1)
    }
    if len(completed["stock_basic"]) != 5 or not expected_name_ranges.issubset(completed["namechange"]):
        raise ValueError("security master or name history snapshot is incomplete")
    missing_suspend = set(sessions) - set(completed["suspend_d"])
    st_sessions = {session for session in sessions if session >= "20000101"}
    missing_st = st_sessions - set(completed["stock_st"])
    if missing_suspend or missing_st:
        raise ValueError(
            f"daily status archive incomplete: missing suspend={len(missing_suspend)} missing_st={len(missing_st)}"
        )


def _build_catalog(database: Path, parquet_root: Path, checkpoint_hash: str, counts: dict[str, int]) -> None:
    connection = duckdb.connect(str(database))
    try:
        for schema in ("raw", "research", "metadata"):
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for dataset in ("trading_calendar", "security_master", "name_history"):
            path = _sql_path(parquet_root / dataset / "data.parquet")
            connection.execute(f"CREATE OR REPLACE VIEW raw.{dataset} AS SELECT * FROM read_parquet('{path}')")
        for dataset in ("stock_st", "suspensions"):
            pattern = _sql_path(parquet_root / dataset / "year=*" / "month=*" / "data.parquet")
            connection.execute(
                f"CREATE OR REPLACE VIEW raw.{dataset} AS "
                f"SELECT * EXCLUDE(year, month) FROM read_parquet('{pattern}', hive_partitioning=true)"
            )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.trading_calendar AS
            SELECT * FROM raw.trading_calendar WHERE exchange = 'SSE'
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.security_master AS
            SELECT
                *,
                CASE
                    WHEN exchange = 'SSE' AND symbol LIKE '6%' THEN true
                    WHEN exchange = 'SZSE' AND (symbol LIKE '00%' OR symbol LIKE '30%') THEN true
                    WHEN exchange = 'BSE' THEN true
                    ELSE false
                END AS is_a_share
            FROM raw.security_master
            """
        )
        connection.execute(
            r"""
            CREATE OR REPLACE VIEW research.security_name_history AS
            WITH ranked AS (
                SELECT
                    *,
                    row_number() OVER (
                        PARTITION BY ts_code, name, start_date, end_date, ann_date, change_reason
                        ORDER BY source_row_number
                    ) AS duplicate_rank
                FROM raw.name_history
            )
            SELECT
                * EXCLUDE (duplicate_rank),
                regexp_matches(upper(name), '^(S\*ST|\*ST|ST|SST|PT)') AS is_st_name
            FROM ranked
            WHERE duplicate_rank = 1
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.security_session_state AS
            WITH listed AS (
                SELECT
                    c.cal_date AS trade_date,
                    s.ts_code,
                    s.symbol,
                    s.exchange,
                    s.market,
                    s.list_date,
                    s.delist_date,
                    s.name AS current_snapshot_name,
                    row_number() OVER (PARTITION BY s.ts_code ORDER BY c.cal_date) AS listed_session_number
                FROM research.trading_calendar c
                JOIN research.security_master s
                  ON c.is_open
                 AND s.is_a_share
                 AND s.list_date IS NOT NULL
                 AND c.cal_date >= s.list_date
                 AND (s.delist_date IS NULL OR c.cal_date <= s.delist_date)
            ),
            named AS (
                SELECT
                    l.*,
                    n.name AS historical_name,
                    n.is_st_name,
                    row_number() OVER (
                        PARTITION BY l.trade_date, l.ts_code
                        ORDER BY n.start_date DESC NULLS LAST, n.ann_date DESC NULLS LAST
                    ) AS name_rank
                FROM listed l
                LEFT JOIN research.security_name_history n
                  ON n.ts_code = l.ts_code
                 AND n.start_date <= l.trade_date
                 AND (n.ann_date IS NULL OR n.ann_date <= l.trade_date)
                 AND (n.end_date IS NULL OR n.end_date >= l.trade_date)
            ),
            st AS (
                SELECT trade_date, ts_code, true AS explicit_is_st, any_value(name) AS st_name
                FROM raw.stock_st
                GROUP BY trade_date, ts_code
            ),
            suspended AS (
                SELECT
                    trade_date,
                    ts_code,
                    bool_or(suspend_type = 'S') AS is_suspended,
                    string_agg(DISTINCT suspend_type, ',' ORDER BY suspend_type) AS suspend_events
                FROM raw.suspensions
                GROUP BY trade_date, ts_code
            )
            SELECT
                n.trade_date,
                n.ts_code,
                n.symbol,
                n.exchange,
                n.market,
                n.list_date,
                n.delist_date,
                n.listed_session_number,
                coalesce(n.historical_name, st.st_name, n.current_snapshot_name) AS security_name,
                n.historical_name IS NOT NULL AS name_is_point_in_time,
                CASE
                    WHEN st.explicit_is_st THEN true
                    WHEN n.is_st_name IS NOT NULL THEN n.is_st_name
                    WHEN n.trade_date >= DATE '2000-01-01' THEN false
                    ELSE NULL
                END AS is_st,
                CASE
                    WHEN st.explicit_is_st THEN 'STOCK_ST'
                    WHEN n.is_st_name IS NOT NULL THEN 'NAME_HISTORY'
                    WHEN n.trade_date >= DATE '2000-01-01' THEN 'STOCK_ST_ABSENCE'
                    ELSE 'UNKNOWN'
                END AS st_source,
                coalesce(s.is_suspended, false) AS is_suspended,
                s.suspend_events,
                p.ts_code IS NOT NULL AS has_market_bar,
                coalesce(p.is_tradeable_bar, false) AS is_tradeable_bar,
                (
                    coalesce(
                        CASE
                            WHEN st.explicit_is_st THEN true
                            WHEN n.is_st_name IS NOT NULL THEN n.is_st_name
                            WHEN n.trade_date >= DATE '2000-01-01' THEN false
                            ELSE NULL
                        END,
                        true
                    ) = false
                    AND NOT coalesce(s.is_suspended, false)
                    AND coalesce(p.is_tradeable_bar, false)
                ) AS eligible_for_signal
            FROM named n
            LEFT JOIN st USING (trade_date, ts_code)
            LEFT JOIN suspended s USING (trade_date, ts_code)
            LEFT JOIN research.market_daily p USING (trade_date, ts_code)
            WHERE n.name_rank = 1
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.universe_daily AS
            SELECT * FROM research.security_session_state WHERE eligible_for_signal
            """
        )
        connection.execute("DROP TABLE IF EXISTS metadata.m2b_archive_manifest")
        connection.execute(
            """
            CREATE TABLE metadata.m2b_archive_manifest (
                dataset VARCHAR PRIMARY KEY,
                row_count BIGINT NOT NULL,
                checkpoint_hash VARCHAR NOT NULL,
                built_at TIMESTAMPTZ NOT NULL,
                pit_grade VARCHAR NOT NULL
            )
            """
        )
        built_at = datetime.now().astimezone()
        connection.executemany(
            "INSERT INTO metadata.m2b_archive_manifest VALUES (?, ?, ?, ?, ?)",
            [
                (
                    dataset,
                    row_count,
                    checkpoint_hash,
                    built_at,
                    "RECONSTRUCTED_PIT" if dataset != "security_master" else "CURRENT_SNAPSHOT_WITH_EVENT_DATES",
                )
                for dataset, row_count in counts.items()
            ],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def build(archive: Path, warehouse: Path) -> dict[str, Any]:
    checkpoint_path = archive / "checkpoint.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    if checkpoint.get("schema") != "tushare-reference-backfill-v1":
        raise ValueError("unsupported M2-B checkpoint")
    _assert_complete(checkpoint)
    artifact_root = archive / "artifacts"
    parquet_root = warehouse / "parquet" / "reference"
    rows_by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for api_name, entries in checkpoint["completed"].items():
        dataset = API_TO_DATASET[api_name]
        selected_entries = entries.items()
        if api_name == "namechange" and any(key.startswith("range:") for key in entries):
            selected_entries = ((key, entry) for key, entry in entries.items() if key.startswith("range:"))
        for _, entry in sorted(selected_entries):
            rows, snapshot_id, payload_id = _read_entry(artifact_root, entry)
            rows_by_dataset[dataset].extend(
                _normalize(dataset, row, snapshot_id, payload_id, source_row_number)
                for source_row_number, row in enumerate(rows)
            )

    key_fields = {
        "trading_calendar": ("exchange", "cal_date"),
        "security_master": ("ts_code",),
        "stock_st": ("trade_date", "ts_code", "type"),
        "suspensions": ("trade_date", "ts_code", "suspend_type", "suspend_timing"),
    }
    for dataset, fields in key_fields.items():
        rows = rows_by_dataset[dataset]
        keys = {tuple(row[field] for field in fields) for row in rows}
        if len(keys) != len(rows):
            raise ValueError(f"duplicate business key in {dataset}")

    counts = {}
    for dataset in ("trading_calendar", "security_master", "name_history"):
        counts[dataset] = _write_table(
            rows_by_dataset[dataset],
            SCHEMAS[dataset],
            parquet_root / dataset / "data.parquet",
        )
    for dataset in ("stock_st", "suspensions"):
        monthly: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for row in rows_by_dataset[dataset]:
            trade_date = row["trade_date"]
            monthly[(trade_date.year, trade_date.month)].append(row)
        counts[dataset] = 0
        for (year, month), rows in sorted(monthly.items()):
            counts[dataset] += _write_table(
                rows,
                SCHEMAS[dataset],
                parquet_root / dataset / f"year={year:04d}" / f"month={month:02d}" / "data.parquet",
            )
        if not monthly:
            raise ValueError(f"{dataset} contains no business rows")

    database = warehouse / "alpha_research.duckdb"
    _build_catalog(database, parquet_root, _sha256(checkpoint_bytes), counts)
    summary = {
        "built_at": datetime.now().astimezone().isoformat(),
        "checkpoint_hash": _sha256(checkpoint_bytes),
        "database": str(database.resolve()),
        "row_counts": counts,
    }
    (warehouse / "reference_build_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/tushare_reference_archive"))
    parser.add_argument("--warehouse", type=Path, default=Path("data/warehouse"))
    args = parser.parse_args()
    print(json.dumps(build(args.archive, args.warehouse), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
