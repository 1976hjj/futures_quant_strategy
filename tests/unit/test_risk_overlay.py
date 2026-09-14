from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from alpha_research_os.portfolio.risk_overlay import RiskOverlaySpec, build_exposure_schedule


def _frame(exposures: list[float], scores: list[float] | None = None) -> pd.DataFrame:
    sessions = pd.bdate_range("2025-01-02", periods=len(exposures))
    values = scores or [10 + index * 10 for index in range(len(exposures))]
    return pd.DataFrame(
        {
            "trade_date": sessions,
            "risk_score": values,
            "level_index": [0 if value < 20 else 1 if value < 35 else 2 for value in values],
            "raw_exposure": exposures,
        }
    )


def test_levels_must_be_contiguous_and_weights_sum_to_one() -> None:
    with pytest.raises(ValidationError, match="sum to one"):
        RiskOverlaySpec(weights={"breadth": .4, "trend": .2, "volatility": .15,
                                 "liquidity": .15, "stress_tail": .2})
    levels = list(RiskOverlaySpec().levels)
    levels[1] = levels[1].model_copy(update={"score_min": 21})
    with pytest.raises(ValidationError, match="contiguous"):
        RiskOverlaySpec(levels=levels)


def test_r3_interval_is_anchored_to_evaluation_start() -> None:
    frame = _frame([1, .9, .8, .9, .8, .65, .5, .65])
    start = frame.iloc[2].trade_date.date()
    end = frame.iloc[-1].trade_date.date()
    initial, schedule, _ = build_exposure_schedule(
        frame, RiskOverlaySpec(experiment_variant="R3", r3_interval_sessions=3), start, end
    )

    assert initial == .8
    assert schedule == {frame.iloc[5].trade_date.date(): .65}


def test_r4_reduces_immediately_but_requires_three_sessions_to_add() -> None:
    frame = _frame([1, .8, .8, 1, 1, 1], [10, 40, 42, 10, 9, 8])
    start = frame.iloc[0].trade_date.date()
    end = frame.iloc[-1].trade_date.date()
    _, schedule, changes = build_exposure_schedule(
        frame,
        RiskOverlaySpec(experiment_variant="R4", hysteresis_score=0,
                        down_confirmation_sessions=1, up_confirmation_sessions=3),
        start,
        end,
    )

    assert list(schedule.values()) == [.8, 1]
    assert list(schedule) == [frame.iloc[1].trade_date.date(), frame.iloc[5].trade_date.date()]
    assert [item["to_level"] for item in changes] == ["M2", "M0"]


def test_r4_hysteresis_allows_reduction_to_highest_crossed_boundary() -> None:
    frame = _frame([.65, .2], [55, 87])
    frame["level_index"] = [3, 6]
    start = frame.iloc[0].trade_date.date()
    end = frame.iloc[-1].trade_date.date()
    _, schedule, _ = build_exposure_schedule(
        frame, RiskOverlaySpec(experiment_variant="R4", hysteresis_score=3), start, end
    )

    assert list(schedule.values()) == [.35]


def test_r5_is_fixed_full_sample_mean_and_marked_without_m_level() -> None:
    frame = _frame([1, .8, .6, .8])
    start, end = frame.iloc[0].trade_date.date(), frame.iloc[-1].trade_date.date()
    initial, schedule, changes = build_exposure_schedule(
        frame, RiskOverlaySpec(experiment_variant="R5"), start, end
    )

    assert initial == pytest.approx(.8)
    assert schedule == {}
    assert changes == []


def test_r6_uses_prior_rolling_mean_at_quarter_boundaries() -> None:
    sessions = pd.bdate_range("2024-01-02", "2025-04-15")
    exposures = pd.Series(.8, index=range(len(sessions)))
    exposures.iloc[-45:] = .4
    frame = pd.DataFrame(
        {"trade_date": sessions, "risk_score": 50.0, "level_index": 3, "raw_exposure": exposures}
    )
    spec = RiskOverlaySpec(experiment_variant="R6", train_lookback_sessions=20)
    start, end = date(2025, 1, 2), date(2025, 4, 15)

    initial, schedule, changes = build_exposure_schedule(frame, spec, start, end)

    prior_quarter_end = frame.loc[frame.trade_date.dt.date < start].iloc[-1]
    prior_index = int(prior_quarter_end.name)
    expected_initial = frame.raw_exposure.iloc[prior_index - 19:prior_index + 1].mean()
    assert initial == pytest.approx(expected_initial)
    assert all(item["to_level"] is None for item in changes)
    assert all(signal.month in {3, 6, 9, 12} for signal in schedule)


def test_r7_uses_configured_fixed_exposure_without_changes() -> None:
    initial, schedule, changes = build_exposure_schedule(
        pd.DataFrame(),
        RiskOverlaySpec(experiment_variant="R7", fixed_exposure=.65),
        date(2025, 1, 2),
        date(2025, 12, 31),
    )

    assert initial == .65
    assert schedule == {}
    assert changes == []
