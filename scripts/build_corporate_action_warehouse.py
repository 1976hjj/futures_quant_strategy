"""Publish M2-C corporate actions and build adjustment-factor reconciliation tables."""

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

SCHEMA = pa.schema(
    [
        ("ts_code", pa.string()),
        ("end_date", pa.date32()),
        ("ann_date", pa.date32()),
        ("div_proc", pa.string()),
        ("stk_div", pa.float64()),
        ("stk_bo_rate", pa.float64()),
        ("stk_co_rate", pa.float64()),
        ("cash_div", pa.float64()),
        ("cash_div_tax", pa.float64()),
        ("record_date", pa.date32()),
        ("ex_date", pa.date32()),
        ("pay_date", pa.date32()),
        ("div_listdate", pa.date32()),
        ("imp_ann_date", pa.date32()),
        ("base_date", pa.date32()),
        ("base_share", pa.float64()),
        ("source_row_number", pa.int64()),
        ("source_snapshot_id", pa.string()),
        ("source_payload_artifact_id", pa.string()),
    ]
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


def _read_entry(root: Path, ts_code: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot_id = str(entry["snapshot_id"])
    snapshot_doc = json.loads(_verified(_artifact_path(root, snapshot_id), snapshot_id))
    snapshot = snapshot_doc["payload"]
    payload_id = str(snapshot["payload_artifact_id"])
    if payload_id != entry["payload_artifact_id"]:
        raise ValueError(f"checkpoint lineage mismatch: {ts_code}")
    stored = _verified(_artifact_path(root, payload_id), payload_id)
    encoding = snapshot["payload_encoding"]
    payload = gzip.decompress(stored) if encoding == "gzip" else stored
    if encoding not in {"gzip", "identity"}:
        raise ValueError(f"unsupported payload encoding: {encoding}")
    if len(payload) != snapshot["uncompressed_byte_size"] or _sha256(payload) != snapshot["uncompressed_payload_hash"]:
        raise ValueError(f"uncompressed payload mismatch: {ts_code}")
    data = json.loads(payload).get("data", {})
    fields, items = data.get("fields"), data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list) or len(items) != entry["rows"]:
        raise ValueError(f"invalid tabular payload or row count: {ts_code}")
    rows = [dict(zip(fields, item, strict=True)) for item in items]
    if any(str(row.get("ts_code")) != ts_code for row in rows):
        raise ValueError(f"partition contains another security: {ts_code}")
    return rows


def _date(value: Any) -> date | None:
    return None if value in (None, "") else datetime.strptime(str(value), "%Y%m%d").date()


def _float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _normalize(raw: dict[str, Any], row_number: int, entry: dict[str, Any]) -> dict[str, Any]:
    date_fields = {
        "end_date",
        "ann_date",
        "record_date",
        "ex_date",
        "pay_date",
        "div_listdate",
        "imp_ann_date",
        "base_date",
    }
    float_fields = {
        "stk_div",
        "stk_bo_rate",
        "stk_co_rate",
        "cash_div",
        "cash_div_tax",
        "base_share",
    }
    row: dict[str, Any] = {}
    for field in SCHEMA:
        name = field.name
        if name == "source_row_number":
            row[name] = row_number
        elif name == "source_snapshot_id":
            row[name] = entry["snapshot_id"]
        elif name == "source_payload_artifact_id":
            row[name] = entry["payload_artifact_id"]
        elif name in date_fields:
            row[name] = _date(raw.get(name))
        elif name in float_fields:
            row[name] = _float(raw.get(name))
        else:
            value = raw.get(name)
            row[name] = None if value in (None, "") else str(value)
    return row


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _write(rows: list[dict[str, Any]], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    pq.write_table(table, temporary, compression="zstd", compression_level=6, use_dictionary=True)
    os.replace(temporary, target)


def _build_catalog(database: Path, parquet: Path, checkpoint_hash: str, row_count: int) -> None:
    connection = duckdb.connect(str(database))
    try:
        for schema in ("raw", "research", "metadata"):
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        path = _sql_path(parquet)
        connection.execute(f"CREATE OR REPLACE VIEW raw.corporate_actions AS SELECT * FROM read_parquet('{path}')")
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.corporate_action_announcements AS
            WITH ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY ts_code, end_date, ann_date, div_proc, stk_div, stk_bo_rate,
                        stk_co_rate, cash_div, cash_div_tax, record_date, ex_date, pay_date,
                        div_listdate, imp_ann_date, base_date, base_share
                    ORDER BY source_snapshot_id, source_row_number
                ) AS exact_duplicate_rank
                FROM raw.corporate_actions
            )
            SELECT
                * EXCLUDE(exact_duplicate_rank),
                CASE
                    WHEN div_proc = '实施' AND imp_ann_date IS NOT NULL THEN imp_ann_date
                    ELSE ann_date
                END AS available_date,
                div_proc = '实施' AS is_implemented
            FROM ranked
            WHERE exact_duplicate_rank = 1
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.corporate_action_event_candidates AS
            WITH implemented AS (
                SELECT *, row_number() OVER (
                    PARTITION BY ts_code, ex_date,
                        coalesce(stk_div, 0), coalesce(cash_div_tax, cash_div, 0)
                    ORDER BY imp_ann_date DESC NULLS LAST, ann_date DESC NULLS LAST,
                        source_row_number DESC
                ) AS revision_rank
                FROM research.corporate_action_announcements
                WHERE is_implemented AND ex_date IS NOT NULL
            )
            SELECT * EXCLUDE(revision_rank)
            FROM implemented WHERE revision_rank = 1
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.corporate_action_events AS
            WITH ranked AS (
                SELECT
                    *,
                    count(*) OVER (PARTITION BY ts_code, ex_date) AS economic_candidate_count,
                    row_number() OVER (
                        PARTITION BY ts_code, ex_date
                        ORDER BY imp_ann_date DESC NULLS LAST, ann_date DESC NULLS LAST,
                            end_date DESC NULLS LAST, source_row_number DESC
                    ) AS event_rank
                FROM research.corporate_action_event_candidates
            )
            SELECT * EXCLUDE(event_rank) FROM ranked WHERE event_rank = 1
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.corporate_action_ex_dates AS
            SELECT
                ts_code,
                ex_date,
                coalesce(stk_div, 0) AS stock_dividend_ratio,
                coalesce(cash_div_tax, cash_div, 0) AS cash_dividend_per_share,
                available_date AS first_available_date,
                available_date AS last_available_date,
                economic_candidate_count AS event_count
            FROM research.corporate_action_events
            """
        )
        connection.execute("DROP TABLE IF EXISTS research.adjustment_factor_jumps")
        connection.execute(
            """
            CREATE TABLE research.adjustment_factor_jumps AS
            WITH factors AS (
                SELECT
                    ts_code,
                    trade_date,
                    adj_factor,
                    lag(trade_date) OVER (PARTITION BY ts_code ORDER BY trade_date) AS previous_trade_date,
                    lag(adj_factor) OVER (PARTITION BY ts_code ORDER BY trade_date) AS previous_adj_factor
                FROM research.adj_factor
            )
            SELECT
                f.ts_code,
                f.trade_date,
                f.previous_trade_date,
                f.adj_factor,
                f.previous_adj_factor,
                f.adj_factor / f.previous_adj_factor AS factor_ratio,
                previous.close AS previous_close,
                current.pre_close AS current_pre_close,
                previous.close / nullif(current.pre_close, 0) AS market_reference_ratio,
                abs(
                    f.adj_factor / f.previous_adj_factor
                    - previous.close / nullif(current.pre_close, 0)
                ) AS factor_market_ratio_abs_error
            FROM factors f
            LEFT JOIN raw.daily previous
              ON previous.ts_code = f.ts_code AND previous.trade_date = f.previous_trade_date
            LEFT JOIN raw.daily current
              ON current.ts_code = f.ts_code AND current.trade_date = f.trade_date
            WHERE f.previous_adj_factor IS NOT NULL
              AND f.previous_adj_factor > 0
              AND abs(f.adj_factor / f.previous_adj_factor - 1) > 1e-10
            """
        )
        connection.execute("DROP TABLE IF EXISTS research.corporate_action_reconciliation")
        connection.execute(
            """
            CREATE TABLE research.corporate_action_reconciliation AS
            SELECT
                coalesce(e.ts_code, j.ts_code) AS ts_code,
                coalesce(e.ex_date, j.trade_date) AS effective_date,
                e.event_count,
                e.stock_dividend_ratio,
                e.cash_dividend_per_share,
                e.first_available_date,
                e.last_available_date,
                j.previous_trade_date,
                j.previous_close,
                j.current_pre_close,
                j.previous_adj_factor,
                j.adj_factor,
                j.factor_ratio,
                j.market_reference_ratio,
                j.factor_market_ratio_abs_error,
                (j.previous_close - e.cash_dividend_per_share)
                    / nullif(1 + e.stock_dividend_ratio, 0) AS event_theoretical_reference_price,
                abs(
                    j.current_pre_close
                    - (j.previous_close - e.cash_dividend_per_share)
                        / nullif(1 + e.stock_dividend_ratio, 0)
                ) AS event_reference_price_abs_error,
                coalesce(j.factor_market_ratio_abs_error <= 0.005, false)
                    AS factor_market_reconciled,
                coalesce(
                    abs(
                        j.current_pre_close
                        - (j.previous_close - e.cash_dividend_per_share)
                            / nullif(1 + e.stock_dividend_ratio, 0)
                    ) <= greatest(0.05, abs(j.current_pre_close) * 0.005),
                    false
                ) AS event_reference_reconciled,
                coalesce(
                    e.ts_code IS NOT NULL
                    AND j.ts_code IS NOT NULL
                    AND e.first_available_date <= coalesce(j.previous_trade_date, j.trade_date)
                    AND j.factor_market_ratio_abs_error <= 0.005
                    AND abs(
                        j.current_pre_close
                        - (j.previous_close - e.cash_dividend_per_share)
                            / nullif(1 + e.stock_dividend_ratio, 0)
                    ) <= greatest(0.05, abs(j.current_pre_close) * 0.005),
                    false
                ) AS approved_for_dividend_adjustment,
                CASE
                    WHEN e.ts_code IS NOT NULL AND j.ts_code IS NOT NULL THEN 'MATCHED_DATE'
                    WHEN e.ts_code IS NOT NULL THEN 'EVENT_WITHOUT_FACTOR_JUMP'
                    ELSE 'FACTOR_JUMP_WITHOUT_DIVIDEND_EVENT'
                END AS match_status,
                CASE
                    WHEN e.ts_code IS NOT NULL AND j.ts_code IS NOT NULL
                        THEN 'DIVIDEND_EVENT_MATCHED_FACTOR_DATE'
                    WHEN e.ts_code IS NOT NULL THEN 'EVENT_WITHOUT_FACTOR_JUMP'
                    WHEN j.market_reference_ratio IS NULL THEN 'FACTOR_JUMP_NO_MARKET_CONTEXT'
                    WHEN abs(j.market_reference_ratio - 1) <= 1e-5
                         AND abs(j.factor_ratio - 1) <= 0.001 THEN 'TECHNICAL_FACTOR_DRIFT'
                    WHEN j.factor_market_ratio_abs_error <= 0.005
                         AND abs(j.market_reference_ratio - 1) > 0.001
                        THEN 'UNEXPLAINED_PRICE_ADJUSTMENT'
                    WHEN j.factor_market_ratio_abs_error > 0.005 THEN 'FACTOR_MARKET_MISMATCH'
                    ELSE 'UNEXPLAINED_FACTOR_CHANGE'
                END AS diagnostic_status
            FROM research.corporate_action_ex_dates e
            FULL OUTER JOIN research.adjustment_factor_jumps j
              ON j.ts_code = e.ts_code AND j.trade_date = e.ex_date
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.corporate_action_reconciliation_approved AS
            SELECT * FROM research.corporate_action_reconciliation
            WHERE approved_for_dividend_adjustment
            """
        )
        connection.execute(
            """
            CREATE OR REPLACE VIEW research.corporate_action_reconciliation_exceptions AS
            SELECT * FROM research.corporate_action_reconciliation
            WHERE NOT approved_for_dividend_adjustment
            """
        )
        connection.execute("DROP TABLE IF EXISTS metadata.m2c_archive_manifest")
        connection.execute(
            """
            CREATE TABLE metadata.m2c_archive_manifest (
                dataset VARCHAR PRIMARY KEY,
                row_count BIGINT NOT NULL,
                checkpoint_hash VARCHAR NOT NULL,
                built_at TIMESTAMPTZ NOT NULL,
                pit_grade VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO metadata.m2c_archive_manifest VALUES (?, ?, ?, ?, ?)",
            ["corporate_actions", row_count, checkpoint_hash, datetime.now().astimezone(), "RECONSTRUCTED_PIT"],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def build(archive: Path, warehouse: Path) -> dict[str, Any]:
    checkpoint_path = archive / "checkpoint.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes)
    if checkpoint.get("schema") != "tushare-corporate-action-backfill-v1":
        raise ValueError("unsupported M2-C checkpoint")
    entries = checkpoint.get("completed", {}).get("dividend", {})
    if not entries:
        raise ValueError("M2-C archive has no completed dividend partitions")
    rows: list[dict[str, Any]] = []
    root = archive / "artifacts"
    for ts_code, entry in sorted(entries.items()):
        raw_rows = _read_entry(root, ts_code, entry)
        rows.extend(_normalize(row, index, entry) for index, row in enumerate(raw_rows))
    target = warehouse / "parquet" / "corporate_actions" / "dividend" / "data.parquet"
    _write(rows, target)
    checkpoint_hash = _sha256(checkpoint_bytes)
    _build_catalog(warehouse / "alpha_research.duckdb", target, checkpoint_hash, len(rows))
    summary = {
        "built_at": datetime.now().astimezone().isoformat(),
        "checkpoint_hash": checkpoint_hash,
        "database": str((warehouse / "alpha_research.duckdb").resolve()),
        "partitions": len(entries),
        "rows": len(rows),
    }
    output = warehouse / "corporate_action_build_summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/tushare_corporate_action_archive"))
    parser.add_argument("--warehouse", type=Path, default=Path("data/warehouse"))
    args = parser.parse_args()
    print(json.dumps(build(args.archive, args.warehouse), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
