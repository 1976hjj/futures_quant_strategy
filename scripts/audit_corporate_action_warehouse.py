"""Audit M2-C corporate-action publication and factor reconciliation."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb


def _value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def audit(database: Path) -> dict[str, Any]:
    connection = duckdb.connect(str(database), read_only=True)
    failures: list[str] = []
    warnings: list[str] = []
    try:
        manifest = connection.execute(
            "SELECT dataset, row_count, checkpoint_hash, pit_grade FROM metadata.m2c_archive_manifest"
        ).fetchone()
        raw_count = connection.execute("SELECT count(*) FROM raw.corporate_actions").fetchone()[0]
        if not manifest or manifest[1] != raw_count:
            failures.append("M2-C manifest row count differs from raw publication")
        null_codes = connection.execute(
            "SELECT count(*) FROM raw.corporate_actions WHERE ts_code IS NULL"
        ).fetchone()[0]
        duplicate_lineage = connection.execute(
            """
            SELECT count(*) FROM (
                SELECT source_snapshot_id, source_row_number
                FROM raw.corporate_actions GROUP BY 1, 2 HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
        if null_codes or duplicate_lineage:
            failures.append(
                f"raw action identity invalid: null_codes={null_codes} duplicate_lineage={duplicate_lineage}"
            )
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM research.corporate_action_announcements),
                (SELECT count(*) FROM research.corporate_action_events),
                (SELECT count(*) FROM research.corporate_action_ex_dates),
                (SELECT count(*) FROM research.adjustment_factor_jumps),
                (SELECT count(*) FROM research.corporate_action_reconciliation)
            """
        ).fetchone()
        statuses = dict(
            connection.execute(
                "SELECT match_status, count(*) FROM research.corporate_action_reconciliation GROUP BY 1"
            ).fetchall()
        )
        diagnostics = dict(
            connection.execute(
                "SELECT diagnostic_status, count(*) FROM research.corporate_action_reconciliation GROUP BY 1"
            ).fetchall()
        )
        approval = connection.execute(
            """
            SELECT
                count(*) FILTER (WHERE approved_for_dividend_adjustment),
                count(*) FILTER (WHERE match_status = 'MATCHED_DATE' AND factor_market_reconciled),
                count(*) FILTER (WHERE match_status = 'MATCHED_DATE' AND event_reference_reconciled),
                count(*) FILTER (WHERE match_status = 'MATCHED_DATE' AND NOT approved_for_dividend_adjustment)
            FROM research.corporate_action_reconciliation
            """
        ).fetchone()
        invalid_jump = connection.execute(
            """
            SELECT count(*) FROM research.adjustment_factor_jumps
            WHERE factor_ratio IS NULL OR factor_ratio <= 0
            """
        ).fetchone()[0]
        duplicate_jump = connection.execute(
            """
            SELECT count(*) FROM (
                SELECT ts_code, trade_date FROM research.adjustment_factor_jumps
                GROUP BY 1, 2 HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
        late_implementation = connection.execute(
            """
            SELECT count(*) FROM research.corporate_action_events
            WHERE available_date IS NOT NULL AND available_date > ex_date
            """
        ).fetchone()[0]
        if invalid_jump or duplicate_jump:
            failures.append(f"factor jump identity invalid: nonpositive={invalid_jump} duplicate={duplicate_jump}")
        if late_implementation:
            warnings.append(f"{late_implementation} implemented events have available_date after ex_date")
        candidate_conflicts = connection.execute(
            "SELECT count(*) FROM research.corporate_action_events WHERE economic_candidate_count > 1"
        ).fetchone()[0]
        if candidate_conflicts:
            warnings.append(
                f"{candidate_conflicts} ex-dates have multiple economic candidates; latest candidate selected"
            )
        ratio_stats = connection.execute(
            """
            SELECT
                count(*) FILTER (WHERE factor_market_ratio_abs_error IS NOT NULL),
                quantile_cont(factor_market_ratio_abs_error, 0.5),
                quantile_cont(factor_market_ratio_abs_error, 0.95),
                quantile_cont(factor_market_ratio_abs_error, 0.99),
                max(factor_market_ratio_abs_error)
            FROM research.corporate_action_reconciliation
            WHERE match_status = 'MATCHED_DATE'
            """
        ).fetchone()
        event_price_stats = connection.execute(
            """
            SELECT
                count(*) FILTER (WHERE event_reference_price_abs_error IS NOT NULL),
                quantile_cont(event_reference_price_abs_error, 0.5),
                quantile_cont(event_reference_price_abs_error, 0.95),
                quantile_cont(event_reference_price_abs_error, 0.99),
                max(event_reference_price_abs_error)
            FROM research.corporate_action_reconciliation
            WHERE match_status = 'MATCHED_DATE'
            """
        ).fetchone()
        return {
            "audited_at": datetime.now().astimezone().isoformat(),
            "database": str(database.resolve()),
            "status": "FAILED" if failures else ("PASSED_WITH_WARNINGS" if warnings else "PASSED"),
            "manifest": {
                "dataset": manifest[0] if manifest else None,
                "row_count": manifest[1] if manifest else None,
                "checkpoint_hash": manifest[2] if manifest else None,
                "pit_grade": manifest[3] if manifest else None,
            },
            "raw_rows": raw_count,
            "research_counts": {
                "announcements": counts[0],
                "implemented_events": counts[1],
                "event_dates": counts[2],
                "factor_jumps": counts[3],
                "reconciliation_rows": counts[4],
            },
            "match_statuses": statuses,
            "diagnostic_statuses": diagnostics,
            "approval_gate": {
                "approved_dividend_adjustments": approval[0],
                "matched_factor_market_reconciled": approval[1],
                "matched_event_reference_reconciled": approval[2],
                "matched_but_quarantined": approval[3],
            },
            "factor_market_ratio_error": {
                "observations": ratio_stats[0],
                "median": _value(ratio_stats[1]),
                "p95": _value(ratio_stats[2]),
                "p99": _value(ratio_stats[3]),
                "max": _value(ratio_stats[4]),
            },
            "event_reference_price_error": {
                "observations": event_price_stats[0],
                "median": _value(event_price_stats[1]),
                "p95": _value(event_price_stats[2]),
                "p99": _value(event_price_stats[3]),
                "max": _value(event_price_stats[4]),
            },
            "warnings": warnings,
            "failures": failures,
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--output", type=Path, default=Path("reports/m2c_corporate_action_audit.json"))
    args = parser.parse_args()
    result = audit(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
