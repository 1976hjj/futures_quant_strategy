"""Shared parallel-computation and accuracy-gate helpers for local factors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

LOCAL_FACTOR_YEAR_WORKERS = 4
REFERENCE_SESSION_COUNT = 20
VALUE_ABSOLUTE_TOLERANCE = 1e-12
VALUE_RELATIVE_TOLERANCE = 1e-10
ACCURACY_STATUS_FILE = "accuracy_verification.json"


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def accuracy_status(release_dir: Path) -> dict[str, Any]:
    status_path = release_dir / ACCURACY_STATUS_FILE
    if status_path.exists():
        return json.loads(status_path.read_text(encoding="utf-8"))
    quality_path = release_dir / "quality_summary.json"
    if quality_path.exists():
        embedded = json.loads(quality_path.read_text(encoding="utf-8")).get("accuracy_gate")
        if embedded:
            return embedded
    return {"status": "NOT_REQUIRED"}


def schedule_accuracy_verification(
    database: Path,
    store: Path,
    release_id: str,
    factor_id: str,
) -> dict[str, Any]:
    release_dir = store / "releases" / release_id.removeprefix("sha256:")
    current = accuracy_status(release_dir)
    if current.get("status") in {"PENDING", "PASS", "FAIL"}:
        return current
    pending = {
        "status": "PENDING",
        "factor_id": factor_id,
        "release_id": release_id,
        "started_at": datetime.now().astimezone().isoformat(),
    }
    _write_status(release_dir / ACCURACY_STATUS_FILE, pending)
    log_path = release_dir / "accuracy_verification.log"
    stream = log_path.open("ab")
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [
                sys.executable,
                "scripts/verify_local_factor_release.py",
                "--database",
                str(database),
                "--store",
                str(store),
                "--release-id",
                release_id,
                "--factor-id",
                factor_id,
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=stream,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    except Exception as error:
        failed = {**pending, "status": "FAIL", "error": str(error)}
        _write_status(release_dir / ACCURACY_STATUS_FILE, failed)
        raise
    finally:
        stream.close()
    return pending


def year_ranges(start: date, end: date) -> tuple[tuple[date, date], ...]:
    return tuple(
        (max(start, date(year, 1, 1)), min(end, date(year, 12, 31)))
        for year in range(start.year, end.year + 1)
    )


def reference_window(
    connection: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    session_count: int = REFERENCE_SESSION_COUNT,
) -> tuple[date, date]:
    rows = connection.execute(
        """SELECT cal_date FROM research.trading_calendar
        WHERE exchange='SSE' AND is_open AND cal_date BETWEEN ? AND ?
        ORDER BY cal_date DESC LIMIT ?""",
        [start, end, session_count],
    ).fetchall()
    if not rows:
        raise ValueError("factor accuracy gate has no reference sessions")
    sessions = [row[0] for row in rows]
    return min(sessions), max(sessions)


def validate_local_factor_output(
    database: Path,
    target: Path,
    reference: Path,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Require exact universe keys and exact agreement with a serial reference."""
    target_path = target.resolve().as_posix().replace("'", "''")
    reference_path = reference.resolve().as_posix().replace("'", "''")
    with duckdb.connect(str(database), read_only=True) as connection:
        actual_count, duplicate_count, expected_count, key_difference_count = connection.execute(
            f"""WITH expected AS MATERIALIZED (
              SELECT trade_date AS session, ts_code AS instrument_id
              FROM research.universe_daily
              WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
                AND eligible_for_signal
            ), actual AS MATERIALIZED (
              SELECT session,instrument_id FROM read_parquet('{target_path}')
            ), differences AS (
              (SELECT * FROM expected EXCEPT SELECT * FROM actual)
              UNION ALL
              (SELECT * FROM actual EXCEPT SELECT * FROM expected)
            ) SELECT (SELECT count(*) FROM actual),
              (SELECT count(*)-count(DISTINCT (session,instrument_id)) FROM actual),
              (SELECT count(*) FROM expected), count(*)
            FROM differences"""
        ).fetchone()
        (
            reference_count,
            reference_key_difference_count,
            reference_metadata_difference_count,
            reference_null_difference_count,
            reference_value_difference_count,
            reference_max_absolute_difference,
            reference_max_relative_difference,
        ) = connection.execute(
            f"""WITH serial AS (
              SELECT session,instrument_id,factor_id,factor_version,variant,value,available_at,implementation_hash
              FROM read_parquet('{reference_path}')
            ), parallel AS (
              SELECT session,instrument_id,factor_id,factor_version,variant,value,available_at,implementation_hash
              FROM read_parquet('{target_path}')
              WHERE session BETWEEN (SELECT min(session) FROM serial) AND (SELECT max(session) FROM serial)
            ), joined AS (
              SELECT s.*, p.session AS parallel_session, p.factor_id AS parallel_factor_id,
                p.factor_version AS parallel_factor_version, p.variant AS parallel_variant,
                p.value AS parallel_value, p.available_at AS parallel_available_at,
                p.implementation_hash AS parallel_implementation_hash
              FROM serial s
              FULL OUTER JOIN parallel p USING (session,instrument_id)
            ), compared AS (
              SELECT *,
                CASE WHEN value IS NOT NULL AND parallel_value IS NOT NULL
                  THEN abs(value-parallel_value) END AS absolute_difference,
                CASE WHEN value IS NOT NULL AND parallel_value IS NOT NULL
                  THEN abs(value-parallel_value)/greatest(abs(value),abs(parallel_value),1e-300)
                END AS relative_difference
              FROM joined
            )
            SELECT (SELECT count(*) FROM serial),
              count(*) FILTER (WHERE session IS NULL OR parallel_session IS NULL),
              count(*) FILTER (WHERE session IS NOT NULL AND parallel_session IS NOT NULL AND (
                factor_id IS DISTINCT FROM parallel_factor_id
                OR factor_version IS DISTINCT FROM parallel_factor_version
                OR variant IS DISTINCT FROM parallel_variant
                OR available_at IS DISTINCT FROM parallel_available_at
                OR implementation_hash IS DISTINCT FROM parallel_implementation_hash)),
              count(*) FILTER (WHERE (value IS NULL) <> (parallel_value IS NULL)),
              count(*) FILTER (WHERE absolute_difference > {VALUE_ABSOLUTE_TOLERANCE}
                + {VALUE_RELATIVE_TOLERANCE}*greatest(abs(value),abs(parallel_value))),
              coalesce(max(absolute_difference),0.0), coalesce(max(relative_difference),0.0)
            FROM compared"""
        ).fetchone()
    result = {
        "status": "PASS",
        "expected_row_count": expected_count,
        "actual_row_count": actual_count,
        "duplicate_key_count": duplicate_count,
        "key_difference_count": key_difference_count,
        "serial_reference_row_count": reference_count,
        "serial_reference_key_difference_count": reference_key_difference_count,
        "serial_reference_metadata_difference_count": reference_metadata_difference_count,
        "serial_reference_null_difference_count": reference_null_difference_count,
        "serial_reference_value_difference_count": reference_value_difference_count,
        "serial_reference_max_absolute_difference": reference_max_absolute_difference,
        "serial_reference_max_relative_difference": reference_max_relative_difference,
        "value_absolute_tolerance": VALUE_ABSOLUTE_TOLERANCE,
        "value_relative_tolerance": VALUE_RELATIVE_TOLERANCE,
    }
    reference_failed = any(
        (
            reference_key_difference_count,
            reference_metadata_difference_count,
            reference_null_difference_count,
            reference_value_difference_count,
        )
    )
    if actual_count != expected_count or duplicate_count or key_difference_count or reference_failed:
        result["status"] = "FAIL"
        raise ValueError(f"local factor accuracy gate failed: {result}")
    return result
