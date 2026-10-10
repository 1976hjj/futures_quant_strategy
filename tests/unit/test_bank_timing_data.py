from datetime import date

import pandas as pd
import pytest

from alpha_research_os.data.bank_timing import (
    benchmark_timeline,
    disclosure_changes,
    equal_bank_basket,
    history_readiness,
    latest_input_gaps,
    same_period_change,
    stock_total_return_inputs,
)
from scripts.bank_factor_inputs import build_features, financial_state, select_fact
from scripts.build_bank_timing_data import released_window


def test_valuation_warmup_counts_pre_start_history_without_using_future():
    days = [date(2020, 1, day) for day in range(1, 6)]
    values = pd.DataFrame([dict(session=day, instrument_id='BANK', book_to_price=2.,
                                cash_yield365=.01 * i, annual_earnings_yield=.02 * i)
                           for i, day in enumerate(days, 1)])
    panel = values[values.session == days[3]].copy()
    result = history_readiness(panel, [days[3]], window=4, valuation_history=values)
    assert result.yield_history_count.iloc[0] == 4
    assert result.earnings_history_count.iloc[0] == 4
    prefix = history_readiness(panel, [days[3]], window=4, valuation_history=values.iloc[:4])
    pd.testing.assert_frame_equal(result, prefix)


def test_financial_cache_tracks_expiration_and_intraday_new_disclosure():
    days = [date(2020, 7, day) for day in [2, 3, 4, 6, 7]]
    facts = pd.DataFrame([
        dict(code='BANK', metric='common_bvps', report_date=date(2018, 12, 31), value=10.,
             available_at=pd.Timestamp('2019-03-01T00:00:00Z'), basis='same', unit='CNY/share',
             source_id='original', source_priority=1, source_sha256='old'),
        dict(code='BANK', metric='common_bvps', report_date=date(2019, 12, 31), value=20.,
             available_at=pd.Timestamp('2020-07-06T08:00:00Z'), basis='same', unit='CNY/share',
             source_id='original', source_priority=1, source_sha256='new'),
    ])
    bars = pd.DataFrame(dict(session=days, instrument_id='BANK', close=100.))
    events = pd.DataFrame(columns=['ts_code', 'ex_date', 'imp_ann_date', 'cash_div_tax', 'stk_div'])
    capitals = pd.DataFrame(columns=['ts_code', 'trade_date', 'close', 'pre_close'])
    output = build_features(facts, events, bars, capitals, {'BANK'})
    expected = []
    for day in days:
        cutoff = pd.Timestamp(day, tz='Asia/Shanghai') + pd.Timedelta(hours=15)
        state, _ = financial_state(facts[facts.available_at <= cutoff], day)
        expected.append(state['common_bvps'] / 100 if state['common_bvps'] is not None else None)
    pd.testing.assert_series_equal(output.book_to_price, pd.Series(expected, name='book_to_price'))


def fact(year, value, available="2024-04-01T00:00:00Z", basis="same", unit="percent"):
    return dict(code="BANK", metric="cet1_ratio", report_date=date(year, 12, 31), value=value,
                available_at=pd.Timestamp(available), basis=basis, unit=unit, source_id="verified",
                source_priority=1, source_sha256=f"document-{year}-{value}")


def test_financial_change_uses_same_report_period_and_matching_basis():
    known = pd.DataFrame([fact(2023, 12), fact(2022, 10)])
    value, status, current, prior = same_period_change(known, "cet1_ratio", date(2024, 4, 2), select_fact)
    assert value == 2 and status == "READY"
    known.loc[1, "basis"] = "different"
    assert same_period_change(known, "cet1_ratio", date(2024, 4, 2), select_fact)[:2] == (None, "INCOMPARABLE_FACTS")
    known.loc[1, "basis"] = "same"
    assert same_period_change(known, "cet1_ratio", date(2026, 4, 2), select_fact)[:2] == (None, "NO_FRESH_CURRENT_FACT")


@pytest.mark.parametrize("resolution", ["ns", "us", "ms"])
def test_asof_derivation_never_sees_future_revisions_at_any_timestamp_resolution(resolution):
    known = pd.DataFrame([fact(2023, 12), fact(2022, 10), fact(2023, 99, available="2024-06-01T00:00:00Z")])
    known["available_at"] = known.available_at.astype(f"datetime64[{resolution}, UTC]")
    features = pd.DataFrame([dict(session=date(2024, 4, 2), instrument_id="BANK",
                                  available_at=pd.Timestamp("2024-04-02T07:00:00Z"))])
    values, evidence = disclosure_changes(features, known, select_fact)
    assert values.cet1_change_yoy.iloc[0] == 2
    lineage = evidence[evidence.field == "cet1_change_yoy"].iloc[0]
    assert lineage.current_report_date == date(2023, 12, 31)
    assert lineage.prior_report_date == date(2022, 12, 31)
    assert lineage.current_fact_id and lineage.prior_fact_id
    assert lineage.current_available_at <= lineage.signal_cutoff


