from datetime import date, timedelta

import pandas as pd
import pytest

from alpha_research_os.portfolio.external_market_regime import (
    build_external_regime_schedule,
)
from alpha_research_os.portfolio.shadow_health import ShadowHealthSpec


def test_external_regime_uses_three_exposures_with_asymmetric_confirmation() -> None:
    sessions = [date(2020, 1, 1) + timedelta(days=index) for index in range(14)]
    nav = [100.0] * 5 + [90.0, 90.0, 84.0, 83.0] + [84.0] * 5
    shadow = [
        {"session": session.isoformat(), "nav": value}
        for session, value in zip(sessions, nav, strict=True)
    ]
    trend_gap = [0.02] * 5 + [0.02] * 2 + [-0.02] * 2 + [0.0] * 5
    breadth = [0.70] * 5 + [0.70] * 2 + [0.30] * 2 + [0.50] * 5
    market = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(sessions),
            "market_index": [1.0 + value for value in trend_gap],
            "market_ma": [1.0] * len(sessions),
            "market_trend_gap": trend_gap,
            "external_breadth": breadth,
        }
    )
    spec = ShadowHealthSpec(experiment_variant="S4V3")

    initial, schedule, changes, _ = build_external_regime_schedule(shadow, market, spec)

    assert initial == 0.50
    assert schedule == {
        sessions[4]: 1.00,
        sessions[6]: 0.50,
        sessions[8]: 0.30,
        sessions[13]: 0.50,
    }
    assert [change["to_regime"] for change in changes] == [
        "STRONG",
        "BASE",
        "WEAK",
        "BASE",
    ]
    assert changes[2]["shadow_breadth"] is None
    assert changes[2]["external_breadth"] == pytest.approx(0.30)


def test_external_regime_thresholds_are_ordered() -> None:
    with pytest.raises(ValueError):
        ShadowHealthSpec(
            experiment_variant="S4V3",
            regime_weak_exposure=0.60,
            regime_base_exposure=0.50,
        )
