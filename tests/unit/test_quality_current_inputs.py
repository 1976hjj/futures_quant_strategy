from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb

from alpha_research_os.portfolio.quality_inputs import build_inputs, source_signature, CALCULATION_MODE
from alpha_research_os.portfolio.quality_strategies import atomic_json, QualityStrategyBook, fingerprint


def test_full_rebuild_changes_old_dates_when_local_source_is_corrected(tmp_path, monkeypatch):
    from scripts import publish_jqdata_factor, publish_factor_release
    from alpha_research_os.portfolio import strategy_backtest
    fid = "jqdata-natural-log-of-market-cap"
    database = tmp_path / "data/warehouse/alpha_research.duckdb"
    database.parent.mkdir(parents=True)
    with duckdb.connect(str(database)) as c:
        c.execute("CREATE SCHEMA research")
        c.execute("CREATE TABLE research.current_inputs(session DATE, value DOUBLE)")
        c.execute("INSERT INTO research.current_inputs VALUES ('2020-01-02',2),('2020-01-03',4)")
    # Old values deliberately cannot be read: only their formula identity is used.
    digest = "sha256:" + "a" * 64
    candidate = dict(start=date(2016, 1, 1), end=date(2026, 10, 12), created_at="fixture", release_id=digest, parquet=tmp_path / "old.parquet")
    manifest = {"request": {"engine_version": "1.0.0", "factors": [{"factor_id": fid,
        "factor_version": "1.0.0", "spec_hash": digest, "implementation_hash": digest, "catalog_entry_hash": digest}],
        "dataset_lineage": [{"manifest_table": "fixture", "checkpoint_hashes": [digest]}],
        "universe_id": "ALL-A-PIT", "universe_version": "fixture", "start": "2016-01-01", "end": "2026-10-12", "signal_clock_version": "cn-close-postclose-v1"}}
    atomic_json(tmp_path / "data/factor_store/releases" / candidate["release_id"][7:] / "manifest.json", manifest)
    path = tmp_path / "scripts/publish_jqdata_factor.py"
    path.parent.mkdir(parents=True)
    path.write_text("fixture formula", encoding="utf-8")
    monkeypatch.setattr(strategy_backtest, "_factor_release_candidates", lambda *a: [candidate])
    monkeypatch.setattr(publish_factor_release, "_lineage", lambda c: [])

    def sql(item, request, target, warmup):
        assert request.start == date(2020, 1, 2)  # Entire requested window, never only the tail.
        assert warmup < request.start
        return f"""COPY (SELECT session,'600036.SH' instrument_id,'{fid}' factor_id,
        'RAW' variant,value,session::TIMESTAMP AT TIME ZONE 'Asia/Shanghai'+INTERVAL 15 HOURS available_at
        FROM research.current_inputs WHERE session BETWEEN DATE '{request.start}' AND DATE '{request.end}')
        TO '{target.as_posix()}' (FORMAT PARQUET)"""

    monkeypatch.setattr(publish_jqdata_factor, "_local_materialization_sql", sql)
    request = SimpleNamespace(start=date(2020, 1, 2), score_rules=[SimpleNamespace(factor_id=fid)], filter_rules=[], target_count=1)
    before = build_inputs(tmp_path, request, tmp_path / "first", date(2020, 1, 3), lambda _: None)[0]
    with duckdb.connect(str(database)) as c:
        c.execute("UPDATE research.current_inputs SET value=20 WHERE session=DATE '2020-01-02'")
    after = build_inputs(tmp_path, request, tmp_path / "second", date(2020, 1, 3), lambda _: None)[0]
    with duckdb.connect() as c:
        assert c.execute("SELECT value FROM read_parquet(?) ORDER BY session LIMIT 1", [str(before["parquet"])]).fetchone()[0] == 2
        assert c.execute("SELECT value FROM read_parquet(?) ORDER BY session LIMIT 1", [str(after["parquet"])]).fetchone()[0] == 20
    assert before["source_signature"] != after["source_signature"]
    assert after["calculation_mode"] == CALCULATION_MODE


