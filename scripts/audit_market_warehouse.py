"""Audit the local DuckDB/Parquet A-share market warehouse."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

TABLES = ("daily", "adj_factor", "daily_basic")
EXPECTED_START = date(1990, 12, 19)


def _scalar(connection: duckdb.DuckDBPyConnection, sql: str) -> Any:
    return connection.execute(sql).fetchone()[0]


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def audit(database: Path) -> dict[str, Any]:
    if not database.is_file():
        raise FileNotFoundError(database)

    connection = duckdb.connect(str(database), read_only=True)
    try:
        manifest_rows = connection.execute(
            """
            SELECT api_name, row_count, checkpoint_hash, built_at, source
            FROM metadata.archive_manifest
            ORDER BY api_name
            """
        ).fetchall()
        manifest = {
            row[0]: {
                "row_count": row[1],
                "checkpoint_hash": row[2],
                "built_at": _json_value(row[3]),
                "source": row[4],
            }
            for row in manifest_rows
        }

        tables: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        warnings: list[str] = []
        checkpoint_hashes = {item["checkpoint_hash"] for item in manifest.values()}
        if set(manifest) != set(TABLES):
            failures.append("archive manifest does not contain exactly the three expected APIs")
        if len(checkpoint_hashes) != 1:
            failures.append("archive manifest rows do not share one checkpoint hash")

        for table in TABLES:
            row_count, min_date, max_date, null_keys = connection.execute(
                f"""
                SELECT
                    count(*), min(trade_date), max(trade_date),
                    count(*) FILTER (WHERE trade_date IS NULL OR ts_code IS NULL)
                FROM raw.{table}
                """
            ).fetchone()
            duplicate_keys = _scalar(
                connection,
                f"""
                SELECT count(*)
                FROM (
                    SELECT trade_date, ts_code
                    FROM raw.{table}
                    GROUP BY trade_date, ts_code
                    HAVING count(*) > 1
                )
                """,
            )
            tables[table] = {
                "row_count": row_count,
                "min_trade_date": _json_value(min_date),
                "max_trade_date": _json_value(max_date),
                "null_keys": null_keys,
                "duplicate_keys": duplicate_keys,
            }
            expected_rows = manifest.get(table, {}).get("row_count")
            if row_count != expected_rows:
                failures.append(f"raw.{table} row count differs from archive manifest")
            if min_date != EXPECTED_START:
                failures.append(f"raw.{table} starts at {min_date}, expected {EXPECTED_START}")
            if null_keys:
                failures.append(f"raw.{table} has {null_keys} null business keys")
            if duplicate_keys:
                failures.append(f"raw.{table} has {duplicate_keys} duplicate business keys")

        daily_checks = connection.execute(
            """
            SELECT
                count(*) FILTER (
                    WHERE high < greatest(open, low, close)
                       OR low > least(open, high, close)
                ) AS invalid_ohlc,
                count(*) FILTER (WHERE vol < 0) AS negative_volume,
                count(*) FILTER (WHERE amount < 0) AS negative_amount,
                count(*) FILTER (
                    WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
                ) AS null_ohlc
            FROM raw.daily
            """
        ).fetchone()
        tables["daily"].update(
            {
                "invalid_ohlc": daily_checks[0],
                "negative_volume": daily_checks[1],
                "negative_amount": daily_checks[2],
                "null_ohlc": daily_checks[3],
            }
        )
        if daily_checks[0]:
            warnings.append(
                f"raw.daily has {daily_checks[0]} provider OHLC anomalies; "
                "they must remain isolated from the tradable view"
            )
        if any(daily_checks[1:]):
            failures.append(f"raw.daily hard domain checks failed: {daily_checks[1:]}")

        flagged_invalid_ohlc, anomaly_rows, tradable_rows = connection.execute(
            """
            SELECT
                count(*) FILTER (WHERE NOT is_valid_ohlc),
                (SELECT count(*) FROM research.market_daily_anomalies),
                (SELECT count(*) FROM research.market_daily_tradable)
            FROM research.market_daily
            """
        ).fetchone()
        tables["daily"].update(
            {
                "flagged_invalid_ohlc": flagged_invalid_ohlc,
                "anomaly_view_rows": anomaly_rows,
                "tradable_view_rows": tradable_rows,
            }
        )
        if flagged_invalid_ohlc != daily_checks[0] or anomaly_rows != daily_checks[0]:
            failures.append("research quality flags do not isolate every invalid OHLC row")

        invalid_adj_factor = _scalar(
            connection,
            "SELECT count(*) FROM raw.adj_factor WHERE adj_factor IS NULL OR adj_factor <= 0",
        )
        tables["adj_factor"]["invalid_adj_factor"] = invalid_adj_factor
        if invalid_adj_factor:
            failures.append(f"raw.adj_factor has {invalid_adj_factor} null/non-positive factors")

        conversion_errors = _scalar(
            connection,
            """
            SELECT count(*)
            FROM (
                SELECT d.vol, d.amount, r.volume_shares, r.amount_cny
                FROM raw.daily d
                JOIN research.market_daily r USING (trade_date, ts_code)
                USING SAMPLE 10000 ROWS
            )
            WHERE volume_shares != vol * 100.0 OR amount_cny != amount * 1000.0
            """,
        )
        if conversion_errors:
            failures.append(f"research.market_daily has {conversion_errors} sampled unit conversion errors")

        database_objects = [
            {"schema": row[0], "name": row[1], "type": row[2]}
            for row in connection.execute(
                """
                SELECT schema_name, table_name, 'TABLE'
                FROM duckdb_tables()
                WHERE schema_name IN ('raw', 'research', 'metadata')
                UNION ALL
                SELECT schema_name, view_name, 'VIEW'
                FROM duckdb_views()
                WHERE schema_name IN ('raw', 'research', 'metadata')
                ORDER BY 1, 2
                """
            ).fetchall()
        ]
        sample = connection.execute(
            """
            SELECT ts_code, trade_date, close, volume_shares, amount_cny
            FROM research.market_daily
            WHERE ts_code = '000001.SZ'
            ORDER BY trade_date DESC
            LIMIT 3
            """
        ).fetchall()
        return {
            "audited_at": datetime.now().astimezone().isoformat(),
            "database": str(database.resolve()),
            "status": "FAILED" if failures else ("PASSED_WITH_WARNINGS" if warnings else "PASSED"),
            "manifest": manifest,
            "tables": tables,
            "sampled_unit_conversion_errors": conversion_errors,
            "objects": database_objects,
            "sample_000001_sz": [[_json_value(value) for value in row] for row in sample],
            "warnings": warnings,
            "failures": failures,
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/warehouse/alpha_research.duckdb"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = audit(args.database)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
