"""Point-in-time all-A Risk Score and configurable position overlays."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date
from pathlib import Path
from statistics import fmean, pvariance
from typing import Literal

import duckdb
import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from alpha_research_os.kernel.specs import FrozenSpec


class RiskWeights(FrozenSpec):
    breadth: float = Field(default=0.30, ge=0, le=1)
    trend: float = Field(default=0.20, ge=0, le=1)
    volatility: float = Field(default=0.15, ge=0, le=1)
    liquidity: float = Field(default=0.15, ge=0, le=1)
    stress_tail: float = Field(default=0.20, ge=0, le=1)

    @model_validator(mode="after")
    def totals_one(self) -> RiskWeights:
        if not math.isclose(sum(self.model_dump().values()), 1.0, abs_tol=1e-9):
            raise ValueError("risk weights must sum to one")
        return self


class RiskLevel(FrozenSpec):
    level_id: Literal["M0", "M1", "M2", "M3", "M4", "M5", "M6"]
    score_min: float = Field(ge=0, le=100)
    score_max: float = Field(gt=0, le=100)
    exposure: float = Field(ge=0, le=1)


def default_levels() -> tuple[RiskLevel, ...]:
    values = (
        ("M0", 0, 20, 1.00),
        ("M1", 20, 35, 0.90),
        ("M2", 35, 50, 0.80),
        ("M3", 50, 60, 0.65),
        ("M4", 60, 70, 0.50),
        ("M5", 70, 85, 0.35),
        ("M6", 85, 100, 0.20),
    )
    return tuple(
        RiskLevel(level_id=level_id, score_min=minimum, score_max=maximum, exposure=exposure)
        for level_id, minimum, maximum, exposure in values
    )


class RiskOverlaySpec(FrozenSpec):
    experiment_variant: Literal["R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"] = "R0"
    market_scope: Literal["ALL_A_EQUAL_WEIGHT_PIT"] = "ALL_A_EQUAL_WEIGHT_PIT"
    weights: RiskWeights = Field(default_factory=RiskWeights)
    levels: tuple[RiskLevel, ...] = Field(default_factory=default_levels)
    percentile_window_sessions: int = Field(default=756, ge=60, le=2520)
    percentile_min_sessions: int = Field(default=252, ge=20, le=1260)
    breadth_return_sessions: int = Field(default=20, ge=2, le=252)
    trend_sessions: int = Field(default=200, ge=20, le=1000)
    volatility_sessions: int = Field(default=20, ge=5, le=252)
    liquidity_sessions: int = Field(default=60, ge=5, le=504)
    stress_smoothing_sessions: int = Field(default=5, ge=1, le=60)
    tail_loss_threshold: float = Field(default=-0.05, ge=-0.30, le=-0.005)
    r3_interval_sessions: int = Field(default=10, ge=1, le=60)
    down_confirmation_sessions: int = Field(default=1, ge=1, le=20)
    up_confirmation_sessions: int = Field(default=3, ge=1, le=20)
    hysteresis_score: float = Field(default=3.0, ge=0, le=20)
    train_lookback_sessions: int = Field(default=756, ge=20, le=2520)
    fixed_exposure: float = Field(default=0.65, ge=0, le=1)
    kelly_lookback_sessions: int = Field(default=756, ge=20, le=2520)
    kelly_min_sessions: int = Field(default=252, ge=20, le=1260)
    kelly_update_sessions: int = Field(default=63, ge=1, le=252)
    kelly_fraction: float = Field(default=0.50, gt=0, le=1)
    kelly_initial_exposure: float = Field(default=0.65, ge=0, le=1)
    kelly_min_exposure: float = Field(default=0.20, ge=0, le=1)
    kelly_max_exposure: float = Field(default=1.00, ge=0, le=1)
    kelly_drawdown_limit: float = Field(default=0.30, gt=0, le=1)
    kelly_exposure_step: float = Field(default=0.05, gt=0, le=1)
    cash_annual_yield: float = Field(default=0.0, ge=0, le=0.20)

    @model_validator(mode="after")
    def valid_overlay(self) -> RiskOverlaySpec:
        if self.percentile_min_sessions > self.percentile_window_sessions:
            raise ValueError("risk percentile minimum cannot exceed its window")
        if len(self.levels) != 7 or [item.level_id for item in self.levels] != [f"M{i}" for i in range(7)]:
            raise ValueError("risk levels must contain M0 through M6 in order")
        for index, item in enumerate(self.levels):
            if item.score_max <= item.score_min:
                raise ValueError(f"{item.level_id} score maximum must exceed its minimum")
            if index and not math.isclose(item.score_min, self.levels[index - 1].score_max, abs_tol=1e-9):
                raise ValueError("risk level score ranges must be contiguous without overlap")
            if index and item.exposure > self.levels[index - 1].exposure:
                raise ValueError("higher risk levels cannot have higher exposure")
        if self.levels[0].score_min != 0 or self.levels[-1].score_max != 100:
            raise ValueError("risk levels must cover score zero through one hundred")
        if self.kelly_min_sessions > self.kelly_lookback_sessions:
            raise ValueError("Kelly minimum history cannot exceed its lookback")
        if self.kelly_min_exposure > self.kelly_max_exposure:
            raise ValueError("Kelly minimum exposure cannot exceed its maximum")
        if not self.kelly_min_exposure <= self.kelly_initial_exposure <= self.kelly_max_exposure:
            raise ValueError("Kelly initial exposure must stay inside its minimum and maximum")
        return self


def _realized_maximum_drawdown(returns: list[float]) -> float:
    wealth = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        wealth *= 1 + value
        peak = max(peak, wealth)
        maximum_drawdown = min(maximum_drawdown, wealth / peak - 1)
    return maximum_drawdown


def build_rolling_kelly_schedule(
    daily: list[dict[str, object]],
    spec: RiskOverlaySpec,
) -> tuple[float, dict[date, float], list[dict[str, object]]]:
    """Estimate fractional Kelly from trailing R0 returns for the next session.

    Each observation available through signal day T may be used.  The caller
    applies the resulting target on T+1.  A trailing drawdown cap and exposure
    step make the unconstrained Kelly estimate usable as a long-only risk
    budget rather than a leverage recommendation.
    """
    initial = spec.kelly_initial_exposure
    schedule: dict[date, float] = {}
    changes: list[dict[str, object]] = []
    previous = initial
    cash_daily_return = (1 + spec.cash_annual_yield) ** (1 / 252) - 1
    realized_returns = [float(row["daily_return"]) for row in daily]

    first_signal_index = spec.kelly_min_sessions
    for index in range(first_signal_index, len(daily) - 1, spec.kelly_update_sessions):
        start_index = max(1, index - spec.kelly_lookback_sessions + 1)
        trailing = realized_returns[start_index:index + 1]
        if len(trailing) < spec.kelly_min_sessions:
            continue
        mean_return = fmean(trailing)
        variance = pvariance(trailing)
        excess_mean = mean_return - cash_daily_return
        if variance > 0:
            raw_kelly = max(0.0, excess_mean / variance)
        else:
            raw_kelly = spec.kelly_max_exposure if excess_mean > 0 else 0.0
        fractional_kelly = raw_kelly * spec.kelly_fraction
        historical_drawdown = _realized_maximum_drawdown(trailing)
        drawdown_cap = (
            spec.kelly_drawdown_limit / abs(historical_drawdown)
            if historical_drawdown < 0
            else spec.kelly_max_exposure
        )
        unconstrained = min(fractional_kelly, drawdown_cap, spec.kelly_max_exposure)
        stepped = math.floor((unconstrained + 1e-12) / spec.kelly_exposure_step) * spec.kelly_exposure_step
        target = min(
            spec.kelly_max_exposure,
            max(spec.kelly_min_exposure, stepped),
        )
        target = round(target, 10)
        signal_date = date.fromisoformat(str(daily[index]["session"]))
        if not math.isclose(target, previous, abs_tol=1e-12):
            schedule[signal_date] = target
            changes.append(
                {
                    "signal_session": signal_date.isoformat(),
                    "risk_score": None,
                    "from_exposure": previous,
                    "to_exposure": target,
                    "to_level": None,
                    "observations": len(trailing),
                    "annualized_mean_return": mean_return * 252,
                    "annualized_volatility": math.sqrt(variance * 252),
                    "historical_maximum_drawdown": historical_drawdown,
                    "raw_kelly": raw_kelly,
                    "fractional_kelly": fractional_kelly,
                    "drawdown_cap": drawdown_cap,
                }
            )
            previous = target
    return initial, schedule, changes


def _rolling_percentile(series: pd.Series, window: int, minimum: int) -> pd.Series:
    def rank_last(values: np.ndarray) -> float:
        current = values[-1]
        valid = values[np.isfinite(values)]
        if not np.isfinite(current) or not len(valid):
            return np.nan
        return float(100 * ((valid < current).sum() + 0.5 * (valid == current).sum()) / len(valid))

    return series.rolling(window, min_periods=minimum).apply(rank_last, raw=True)


def _level_index(score: float, levels: tuple[RiskLevel, ...]) -> int:
    for index, level in enumerate(levels):
        if level.score_min <= score < level.score_max or (index == len(levels) - 1 and score <= level.score_max):
            return index
    raise ValueError(f"risk score outside configured levels: {score}")


def build_risk_scores(
    connection: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    spec: RiskOverlaySpec,
    cache_dir: Path | None = None,
    source_fingerprint: str = "",
) -> pd.DataFrame:
    cache_path = None
    if cache_dir is not None:
        score_config = spec.model_dump(mode="json")
        for key in (
            "experiment_variant", "r3_interval_sessions", "down_confirmation_sessions",
            "up_confirmation_sessions", "hysteresis_score", "fixed_exposure", "cash_annual_yield",
            "kelly_lookback_sessions", "kelly_min_sessions", "kelly_update_sessions",
            "kelly_fraction", "kelly_initial_exposure", "kelly_min_exposure",
            "kelly_max_exposure", "kelly_drawdown_limit", "kelly_exposure_step",
        ):
            score_config.pop(key, None)
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "engine_version": "1.1.0",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "source": source_fingerprint,
                    "score_config": score_config,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"risk-score-{cache_key}.csv"
        if cache_path.exists():
            cached = pd.read_csv(cache_path, parse_dates=["trade_date"])
            if not cached.empty:
                return cached
    longest_raw_lookback = max(
        spec.breadth_return_sessions,
        spec.trend_sessions,
        spec.volatility_sessions,
        spec.liquidity_sessions,
        spec.stress_smoothing_sessions,
    )
    # R6 needs a complete trailing exposure window at the first evaluation
    # date.  Exposure itself is only available after raw-indicator warm-up and
    # the rolling-percentile minimum, so all three spans are required.
    history = longest_raw_lookback + spec.percentile_min_sessions + spec.train_lookback_sessions + 10
    warmup = connection.execute(
        """SELECT min(cal_date) FROM (
        SELECT cal_date FROM research.trading_calendar
        WHERE exchange='SSE' AND is_open AND cal_date <= ?
        ORDER BY cal_date DESC LIMIT ?)""",
        [start, history],
    ).fetchone()[0]
    frame = connection.execute(
        f"""WITH base AS (
          SELECT m.ts_code, m.trade_date, m.close / m.pre_close - 1.0 AS ret,
                 ln(m.close / m.pre_close) AS log_ret, m.amount_cny
          FROM research.market_daily m
          JOIN research.universe_daily u USING (trade_date, ts_code)
          WHERE m.trade_date BETWEEN ? AND ? AND m.close > 0 AND m.pre_close > 0
            AND m.is_valid_ohlc AND u.has_market_bar
        ), rolling AS (
          SELECT *, sum(log_ret) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN
            {spec.breadth_return_sessions - 1} PRECEDING AND CURRENT ROW) AS ret_n,
            count(*) OVER (PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN
            {spec.breadth_return_sessions - 1} PRECEDING AND CURRENT ROW) AS obs_n
          FROM base
        )
        SELECT trade_date, avg(ret) AS market_return,
          avg(CASE WHEN obs_n={spec.breadth_return_sessions} THEN CAST(ret_n > 0 AS DOUBLE) END) AS breadth_positive,
          sum(amount_cny) AS total_amount_cny,
          avg(CAST(ret <= ? AS DOUBLE)) AS tail_loss_share
        FROM rolling GROUP BY trade_date ORDER BY trade_date""",
        [warmup, end, spec.tail_loss_threshold],
    ).df()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["market_index"] = (1 + frame["market_return"].fillna(0)).cumprod()
    ma = frame["market_index"].rolling(spec.trend_sessions, min_periods=spec.trend_sessions).mean()
    amount_median = frame["total_amount_cny"].rolling(
        spec.liquidity_sessions, min_periods=spec.liquidity_sessions
    ).median()
    raw = {
        "breadth": 1 - frame["breadth_positive"],
        "trend": -(frame["market_index"] / ma - 1),
        "volatility": frame["market_return"].rolling(
            spec.volatility_sessions, min_periods=spec.volatility_sessions
        ).std(ddof=1) * math.sqrt(252),
        "liquidity": -np.log(frame["total_amount_cny"] / amount_median),
        "stress_tail": frame["tail_loss_share"].rolling(
            spec.stress_smoothing_sessions, min_periods=spec.stress_smoothing_sessions
        ).mean(),
    }
    score = pd.Series(0.0, index=frame.index)
    for name, values in raw.items():
        dimension = _rolling_percentile(
            values, spec.percentile_window_sessions, spec.percentile_min_sessions
        )
        frame[f"{name}_score"] = dimension
        score += dimension * getattr(spec.weights, name)
    frame["risk_score"] = score
    frame["level_index"] = frame["risk_score"].map(
        lambda value: _level_index(float(value), spec.levels) if pd.notna(value) else np.nan
    )
    frame["raw_exposure"] = frame["level_index"].map(
        lambda value: spec.levels[int(value)].exposure if pd.notna(value) else np.nan
    )
    evaluation = frame.loc[frame["trade_date"].dt.date >= start]
    if evaluation.empty or pd.isna(evaluation.iloc[0]["risk_score"]):
        raise ValueError("insufficient point-in-time history to form Risk Score at backtest start")
    frame = frame.reset_index(drop=True)
    if cache_path is not None:
        temporary = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
        frame.to_csv(temporary, index=False)
        temporary.replace(cache_path)
    return frame


def build_exposure_schedule(
    frame: pd.DataFrame,
    spec: RiskOverlaySpec,
    start: date,
    end: date,
) -> tuple[float, dict[date, float], list[dict[str, object]]]:
    if spec.experiment_variant == "R0":
        return 1.0, {}, []
    if spec.experiment_variant == "R7":
        return spec.fixed_exposure, {}, []
    frame = frame.loc[frame["risk_score"].notna() & frame["level_index"].notna()].reset_index(drop=True)
    if frame.empty:
        raise ValueError("no valid Risk Score observations are available")
    targets = frame["raw_exposure"].astype(float).copy()
    signal_mask = pd.Series(True, index=frame.index)
    if spec.experiment_variant == "R2":
        weeks = frame["trade_date"].dt.to_period("W-FRI")
        signal_mask = weeks.ne(weeks.shift(-1))
    elif spec.experiment_variant == "R3":
        # The interval is anchored to the requested backtest, rather than to
        # however many warm-up rows happened to be loaded before it.
        signal_mask = pd.Series(False, index=frame.index)
    elif spec.experiment_variant == "R4":
        current = int(frame.iloc[0]["level_index"])
        candidate: int | None = None
        count = 0
        filtered: list[float] = []
        for row in frame.itertuples(index=False):
            desired = int(row.level_index)
            score = float(row.risk_score)
            if desired > current:
                while (
                    desired > current
                    and score < spec.levels[desired].score_min + spec.hysteresis_score
                ):
                    desired -= 1
            elif desired < current and score >= spec.levels[current].score_min - spec.hysteresis_score:
                desired = current
            if desired == current:
                candidate, count = None, 0
            else:
                if candidate == desired:
                    count += 1
                else:
                    candidate, count = desired, 1
                needed = spec.down_confirmation_sessions if desired > current else spec.up_confirmation_sessions
                if count >= needed:
                    current = desired
                    candidate, count = None, 0
            filtered.append(spec.levels[current].exposure)
        targets = pd.Series(filtered, index=frame.index)
    elif spec.experiment_variant == "R6":
        rolling = targets.rolling(
            spec.train_lookback_sessions, min_periods=spec.train_lookback_sessions
        ).mean()
        if rolling.isna().all():
            raise ValueError("R6 has insufficient prior sessions for its train lookback")
        quarters = frame["trade_date"].dt.to_period("Q")
        completed_quarter_end = quarters.ne(quarters.shift(-1)) & quarters.shift(-1).notna()
        targets = rolling.where(completed_quarter_end).ffill()
        signal_mask = completed_quarter_end
    eligible = (frame["trade_date"].dt.date >= start) & (frame["trade_date"].dt.date <= end)
    frame = frame.loc[eligible].reset_index(drop=True)
    targets = targets.loc[eligible].reset_index(drop=True)
    signal_mask = signal_mask.loc[eligible].reset_index(drop=True)
    if spec.experiment_variant == "R3":
        signal_mask[:] = False
        signal_mask.iloc[:: spec.r3_interval_sessions] = True
    if spec.experiment_variant == "R5":
        targets[:] = float(targets.mean())
    if targets.empty or pd.isna(targets.iloc[0]):
        raise ValueError("insufficient point-in-time history to form the selected risk variant")
    initial = float(targets.iloc[0])
    changes: list[dict[str, object]] = []
    schedule: dict[date, float] = {}
    previous = initial
    for index, row in frame.iterrows():
        target = float(targets.iloc[index])
        if bool(signal_mask.iloc[index]) and not math.isclose(target, previous, abs_tol=1e-12):
            signal_date = row["trade_date"].date()
            schedule[signal_date] = target
            changes.append(
                {
                    "signal_session": signal_date.isoformat(),
                    "risk_score": float(row["risk_score"]),
                    "from_exposure": previous,
                    "to_exposure": target,
                    "to_level": (
                        None
                        if spec.experiment_variant in {"R5", "R6"}
                        else spec.levels[int(row["level_index"])].level_id
                    ),
                }
            )
            previous = target
    return initial, schedule, changes
