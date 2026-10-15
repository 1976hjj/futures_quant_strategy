from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from alpha_research_os.portfolio.quality_strategies import (
    CalculateSignal,
    QualityStrategyBook,
    atomic_json,
    fingerprint,
    inverse_risk_weights,
    quarterly_signals,
    chart_history,
)


def test_chart_holding_pnl_uses_own_trades_at_date_with_fees_splits_and_partial_sales():
    import copy
    recorded = {
        'trades': [
            {'session': '2025-01-02', 'ts_code': 'A', 'security_name': '当时名称', 'side': 'BUY',
             'amount_cny': 1000, 'total_cost_cny': 5, 'post_quantity': 100},
            # Split from 100 to 200 shares leaves total remaining cost unchanged.
            {'session': '2025-01-06', 'ts_code': 'A', 'side': 'SELL',
             'cost_basis_cny': 251.25, 'post_quantity': 150},
            {'session': '2025-01-07', 'ts_code': 'A', 'side': 'BUY',
             'amount_cny': 300, 'total_cost_cny': 5, 'post_quantity': 200},
            {'session': '2025-01-08', 'ts_code': 'A', 'side': 'SELL',
             'cost_basis_cny': 1058.75, 'post_quantity': 0},
            # Future trades/name must not affect an earlier day's P/L.
            {'session': '2025-01-09', 'ts_code': 'A', 'security_name': '未来名称', 'side': 'BUY',
             'amount_cny': 100000, 'total_cost_cny': 100, 'post_quantity': 100},
        ],
        'daily': [
            {'session': '2025-01-02', 'nav': 2000, 'holdings': [{'ts_code': 'A', 'quantity': 100, 'market_value_cny': 1100}]},
            {'session': '2025-01-03', 'nav': 2100, 'dividend_cash': 50, 'holdings': [{'ts_code': 'A', 'quantity': 200, 'market_value_cny': 1100}]},
            {'session': '2025-01-06', 'nav': 2100, 'holdings': [{'ts_code': 'A', 'quantity': 150, 'market_value_cny': 900}]},
            {'session': '2025-01-07', 'nav': 2100, 'holdings': [{'ts_code': 'A', 'quantity': 200, 'market_value_cny': 1000}]},
            {'session': '2025-01-08', 'nav': 2100, 'holdings': []},
        ],
    }
    original = copy.deepcopy(recorded)
    replay = chart_history(recorded)
    first, split, sold, added, empty = replay['daily']
    assert first['holdings'][0]['cost_basis_cny'] == 1005
    assert first['holdings'][0]['average_cost_price'] == 10.05
    assert first['holdings'][0]['unrealized_pnl_cny'] == 95
    assert first['holdings'][0]['unrealized_pnl_pct'] == pytest.approx(95/1005)
    assert split['holdings'][0]['average_cost_price'] == 5.025
    assert split['holdings'][0]['unrealized_pnl_cny'] == 95  # cash dividend excluded
    assert first['holdings'][0]['last_rebalance_session'] == '2025-01-02'
    assert split['holdings'][0]['last_rebalance_session'] == '2025-01-02'
    assert sold['holdings'][0]['last_rebalance_session'] == '2025-01-06'
    assert added['holdings'][0]['last_rebalance_session'] == '2025-01-07'
    assert sold['holdings'][0]['cost_basis_cny'] == 753.75
    assert added['holdings'][0]['cost_basis_cny'] == 1058.75
    assert added['holdings'][0]['unrealized_pnl_cny'] == -58.75
    assert added['holdings'][0]['security_name'] == '当时名称'
    assert empty['holdings'] == []
    assert recorded == original


def test_chart_never_invents_missing_holding_cost_or_allocated_sale_cost():
    replay = chart_history({'trades': [
        {'session': '2025-01-02', 'ts_code': 'A', 'side': 'BUY', 'amount_cny': 1000, 'total_cost_cny': 5},
        {'session': '2025-01-03', 'ts_code': 'A', 'side': 'SELL', 'post_quantity': 50},
    ], 'daily': [
        {'session': '2025-01-01', 'nav': 100, 'holdings': [{'ts_code': 'A', 'quantity': 100, 'market_value_cny': 1100}]},
        {'session': '2025-01-03', 'nav': 100, 'holdings': [{'ts_code': 'A', 'quantity': 50, 'market_value_cny': 600}]},
    ]})
    assert all('unrealized_pnl_cny' not in d['holdings'][0] for d in replay['daily'])
    assert 'last_rebalance_session' not in replay['daily'][0]['holdings'][0]
    assert replay['daily'][1]['holdings'][0]['last_rebalance_session'] == '2025-01-03'


