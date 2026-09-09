from __future__ import annotations

from datetime import date

import duckdb
import pytest

from scripts.factor_compute_runtime import validate_local_factor_output, year_ranges


def _write_factor_rows(connection: duckdb.DuckDBPyConnection, path: str, second_value: float) -> None:
    connection.execute(
        f"""COPY (
          SELECT 'release' AS release_id, DATE '2024-01-02' AS session,
            '000001.SZ' AS instrument_id, 'factor' AS factor_id, '1' AS factor_version,
            'RAW' AS variant, 1.0 AS value,
            TIMESTAMPTZ '2024-01-02 15:00:00+08:00' AS available_at,
            'hash' AS implementation_hash
          UNION ALL
          SELECT 'release', DATE '2024-01-03', '000001.SZ', 'factor', '1', 'RAW', {second_value},
            TIMESTAMPTZ '2024-01-03 15:00:00+08:00', 'hash'
        ) TO '{path}' (FORMAT PARQUET)"""
    )


def test_year_ranges_cover_each_calendar_year() -> None:
    assert year_ranges(date(2023, 6, 1), date(2025, 2, 3)) == (
        (date(2023, 6, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 2, 3)),
    )


def test_accuracy_gate_requires_exact_keys_and_serial_values(tmp_path) -> None:
    database = tmp_path / "warehouse.duckdb"
    target = tmp_path / "target.parquet"
    reference = tmp_path / "reference.parquet"
    rounded_reference = tmp_path / "rounded-reference.parquet"
    bad_reference = tmp_path / "bad-reference.parquet"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE SCHEMA research")
        connection.execute(
            """CREATE TABLE research.universe_daily(
            trade_date DATE, ts_code VARCHAR, eligible_for_signal BOOLEAN)"""
        )
        connection.execute(
            """INSERT INTO research.universe_daily VALUES
            (DATE '2024-01-02','000001.SZ',true),(DATE '2024-01-03','000001.SZ',true)"""
        )
        _write_factor_rows(connection, target.as_posix(), 2.0)
        connection.execute(
            f"""COPY (SELECT * FROM read_parquet('{target.as_posix()}') WHERE session=DATE '2024-01-03')
            TO '{reference.as_posix()}' (FORMAT PARQUET)"""
        )
        _write_factor_rows(connection, bad_reference.as_posix(), 3.0)
        _write_factor_rows(connection, rounded_reference.as_posix(), 2.0 + 1e-13)

    result = validate_local_factor_output(
        database, target, reference, date(2024, 1, 2), date(2024, 1, 3)
    )
    assert result["status"] == "PASS"
    assert result["serial_reference_value_difference_count"] == 0
    rounded_result = validate_local_factor_output(
        database, target, rounded_reference, date(2024, 1, 2), date(2024, 1, 3)
    )
    assert rounded_result["status"] == "PASS"
    assert rounded_result["serial_reference_max_absolute_difference"] > 0
    with pytest.raises(ValueError, match="accuracy gate failed"):
        validate_local_factor_output(
            database, target, bad_reference, date(2024, 1, 2), date(2024, 1, 3)
        )
