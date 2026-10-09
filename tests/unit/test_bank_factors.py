from datetime import date

import pandas as pd
import pytest

from alpha_research_os.factors.bank import (
    bank_catalog,
    bank_factor_catalog,
    cash_per_current_share,
    quality_gate,
    quality_score,
    roe_history_statistics,
)
from alpha_research_os.reporting.factor_catalog_overview import build_factor_catalog_overview, query_factor_catalog
from scripts.bank_factor_inputs import (
    build_features,
    financial_state,
    normalize_per_share,
    per_share_invalidations,
    reconcile_dividend_events,
)
from scripts.serve_m4_control_api import FactorBatchRequest, FactorComputeRequest, M4RunRequest


def fact(metric, value, year=2022, available="2023-04-01T00:00:00Z", priority=1, basis=None):
    return dict(
        code="BANK",
        metric=metric,
        value=value,
        report_date=date(year, 12, 31),
        available_at=pd.Timestamp(available),
        source_priority=priority,
        source_id="issuer",
        source_sha256="digest",
        basis=basis or metric,
    )


def dividend(day, cash, stock=0, publication=date(2023, 1, 1)):
    return dict(ex_date=day, cash_div_tax=cash, stk_div=stock, imp_ann_date=publication)


def test_catalog_bank_factors_are_distinct_and_paginated(tmp_path):
    factors = bank_factor_catalog()
    assert len(factors) == len({x.factor_id for x in factors}) == 15
    assert len(bank_catalog().list()) == 15
    response = query_factor_catalog(build_factor_catalog_overview(tmp_path), source="BANK", page_size=5, page=2)
    assert response["totalItems"] == 15
    assert response["totalPages"] == 3
    assert response["counts"]["bank"] == 15
    assert len(response["items"]) == 5
    assert all(x["source_collection"] == "BANK" for x in response["items"])


def test_quality_score_and_veto_are_different():
    assert quality_score(11, 1.2, 250) == 100
    assert quality_score(None, 1.2, 250) is None
    assert quality_gate(11, 1.2, 250) == 1
    assert quality_gate(11, 1.2, 250, cet1=8) == 0
    assert quality_score(11, 1.2, 250) == 100  # capital veto does not change Q
    assert quality_gate(11, 1.2, 250, profit_growth=-0.051) == 0
    assert quality_gate(11, 1.2, 250, npl_change=0.201) == 0
    assert quality_gate(11, 1.2, 250, npl_change=0.2, profit_growth=-0.05, cet1=8.5) == 1
    assert quality_gate(None, 1.2, 250) is None


def test_dividend_old_share_units_and_no_double_count():
    # One old share receives 1 cash, then 0.5 cash + 0.2 stock. Final 1.2 shares:
    # total cash 1.5 / 1.2 = 1.25 per current share, not 1.33333.
    events = [dividend(date(2023, 2, 1), 1), dividend(date(2023, 6, 1), 0.5, 0.2)]
    assert cash_per_current_share(events, date(2023, 7, 1)) == pytest.approx(1.25)
    assert cash_per_current_share(events, date(2024, 5, 1)) == pytest.approx(0.5 / 1.2)
    assert cash_per_current_share([], date(2023, 7, 1)) == 0
    assert cash_per_current_share([], date(2023, 7, 1), False) is None
    assert cash_per_current_share([dividend(date(2023, 6, 1), 0.5, None)], date(2023, 7, 1)) is None
    assert cash_per_current_share(events + [dividend(date(2025, 1, 1), 50)], date(2023, 7, 1)) == pytest.approx(1.25)


def test_three_year_statistics_need_three_consecutive_reports():
    values = [(date(y, 12, 31), v) for y, v in [(2020, 10), (2021, 12), (2022, 14)]]
    assert roe_history_statistics(values, date(2022, 12, 31)) == pytest.approx((12, (8 / 3) ** 0.5))
    assert roe_history_statistics(values[1:], date(2022, 12, 31)) == (None, None)


