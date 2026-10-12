"""Research safety checks: time direction, gaps, calendar inference and state masks."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.research.research_bank_sector_timing import (
    ACTIVITY,
    PB,
    TREND_CHANGE,
    activity_features,
    assign_states,
    bootstrap_contrast,
    forward_labels,
    hac_regression,
    holm_adjust,
    nonoverlap_positions,
)


def references(n=12):
    sessions = pd.bdate_range("2025-01-02", periods=n)
    values = 100 * 1.01 ** np.arange(n)
    bank = pd.DataFrame(
        {"session": sessions, "bank_equal_total_return_index": values, "basket_segment": 0, "status": "READY"}
    )
    broad = pd.DataFrame(
        {
            "session": sessions,
            "gross_total_return_index": np.full(n, 100.0),
            "benchmark_segment": 0,
            "benchmark_status": "READY",
        }
    )
    return sessions, bank, broad


def test_label_starts_next_close_and_has_exact_horizon():
    sessions, bank, broad = references()
    bank.loc[0, "bank_equal_total_return_index"] = 1
    result = forward_labels(bank, broad, sessions, horizons=(3,))
    first = result.iloc[0]
    assert first.entry_session == sessions[1]
    assert first.exit_session == sessions[4]
    assert first.bank_return == pytest.approx(1.01**3 - 1)
    assert first.active_return == pytest.approx(first.bank_return)
    assert first.max_drawdown == 0


def test_labels_do_not_bridge_a_bank_gap_or_reset_segment():
    sessions, bank, broad = references()
    bank.loc[3, "bank_equal_total_return_index"] = np.nan
    bank.loc[3, "status"] = "MISSING_RETURN"
    bank.loc[4:, "basket_segment"] = 1
    result = forward_labels(bank, broad, sessions, horizons=(3,))
    assert result.iloc[0].status == "BANK_PATH_BREAK"
    assert pd.isna(result.iloc[0].bank_return)
    assert result.iloc[3].status == "READY_ANALYTICAL"


def test_benchmark_gap_invalidates_a_relative_label():
    sessions, bank, broad = references()
    broad.loc[2, "gross_total_return_index"] = np.nan
    result = forward_labels(bank, broad, sessions, horizons=(3,))
    assert result.iloc[0].status == "BENCHMARK_PATH_BREAK"
    assert pd.isna(result.iloc[0].active_return)


def test_unobserved_tail_is_not_shortened_or_filled():
    sessions, bank, broad = references(5)
    result = forward_labels(bank, broad, sessions, horizons=(3,))
    assert result.iloc[0].status == "READY_ANALYTICAL"
    assert result.iloc[1].status == "RELEASED_TAIL_UNOBSERVED"
    assert pd.isna(result.iloc[1].bank_return)


def test_drawdown_includes_intermediate_losses_not_just_exit_return():
    sessions, bank, broad = references(5)
    bank["bank_equal_total_return_index"] = [100, 100, 80, 120, 110]
    result = forward_labels(bank, broad, sessions, horizons=(3,))
    assert result.iloc[0].bank_return == pytest.approx(0.1)
    assert result.iloc[0].max_drawdown == pytest.approx(-0.2)
    assert result.iloc[0].minimum_return == pytest.approx(-0.2)


def turnover_fixture():
    days = pd.bdate_range("2020-01-02", periods=100)
    raw = pd.DataFrame(
        [{"session": day, "instrument_id": f"B{code:02}", "turnover_rate": 1.0} for day in days for code in range(12)]
    )
    universe = raw[["session", "instrument_id"]].copy()
    return days, raw, universe


def test_activity_is_past_only_and_has_minimum_history():
    days, raw, universe = turnover_fixture()
    first, _ = activity_features(raw, days, universe)
    changed = raw.copy()
    changed.loc[changed.session >= days[80], "turnover_rate"] = 100
    second, _ = activity_features(changed, days, universe)
    pd.testing.assert_series_equal(first[ACTIVITY].iloc[:80], second[ACTIVITY].iloc[:80])
    assert pd.isna(first[ACTIVITY].iloc[43])
    assert first[ACTIVITY].iloc[44] == pytest.approx(1)
    assert second[ACTIVITY].iloc[-1] > 1


def test_activity_does_not_compress_missing_calendar_days_or_accept_zero_denominator():
    days, raw, universe = turnover_fixture()
    raw = raw[~raw.session.isin(days[40:60])].copy()
    result, _ = activity_features(raw, days, universe)
    assert pd.isna(result.loc[days[60], ACTIVITY])  # 40 valid/60, below 45
    raw["turnover_rate"] = 0
    result, _ = activity_features(raw, days, universe)
    assert result[ACTIVITY].isna().all()


def test_duplicate_turnover_fails_instead_of_double_weighting():
    days, raw, universe = turnover_fixture()
    with pytest.raises(ValueError, match="duplicate"):
        activity_features(pd.concat([raw, raw.iloc[:1]]), days, universe)


def test_unknown_state_remains_unknown_and_boundaries_are_explicit():
    features = pd.DataFrame({PB: [0.3, 0.3, 0.31, 0.31, np.nan, 0.2], TREND_CHANGE: [0.01, 0, 0.01, 0, 0.1, np.nan]})
    state = assign_states(features)
    assert state.iloc[:4].tolist() == [0, 1, 2, 3]
    assert state.iloc[4:].isna().all()


def test_hac_matches_direct_calendar_score_calculation_with_missing_days():
    sessions = pd.bdate_range("2020-01-02", periods=100)
    rng = np.random.default_rng(17)
    x = rng.normal(size=100)
    y = 0.2 * x + rng.normal(scale=0.1, size=100)
    y[[10, 30, 31, 50]] = np.nan
    lag = 4
    result = hac_regression(x, y, sessions, lag)
    good = np.isfinite(y)
    full = np.column_stack([np.ones(100), x])
    xx = full[good]
    inv = np.linalg.inv(xx.T @ xx)
    beta = inv @ xx.T @ y[good]
    scores = np.zeros_like(full)
    scores[good] = xx * (y[good] - xx @ beta)[:, None]
    meat = scores.T @ scores
    for offset in range(1, lag + 1):
        cross = scores[offset:].T @ scores[:-offset]
        meat += (1 - offset / (lag + 1)) * (cross + cross.T)
    covariance = inv @ meat @ inv * good.sum() / (good.sum() - 2)
    assert result["coefficient"] == pytest.approx(beta[1])
    assert result["standard_error"] == pytest.approx(np.sqrt(covariance[1, 1]))


def test_binary_contrast_requires_enough_dates_in_each_group():
    dates = pd.bdate_range("2020-01-02", periods=100)
    result = hac_regression(np.r_[np.ones(20), np.zeros(80)], np.arange(100.0), dates, 4, binary=True)
    assert result["p_value"] is None


def test_holm_keeps_unavailable_hypotheses_in_registered_family():
    assert holm_adjust([0.01, 0.04, None, 0.03]) == pytest.approx([0.04, 0.09, 1.0, 0.09])


def test_bootstrap_reproducible_with_full_calendar_and_missing_labels():
    y = np.r_[np.full(80, 0.2), np.full(80, 0.1)]
    y[10:20] = np.nan
    treated, control = np.arange(160) < 80, np.arange(160) >= 80
    a = bootstrap_contrast(y, treated, control, 20, 7, resamples=199)
    b = bootstrap_contrast(y, treated, control, 20, 7, resamples=199)
    assert a == b
    assert a["mean_difference"] == pytest.approx(0.1)
    assert a["confidence_lower"] == pytest.approx(0.1)
    assert a["confidence_upper"] == pytest.approx(0.1)


def test_nonoverlap_sampling_is_chronological_and_not_result_selected():
    assert nonoverlap_positions(np.ones(70, dtype=bool), 20) == [0, 21, 42, 63]
