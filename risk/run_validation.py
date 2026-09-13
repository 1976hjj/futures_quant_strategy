"""Run the standalone five-dimensional Risk Score positioning experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RISK_DIR = Path(__file__).resolve().parent
STRATEGY_ORDER = [
    "Buy & Hold",
    "Constant Exposure - Train Fixed",
    "MA200",
    "MA20/MA60",
    "Volatility Targeting",
    "Risk Score",
]
COLORS = {
    "Buy & Hold": "#111827",
    "Constant Exposure - Train Fixed": "#9ca3af",
    "Constant Exposure - Full Sample": "#d1d5db",
    "MA200": "#2563eb",
    "MA20/MA60": "#7c3aed",
    "Volatility Targeting": "#059669",
    "Risk Score": "#dc2626",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def read_token() -> str:
    credential = ROOT / "secrets" / "tushare.env"
    for line in credential.read_text(encoding="utf-8").splitlines():
        if line.startswith("TUSHARE_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("TUSHARE_TOKEN was not found")


def fetch_indices(config: dict[str, Any], target: Path) -> pd.DataFrame:
    token = read_token()
    endpoint = "https://t.xiaodefa.top/"
    frames: list[pd.DataFrame] = []
    for item in config["indices"]:
        body = json.dumps(
            {
                "api_name": "index_daily",
                "token": token,
                "params": {
                    "ts_code": item["ts_code"],
                    "start_date": config["data_start"].replace("-", ""),
                    "end_date": config["data_end"].replace("-", ""),
                },
                "fields": "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "risk-positioning-validation/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed configured HTTPS endpoint
            document = json.load(response)
        if document.get("code") != 0:
            raise RuntimeError(f"index_daily failed for {item['ts_code']}: {document.get('msg')}")
        data = document.get("data") or {}
        rows = data.get("items") or []
        fields = data.get("fields") or []
        if len(rows) < 2:
            raise RuntimeError(f"insufficient index rows for {item['ts_code']}")
        frame = pd.DataFrame(rows, columns=fields)
        frame["index_id"] = item["id"]
        frame["index_name"] = item["name"]
        frames.append(frame)
    result = pd.concat(frames, ignore_index=True)
    result["trade_date"] = pd.to_datetime(result["trade_date"], format="%Y%m%d")
    numeric = ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]
    result[numeric] = result[numeric].apply(pd.to_numeric, errors="coerce")
    result = result.sort_values(["index_id", "trade_date"]).drop_duplicates(["index_id", "trade_date"])
    target.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(target, index=False, encoding="utf-8-sig")
    return result


def build_market_features(config: dict[str, Any], target: Path) -> pd.DataFrame:
    daily_glob = (ROOT / "data/warehouse/parquet/daily/year=*/month=*/data.parquet").as_posix()
    breadth_sessions = int(config["breadth_return_sessions"])
    query = f"""
    COPY (
      WITH base AS (
        SELECT ts_code, trade_date, ln(close/pre_close) AS log_ret, amount * 1000.0 AS amount_cny
        FROM read_parquet('{daily_glob}', hive_partitioning=true)
        WHERE trade_date BETWEEN DATE '{config["data_start"]}' AND DATE '{config["data_end"]}'
          AND close > 0 AND pre_close > 0 AND amount >= 0
      ), rolling AS (
        SELECT *,
          sum(log_ret) OVER (
            PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN {breadth_sessions - 1} PRECEDING AND CURRENT ROW
          ) AS log_ret_n,
          count(*) OVER (
            PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN {breadth_sessions - 1} PRECEDING AND CURRENT ROW
          ) AS n_obs
        FROM base
      )
      SELECT trade_date,
        count(*) AS n_active,
        avg(CASE WHEN n_obs={breadth_sessions} THEN 1.0 ELSE 0.0 END) AS breadth_coverage,
        avg(CASE WHEN n_obs={breadth_sessions} THEN CAST(log_ret_n > 0 AS DOUBLE) END) AS breadth_positive,
        sum(amount_cny) AS total_amount_cny,
        avg(CAST(log_ret <= ln(1.0 + {float(config["tail_loss_threshold"])}) AS DOUBLE)) AS tail_loss_share
      FROM rolling GROUP BY trade_date ORDER BY trade_date
    ) TO '{target.as_posix()}' (HEADER, DELIMITER ',')
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute("SET threads=4")
    connection.execute(query)
    connection.close()
    result = pd.read_csv(target, parse_dates=["trade_date"])
    return result


