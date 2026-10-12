"""Past-only incremental conditions and fixed horizon-comparable entry anchors."""
import numpy as np
import pandas as pd

from scripts.research.research_bank_dividend_horizons import add_features, entry_positions, HORIZONS, RULES
from scripts.research.research_bank_dividend_rebound import labels


def test_conditions_are_past_only_and_unknown_is_not_false():
    dates = pd.bdate_range('2020-01-01', periods=100)
    base = pd.DataFrame({'high80': 1., 'cash_yoy252': .01}, index=dates)
    prices = pd.Series(np.arange(100.)+100, index=dates)
    activity = pd.Series(1.1, index=dates)
    activity.iloc[:30] = np.nan
    f = add_features(base, prices, activity)
    assert f.trend_recovery.iloc[:59].isna().all()
    assert f.high80_activity_rising.iloc[:30].isna().all()
    assert f.within_high80_trend.iloc[60] == 1
    changed = prices.copy(); changed.iloc[75:] = 1
    changed_a = activity.copy(); changed_a.iloc[75:] = .1
    pd.testing.assert_frame_equal(f.iloc[:75], add_features(base, changed, changed_a).iloc[:75])


def test_incremental_comparison_never_includes_outside_high_yield_dates():
    dates = pd.bdate_range('2020-01-01', periods=100)
    base = pd.DataFrame({'high80': 0., 'cash_yoy252': .01}, index=dates)
    base.loc[dates[60:70], 'high80'] = 1
    base.loc[dates[80:], 'high80'] = np.nan
    prices = pd.Series(np.arange(100.)+100, index=dates)
    f = add_features(base, prices, pd.Series(.9, index=dates))
    assert f.within_high80_activity.iloc[60:70].eq(0).all()
    assert f.within_high80_activity.iloc[:60].isna().all()
    assert f.high80_activity_rising.iloc[80:].isna().all()


def test_entry_dates_are_fixed_without_label_or_horizon_selection():
    signal = pd.Series(np.zeros(160))
    signal.iloc[0] = np.nan; signal.iloc[1] = 1  # Unknown is not a crossing.
    signal.iloc[5:10] = 1
    signal.iloc[15:20] = 1  # Within61 offirstentry, skipped.
    signal.iloc[70:80] = 1
    assert entry_positions(signal) == [5, 70]


def test_one_year_label_uses_next_close_and_unobserved_tail_stays_missing():
    dates = pd.bdate_range('2020-01-01', periods=300)
    values = np.arange(300.)+100
    lab = labels(values, dates, horizons=HORIZONS)
    first = lab[lab.horizon.eq(252)].iloc[0]
    assert first['return'] == values[253]/values[1]-1
    assert first.exit == dates[253]
    assert lab[lab.session.eq(dates[100]) & lab.horizon.eq(252)].iloc[0].status == 'TAIL'
    assert len(RULES)*len(HORIZONS)*2*2 == 128
