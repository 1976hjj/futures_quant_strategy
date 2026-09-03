"""Audit M2-D archive lineage, row counts, PIT dates, and revision intervals."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import duckdb


def audit(database: Path, checkpoint: Path) -> dict[str, object]:
    checkpoint_hash = f"sha256:{hashlib.sha256(checkpoint.read_bytes()).hexdigest()}"
    failures: list[str] = []
    with duckdb.connect(str(database), read_only=True) as connection:
        manifest = connection.execute(
            "SELECT dataset, row_count, checkpoint_hash, pit_grade FROM metadata.m2d_archive_manifest ORDER BY 1"
        ).fetchall()
        if len(manifest) != 4:
            failures.append("M2-D manifest must contain four datasets")
        if any(row[2] != checkpoint_hash for row in manifest):
            failures.append("M2-D manifest checkpoint hash mismatch")
        raw_counts = {
            "income_statement_versions": connection.execute(
                "SELECT count(*) FROM raw.income_statement_versions"
            ).fetchone()[0],
            "balance_sheet_versions": connection.execute("SELECT count(*) FROM raw.balance_sheet_versions").fetchone()[
                0
            ],
            "cashflow_statement_versions": connection.execute(
                "SELECT count(*) FROM raw.cashflow_statement_versions"
            ).fetchone()[0],
            "financial_indicator_versions": connection.execute(
                "SELECT count(*) FROM raw.financial_indicator_versions"
            ).fetchone()[0],
        }
        for dataset, row_count, _, _ in manifest:
            if raw_counts.get(dataset) != row_count:
                failures.append(f"manifest row count mismatch: {dataset}")
        exception_counts = dict(
            connection.execute(
                "SELECT exception_reason, count(*) FROM research.financial_pit_exceptions GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )
        interval_errors = connection.execute(
            "SELECT count(*) FROM research.financial_pit_asof WHERE valid_to < valid_from"
        ).fetchone()[0]
        if interval_errors:
            failures.append("financial PIT contains reversed validity intervals")
        canonical_count = connection.execute("SELECT count(*) FROM research.financial_versions_canonical").fetchone()[0]
        if canonical_count != sum(raw_counts.values()):
            failures.append("canonical union row count differs from raw tables")
        revision_stats = connection.execute(
            """SELECT source_api, count(*), max(revision_number),
            count(*) FILTER (WHERE revision_number > 1)
            FROM research.financial_revision_events GROUP BY 1 ORDER BY 1"""
        ).fetchall()
    return {
        "audited_at": datetime.now().astimezone().isoformat(),
        "checkpoint_hash": checkpoint_hash,
        "failures": failures,
        "manifest": [dict(dataset=r[0], row_count=r[1], checkpoint_hash=r[2], pit_grade=r[3]) for r in manifest],
        "raw_counts": raw_counts,
        "canonical_count": canonical_count,
        "pit_exception_counts": exception_counts,
        "revision_stats": [
            dict(source_api=r[0], versions=r[1], max_revision=r[2], revised_versions=r[3]) for r in revision_stats
        ],
        "status": "PASS" if not failures else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--checkpoint", type=Path, default=Path("data/tushare_financial_archive/checkpoint.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/m2d_financial_audit.json"))
    args = parser.parse_args()
    report = audit(args.database, args.checkpoint)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
