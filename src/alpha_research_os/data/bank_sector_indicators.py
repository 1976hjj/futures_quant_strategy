"""Date-level bank indicators, consuming bounded immutable feature inputs only."""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha_research_os.factors.bank_timing import bank_timing_catalog


def historical_percentiles(panel, calendar, window=756, minimum=504):
    parts = []
    for code, group in panel.groupby("instrument_id", sort=True):
        indexed = group.set_index("session").reindex(calendar)
        result = pd.DataFrame({"session": calendar, "instrument_id": code})
        for source, name in (("book_to_price", "pb_percentile"), ("cash_yield365", "yield_percentile"),
                             ("annual_earnings_yield", "earnings_percentile")):
            values = pd.to_numeric(indexed[source], errors="coerce").where(lambda value: np.isfinite(value))
            if source == "book_to_price":
                values = 1 / values.where(values > 0)

            def rank(array):
                current = array[-1]
                valid = array[np.isfinite(array)]
                if not np.isfinite(current) or len(valid) < minimum:
                    return np.nan
                return (np.sum(valid < current) + .5 * np.sum(valid == current)) / len(valid)

            result[name] = values.rolling(window, min_periods=minimum).apply(rank, raw=True).to_numpy()
        parts.append(result)
    return pd.concat(parts, ignore_index=True)


def calculate_sector_indicators(history, technical, basket, benchmark, readiness, calendar, config, selected=None,
                                pb_history=None, valuation_history=None):
    pb_ids = {item.factor_id for item in bank_timing_catalog() if "pb_daily" in item.required_fields}
    requested = readiness if not selected else readiness[readiness.factor_id.isin(selected)]
    if pb_history is None and requested.factor_id.isin(pb_ids).any():
        raise ValueError("PB板块指标需要日PB历史，请重新准备依赖；不能复用旧普通股账面市值比输入。")
    valuation = history if valuation_history is None else valuation_history[
        valuation_history.session <= max(calendar)]
    valuation_calendar = sorted(set(calendar) | set(valuation.session))
    percentiles = historical_percentiles(valuation, valuation_calendar, config["history_sessions"],
                                         config["minimum_history_sessions"])
    trend = technical.sort_values(["instrument_id", "session"]).copy()
    trend["trend_sma"] = trend.groupby(["instrument_id", "total_return_segment"])["bank_total_return_index"].transform(
        lambda values: values.rolling(config["trend_sessions"], min_periods=config["trend_sessions"]).mean()
    )
    if pb_history is not None:
        from alpha_research_os.data.bank_timing import daily_pb_history_statistics

        pb = daily_pb_history_statistics(pb_history, calendar, config["history_sessions"],
                                         config["minimum_history_sessions"])
        percentiles = percentiles.drop(columns="pb_percentile").merge(
            pb[["session", "instrument_id", "pb_percentile"]], on=["session", "instrument_id"],
            how="left", validate="one_to_one")
    panel = history.drop(columns="pb_percentile", errors="ignore").merge(
        percentiles, on=["session", "instrument_id"], validate="one_to_one")
    panel = panel.merge(trend[["session", "instrument_id", "trend_sma"]],
                        on=["session", "instrument_id"], validate="one_to_one")
    relative = basket[["session", "basket_segment", "bank_equal_total_return_index"]].merge(
        benchmark[["session", "benchmark_segment", "gross_total_return_index"]], on="session", validate="one_to_one"
    ).sort_values("session")
    lag = config["relative_strength_sessions"]
    paired = ((relative.basket_segment == relative.basket_segment.shift(lag))
              & (relative.benchmark_segment == relative.benchmark_segment.shift(lag)))
    relative["relative_return"] = (
        relative.bank_equal_total_return_index / relative.bank_equal_total_return_index.shift(lag)
        - relative.gross_total_return_index / relative.gross_total_return_index.shift(lag)
    ).where(paired)
    relative_lookup = relative.set_index("session").relative_return.to_dict()
    groups = dict(tuple(panel.groupby("session", sort=True)))
    records = []
    for row in readiness.sort_values(["session", "factor_id"]).itertuples():
        if selected and row.factor_id not in selected:
            continue
        group = groups[row.session]
        pb, dy = group.pb_percentile, group.yield_percentile
        both = pb.notna() & dy.notna()
        joint = both & (pb <= .2) & (dy >= .8)
        components = {}
        key = row.factor_id.removeprefix("bank-sector-")
        value = None
        good = bool(row.data_ready)
        if good:
            if key in ("pb-history-percentile", "dividend-history-percentile", "earnings-yield-history-percentile"):
                series = (pb if key.startswith("pb-") else dy if key.startswith("dividend-")
                          else group.earnings_percentile)
                value = series.mean()
                components = {"mean": value, "median": series.median()}
            elif key == "cheap-high-yield-breadth":
                value = joint.sum() / both.sum()
                components = {"breadth": value, "valid_count": int(both.sum())}
            elif key == "quality-cheap-high-yield-breadth":
                valid = both & (group.quality_gate == 1)
                value = (joint & valid).sum() / valid.sum()
                components = {"breadth": value, "quality_eligible_count": int(valid.sum())}
            elif key == "nim-deterioration-breadth":
                values = group.nim_change.dropna()
                value = (values < 0).mean()
                components = {"breadth": value, "median_change_pp": values.median()}
            elif key == "trend-breadth":
                valid = group.trend_sma.notna() & group.bank_total_return_index.notna()
                value = (group.loc[valid, "bank_total_return_index"] > group.loc[valid, "trend_sma"]).mean()
                components = {"breadth": value}
            elif key == "relative-strength":
                value = relative_lookup.get(row.session)
                components = {"relative_return": value}
            elif key == "valuation-dispersion":
                value = pb.dropna().quantile(.75) - pb.dropna().quantile(.25)
                components = {"pb_iqr": value, "yield_iqr": dy.dropna().quantile(.75) - dy.dropna().quantile(.25)}
            elif key == "operating-deterioration":
                for field, name in (("profit_growth", "profit_breadth"), ("nim_change", "nim_breadth"),
                                    ("npl_improvement", "npl_breadth"), ("provision_change_yoy", "provision_breadth"),
                                    ("cet1_change_yoy", "cet1_breadth")):
                    components[name] = (group[field].dropna() < 0).mean()
                value = np.mean(list(components.values()))
                components["score"] = value
        good = good and value is not None and np.isfinite(value)
        records.append({"session": row.session, "sector_id": "BANK", "factor_id": row.factor_id,
                        "factor_version": row.factor_version, "value": float(value) if good else None,
                        "status": "READY_RESEARCH_ONLY" if good else "INSUFFICIENT_DATA",
                        "reason": "" if good else row.reason or "CALCULATION_INPUT_UNAVAILABLE",
                        "reason_detail": "" if good else getattr(row, "reason_detail", ""),
                        "coverage": row.coverage, "valid_count": row.valid_count, "universe_count": row.universe_count,
                        "denominator_count": int(getattr(row, 'denominator_count', row.universe_count)),
                        "coverage_basis": getattr(row, 'coverage_basis', 'bank_universe'),
                        **{name: float(number) if np.isfinite(number) else None for name, number in components.items()
                           if number is not None}})
    result = pd.DataFrame(records)
    expected = {item.factor_id for item in bank_timing_catalog()}
    if result.duplicated(["session", "factor_id"]).any() or not set(result.factor_id).issubset(expected):
        raise ValueError("Sector indicator key validation failed")
    return result
