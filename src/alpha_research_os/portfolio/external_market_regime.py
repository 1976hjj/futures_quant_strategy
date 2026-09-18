"""External point-in-time all-A regime signals for shadow position sizing."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import duckdb
import pandas as pd

from alpha_research_os.portfolio.shadow_health import ShadowHealthSpec


def build_external_market_frame(
    connection: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    spec: ShadowHealthSpec,
) -> pd.DataFrame:
    """Build an equal-weight all-A trend and breadth series using data known on T."""
    history = max(spec.external_trend_sessions, spec.external_breadth_return_sessions) + 10
    warmup = connection.execute(
        """SELECT min(cal_date) FROM (
        SELECT cal_date FROM research.trading_calendar
        WHERE exchange='SSE' AND is_open AND cal_date <= ?
        ORDER BY cal_date DESC LIMIT ?)""",
        [start, history],
    ).fetchone()[0]
    if warmup is None:
        raise ValueError("no trading-calendar history is available for the external regime")
    frame = connection.execute(
        f"""WITH base AS (
          SELECT m.ts_code, m.trade_date,
                 m.close / m.pre_close - 1.0 AS ret,
                 ln(m.close / m.pre_close) AS log_ret
          FROM research.market_daily m
          JOIN research.universe_daily u USING (trade_date, ts_code)
          WHERE m.trade_date BETWEEN ? AND ? AND m.close > 0 AND m.pre_close > 0
            AND m.is_valid_ohlc AND u.has_market_bar
        ), rolling AS (
          SELECT *,
            sum(log_ret) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN
              {spec.external_breadth_return_sessions - 1} PRECEDING AND CURRENT ROW) AS ret_n,
            count(*) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN
              {spec.external_breadth_return_sessions - 1} PRECEDING AND CURRENT ROW) AS obs_n
          FROM base
        )
        SELECT trade_date, avg(ret) AS market_return,
          avg(CASE WHEN obs_n={spec.external_breadth_return_sessions}
                   THEN CAST(ret_n > 0 AS DOUBLE) END) AS external_breadth
        FROM rolling GROUP BY trade_date ORDER BY trade_date""",
        [warmup, end],
    ).df()
    if frame.empty:
        raise ValueError("external all-A regime query returned no observations")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["market_index"] = (1 + frame["market_return"].fillna(0)).cumprod()
    frame["market_ma"] = frame["market_index"].rolling(
        spec.external_trend_sessions,
        min_periods=spec.external_trend_sessions,
    ).mean()
    frame["market_trend_gap"] = frame["market_index"] / frame["market_ma"] - 1
    evaluation = frame.loc[frame["trade_date"].dt.date >= start].reset_index(drop=True)
    if evaluation.empty or evaluation.iloc[0][["market_ma", "external_breadth"]].isna().any():
        raise ValueError("insufficient point-in-time history to form the external regime at backtest start")
    return evaluation


def build_external_regime_schedule(
    shadow_daily: list[dict[str, Any]],
    market_frame: pd.DataFrame,
    spec: ShadowHealthSpec,
) -> tuple[float, dict[date, float], list[dict[str, object]], list[dict[str, object]]]:
    """Map external market state and shadow drawdown to 100%/50%/30% exposure."""
    shadow = pd.DataFrame(shadow_daily)
    if shadow.empty:
        raise ValueError("shadow strategy returned no daily observations")
    shadow["session"] = pd.to_datetime(shadow["session"])
    shadow["peak"] = shadow["nav"].rolling(
        spec.drawdown_peak_lookback_sessions,
        min_periods=1,
    ).max()
    shadow["shadow_drawdown"] = shadow["nav"] / shadow["peak"] - 1
    market = market_frame.rename(columns={"trade_date": "session"})
    frame = shadow.merge(
        market[["session", "market_index", "market_ma", "market_trend_gap", "external_breadth"]],
        on="session",
        how="left",
        validate="one_to_one",
    )
    if frame[["market_ma", "external_breadth"]].isna().any().any():
        raise ValueError("external regime is missing one or more shadow trading sessions")

    initial = spec.regime_base_exposure
    current = initial
    current_regime = "BASE"
    candidate_regime: str | None = None
    candidate_count = 0
    schedule: dict[date, float] = {}
    changes: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []

    exposure_by_regime = {
        "STRONG": spec.regime_strong_exposure,
        "BASE": spec.regime_base_exposure,
        "WEAK": spec.regime_weak_exposure,
    }
    for row in frame.itertuples(index=False):
        drawdown = float(row.shadow_drawdown)
        trend_gap = float(row.market_trend_gap)
        breadth = float(row.external_breadth)
        strong = (
            trend_gap > 0
            and breadth >= spec.external_strong_breadth
            and drawdown > -spec.regime_ordinary_drawdown
        )
        weak = (
            trend_gap < 0
            and breadth <= spec.external_weak_breadth
            and drawdown <= -spec.regime_severe_drawdown
        )
        desired_regime = "STRONG" if strong else "WEAK" if weak else "BASE"
        desired = exposure_by_regime[desired_regime]
        if math.isclose(desired, current, abs_tol=1e-12):
            candidate_regime = None
            candidate_count = 0
        else:
            if candidate_regime == desired_regime:
                candidate_count += 1
            else:
                candidate_regime = desired_regime
                candidate_count = 1
            required = (
                spec.regime_down_confirmation_sessions
                if desired < current
                else spec.regime_up_confirmation_sessions
            )
            if candidate_count >= required:
                signal_session = row.session.date()
                observation = {
                    "signal_session": signal_session.isoformat(),
                    "shadow_nav": float(row.nav),
                    "shadow_nav_ma": None,
                    "shadow_drawdown": drawdown,
                    "shadow_breadth": None,
                    "external_market_index": float(row.market_index),
                    "external_market_ma": float(row.market_ma),
                    "external_trend_gap": trend_gap,
                    "external_breadth": breadth,
                    "from_regime": current_regime,
                    "to_regime": desired_regime,
                    "from_exposure": current,
                    "to_exposure": desired,
                    "trigger": (
                        "外部趋势向上、20日上涨比例达标且影子回撤小于普通回撤"
                        if desired_regime == "STRONG"
                        else "外部趋势向下、20日上涨比例偏低且影子达到严重回撤"
                        if desired_regime == "WEAK"
                        else "强/弱环境条件不再同时成立"
                    ),
                }
                schedule[signal_session] = desired
                changes.append(observation)
                current = desired
                current_regime = desired_regime
                candidate_regime = None
                candidate_count = 0
        observations.append(
            {
                "signal_session": row.session.date().isoformat(),
                "shadow_nav": float(row.nav),
                "shadow_drawdown": drawdown,
                "external_market_index": float(row.market_index),
                "external_market_ma": float(row.market_ma),
                "external_trend_gap": trend_gap,
                "external_breadth": breadth,
                "raw_regime": desired_regime,
                "target_exposure": current,
            }
        )
    return initial, schedule, changes, observations