def test_current_result_becomes_stale_after_source_change_and_legacy_is_not_current(tmp_path):
    import hashlib
    source = tmp_path / "original.json"
    atomic_json(source, {"summary": {"total_return": .2}, "daily": []})
    definition = {"strategy_id": "seed", "version": 1, "category": "SMALL_CAP", "source_path": "original.json",
                  "source_sha256": fingerprint(source), "request": {"start": "2020-01-02", "end": "2025-12-31", "target_count": 5, "rebalance_sessions": 5}}
    atomic_json(tmp_path / "config/quality_strategies.json", {"strategies": [definition]})
    folder = tmp_path / "data/quality_strategies/jobs"
    jid = "seed--20260101T000000-aaaaaa"
    atomic_json(folder / f"{jid}.request.json", {})
    atomic_json(folder / f"{jid}.state.json", {"job_id": jid, "status": "PASS"})
    result = {"historical_replay": {"summary": {"total_return": .3}}, "as_of": "2025-12-31",
              "calculation_mode": CALCULATION_MODE, "source_signature": source_signature(tmp_path),
              "definition_sha256": hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()}
    atomic_json(folder / f"{jid}.result.json", result)
    book = QualityStrategyBook(tmp_path)
    assert book.list()["items"][0]["calculation_state"] == "UP_TO_DATE"
    path = tmp_path / "data/warehouse/alpha_research.duckdb"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"changed raw source")
    assert book.list()["items"][0]["calculation_state"] == "STALE"
    result.pop("calculation_mode")
    atomic_json(folder / f"{jid}.result.json", result)
    assert book.list()["items"][0]["calculation_state"] == "LEGACY_INPUTS"


def test_bank_versions_choose_only_evidence_available_at_requested_cutoff():
    import pandas as pd
    from scripts.bank_factor_inputs import bank_lineage_as_of, select_fact
    with duckdb.connect() as c:
        c.execute("CREATE SCHEMA run_20261012_222119_4482db")
        c.execute("""CREATE TABLE run_20261012_222119_4482db.bank_metric_lineage AS SELECT
          '600036.SH' code, DATE '2025-12-31' report_date, 'npl_ratio' metric, 1.0 AS "value",
          TIMESTAMPTZ '2026-10-12 14:28:00+00' available_at,
          TIMESTAMPTZ '2026-10-12 14:28:00+00' retrieved_at, false historical_pit_verified""")
        c.execute("CREATE SCHEMA run_20261014_235540_78bc1f")
        c.execute("""CREATE TABLE run_20261014_235540_78bc1f.bank_metric_lineage AS SELECT *,
          'extra_column_from_new_schema' future_schema_field
          FROM run_20261012_222119_4482db.bank_metric_lineage""")
        c.execute("""UPDATE run_20261014_235540_78bc1f.bank_metric_lineage
          SET "value"=2.0,available_at=TIMESTAMPTZ '2026-10-14 16:07:00+00',retrieved_at=TIMESTAMPTZ '2026-10-14 16:07:00+00'""")
        c.execute("CREATE VIEW bank_metric_lineage AS SELECT * FROM run_20261014_235540_78bc1f.bank_metric_lineage")
        before = bank_lineage_as_of(c, date(2026,10,14), pd.Timestamp('2026-10-14T15:59:59Z'))
        assert list(before.value) == [1.0]
        after = bank_lineage_as_of(c, date(2026,10,15), pd.Timestamp('2026-10-15T01:00:00Z'))
        after['source_priority'], after['basis'], after['source_id'], after['source_sha256'] = 2, 'same_scope', 'free', 'hash'
        assert select_fact(after, 'npl_ratio')['value'] == 2.0
        assert len(after) == 2  # Latest published alias does not count twice.