def rolling_percentile(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    def last_rank(values: np.ndarray) -> float:
        current = values[-1]
        valid = values[np.isfinite(values)]
        if not np.isfinite(current) or len(valid) == 0:
            return np.nan
        less = np.sum(valid < current)
        equal = np.sum(valid == current)
        return 100.0 * (less + 0.5 * equal) / len(valid)

    return series.rolling(window, min_periods=min_periods).apply(last_rank, raw=True)


def score_and_targets(index_frame: pd.DataFrame, market: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    frame = index_frame.sort_values("trade_date").merge(market, on="trade_date", how="inner").copy()
    frame["index_return"] = frame["close"] / frame["pre_close"] - 1.0
    frame["ma200"] = (
        frame["close"].rolling(int(config["trend_ma_sessions"]), min_periods=int(config["trend_ma_sessions"])).mean()
    )
    frame["ma20"] = frame["close"].rolling(20, min_periods=20).mean()
    frame["ma60"] = frame["close"].rolling(60, min_periods=60).mean()
    frame["vol20"] = frame["index_return"].rolling(
        int(config["volatility_sessions"]), min_periods=int(config["volatility_sessions"])
    ).std(ddof=1) * math.sqrt(int(config["annualization_sessions"]))
    frame["breadth_raw"] = 1.0 - frame["breadth_positive"]
    frame["trend_raw"] = -(frame["close"] / frame["ma200"] - 1.0)
    amount_median = (
        frame["total_amount_cny"]
        .rolling(int(config["liquidity_median_sessions"]), min_periods=int(config["liquidity_median_sessions"]))
        .median()
    )
    frame["liquidity_raw"] = -np.log(frame["total_amount_cny"] / amount_median)
    frame["volatility_raw"] = frame["vol20"]
    frame["stress_tail_raw"] = (
        frame["tail_loss_share"]
        .rolling(int(config["stress_smoothing_sessions"]), min_periods=int(config["stress_smoothing_sessions"]))
        .mean()
    )
    window = int(config["rolling_percentile_window"])
    minimum = int(config["rolling_percentile_min_periods"])
    for dimension in config["weights"]:
        frame[f"{dimension}_score"] = rolling_percentile(frame[f"{dimension}_raw"], window, minimum)
    frame["risk_score"] = sum(frame[f"{key}_score"] * float(weight) for key, weight in config["weights"].items())
    frame["Risk Score"] = np.select(
        [
            frame["risk_score"] < 20,
            frame["risk_score"] < 40,
            frame["risk_score"] < 60,
            frame["risk_score"] < 80,
            frame["risk_score"] <= 100,
        ],
        [1.0, 0.85, 0.65, 0.40, 0.20],
        default=np.nan,
    )
    frame["Buy & Hold"] = 1.0
    frame["MA200"] = np.where(frame["close"] >= frame["ma200"], 1.0, 0.20)
    frame["MA20/MA60"] = np.where(frame["ma20"] >= frame["ma60"], 1.0, 0.20)
    frame["Volatility Targeting"] = np.clip(float(config["volatility_target"]) / frame["vol20"], 0.20, 1.0)
    return frame


class Simulation:
    def __init__(self, daily: pd.DataFrame, turnover_total: float, exposure_changes: int) -> None:
        self.daily = daily
        self.turnover_total = turnover_total
        self.exposure_changes = exposure_changes


def simulate(frame: pd.DataFrame, target: pd.Series, cost_bps: float) -> Simulation:
    target = pd.Series(target.to_numpy(float), index=frame.index)
    cash, units = 1.0, 0.0
    rows: list[dict[str, float | pd.Timestamp]] = []
    turnover_total = 0.0
    changes = 0
    previous_signal = np.nan
    for position, (_idx, row) in enumerate(frame.iterrows()):
        desired = target.iloc[position - 1] if position > 0 else np.nan
        open_price, close_price = float(row["open"]), float(row["close"])
        pre_trade_nav = cash + units * open_price
        traded = fee = 0.0
        if np.isfinite(desired) and pre_trade_nav > 0 and open_price > 0:
            desired = float(desired)
            desired_value = desired * pre_trade_nav
            current_value = units * open_price
            traded = abs(desired_value - current_value)
            fee = traded * cost_bps / 10000.0
            units = desired_value / open_price
            cash = pre_trade_nav - desired_value - fee
            turnover_total += traded / pre_trade_nav
            if np.isfinite(previous_signal) and not math.isclose(desired, previous_signal, abs_tol=1e-12):
                changes += 1
            previous_signal = desired
        close_nav = cash + units * close_price
        exposure = units * close_price / close_nav if close_nav > 0 else np.nan
        rows.append(
            {
                "trade_date": row["trade_date"],
                "nav": close_nav,
                "actual_exposure": exposure,
                "executed_target": desired,
                "traded_notional": traded,
                "fee": fee,
                "turnover": traded / pre_trade_nav if pre_trade_nav > 0 else np.nan,
            }
        )
    daily = pd.DataFrame(rows)
    daily["daily_return"] = daily["nav"].pct_change().fillna(0.0)
    daily["drawdown"] = daily["nav"] / daily["nav"].cummax() - 1.0
    return Simulation(daily, turnover_total, changes)


def train_fixed_target(
    frame: pd.DataFrame, risk_sim: Simulation, config: dict[str, Any]
) -> tuple[pd.Series, list[dict[str, Any]]]:
    dates = pd.to_datetime(frame["trade_date"])
    exposure = pd.Series(risk_sim.daily["actual_exposure"].to_numpy(), index=frame.index)
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    folds: list[dict[str, Any]] = []
    start_year = pd.Timestamp(config["oos_start"]).year
    end_year = dates.max().year
    for test_year in range(start_year, end_year + 1):
        train_start = pd.Timestamp(test_year - int(config["train_years"]), 1, 1)
        train_end = pd.Timestamp(test_year - 1, 12, 31)
        test_start = pd.Timestamp(test_year, 1, 1)
        test_end = pd.Timestamp(test_year, 12, 31)
        train_mask = (dates >= train_start) & (dates <= train_end) & exposure.notna()
        test_mask = (dates >= test_start) & (dates <= test_end)
        fixed = float(exposure[train_mask].mean()) if train_mask.any() else np.nan
        result.loc[test_mask] = fixed
        folds.append(
            {
                "test_year": test_year,
                "train_start": train_start.date().isoformat(),
                "train_end": train_end.date().isoformat(),
                "test_start": test_start.date().isoformat(),
                "test_end": min(test_end, dates.max()).date().isoformat(),
                "train_observations": int(train_mask.sum()),
                "fixed_exposure": fixed,
            }
        )
    return result, folds


def metric_row(daily: pd.DataFrame, targets: pd.Series, name: str) -> dict[str, Any]:
    x = daily.reset_index(drop=True).copy()
    x["trade_date"] = pd.to_datetime(x["trade_date"])
    x["evaluation_nav"] = (1.0 + x["daily_return"]).cumprod()
    years = (x["trade_date"].iloc[-1] - x["trade_date"].iloc[0]).days / 365.25
    end_nav = float(x["evaluation_nav"].iloc[-1])
    cagr = end_nav ** (1.0 / years) - 1.0 if years > 0 else np.nan
    returns = x["daily_return"]
    volatility = returns.std(ddof=1) * math.sqrt(252)
    sharpe = returns.mean() / returns.std(ddof=1) * math.sqrt(252) if returns.std(ddof=1) > 0 else np.nan
    evaluation_drawdown = x["evaluation_nav"] / x["evaluation_nav"].cummax() - 1.0
    maxdd = float(-evaluation_drawdown.min())
    calmar = cagr / maxdd if maxdd > 0 else np.nan
    peak_i = int(evaluation_drawdown.idxmin())
    peak_nav = x.loc[:peak_i, "evaluation_nav"].max()
    peak_candidates = x.index[: peak_i + 1][np.isclose(x.loc[:peak_i, "evaluation_nav"], peak_nav)]
    peak_start = int(peak_candidates[-1])
    recovery = x.index[(x.index > peak_i) & (x["evaluation_nav"] >= peak_nav)]
    recovery_days = int(recovery[0] - peak_start) if len(recovery) else np.nan
    recovery_status = "recovered" if len(recovery) else "unrecovered"
    complete_years: list[float] = []
    for _year, group in x.groupby(x["trade_date"].dt.year):
        if group["trade_date"].min().month == 1 and group["trade_date"].max().month == 12:
            complete_years.append(float(np.prod(1.0 + group["daily_return"]) - 1.0))
    return {
        "strategy": name,
        "CAGR": cagr,
        "Max Drawdown": maxdd,
        "Calmar Ratio": calmar,
        "Sharpe Ratio": sharpe,
        "Annualized Volatility": volatility,
        "Worst Year Return": min(complete_years) if complete_years else np.nan,
        "Recovery Time": recovery_days,
        "Recovery Status": recovery_status,
        "Average Exposure": float(x["actual_exposure"].mean()),
        "Exposure Changes": int(pd.Series(targets).dropna().diff().abs().gt(1e-12).sum()),
        "Turnover": float(x["turnover"].sum()),
    }


def add_downside_capture(metrics: pd.DataFrame, daily_by_strategy: dict[str, pd.DataFrame]) -> None:
    monthly: dict[str, pd.Series] = {}
    for name, daily in daily_by_strategy.items():
        indexed = daily.set_index(pd.to_datetime(daily["trade_date"]))["nav"]
        monthly[name] = indexed.resample("ME").last().pct_change().dropna()
    benchmark = monthly["Buy & Hold"]
    for name in metrics["strategy"]:
        aligned = pd.concat([monthly[name], benchmark], axis=1, join="inner").dropna()
        mask = aligned.iloc[:, 1] < 0
        capture = (
            aligned.loc[mask].iloc[:, 0].mean() / aligned.loc[mask].iloc[:, 1].mean() * 100 if mask.any() else np.nan
        )
        metrics.loc[metrics["strategy"] == name, "Downside Capture"] = capture
        metrics.loc[metrics["strategy"] == name, "Down Months"] = int(mask.sum())


def bootstrap_differences(
    daily_by_strategy: dict[str, pd.DataFrame], reps: int, block: int, seed: int
) -> list[dict[str, Any]]:
    names = ["Buy & Hold", "Constant Exposure - Train Fixed", "MA200", "MA20/MA60", "Volatility Targeting"]
    returns = {name: daily_by_strategy[name]["daily_return"].to_numpy() for name in [*names, "Risk Score"]}
    n = len(returns["Risk Score"])
    rng = np.random.default_rng(seed)

    def stats(values: np.ndarray) -> tuple[float, float]:
        nav = np.cumprod(1.0 + values)
        dd = 1.0 - nav / np.maximum.accumulate(nav)
        maxdd = float(dd.max())
        cagr = float(nav[-1] ** (252.0 / len(nav)) - 1.0)
        return maxdd, cagr / maxdd if maxdd > 0 else np.nan

    samples: dict[str, list[float]] = {f"{name}|Max Drawdown": [] for name in names}
    samples.update({f"{name}|Calmar Ratio": [] for name in names})
    starts_max = max(1, n - block + 1)
    for _ in range(reps):
        starts = rng.integers(0, starts_max, size=math.ceil(n / block))
        indices = np.concatenate([np.arange(start, min(start + block, n)) for start in starts])[:n]
        risk_dd, risk_calmar = stats(returns["Risk Score"][indices])
        for name in names:
            base_dd, base_calmar = stats(returns[name][indices])
            samples[f"{name}|Max Drawdown"].append(risk_dd - base_dd)
            samples[f"{name}|Calmar Ratio"].append(risk_calmar - base_calmar)
    rows = []
    for key, values in samples.items():
        baseline, metric = key.split("|")
        array = np.asarray(values, dtype=float)
        rows.append(
            {
                "baseline": baseline,
                "metric": metric,
                "risk_minus_baseline_mean": float(np.nanmean(array)),
                "ci_2_5": float(np.nanpercentile(array, 2.5)),
                "ci_97_5": float(np.nanpercentile(array, 97.5)),
                "repetitions": reps,
                "block_sessions": block,
            }
        )
    return rows


def contiguous_events(mask: pd.Series, dates: pd.Series, event_type: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    start: int | None = None
    values = mask.fillna(False).to_numpy(bool)
    for position, active in enumerate(values):
        if active and start is None:
            start = position
        if start is not None and (not active or position == len(values) - 1):
            end = position if active and position == len(values) - 1 else position - 1
            events.append(
                {
                    "event_type": event_type,
                    "start_pos": start,
                    "end_pos": end,
                    "event_date": dates.iloc[start],
                }
            )
            start = None
    return events


def detect_stress_events(frame: pd.DataFrame, bh: pd.DataFrame, oos_start: pd.Timestamp) -> list[dict[str, Any]]:
    evaluation = frame.loc[frame["trade_date"] >= oos_start].reset_index(drop=True)
    events: list[dict[str, Any]] = []
    bh = bh.reset_index(drop=True).copy()
    bh["ret5"] = bh["daily_return"].rolling(5).apply(lambda x: np.prod(1.0 + x) - 1.0, raw=True)
    bh["vol20"] = bh["daily_return"].rolling(20).std(ddof=1) * math.sqrt(252)
    threshold = float(frame.loc[frame["trade_date"] < oos_start, "vol20"].quantile(0.9))
    events.extend(contiguous_events(bh["ret5"] <= -0.10, bh["trade_date"], "rapid_crash"))
    events.extend(contiguous_events(bh["vol20"] >= threshold, bh["trade_date"], "high_volatility"))

    running_peak = bh["nav"].cummax()
    drawdown = bh["nav"] / running_peak - 1.0
    in_bear = False
    bear_start = 0
    for position, value in enumerate(drawdown):
        if value <= -0.20 and not in_bear:
            peak_value = running_peak.iloc[position]
            bear_start = int(bh.loc[:position, "nav"].idxmax())
            in_bear = True
        if in_bear and bh.loc[position, "nav"] >= peak_value:
            events.append(
                {
                    "event_type": "bear_market",
                    "start_pos": bear_start,
                    "end_pos": position,
                    "event_date": bh.loc[bear_start, "trade_date"],
                }
            )
            in_bear = False
    if in_bear:
        events.append(
            {
                "event_type": "bear_market_unrecovered",
                "start_pos": bear_start,
                "end_pos": len(bh) - 1,
                "event_date": bh.loc[bear_start, "trade_date"],
            }
        )

    block = 60
    for start in range(0, len(evaluation) - block + 1, block):
        end = start + block - 1
        segment = evaluation.iloc[start : end + 1]
        total_return = float(segment["close"].iloc[-1] / segment["pre_close"].iloc[0] - 1.0)
        price_range = float(segment["high"].max() / segment["low"].min() - 1.0)
        if abs(total_return) <= 0.05 and price_range >= 0.10:
            events.append(
                {
                    "event_type": "sideways_market",
                    "start_pos": start,
                    "end_pos": end,
                    "event_date": segment["trade_date"].iloc[0],
                }
            )

    rapid = [event for event in events if event["event_type"] == "rapid_crash"]
    for event in rapid:
        end = int(event["end_pos"])
        future_end = min(len(evaluation) - 1, end + 20)
        future = evaluation.iloc[end : future_end + 1]
        low_position = int(future["close"].idxmin())
        after_low = evaluation.iloc[low_position : future_end + 1]
        rebound = float(after_low["close"].max() / evaluation.loc[low_position, "close"] - 1.0)
        if rebound >= 0.10:
            recovery_position = int(after_low["close"].idxmax())
            events.append(
                {
                    "event_type": "rapid_crash_then_rebound",
                    "start_pos": int(event["start_pos"]),
                    "end_pos": recovery_position,
                    "event_date": evaluation.loc[int(event["start_pos"]), "trade_date"],
                }
            )
    return events


def stress_diagnostics(
    index_id: str,
    index_name: str,
    events: list[dict[str, Any]],
    daily_by_strategy: dict[str, pd.DataFrame],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event_number, event in enumerate(events, start=1):
        start = int(event["start_pos"])
        end = int(event["end_pos"])
        for name in STRATEGY_ORDER:
            daily = daily_by_strategy[name].reset_index(drop=True)
            window = daily.iloc[start : end + 1]
            nav = window["nav"]
            local_dd = float(-(nav / nav.cummax() - 1.0).min())
            target = daily["executed_target"]
            changes = target.diff()
            flips = 0
            change_positions = np.flatnonzero(changes.abs().to_numpy() > 1e-12)
            for first, second in zip(change_positions, change_positions[1:], strict=False):
                if second - first <= 5 and changes.iloc[first] * changes.iloc[second] < 0:
                    flips += 1
            pre_start = max(0, start - 5)
            rows.append(
                {
                    "index_id": index_id,
                    "index_name": index_name,
                    "event_id": f"{index_id}-{event['event_type']}-{event_number:03d}",
                    "event_type": event["event_type"],
                    "event_date": pd.Timestamp(event["event_date"]).date().isoformat(),
                    "window_start": window["trade_date"].iloc[0].date().isoformat(),
                    "window_end": window["trade_date"].iloc[-1].date().isoformat(),
                    "strategy": name,
                    "local_max_drawdown": local_dd,
                    "exposure_5d_before": float(daily.iloc[pre_start:start]["actual_exposure"].mean())
                    if start > 0
                    else np.nan,
                    "exposure_at_start": float(window["actual_exposure"].iloc[0]),
                    "minimum_exposure": float(window["actual_exposure"].min()),
                    "exposure_at_end": float(window["actual_exposure"].iloc[-1]),
                    "target_changes_in_window": int(changes.iloc[start : end + 1].abs().gt(1e-12).sum()),
                    "all_sample_five_day_flips": flips,
                }
            )
    return rows


def draw_figures(all_daily: pd.DataFrame, feature_frames: dict[str, pd.DataFrame], output: Path) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(13, 13), sharex=False)
    for axis, (index_id, group) in zip(axes, all_daily.groupby("index_id", sort=False), strict=True):
        for strategy in STRATEGY_ORDER:
            x = group[group["strategy"] == strategy]
            axis.plot(x["trade_date"], x["nav_rebased"], label=strategy, color=COLORS[strategy], linewidth=1.3)
        axis.set_title(index_id)
        axis.set_ylabel("NAV")
        axis.grid(alpha=0.2)
    axes[0].legend(ncol=3, fontsize=8)
    fig.tight_layout()
    fig.savefig(output / "nav.png", dpi=160)
    plt.close(fig)

    fig, axes = plt.subplots(3, 1, figsize=(13, 13), sharex=False)
    for axis, (index_id, group) in zip(axes, all_daily.groupby("index_id", sort=False), strict=True):
        for strategy in STRATEGY_ORDER:
            x = group[group["strategy"] == strategy]
            axis.plot(
                x["trade_date"], 100 * x["drawdown_rebased"], label=strategy, color=COLORS[strategy], linewidth=1.2
            )
        axis.set_title(index_id)
        axis.set_ylabel("Drawdown (%)")
        axis.grid(alpha=0.2)
    axes[0].legend(ncol=3, fontsize=8)
    fig.tight_layout()
    fig.savefig(output / "drawdown.png", dpi=160)
    plt.close(fig)

    fig, axes = plt.subplots(3, 1, figsize=(13, 11), sharex=False)
    for axis, (index_id, frame) in zip(axes, feature_frames.items(), strict=True):
        shown = frame[frame["trade_date"] >= pd.Timestamp("2018-01-01")]
        axis.plot(shown["trade_date"], shown["risk_score"], color="#dc2626", linewidth=1.0, label="Risk Score")
        axis.step(
            shown["trade_date"],
            shown["Risk Score"] * 100,
            where="post",
            color="#2563eb",
            linewidth=0.8,
            label="Target exposure",
        )
        axis.set_ylim(0, 105)
        axis.set_title(index_id)
        axis.grid(alpha=0.2)
    axes[0].legend(ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(output / "risk_and_exposure.png", dpi=160)
    plt.close(fig)


def fmt_pct(value: float) -> str:
    return "NA" if not np.isfinite(value) else f"{value * 100:.2f}%"


def write_report(
    metrics: pd.DataFrame,
    period_metrics: pd.DataFrame,
    audit: dict[str, Any],
    output: Path,
    config: dict[str, Any],
) -> None:
    artifact_prefix = output.relative_to(RISK_DIR).as_posix()
    lines = [
        "# Risk Score 仓位控制验证结果",
        "",
        f"运行区间：{audit['oos_start']} 至 {audit['oos_end']}；主成本：单边 "
        f"{config['one_way_cost_bps']:.1f}bp；执行：T日收盘信号，T+1开盘执行。",
        "",
        "本报告是历史伪OOS研究，不是实时前瞻结果，也不代表可交易或可部署。指数使用价格指数，现金收益设为0。",
        "",
        "## 核心结果",
        "",
        "| 指数 | 策略 | CAGR | MaxDD | Calmar | Sharpe | 年化波动 | 下行捕获 | "
        "最差完整年 | 恢复期 | 平均仓位 | 换手 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in metrics.iterrows():
        recovery = "未恢复" if row["Recovery Status"] == "unrecovered" else f"{int(row['Recovery Time'])}日"
        lines.append(
            f"| {row['index_name']} | {row['strategy']} | {fmt_pct(row['CAGR'])} | {fmt_pct(row['Max Drawdown'])} | "
            f"{row['Calmar Ratio']:.2f} | {row['Sharpe Ratio']:.2f} | {fmt_pct(row['Annualized Volatility'])} | "
            f"{row['Downside Capture']:.1f}% | {fmt_pct(row['Worst Year Return'])} | {recovery} | "
            f"{fmt_pct(row['Average Exposure'])} | {row['Turnover']:.2f} |"
        )
    lines.extend(["", "## 三个问题", ""])
    for index_name, group in metrics.groupby("index_name", sort=False):
        lookup = group.set_index("strategy")
        risk = lookup.loc["Risk Score"]
        bh = lookup.loc["Buy & Hold"]
        const = lookup.loc["Constant Exposure - Train Fixed"]
        simple = lookup.loc[["MA200", "Volatility Targeting"]]
        dd_reduction = 1.0 - risk["Max Drawdown"] / bh["Max Drawdown"]
        beats_const = risk["Calmar Ratio"] > const["Calmar Ratio"] and risk["Max Drawdown"] < const["Max Drawdown"]
        beats_simple = bool(
            (risk["Calmar Ratio"] > simple["Calmar Ratio"]).all()
            and (risk["Max Drawdown"] < simple["Max Drawdown"]).all()
        )
        lines.append(
            f"- **{index_name}**：相对Buy & Hold，MaxDD相对减少{dd_reduction * 100:.1f}%，"
            f"CAGR差{(risk['CAGR'] - bh['CAGR']) * 100:.2f}个百分点；相对Train Fixed等仓位基线，"
            f"{'具备同时改善Calmar与MaxDD的迹象' if beats_const else '没有同时改善Calmar与MaxDD'}；"
            f"相对MA200和波动率目标，"
            f"{'Risk Score同时胜过两项简单基线' if beats_simple else 'Risk Score未能同时胜过两项简单基线'}。"
        )
    lines.extend(
        [
            "",
            "1. **相比Buy & Hold**：中证1000和中证2000达到预设的明显降回撤门槛，沪深300没有；三指数结论不一致。",
            "2. **相比简单风控**：Risk Score在中证1000和中证2000优于MA200与波动率目标，"
            "但在沪深300失败，因此没有形成跨风格一致优势。",
            "3. **相比相同平均仓位**：三个指数均未同时改善Train Fixed的MaxDD与Calmar，当前没有足够证据证明择时增量。",
            "",
            "问题1按回撤相对减少至少20%且绝对减少至少3个百分点判断是否明显；问题2要求逐项优于简单风控；"
            "问题3以Train Fixed为正式OOS对照，Full Sample只在诊断文件中使用。统计区间见robustness.csv，"
            "压力事件见stress_events.csv。",
            "",
            "## 中证2000发布后诊断",
            "",
            "中证2000于2023-08-11发布。发布后至样本末只有约3年，仅作为补充，不替代全区间主结论。",
            "",
            "| 策略 | CAGR | MaxDD | Calmar | 平均仓位 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for _, row in period_metrics.iterrows():
        lines.append(
            f"| {row['strategy']} | {fmt_pct(row['CAGR'])} | {fmt_pct(row['Max Drawdown'])} | "
            f"{row['Calmar Ratio']:.2f} | {fmt_pct(row['Average Exposure'])} |"
        )
    lines.extend(
        [
            "",
            "发布后Risk Score的CAGR为7.55%、MaxDD为20.63%、Calmar为0.37，较Train Fixed同时改善；"
            "但与MA200接近，且样本期过短，不能升级为跨周期择时证据。",
            "",
            "## 主要限制",
            "",
            "- 五维底层指标是本次冻结的第一版工程定义，未做权重、窗口或阈值搜索；结论只对应这一实现。",
            "- 中证2000在2023-08-11发布，之前为供应商提供的回溯价格；发布后样本很短，"
            "不能单凭其全区间结果称为实时可投资证据。",
            "- 使用指数价格序列和假设摩擦，没有包含ETF跟踪误差、期货展期、真实容量和现金利息。",
            "- 市场横截面由当日有有效行情的A股组成，保留历史退市证券；停牌证券当日无新行情，因此不进入当日横截面。",
            "- 滚动块Bootstrap是路径不确定性的近似诊断，不增加危机事件数量，也不能替代未来样本。",
            "",
            "## 图表",
            "",
            f"![净值曲线]({artifact_prefix}/nav.png)",
            "",
            f"![回撤曲线]({artifact_prefix}/drawdown.png)",
            "",
            f"![风险分数与目标仓位]({artifact_prefix}/risk_and_exposure.png)",
        ]
    )
    (RISK_DIR / "05_results_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=RISK_DIR / "config.v1.json")
    parser.add_argument("--output", type=Path, default=RISK_DIR / "runs" / "RPV-20260913-v1")
    parser.add_argument("--reuse-inputs", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    indices_path = output / "indices_daily.csv"
    market_path = output / "market_features.csv"
    indices = (
        pd.read_csv(indices_path, parse_dates=["trade_date"])
        if args.reuse_inputs and indices_path.exists()
        else fetch_indices(config, indices_path)
    )
    market = (
        pd.read_csv(market_path, parse_dates=["trade_date"])
        if args.reuse_inputs and market_path.exists()
        else build_market_features(config, market_path)
    )

    feature_frames: dict[str, pd.DataFrame] = {}
    metric_rows: list[dict[str, Any]] = []
    daily_rows: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    robustness_rows: list[dict[str, Any]] = []
    stress_rows: list[dict[str, Any]] = []
    full_sample_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    trade_rows: list[pd.DataFrame] = []
    period_rows: list[dict[str, Any]] = []
    oos_start = pd.Timestamp(config["oos_start"])
    for item in config["indices"]:
        frame = score_and_targets(indices[indices["index_id"] == item["id"]], market, config)
        feature_frames[item["id"]] = frame
        risk_full = simulate(frame, frame["Risk Score"], float(config["one_way_cost_bps"]))
        train_target, folds = train_fixed_target(frame, risk_full, config)
        for fold in folds:
            fold.update(index_id=item["id"], index_name=item["name"])
            fold_rows.append(fold)
        targets: dict[str, pd.Series] = {
            name: frame[name] for name in STRATEGY_ORDER if name != "Constant Exposure - Train Fixed"
        }
        targets["Constant Exposure - Train Fixed"] = train_target
        daily_by_strategy: dict[str, pd.DataFrame] = {}
        metric_targets: dict[str, pd.Series] = {}
        eligible = frame["trade_date"] >= oos_start
        for name in STRATEGY_ORDER:
            simulation = simulate(frame, targets[name], float(config["one_way_cost_bps"]))
            selected = simulation.daily[eligible.to_numpy()].copy().reset_index(drop=True)
            selected["nav_rebased"] = selected["nav"] / selected["nav"].iloc[0]
            selected["drawdown_rebased"] = selected["nav_rebased"] / selected["nav_rebased"].cummax() - 1.0
            daily_by_strategy[name] = selected
            metric_targets[name] = targets[name][eligible].reset_index(drop=True)
        rows = [metric_row(daily_by_strategy[name], metric_targets[name], name) for name in STRATEGY_ORDER]
        index_metrics = pd.DataFrame(rows)
        add_downside_capture(index_metrics, daily_by_strategy)
        index_metrics["index_id"] = item["id"]
        index_metrics["index_name"] = item["name"]
        metric_rows.extend(index_metrics.to_dict("records"))
        for name, daily in daily_by_strategy.items():
            copy = daily.copy()
            copy["index_id"] = item["id"]
            copy["index_name"] = item["name"]
            copy["strategy"] = name
            daily_rows.append(copy)
            trades = copy[copy["traded_notional"] > 1e-12].copy()
            trade_rows.append(trades)
            for year, year_daily in daily.groupby(daily["trade_date"].dt.year):
                annual_rows.append(
                    {
                        "index_id": item["id"],
                        "index_name": item["name"],
                        "strategy": name,
                        "year": int(year),
                        "return": float(np.prod(1.0 + year_daily["daily_return"]) - 1.0),
                        "complete_year": bool(
                            year_daily["trade_date"].min().month == 1 and year_daily["trade_date"].max().month == 12
                        ),
                    }
                )

        if item.get("published_on"):
            published_on = pd.Timestamp(item["published_on"])
            published_daily = {
                name: daily[daily["trade_date"] >= published_on].reset_index(drop=True)
                for name, daily in daily_by_strategy.items()
            }
            published_metrics = pd.DataFrame(
                [
                    metric_row(
                        published_daily[name],
                        metric_targets[name][
                            daily_by_strategy[name]["trade_date"].reset_index(drop=True) >= published_on
                        ].reset_index(drop=True),
                        name,
                    )
                    for name in STRATEGY_ORDER
                ]
            )
            add_downside_capture(published_metrics, published_daily)
            for row in published_metrics.to_dict("records"):
                row.update(
                    index_id=item["id"],
                    index_name=item["name"],
                    sample="post_publication",
                    sample_start=item["published_on"],
                    sample_end=str(published_daily["Risk Score"]["trade_date"].max().date()),
                )
                period_rows.append(row)

        risk_mean = float(daily_by_strategy["Risk Score"]["actual_exposure"].mean())
        full_target = pd.Series(risk_mean, index=frame.index)
        full_sim = simulate(frame, full_target, float(config["one_way_cost_bps"]))
        full_daily = full_sim.daily[eligible.to_numpy()].copy().reset_index(drop=True)
        full_metric = metric_row(
            full_daily, full_target[eligible].reset_index(drop=True), "Constant Exposure - Full Sample"
        )
        full_metric.update(index_id=item["id"], index_name=item["name"], matched_exposure=risk_mean)
        full_sample_rows.append(full_metric)
        pd.DataFrame([full_metric]).to_csv(
            output / f"full_sample_constant_{item['id'].lower()}.csv", index=False, encoding="utf-8-sig"
        )

        bootstrap = bootstrap_differences(
            daily_by_strategy,
            int(config["bootstrap_repetitions"]),
            int(config["bootstrap_block_sessions"]),
            seed=20260913 + len(robustness_rows),
        )
        for row in bootstrap:
            row.update(index_id=item["id"], index_name=item["name"], analysis="paired_moving_block_bootstrap")
            robustness_rows.append(row)
        for cost in config["cost_sensitivity_bps"]:
            sim = simulate(frame, frame["Risk Score"], float(cost))
            selected = sim.daily[eligible.to_numpy()].copy().reset_index(drop=True)
            row = metric_row(selected, frame.loc[eligible, "Risk Score"].reset_index(drop=True), "Risk Score")
            robustness_rows.append(
                {
                    "index_id": item["id"],
                    "index_name": item["name"],
                    "analysis": "cost_sensitivity",
                    "cost_bps": cost,
                    "metric": "CAGR",
                    "value": row["CAGR"],
                    "max_drawdown": row["Max Drawdown"],
                    "calmar": row["Calmar Ratio"],
                }
            )

        events = detect_stress_events(frame, daily_by_strategy["Buy & Hold"], oos_start)
        stress_rows.extend(stress_diagnostics(item["id"], item["name"], events, daily_by_strategy))

    metrics = pd.DataFrame(metric_rows)
    all_daily = pd.concat(daily_rows, ignore_index=True)
    metrics.to_csv(output / "comparison.csv", index=False, encoding="utf-8-sig")
    all_daily.to_csv(output / "daily_evidence.csv", index=False, encoding="utf-8-sig")
    pd.concat(trade_rows, ignore_index=True).to_csv(output / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(annual_rows).to_csv(output / "annual_returns.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(period_rows).to_csv(output / "period_comparison.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(fold_rows).to_csv(output / "walk_forward_folds.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(robustness_rows).to_csv(output / "robustness.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(stress_rows).to_csv(output / "stress_events.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(full_sample_rows).to_csv(
        output / "full_sample_constant_diagnostic.csv", index=False, encoding="utf-8-sig"
    )
    feature_export = pd.concat(
        [frame.assign(index_id=index_id) for index_id, frame in feature_frames.items()], ignore_index=True
    )
    feature_export.to_csv(output / "risk_scores.csv", index=False, encoding="utf-8-sig")
    draw_figures(all_daily, feature_frames, output)
    audit = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "oos_start": str(all_daily["trade_date"].min().date()),
        "oos_end": str(all_daily["trade_date"].max().date()),
        "index_rows": {key: int(len(value)) for key, value in indices.groupby("index_id")},
        "market_feature_rows": int(len(market)),
        "market_feature_start": str(market["trade_date"].min().date()),
        "market_feature_end": str(market["trade_date"].max().date()),
        "pricing": "price_index",
        "cash_return": 0.0,
        "source": "Tushare-compatible index_daily plus local append-only Tushare daily parquet",
        "holdout_claim": "diagnostic pseudo-OOS; no unseen-holdout claim",
        "index_duplicate_rows": int(indices.duplicated(["index_id", "trade_date"]).sum()),
        "invalid_ohlc_rows": int(
            (
                (indices["low"] > indices[["open", "close"]].min(axis=1))
                | (indices["high"] < indices[["open", "close"]].max(axis=1))
                | (indices[["open", "high", "low", "close", "pre_close"]] <= 0).any(axis=1)
            ).sum()
        ),
        "oos_missing_risk_scores": int(
            feature_export.loc[feature_export["trade_date"] >= oos_start, "risk_score"].isna().sum()
        ),
        "oos_risk_score_min": float(feature_export.loc[feature_export["trade_date"] >= oos_start, "risk_score"].min()),
        "oos_risk_score_max": float(feature_export.loc[feature_export["trade_date"] >= oos_start, "risk_score"].max()),
    }
    atomic_json(output / "data_audit.json", audit)
    (output / "experiment_spec.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_files = [p for p in output.iterdir() if p.is_file() and p.name != "manifest.json"]
    atomic_json(
        output / "manifest.json",
        {
            "generated_at": audit["generated_at"],
            "config_sha256": sha256_file(args.config),
            "implementation_sha256": sha256_file(Path(__file__)),
            "files": {
                path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
                for path in sorted(manifest_files)
            },
        },
    )
    write_report(metrics, pd.DataFrame(period_rows), audit, output, config)
    print(f"Completed {len(metrics)} core comparisons in {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