def market(days, closes, references):
    return pd.DataFrame([dict(ts_code="BANK", trade_date=day, close=price, pre_close=reference,
                              is_valid_close=True, source_snapshot_id="snapshot")
                         for day, price, reference in zip(days, closes, references, strict=True)])


def action(day, availability, cash=1, stock=0):
    return pd.DataFrame([dict(ts_code="BANK", effective_date=day, first_available_date=availability,
                              last_available_date=availability, cash_dividend_per_share=cash,
                              stock_dividend_ratio=stock, approved_for_dividend_adjustment=True)])


def test_dividend_is_added_once_and_does_not_look_like_a_price_loss():
    days = [date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)]
    data = stock_total_return_inputs(market(days, [10, 9, 9.9], [10, 9, 9]),
                                    action(days[1], date(2024, 3, 1)), days)
    assert data.economic_daily_return.iloc[1] == 0
    assert data.economic_daily_return.iloc[2] == pytest.approx(0.1)
    assert data.bank_total_return_index.tolist() == pytest.approx([100, 100, 110])


def test_future_event_versions_and_missing_sessions_break_technical_history():
    days = [date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)]
    data = stock_total_return_inputs(market(days, [10, 9, 9], [10, 9, 9]), action(days[1], days[2]), days)
    assert data.tr_status.iloc[1] == "EVENT_VERSION_NOT_KNOWN"
    assert pd.isna(data.economic_daily_return.iloc[1])
    assert data.total_return_segment.iloc[1] > data.total_return_segment.iloc[0]
    missing = stock_total_return_inputs(market([days[0], days[2]], [10, 11], [10, 10]),
                                       pd.DataFrame(columns=action(days[1], days[0]).columns), days)
    assert missing.tr_status.iloc[1] == "MISSING_MARKET_SESSION"
    assert pd.isna(missing.economic_daily_return.iloc[1])


def test_basket_uses_older_membership_and_does_not_reweight_away_missing_returns():
    days = [date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)]
    universe = pd.DataFrame([dict(session=days[0], instrument_id=code) for code in ("A", "B")]
                          + [dict(session=days[1], instrument_id="C")])
    returns = pd.DataFrame([dict(session=days[2], instrument_id="A", economic_daily_return=.1)])
    basket, weights = equal_bank_basket(universe, returns, days)
    assert basket.status.iloc[2] == "MISSING_HELD_RETURN"
    assert pd.isna(basket.daily_return.iloc[2])
    assert weights[weights.session == days[2]].instrument_id.tolist() == ["A", "B"]
    assert weights[weights.session == days[2]].weight.tolist() == [.5, .5]
    with pytest.raises(ValueError, match="known before"):
        equal_bank_basket(universe, returns, days, lag=1)


def test_history_counts_consume_missing_calendar_sessions_and_do_not_count_future():
    days = [date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)]
    panel = pd.DataFrame([dict(session=day, instrument_id="BANK", book_to_price=2., cash_yield365=.06,
                               annual_earnings_yield=.1) for day in (days[0], days[2])])
    result = history_readiness(panel, days, window=2)
    assert result.pb_history_count.tolist() == [1, 1]
    assert result.yield_history_count.tolist() == [1, 1]


def test_unreleased_window_is_rejected_before_any_data_read():
    config = {"released_feature_end": "2025-12-31"}
    with pytest.raises(ValueError, match="holdout"):
        released_window(config, date(2020, 1, 2), date(2026, 6, 30))


def test_benchmark_missing_day_is_preserved_and_breaks_return_window():
    days = [date(2024, 4, day) for day in range(1, 5)]
    raw = pd.DataFrame({"session": [days[0], days[2], days[3]],
                        "gross_total_return_index": [100., 102., 103.]})
    result = benchmark_timeline(raw, days)
    assert result.segment_observations.tolist() == [1, 0, 1, 2]
    assert result.benchmark_segment.tolist() == [0, 1, 1, 1]
    assert pd.isna(result.gross_total_return_index.iloc[1])
    assert result.benchmark_status.iloc[1] == "MISSING_SOURCE_SESSION"
    with pytest.raises(ValueError, match="keys"):
        benchmark_timeline(pd.concat([raw, raw.iloc[:1]]), days)