def test_financial_changes_require_same_period_and_basis():
    rows = [
        fact("npl_ratio", 1.2),
        fact("npl_ratio", 1.4, 2021),
        fact("net_interest_margin", 2),
        fact("net_interest_margin", 2.2, 2021, basis="different"),
        fact("parent_profit", 110),
        fact("parent_profit", 100, 2021),
        fact("roe_weighted", 12),
        fact("roe_weighted", 10, 2021),
        fact("roe_weighted", 11, 2020),
    ]
    state, _ = financial_state(pd.DataFrame(rows), date(2023, 4, 4))
    assert state["npl_improvement"] == pytest.approx(0.2)
    assert state["nim_change"] is None
    assert state["profit_growth"] == pytest.approx(0.1)
    assert state["roe_median3"] == 11


def test_features_do_not_use_future_publication_or_non_bank_keys():
    base = [fact("roe_weighted", 12), fact("npl_ratio", 1.2), fact("provision_coverage_ratio", 250)]
    future = [fact("roe_weighted", 1, available="2023-05-01T00:00:00Z", priority=0)]
    market = pd.DataFrame([dict(instrument_id="BANK", session=date(2023, 4, 4), close=10)])
    events = pd.DataFrame(columns=["ts_code", "ex_date"])
    bars = pd.DataFrame(columns=["ts_code", "trade_date", "close", "pre_close"])
    before = build_features(pd.DataFrame(base), events, market, bars, {"BANK"})
    after = build_features(pd.DataFrame(base + future), events, market, bars, {"BANK"})
    pd.testing.assert_frame_equal(before, after)
    assert before.quality_score.iloc[0] == 100
    assert before.quality_gate.iloc[0] == 1
    assert before.instrument_id.tolist() == ["BANK"]


def test_newly_available_token_has_priority_without_backfill():
    rows = [fact("roe_weighted", 12), fact("roe_weighted", 9, priority=0)]
    state, _ = financial_state(pd.DataFrame(rows), date(2023, 4, 4))
    assert state["roe_weighted"] == 9
    rows.append(fact("roe_weighted", 8, priority=0))
    state, _ = financial_state(pd.DataFrame(rows), date(2023, 4, 4))
    assert state["roe_weighted"] is None  # same source/version conflict quarantined


def test_reference_adjustments_abstain_and_free_stock_normalizes():
    events = [dividend(date(2023, 6, 1), 0.5, 0.2)]
    bars = pd.DataFrame(
        [
            dict(trade_date=date(2023, 5, 31), close=12.5, pre_close=12.5),
            dict(trade_date=date(2023, 6, 1), close=10, pre_close=10),
            dict(trade_date=date(2023, 6, 2), close=9, pre_close=9),
        ]
    )
    invalidations = per_share_invalidations(bars, events)
    assert invalidations == [date(2023, 6, 2)]
    assert normalize_per_share(fact("common_bvps", 12), events, invalidations, date(2023, 6, 1)) == 10
    assert normalize_per_share(fact("common_bvps", 12), events, invalidations, date(2023, 6, 2)) is None
    future_ann = [dividend(date(2023, 6, 1), 0.5, 0.2, date(2024, 1, 1))]
    assert date(2023, 6, 1) in per_share_invalidations(bars, future_ann)


@pytest.mark.parametrize("horizon", [63, 126])
def test_bank_compute_batch_and_horizon_contract(horizon):
    item = bank_factor_catalog()[0]
    reference = dict(factor_id=item.factor_id, factor_version=item.factor_version)
    FactorComputeRequest(**reference, start="2023-01-01", end="2024-12-31")
    FactorBatchRequest(factors=[reference], start="2023-01-01", end="2024-12-31", holding_sessions=horizon)
    M4RunRequest(
        factor_release_id="sha256:" + "a" * 64,
        stages=["m4_1"],
        window_start="2023-01-01",
        window_end="2024-12-31",
        holding_sessions=horizon,
    )


def test_ambiguous_same_day_dividends_are_not_summed():
    rows = [
        dict(ts_code="BANK", **dividend(date(2023, 6, 1), 0.5)),
        dict(ts_code="BANK", **dividend(date(2023, 6, 1), 0.5)),
    ]
    result = reconcile_dividend_events(pd.DataFrame(rows))
    assert len(result) == 1
    assert result.event_conflict.iloc[0]
    assert result.cash_div_tax.isna().all()
    assert cash_per_current_share(result.to_dict("records"), date(2023, 6, 2)) is None