def test_chart_holding_start_survives_rebalances_and_only_resets_after_full_exit():
    import copy
    recorded = {'trades': [
        {'session': '2025-01-02', 'ts_code': 'A', 'side': 'BUY', 'quantity': 100, 'post_quantity': 100},
        {'session': '2025-04-02', 'ts_code': 'A', 'side': 'BUY', 'quantity': 10, 'post_quantity': 210},
        {'session': '2025-07-02', 'ts_code': 'A', 'side': 'SELL', 'quantity': 50, 'post_quantity': 160},
        {'session': '2025-10-02', 'ts_code': 'A', 'side': 'SELL', 'quantity': 160, 'post_quantity': 0},
        {'session': '2025-10-03', 'ts_code': 'A', 'side': 'BUY', 'quantity': 200, 'post_quantity': 200},
        # An incomplete old trade cannot prove when an existing holding began.
        {'session': '2025-10-03', 'ts_code': 'B', 'side': 'BUY', 'quantity': 10, 'post_quantity': 110},
        {'session': '2026-01-02', 'ts_code': 'A', 'side': 'BUY', 'quantity': 10, 'post_quantity': 210},
    ], 'daily': [
        {'session': session, 'nav': 1000, 'holdings': holdings}
        for session, holdings in [
            ('2025-01-02', [{'ts_code': 'A', 'quantity': 100}]),
            ('2025-01-03', [{'ts_code': 'A', 'quantity': 200}]),  # stock split
            ('2025-04-02', [{'ts_code': 'A', 'quantity': 210}]),
            ('2025-07-02', [{'ts_code': 'A', 'quantity': 160}]),
            ('2025-10-02', []),
            ('2025-10-03', [{'ts_code': 'A', 'quantity': 200}, {'ts_code': 'B', 'quantity': 110}]),
        ]
    ]}
    original = copy.deepcopy(recorded)
    rows = chart_history(recorded)['daily']
    assert [r['holdings'][0]['holding_since_session'] for r in rows[:4]] == ['2025-01-02'] * 4
    assert rows[1]['holdings'][0]['last_rebalance_session'] == '2025-01-02'
    assert rows[2]['holdings'][0]['last_rebalance_session'] == '2025-04-02'
    assert rows[3]['holdings'][0]['last_rebalance_session'] == '2025-07-02'
    assert rows[4]['holdings'] == []
    assert rows[5]['holdings'][0]['holding_since_session'] == '2025-10-03'
    assert rows[5]['holdings'][0]['last_rebalance_session'] == '2025-10-03'
    assert 'holding_since_session' not in rows[5]['holdings'][1]
    assert recorded == original


def result():
    return {
        "status": "PASS",
        "config": {"name": "original", "start": "2020-01-02", "end": "2025-12-31",
                   "score_rules": [{"factor_id": "quality", "release_id": "sha256:" + "a" * 64, "weight": 1}]},
        "summary": {"total_return": .2, "maximum_drawdown": -.1},
        "daily": [{"session": "2020-01-02", "nav": 100}, {"session": "2025-12-31", "nav": 120}],
        "annual": [{"year": 2025, "return": .2}],
    }


def test_chart_accounting_uses_hash_bound_backtest_without_mutating_saved_signal(tmp_path):
    book = QualityStrategyBook(tmp_path)
    path = book.store / "jobs/strategy--job.backtest.json"
    backtest = {**result(), "config": {**result()["config"], "initial_cash_cny": 100},
                "daily": [{"session": "2020-01-02", "nav": 99, "cash": 19, "positions": 1,
                           "actual_stock_exposure": 80 / 99, "daily_return": -.01},
                          {"session": "2025-12-31", "nav": 120, "cash": 120, "positions": 0,
                           "holdings": [], "actual_stock_exposure": 0}]}
    atomic_json(path, backtest)
    signal_path = book.store / "jobs/strategy--job.result.json"
    saved = {"historical_replay": {"daily": result()["daily"]},
             "source_backtest_sha256": fingerprint(path),
             "model_positions": [{"ts_code": "600036.SH", "quantity": 999}]}
    atomic_json(signal_path, saved)
    original_bytes = signal_path.read_bytes()
    replay = book.result("strategy--job")["historical_replay"]
    assert replay["initial_cash_cny"] == 100
    assert replay["daily"][0]["cash"] == 19
    assert replay["daily"][1]["positions"] == 0
    assert replay["daily"][1]["holdings"] == []
    assert "holdings" not in replay["daily"][0]
    assert signal_path.read_bytes() == original_bytes
    atomic_json(path, {**backtest, "daily": [{"session": "2020-01-02", "nav": 999999}]})
    assert book.result("strategy--job")["historical_replay"] == saved["historical_replay"]


