from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

MODULE_PATH = Path(__file__).with_name("run_validation.py")
SPEC = importlib.util.spec_from_file_location("risk_validation", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_rolling_percentile_uses_only_past_and_present() -> None:
    source = pd.Series([1.0, 2.0, 3.0, 100.0])
    result = MODULE.rolling_percentile(source, window=3, min_periods=2)
    assert np.isnan(result.iloc[0])
    assert result.iloc[1] == 75.0
    assert np.isclose(result.iloc[2], 5 / 6 * 100)
    changed_future = source.copy()
    changed_future.iloc[3] = -100.0
    changed = MODULE.rolling_percentile(changed_future, window=3, min_periods=2)
    assert result.iloc[:3].equals(changed.iloc[:3])


def test_t_plus_one_execution_keeps_first_day_in_cash() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]),
            "open": [100.0, 110.0, 120.0],
            "close": [105.0, 120.0, 120.0],
        }
    )
    simulation = MODULE.simulate(frame, pd.Series([1.0, 1.0, 1.0]), cost_bps=0.0)
    assert simulation.daily.loc[0, "nav"] == 1.0
    assert simulation.daily.loc[1, "nav"] == 120 / 110
    assert simulation.daily.loc[2, "nav"] == 120 / 110


def test_position_change_pays_cost() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2020-01-01", "2020-01-02"]),
            "open": [100.0, 100.0],
            "close": [100.0, 100.0],
        }
    )
    simulation = MODULE.simulate(frame, pd.Series([1.0, 1.0]), cost_bps=10.0)
    assert np.isclose(simulation.daily.loc[1, "nav"], 0.999)
