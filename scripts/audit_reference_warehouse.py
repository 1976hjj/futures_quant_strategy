"""Audit M2-B security history and point-in-time universe views."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def audit(database: Path) -> dict[str, Any]:
    if not database.is_file():
        raise FileNotFoundError(database)
    connection = duckdb.connect(str(database), read_only=True)
    try:
        failures: list[str] = []
        warnings: list[str] = []
        manifest = {
            row[0]: {"row_count": row[1], "checkpoint_hash": row[2], "pit_grade": row[3]}
            for row in connection.execute(
                """
                SELECT dataset, row_count, checkpoint_hash, pit_grade
                FROM metadata.m2b_archive_manifest
                ORDER BY dataset
                """
            ).fetchall()
        }
        checkpoint_hashes = {item["checkpoint_hash"] for item in manifest.values()}
        if len(checkpoint_hashes) != 1:
            failures.append("M2-B manifest does not reference one frozen checkpoint")

        raw_tables = {
            "trading_calendar": "trading_calendar",
            "security_master": "security_master",
            "name_history": "name_history",
            "stock_st": "stock_st",
            "suspensions": "suspensions",
        }
        raw_counts = {
            name: connection.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
            for name, table in raw_tables.items()
        }
        for name, count in raw_counts.items():
            if manifest.get(name, {}).get("row_count") != count:
                failures.append(f"raw.{name} row count differs from M2-B manifest")

        duplicate_calendar = connection.execute(
            """
            SELECT count(*) FROM (
                SELECT exchange, cal_date FROM raw.trading_calendar
                GROUP BY exchange, cal_date HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
        duplicate_master = connection.execute(
            """
            SELECT count(*) FROM (
                SELECT ts_code FROM raw.security_master
                GROUP BY ts_code HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
        if duplicate_calendar or duplicate_master:
            failures.append(
                f"reference business keys are not unique: calendar={duplicate_calendar}, master={duplicate_master}"
            )

        calendar_stats = connection.execute(
            """
            SELECT min(cal_date), max(cal_date), count(*) FILTER (WHERE is_open)
            FROM research.trading_calendar
            """
        ).fetchone()
        pretrade_mismatches = connection.execute(
            """
            WITH opens AS (
                SELECT cal_date, pretrade_date, lag(cal_date) OVER (ORDER BY cal_date) AS expected
                FROM research.trading_calendar
                WHERE is_open
            )
            SELECT count(*) FROM opens
            WHERE expected IS NOT NULL AND pretrade_date != expected
            """
        ).fetchone()[0]
        if pretrade_mismatches:
            failures.append(f"calendar has {pretrade_mismatches} broken previous-session links")

        master_stats = connection.execute(
            """
            SELECT
                count(*),
                count(*) FILTER (WHERE is_a_share),
                count(*) FILTER (WHERE is_a_share AND list_date IS NULL),
                count(*) FILTER (WHERE is_a_share AND delist_date IS NOT NULL)
            FROM research.security_master
            """
        ).fetchone()
        if master_stats[2]:
            failures.append(f"A-share master has {master_stats[2]} rows without listing date")

        state_stats = connection.execute(
            """
            SELECT
                count(*), min(trade_date), max(trade_date),
                count(*) FILTER (WHERE is_st IS NULL),
                count(*) FILTER (WHERE is_suspended),
                count(*) FILTER (WHERE eligible_for_signal),
                count(*) FILTER (WHERE NOT name_is_point_in_time)
            FROM research.security_session_state
            """
        ).fetchone()
        duplicate_states = connection.execute(
            """
            SELECT count(*) FROM (
                SELECT trade_date, ts_code FROM research.security_session_state
                GROUP BY trade_date, ts_code HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
        invalid_eligible = connection.execute(
            """
            SELECT count(*) FROM research.security_session_state
            WHERE eligible_for_signal
              AND (is_st IS DISTINCT FROM false OR is_suspended OR NOT is_tradeable_bar)
            """
        ).fetchone()[0]
        after_delisting = connection.execute(
            """
            SELECT count(*) FROM research.security_session_state
            WHERE delist_date IS NOT NULL AND trade_date > delist_date
            """
        ).fetchone()[0]
        missing_delisted_history = connection.execute(
            """
            SELECT count(*)
            FROM research.security_master m
            WHERE m.is_a_share AND m.delist_date IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM research.security_session_state s WHERE s.ts_code = m.ts_code
              )
            """
        ).fetchone()[0]
        if duplicate_states:
            failures.append(f"security_session_state has {duplicate_states} duplicate keys")
        if invalid_eligible:
            failures.append(f"universe admits {invalid_eligible} ST/suspended/untradeable rows")
        if after_delisting:
            failures.append(f"state view contains {after_delisting} rows after delisting")
        if missing_delisted_history:
            failures.append(f"{missing_delisted_history} delisted A-shares disappeared from history")
        if state_stats[3]:
            warnings.append(f"{state_stats[3]} pre-2000 security-session rows have unknown ST state")
        if state_stats[6]:
            warnings.append(f"{state_stats[6]} rows use a current-name fallback; the flag remains explicit")

        return {
            "audited_at": datetime.now().astimezone().isoformat(),
            "database": str(database.resolve()),
            "status": "FAILED" if failures else ("PASSED_WITH_WARNINGS" if warnings else "PASSED"),
            "manifest": manifest,
            "raw_counts": raw_counts,
            "calendar": {
                "min_date": _json_value(calendar_stats[0]),
                "max_date": _json_value(calendar_stats[1]),
                "open_sessions": calendar_stats[2],
                "pretrade_mismatches": pretrade_mismatches,
            },
            "security_master": {
                "rows": master_stats[0],
                "a_shares": master_stats[1],
                "a_shares_without_list_date": master_stats[2],
                "delisted_a_shares": master_stats[3],
            },
            "security_session_state": {
                "rows": state_stats[0],
                "min_date": _json_value(state_stats[1]),
                "max_date": _json_value(state_stats[2]),
                "unknown_st_rows": state_stats[3],
                "suspended_rows": state_stats[4],
                "eligible_rows": state_stats[5],
                "current_name_fallback_rows": state_stats[6],
                "duplicate_keys": duplicate_states,
                "after_delisting_rows": after_delisting,
            },
            "warnings": warnings,
            "failures": failures,
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
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