def test_registration_copies_evidence_and_uses_server_metrics(tmp_path):
    path = tmp_path / "reports/strategy_backtests/example.result.json"
    atomic_json(path, result())
    book = QualityStrategyBook(tmp_path)
    recorded = book.register({"job_id": "example", "category": "BANK", "name": "bank candidate", "notes": "keep"})
    path.unlink()
    detail = book.detail(recorded["strategy_id"])
    assert detail["summary"]["total_return"] == .2
    assert len(detail["daily"]) == 2
    assert detail["status"] == "RESEARCH_CANDIDATE"
    assert book.list("BANK")["items"][0]["strategy_id"] == recorded["strategy_id"]
    assert book.list("SMALL_CAP")["items"] == []


@pytest.mark.parametrize("payload", [
    {"job_id": "../escape", "category": "BANK", "name": "bad"},
    {"job_id": "example", "category": "INVALID", "name": "bad"},
    {"job_id": "example", "category": "BANK", "name": "bad", "summary": {"total_return": 100}},
])
def test_invalid_registration_cannot_inject_source_or_metrics(tmp_path, payload):
    with pytest.raises(ValidationError):
        QualityStrategyBook(tmp_path).register(payload)
    assert not (tmp_path / "data/quality_strategies").exists()


def test_failed_backtest_and_blank_name_cannot_be_recorded(tmp_path):
    failed = {**result(), "status": "FAIL"}
    atomic_json(tmp_path / "reports/strategy_backtests/example.result.json", failed)
    book = QualityStrategyBook(tmp_path)
    with pytest.raises(ValueError, match="completed"):
        book.register({"job_id": "example", "category": "BANK", "name": "bad"})
    with pytest.raises(ValueError, match="blank"):
        book.register({"job_id": "example", "category": "BANK", "name": "   "})
    assert book.definitions() == []


def test_changed_source_evidence_is_reported_not_silently_reused(tmp_path):
    source = tmp_path / "report.json"
    atomic_json(source, result())
    config = result()["config"]
    item = {"strategy_id": "bank", "category": "BANK", "version": 1, "source_path": "report.json", "source_sha256": fingerprint(source), "request": {**config, "target_count": 5, "rebalance_sessions": 63}}
    atomic_json(tmp_path / "config/quality_strategies.json", {"strategies": [item]})
    atomic_json(source, {**result(), "summary": {"total_return": 10}})
    book = QualityStrategyBook(tmp_path)
    assert "changed" in book.list()["items"][0]["evidence_error"]
    with pytest.raises(ValueError, match="changed"):
        book.calculate("bank", {})


def test_risk_weights_are_stable_and_preserve_purchase_order():
    original = {str(i): float(i + 1) for i in range(10)}
    weights = inverse_risk_weights(original)
    reversed_weights = inverse_risk_weights(dict(reversed(list(original.items()))))
    assert list(weights) == list(original)
    assert list(reversed_weights) == list(reversed(original))
    assert weights == pytest.approx(reversed_weights, abs=1e-14)
    assert sum(weights.values()) == pytest.approx(1)
    assert max(weights.values()) <= .15 + 1e-12
    assert weights['0'] == weights['1'] == .15


def test_short_risk_list_keeps_unused_budget_in_cash():
    weights = inverse_risk_weights({"a": 1., "b": 2.})
    assert weights == pytest.approx({"a": .15, "b": .15})
    assert sum(weights.values()) == pytest.approx(.3)
    with pytest.raises(ValueError, match="positive"):
        inverse_risk_weights({"a": 0})


@pytest.mark.parametrize("payload", [
    {"capital_cny": float("inf")}, {"capital_cny": -1},
    {"current_holdings": [{"ts_code": "830001.BJ", "quantity": 100}]},
    {"current_holdings": [{"ts_code": "600036.SH", "quantity": -1}]},
    {"current_holdings": [{"ts_code": "600036.SH", "quantity": 1.5}]},
])
def test_signal_inputs_reject_nonfinite_funding_and_invalid_holdings(payload):
    with pytest.raises(ValidationError):
        CalculateSignal.model_validate(payload)


def test_lost_process_is_terminal_and_immutable_results_survive(tmp_path):
    book = QualityStrategyBook(tmp_path)
    path = book.store / "jobs/strategy--job.state.json"
    atomic_json(path, {"job_id": "strategy--job", "status": "RUNNING", "pid": 0})
    assert book.job("strategy--job")["status"] == "FAIL"
    assert not book.running()
    with pytest.raises(ValueError):
        book.job("../escape")