def test_gap_queue_distinguishes_unknown_anchor_from_insufficient_history():
    common = dict(session=date(2025, 12, 31), cash_yield365=.05, yield_history_count=756,
                  annual_earnings_yield=.1, earnings_history_count=756, bank_total_return_index=100,
                  segment_observations=200, quality_gate=1, nim_change=0, profit_growth=0,
                  npl_improvement=0, provision_change_yoy=0, cet1_change_yoy=0)
    panel = pd.DataFrame([dict(common, instrument_id="A", book_to_price=None, common_bvps=None, pb_history_count=0),
                          dict(common, instrument_id="B", book_to_price=2, common_bvps=10, pb_history_count=100)])
    gaps = latest_input_gaps(panel, 504, 120)
    assert gaps.gap_status.tolist() == ["ORDINARY_BVPS_SOURCE_UNAVAILABLE", "INSUFFICIENT_VALID_HISTORY"]
    assert gaps.field.eq("book_to_price").all()


def test_daily_pb_uses_pre_display_history_without_changing_ordinary_equity_or_future():
    from alpha_research_os.data.bank_timing import daily_pb_history_statistics

    days = [date(2019, 12, 30), date(2019, 12, 31), date(2020, 1, 2), date(2020, 1, 3)]
    source = pd.DataFrame(dict(session=days, instrument_id="BANK", pb_daily=[1., 2., 3., 999.]))
    result = daily_pb_history_statistics(source, [days[2]], window=3, minimum=2)
    latest = result[result.session == days[2]].iloc[0]
    assert latest.pb_history_count == 3 and latest.pb_percentile == pytest.approx(5 / 6)
    assert days[3] not in result.session.tolist()
    display = pd.DataFrame([dict(session=days[2], instrument_id="BANK", book_to_price=None,
                                 cash_yield365=.05, annual_earnings_yield=.1)])
    combined = history_readiness(display, [days[2]], 3, source, 2)
    assert combined.pb_daily.iloc[0] == 3 and pd.isna(combined.book_to_price.iloc[0])
    assert combined.pb_history_count.iloc[0] == 3


def test_daily_pb_missing_calendar_day_is_not_filled_or_removed_from_window():
    from alpha_research_os.data.bank_timing import daily_pb_history_statistics

    days = [date(2024, 4, day) for day in range(1, 5)]
    source = pd.DataFrame(dict(session=[days[0], days[3]], instrument_id="BANK", pb_daily=[1., 2.]))
    result = daily_pb_history_statistics(source, days, window=3, minimum=2)
    assert result.pb_history_count.iloc[-1] == 1
    assert result.pb_daily.iloc[1:3].isna().all() and pd.isna(result.pb_percentile.iloc[-1])


def test_daily_pb_source_rejects_holdout_before_opening_database(tmp_path):
    import json

    from scripts.bank_factor_inputs import daily_pb_inputs

    (tmp_path / "config").mkdir()
    (tmp_path / "config/bank_timing_data.json").write_text(json.dumps({"released_feature_end": "2025-12-31"}))
    with pytest.raises(ValueError, match="holdout"):
        daily_pb_inputs(tmp_path, date(2016, 1, 1), date(2026, 1, 1))
    assert not (tmp_path / "data").exists()


def test_pb_readiness_accepts_complete_daily_pb_and_explains_missing_input():
    from scripts.build_bank_timing_data import FIELDS, input_readiness

    day = date(2025, 12, 31)
    rows = pd.DataFrame([dict.fromkeys(FIELDS, 1.) | dict(session=day, instrument_id="A", book_to_price=None,
                          pb_daily=.5, pb_history_count=756, yield_history_count=756, earnings_history_count=756,
                          segment_observations=200)])
    settings = dict(minimum_history_sessions=504, minimum_valid_banks=1, minimum_coverage=.7,
                    trend_sessions=120, relative_strength_sessions=63)
    basket = pd.DataFrame(dict(session=[day], segment_returns=[100]))
    benchmark = pd.DataFrame(dict(session=[day], segment_observations=[100]))
    _, ready = input_readiness(rows, basket, benchmark, settings)
    assert ready[ready.factor_id == "bank-sector-pb-history-percentile"].data_ready.iloc[0]
    rows["pb_daily"] = None
    _, missing = input_readiness(rows, basket, benchmark, settings)
    pb = missing[missing.factor_id == "bank-sector-pb-history-percentile"].iloc[0]
    assert pb.reason == "PB_CURRENT_INPUT_MISSING" and "非正1家" in pb.reason_detail
