from __future__ import annotations

import time
from datetime import date, timedelta

import duckdb

from scripts.factor_compute_runtime import accuracy_status
from scripts.publish_alpha158_factor import publish


def test_single_alpha158_factor_publishes_an_independent_release(tmp_path) -> None:
    database = tmp_path / "alpha.duckdb"
    store = tmp_path / "factor_store"
    digest = "sha256:" + "a" * 64
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE SCHEMA metadata")
        connection.execute("CREATE SCHEMA research")
        for table in (
            "archive_manifest", "m2b_archive_manifest", "m2c_archive_manifest", "m2d_archive_manifest"
        ):
            connection.execute(f"CREATE TABLE metadata.{table}(checkpoint_hash VARCHAR)")
            connection.execute(f"INSERT INTO metadata.{table} VALUES (?)", [digest])
        connection.execute("CREATE TABLE research.trading_calendar(cal_date DATE, exchange VARCHAR, is_open BOOLEAN)")
        connection.execute(
            "CREATE TABLE research.universe_daily("
            "trade_date DATE, ts_code VARCHAR, eligible_for_signal BOOLEAN)"
        )
        connection.execute(
            "CREATE TABLE research.security_session_state("
            "trade_date DATE, ts_code VARCHAR, eligible_for_signal BOOLEAN)"
        )
        connection.execute(
            "CREATE TABLE research.market_daily("
            "trade_date DATE, ts_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, "
            "close DOUBLE, volume_shares DOUBLE, amount_cny DOUBLE)"
        )
        connection.execute("CREATE TABLE research.adj_factor(trade_date DATE, ts_code VARCHAR, adj_factor DOUBLE)")
        start = date(2020, 1, 2)
        for offset in range(6):
            session = start + timedelta(days=offset)
            connection.execute("INSERT INTO research.trading_calendar VALUES (?, 'SSE', true)", [session])
            for instrument, shift in (("000001.SZ", 0.0), ("600000.SH", 1.0)):
                close = 10.5 + offset + shift
                connection.execute("INSERT INTO research.universe_daily VALUES (?, ?, true)", [session, instrument])
                connection.execute(
                    "INSERT INTO research.security_session_state VALUES (?, ?, true)",
                    [session, instrument],
                )
                connection.execute(
                    "INSERT INTO research.market_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [session, instrument, close - 0.5, close + 0.5, close - 1.0, close, 1000.0, close * 1000.0],
                )
                connection.execute("INSERT INTO research.adj_factor VALUES (?, ?, 1.0)", [session, instrument])

    result = publish(database, store, "alpha158-kmid", date(2020, 1, 2), date(2020, 1, 7))
    assert result["factor_id"] == "alpha158-kmid"
    assert result["accuracy_status"] == "PENDING"
    assert result["row_count"] == 12
    release_dir = store / "releases" / result["release_id"].removeprefix("sha256:")
    deadline = time.monotonic() + 10
    while accuracy_status(release_dir)["status"] == "PENDING" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert accuracy_status(release_dir)["status"] == "PASS"

    repeated = publish(database, store, "alpha158-kmid", date(2020, 1, 2), date(2020, 1, 7))
    assert repeated["cache_hit"] is True
    assert repeated["accuracy_status"] == "PASS"
    assert repeated["release_id"] == result["release_id"]
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT factor_count FROM metadata.factor_release_manifest").fetchone() == (1,)
        assert connection.execute("SELECT count(value) FROM research.factor_values_raw").fetchone() == (12,)