def test_partial_month_or_quarter_cannot_become_a_rebalance_date():
    from datetime import date
    days = [date(2026, 1, 5), date(2026, 3, 30), date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30), date(2026, 10, 12)]
    assert quarterly_signals(days, date(2026, 10, 12), date(2026, 10, 12)) == set(days[:-1]) - {date(2026, 3, 30)}
    assert quarterly_signals(days[:2], date(2026, 3, 30), date(2026, 3, 30)) == {days[0]}
    # A real last Friday may be quarter end when the full calendar covers Sunday.
    friday = [date(2023, 1, 3), date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 29)]
    assert date(2023, 9, 29) in quarterly_signals(friday, date(2023, 9, 30), date(2023, 9, 29))


def test_data_updates_cannot_change_inputs_during_a_signal_job(tmp_path, monkeypatch):
    from scripts.data_update_api import DataUpdateManager
    monkeypatch.setattr(QualityStrategyBook, "running", lambda self: True)
    with pytest.raises(RuntimeError, match="优质策略"):
        DataUpdateManager(tmp_path).start({})


def test_bank_scope_is_enforced_before_reading_any_factor_or_database(tmp_path):
    from datetime import date
    from types import SimpleNamespace
    from scripts.run_quality_strategy import prepare_factors
    atomic_json(tmp_path / "config/bank_timing_data.json", {"released_feature_end": "2025-12-31"})
    request = SimpleNamespace(score_rules=[SimpleNamespace(factor_id="bank-quality-dividend-yield-365")], filter_rules=[])
    folder = tmp_path / "inputs"
    with pytest.raises(ValueError, match="2025-12-31"):
        prepare_factors(tmp_path, request, folder, date(2026, 10, 12), lambda _: None)
    assert not folder.exists()


def test_seed_evidence_survives_original_cleanup_and_rejects_archive_corruption(tmp_path):
    path = tmp_path / "reports/source.json"
    atomic_json(path, result())
    definition = {"strategy_id": "seed", "source_path": "reports/source.json", "source_sha256": fingerprint(path)}
    book = QualityStrategyBook(tmp_path)
    assert book._source(definition)["summary"]["total_return"] == .2
    path.unlink()
    assert book._source(definition)["summary"]["total_return"] == .2
    archive = next((book.store / "evidence").glob("*.json"))
    atomic_json(archive, {"changed": True})
    with pytest.raises(ValueError, match="integrity"):
        book._source(definition)


def test_bank_postclose_snapshot_does_not_backfill_close_information():
    from datetime import date
    import pandas as pd
    from scripts.bank_factor_inputs import build_features
    day = date(2026, 10, 12)
    facts = pd.DataFrame([
        {"code": "600036.SH", "metric": metric, "value": value,
         "report_date": date(2025, 12, 31), "available_at": pd.Timestamp("2026-10-12T22:28:00+08:00"),
         "source_priority": 0, "basis": metric, "source_id": "token", "source_sha256": "hash"}
        for metric, value in [("roe_weighted", 18.), ("npl_ratio", .9), ("provision_coverage_ratio", 450.)]
    ])
    events = pd.DataFrame(columns=["ts_code", "ex_date"])
    market = pd.DataFrame([{ "instrument_id": "600036.SH", "session": day, "close": 40. }])
    capital = pd.DataFrame([{ "ts_code": "600036.SH", "trade_date": day, "close": 40., "pre_close": 40. }])
    historical = build_features(facts, events, market, capital, set())
    scheduled = build_features(facts, events, market, capital, set(), post_close=True)
    live = build_features(facts, events, market, capital, set(), information_cutoff=pd.Timestamp("2026-10-12T23:59:59+08:00"))
    assert pd.isna(historical.quality_gate.iloc[0])
    assert scheduled.quality_gate.iloc[0] == 1
    assert scheduled.available_at.iloc[0] == pd.Timestamp("2026-10-12T23:59:59+08:00")
    assert live.quality_gate.iloc[0] == 1
    assert live.available_at.iloc[0] == pd.Timestamp("2026-10-12T23:59:59+08:00")
    with pytest.raises(ValueError, match="single matching"):
        build_features(facts, events, market, capital, set(), information_cutoff=pd.Timestamp("2026-10-13T00:00:00+08:00"))


def test_atomic_status_recovers_a_brief_windows_reader_lock(tmp_path, monkeypatch):
    original = Path.replace
    calls = []

    def locked(self, target):
        calls.append(self)
        if len(calls) < 3:
            raise PermissionError("temporary reader lock")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", locked)
    atomic_json(tmp_path / "state.json", {"status": "RUNNING"})
    assert json.loads((tmp_path / "state.json").read_bytes()) == {"status": "RUNNING"}
    assert len(calls) == 3 and len(set(calls)) == 1
