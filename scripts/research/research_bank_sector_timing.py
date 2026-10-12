"""Versioned, bounded bank-sector signal research; never a tradable backtest.

Prepare freezes the experiment and builds past-only features. Analyse runs a
separate forward-label stage against the released input pack and renders evidence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from alpha_research_os.factors.bank_timing import bank_timing_catalog  # noqa: E402
from scripts.bank_processing import manifests, resolve_asset, verify_files  # noqa: E402

VERSION = "bank-sector-signal-research-1.0.0"
HORIZONS = (20, 60, 120)
TARGETS = ("bank_return", "active_return")
PB = "bank-sector-pb-history-percentile"
TREND = "bank-sector-trend-breadth"
ACTIVITY = "activity-turnover-ratio-20-60"
ACTIVITY_BREADTH = "activity-turnover-warming-breadth"
TREND_CHANGE = "trend-breadth-change-20"
STATES = {0: "低估·趋势改善", 1: "低估·趋势未改善", 2: "非低估·趋势改善", 3: "非低估·趋势未改善"}
EXTRA = [
    {
        "factor_id": ACTIVITY,
        "name": "换手率升温比",
        "direction": "DIAGNOSTIC",
        "formula": "median_i(mean(turnover_i,last20)/mean(turnover_i,last60))",
    },
    {
        "factor_id": ACTIVITY_BREADTH,
        "name": "交易升温覆盖面",
        "direction": "DIAGNOSTIC",
        "formula": "count(ratio_i>1)/count(valid ratio_i)",
    },
    {
        "factor_id": TREND_CHANGE,
        "name": "趋势覆盖面20日变化",
        "direction": "HIGH",
        "formula": "trend_breadth_t - trend_breadth_t_minus_20",
    },
]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def serial(value):
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def write_json(path, data):
    """Exclusive creation: no source, experiment, or result overwrites."""
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(serial(data), stream, ensure_ascii=False, indent=2, allow_nan=False)


def activity_features(raw, calendar, universe, minimum_coverage=0.7):
    """Use market-calendar windows and no missing-day forward filling."""
    calendar = pd.DatetimeIndex(pd.to_datetime(calendar))
    raw = raw.copy()
    raw["session"] = pd.to_datetime(raw.session)
    if raw.duplicated(["session", "instrument_id"]).any():
        raise ValueError("duplicate turnover observations")
    history = raw.pivot(index="session", columns="instrument_id", values="turnover_rate").reindex(calendar)
    history = history.where(np.isfinite(history) & (history >= 0))
    short = history.rolling(20, min_periods=15).mean()
    long = history.rolling(60, min_periods=45).mean()
    ratios = (short / long.where(long > 0)).stack().rename("turnover_ratio").reset_index()
    ratios.columns = ["session", "instrument_id", "turnover_ratio"]
    universe = universe[["session", "instrument_id"]].copy()
    universe["session"] = pd.to_datetime(universe.session)
    ratios = universe.merge(ratios, on=["session", "instrument_id"], how="left", validate="one_to_one")
    rows = []
    for day, group in ratios.groupby("session", sort=True):
        valid = group.turnover_ratio.dropna()
        required = max(10, math.ceil(len(group) * minimum_coverage))
        ready = len(valid) >= required
        rows.append(
            {
                "session": day,
                ACTIVITY: valid.median() if ready else np.nan,
                ACTIVITY_BREADTH: (valid > 1).mean() if ready else np.nan,
                "activity_valid_count": len(valid),
                "activity_universe_count": len(group),
                "activity_coverage": len(valid) / len(group),
                "activity_status": "READY_RESEARCH_ONLY" if ready else "INSUFFICIENT_HISTORY",
            }
        )
    return pd.DataFrame(rows).set_index("session"), ratios


def assign_states(features, pb_ceiling=0.30):
    """Fixed thresholds, same-period features only; unknown does not mean false."""
    known = features[PB].notna() & features[TREND_CHANGE].notna()
    cheap = features[PB] <= pb_ceiling
    warming = features[TREND_CHANGE] > 0
    state = pd.Series(np.nan, index=features.index)
    state.loc[known] = (2 * (~cheap.loc[known]).astype(int) + (~warming.loc[known]).astype(int)).astype(float)
    return state


def prepare(root, output_root):
    settings = json.loads((root / "config/bank_timing_data.json").read_bytes())
    start, end = settings["start"], settings["released_feature_end"]
    definitions = list(bank_timing_catalog())
    all_manifests = manifests(root, "INDICATORS")
    sources = []
    for definition in definitions:
        candidate = next(
            (
                m
                for m in all_manifests
                if m["start"] == start
                and m["end"] == end
                and any(
                    s["factor_id"] == definition.factor_id and s["factor_version"] == definition.factor_version
                    for s in m["summaries"]
                )
            ),
            None,
        )
        if not candidate:
            raise ValueError(f"missing calculated factor: {definition.factor_id}")
        folder, manifest = resolve_asset(root, "INDICATORS", candidate["asset_id"])
        verify_files(folder, manifest)
        sources.append(
            {
                "factor_id": definition.factor_id,
                "name": definition.chinese_name,
                "direction": definition.expected_direction,
                "formula": definition.formula,
                "release_id": manifest["asset_id"],
                "source_id": manifest["source_id"],
                "values_path": str(folder / "sector_values.parquet"),
                "values_sha256": manifest["files"]["sector_values.parquet"],
                "summary": next(s for s in manifest["summaries"] if s["factor_id"] == definition.factor_id),
            }
        )
    ids = {s["source_id"] for s in sources}
    if len(ids) != 1:
        raise ValueError("sector factors do not share one upstream data version")
    pack, manifest = resolve_asset(root, "DERIVED", ids.pop())
    verify_files(pack, manifest)
    if manifest["end"] > end:
        raise ValueError("upstream pack extends beyond released range")
    spec = {
        "version": VERSION,
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "start": start,
        "end": end,
        "released_end": end,
        "scope": "EXPOSED_HISTORY_EXPLORATORY; RESEARCH_ONLY; NOT_M4; NOT_TRADABLE",
        "source_pack": str(pack),
        "source_pack_id": manifest["pack_id"],
        "source_manifest_sha256": sha(pack / "manifest.json"),
        "source_files": manifest["files"],
        "factor_sources": sources,
        "additional_diagnostics": EXTRA,
        "feature_cutoff": "T post-close; daily_basic treated as available at 18:00 Asia/Shanghai",
        "turnover": {
            "field": "research.daily_basic.turnover_rate",
            "history_start": "2019-01-01",
            "short_window": 20,
            "short_minimum": 15,
            "long_window": 60,
            "long_minimum": 45,
            "aggregation": "median of per-bank short/long ratios; historical eligible membership",
            "minimum_banks": 10,
            "minimum_coverage": 0.7,
            "threshold": 1.0,
            "missing_policy": "market calendar; no fill; zero denominator invalid",
        },
        "primary_hypothesis": "PB mean percentile<=0.30 and positive 20-session trend-breadth change",
        "pb_ceiling": 0.30,
        "warming_threshold": 0.0,
        "secondary_hypothesis": "among cheap+trend-warming dates, turnover-ratio>1 vs <=1",
        "horizons": list(HORIZONS),
        "targets": list(TARGETS),
        "labels": {
            "entry_offset": 1,
            "entry_reference": "T+1 analytical basket close",
            "exit_offset": "1+horizon",
            "gross_return": True,
            "segment_policy": "reject every window crossing an invalid bank or benchmark day",
            "cash_arrival_tax_cost_fill": "not modelled; signal diagnostic only",
        },
        "statistics": {
            "family_size": 90,
            "single_factor_tests": 78,
            "combination_tests": 12,
            "single_model": "raw factor slope plus calendar-year fixed effects",
            "combination_model": "binary contrast plus calendar-year fixed effects",
            "hac_lag": "horizon+20; Bartlett; preserve original calendar gaps",
            "multiple_testing": "Holm 90 tests; unavailable tests retained with p=1",
            "bootstrap": "calendar circular moving-block conditional-mean contrast",
            "bootstrap_block": "max(60,horizon+20)",
            "resamples": 1999,
            "seed": 20261011,
            "minimum_model_rows": 80,
            "minimum_binary_group": 30,
        },
        "group_cutpoints": "fit factor quintile cuts on 2020-2022 only; freeze for 2023-2025 diagnostics",
        "chronological_checks": "2020-2022 and 2023-2025 separately; year-contained labels",
        "search_policy": "one preset threshold/window family; no parameter search or direction selection",
        "limitations": [
            "2020-2025 already exposed; chronological checks are not untouched OOS",
            "current42 source cohort; complete historical industry/disclosure certification incomplete",
            "bank basket is an analytical equal-weight gross-return reference with real suspension breaks",
            "CET1 cross-regulatory comparability not fully certified",
            "turnover availability/revision history remains vendor evidence, not fully certified PIT",
            "no position strategy, trading costs, tax, liquidity, fills or capacity evaluation",
        ],
        "constitution_sha256": sha(root / "RESEARCH_CONSTITUTION.md"),
        "script_sha256": sha(__file__),
    }
    prior_paths = [root/'reports/bank_sector_research/20261011-215220/results.json',
                   root/'reports/bank_sector_research/20261012-072739-dividend-rebound/results.json']
    spec['related_prior_results'] = [{'path':str(p),'sha256':sha(p)} for p in prior_paths if p.exists()]
    spec['limitations'].append('周期股息率修正后重算；联合校正保留此前90项板块及36项股息率研究，旧口径结果不作为新版结论。')
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    folder = output_root / stamp
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "experiment_spec.json", spec)  # frozen before any forward label calculations
    frames = []
    for s in sources:
        values = pd.read_parquet(s["values_path"])
        values = values[values.factor_id == s["factor_id"]].copy()
        values["session"] = pd.to_datetime(values.session)
        if values.session.duplicated().any():
            raise ValueError("duplicate sector observations")
        frames.append(values.set_index("session").value.rename(s["factor_id"]))
    features = pd.concat(frames, axis=1).sort_index()
    if (
        len(features) != 1455
        or features.index.min() != pd.Timestamp(start)
        or features.index.max() != pd.Timestamp(end)
    ):
        raise ValueError("unexpected released feature calendar")
    universe = pd.read_parquet(pack / "bank_universe_daily.parquet")
    # This feature stage reads dates only, never future basket/index levels.
    calendar = pd.to_datetime(pd.read_parquet(pack / "benchmark_daily.parquet", columns=["session"]).session)
    codes = sorted(universe.instrument_id.unique())
    with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as connection:
        raw = connection.execute(
            """SELECT d.trade_date AS session,d.ts_code AS instrument_id,
            d.turnover_rate,d.source_snapshot_id,d.source_payload_artifact_id
            FROM research.daily_basic d WHERE d.trade_date BETWEEN ? AND ?
            AND d.ts_code IN ("""
            + ",".join("?" for _ in codes)
            + """ )
            AND EXISTS (SELECT 1 FROM research.sw_industry_membership i WHERE i.ts_code=d.ts_code
                AND i.l1_name='银行' AND i.in_date<=d.trade_date AND (i.out_date IS NULL OR d.trade_date<i.out_date))
            AND EXISTS (SELECT 1 FROM research.market_daily m WHERE m.ts_code=d.ts_code
                AND m.trade_date=d.trade_date AND m.is_tradeable_bar)
            ORDER BY d.trade_date,d.ts_code""",
            ["2019-01-01", end, *codes],
        ).df()
    if raw[["source_snapshot_id", "source_payload_artifact_id"]].isna().any().any():
        raise ValueError("turnover data lacks original source lineage")
    activity, ratios = activity_features(raw, calendar, universe)
    features = features.join(activity)
    features[TREND_CHANGE] = features[TREND] - features[TREND].shift(20)
    features["state"] = assign_states(features, spec["pb_ceiling"])
    for name, data in [("turnover_source", raw), ("turnover_ratios", ratios), ("features", features.reset_index())]:
        data.to_parquet(folder / (name + ".parquet"), index=False)
    write_json(
        folder / "feature_manifest.json",
        {
            "experiment_sha256": sha(folder / "experiment_spec.json"),
            "files": {
                name + ".parquet": sha(folder / (name + ".parquet"))
                for name in ("turnover_source", "turnover_ratios", "features")
            },
            "source_pack_verified": True,
            "label_data_read": False,
            "source_rows": len(raw),
            "signal_rows": len(features),
            "source_cohort": len(codes),
            "activity_ready_days": int(features[ACTIVITY].notna().sum()),
        },
    )
    print(
        json.dumps(
            {
                "status": "SPEC_AND_FEATURES_FROZEN",
                "run_dir": str(folder),
                "family_size": 90,
                "activity_ready_days": int(features[ACTIVITY].notna().sum()),
            },
            ensure_ascii=False,
        )
    )
    return folder


def forward_labels(basket, benchmark, sessions, horizons=HORIZONS):
    """T+1 close to T+1+h close, reject segment breaks and the unobserved tail."""
    sessions = pd.DatetimeIndex(pd.to_datetime(sessions))
    b = basket.copy().set_index(pd.to_datetime(basket.session)).reindex(sessions)
    m = benchmark.copy().set_index(pd.to_datetime(benchmark.session)).reindex(sessions)
    bank = b.bank_equal_total_return_index.to_numpy(float)
    broad = m.gross_total_return_index.to_numpy(float)
    rows = []
    for i, day in enumerate(sessions):
        entry = i + 1
        for horizon in horizons:
            exit_index = entry + horizon
            result = {
                "session": day,
                "horizon": horizon,
                "entry_session": pd.NaT,
                "exit_session": pd.NaT,
                "bank_return": np.nan,
                "active_return": np.nan,
                "benchmark_return": np.nan,
                "max_drawdown": np.nan,
                "minimum_return": np.nan,
                "status": "RELEASED_TAIL_UNOBSERVED",
            }
            if exit_index < len(sessions):
                result.update(entry_session=sessions[entry], exit_session=sessions[exit_index])
                bs = b.iloc[entry : exit_index + 1]
                ms = m.iloc[entry : exit_index + 1]
                path = bank[entry : exit_index + 1]
                bp = broad[entry : exit_index + 1]
                bank_good = (
                    np.isfinite(path).all()
                    and (path > 0).all()
                    and bs.basket_segment.nunique() == 1
                    and (bs.status == "READY").all()
                )
                broad_good = (
                    np.isfinite(bp).all()
                    and (bp > 0).all()
                    and ms.benchmark_segment.nunique() == 1
                    and (ms.benchmark_status == "READY").all()
                )
                result["status"] = "BANK_PATH_BREAK" if not bank_good else "BENCHMARK_PATH_BREAK"
                if bank_good and broad_good:
                    br, mr = path[-1] / path[0] - 1, bp[-1] / bp[0] - 1
                    result.update(
                        bank_return=br,
                        benchmark_return=mr,
                        active_return=br - mr,
                        max_drawdown=np.min(path / np.maximum.accumulate(path) - 1),
                        minimum_return=np.min(path / path[0] - 1),
                        status="READY_ANALYTICAL",
                    )
            rows.append(result)
    return pd.DataFrame(rows)


def hac_regression(x, y, sessions, max_lag, minimum_rows=80, binary=False):
    """OLS with year effects and calendar-preserving Newey-West score covariance."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    years = pd.DatetimeIndex(sessions).year.to_numpy()
    present = np.isfinite(x) & np.isfinite(y)
    n = int(present.sum())
    result = {
        "n": n,
        "coefficient": None,
        "standard_error": None,
        "p_value": None,
        "confidence_lower": None,
        "confidence_upper": None,
        "max_lag": max_lag,
        "status": "INSUFFICIENT",
    }
    if n < minimum_rows or (binary and min((x[present] == 0).sum(), (x[present] == 1).sum()) < 30):
        return result
    unique = sorted(set(years[present]))
    design = np.column_stack([np.ones(len(x)), x, *((years == v).astype(float) for v in unique[1:])])
    xx = design[present]
    if np.linalg.matrix_rank(xx) != xx.shape[1]:
        result["status"] = "NO_WITHIN_YEAR_VARIATION"
        return result
    inverse = np.linalg.inv(xx.T @ xx)
    coefficient = inverse @ xx.T @ y[present]
    residual = y[present] - xx @ coefficient
    scores = np.zeros_like(design)
    scores[present] = xx * residual[:, None]
    meat = scores.T @ scores
    for lag in range(1, min(max_lag, len(x) - 1) + 1):
        cross = scores[lag:].T @ scores[:-lag]
        meat += (1 - lag / (max_lag + 1)) * (cross + cross.T)
    covariance = inverse @ meat @ inverse * n / (n - xx.shape[1])
    std = math.sqrt(max(0, covariance[1, 1]))
    beta = float(coefficient[1])
    pvalue = math.erfc(abs(beta / std) / math.sqrt(2)) if std > 0 else (1.0 if abs(beta) < 1e-12 else 0.0)
    result.update(
        coefficient=beta,
        standard_error=std,
        p_value=pvalue,
        confidence_lower=beta - 1.96 * std,
        confidence_upper=beta + 1.96 * std,
        status="ASYMPTOTIC_DIAGNOSTIC",
    )
    return result


def holm_adjust(pvalues):
    """Retain every planned test, including unavailable hypotheses, in the family."""
    values = np.array([1.0 if p is None else p for p in pvalues], float)
    if (~np.isfinite(values)).any() or ((values < 0) | (values > 1)).any():
        raise ValueError("invalid p-value")
    order = np.argsort(values, kind="stable")
    adjusted = np.empty(len(values))
    adjusted[order] = np.minimum(1, np.maximum.accumulate(values[order] * np.arange(len(values), 0, -1)))
    return adjusted.tolist()


def bootstrap_contrast(y, treated, control, block_length, seed, resamples=1999):
    """Resample the full calendar, including missing labels and state gaps."""
    y = np.asarray(y, float)
    treated, control = np.asarray(treated, bool) & np.isfinite(y), np.asarray(control, bool) & np.isfinite(y)
    result = {
        "block_length": block_length,
        "resamples": resamples,
        "seed": seed,
        "mean_difference": None,
        "confidence_lower": None,
        "confidence_upper": None,
        "valid_resamples": 0,
    }
    if min(treated.sum(), control.sum()) < 30:
        return result
    observed = y[treated].mean() - y[control].mean()
    rng = np.random.default_rng(seed)
    n = len(y)
    starts = rng.integers(0, n, size=(resamples, math.ceil(n / block_length)))
    indices = ((starts[:, :, None] + np.arange(block_length)) % n).reshape(resamples, -1)[:, :n]
    t, c = treated[indices], control[indices]
    sampled_y = np.nan_to_num(y)[indices]
    enough = (t.sum(axis=1) >= 5) & (c.sum(axis=1) >= 5)
    differences = (
        (sampled_y * t).sum(axis=1) / np.maximum(1, t.sum(axis=1))
        - (sampled_y * c).sum(axis=1) / np.maximum(1, c.sum(axis=1))
    )[enough]
    if len(differences) >= resamples * 0.9:
        lo, hi = np.quantile(differences, [0.025, 0.975])
        result.update(mean_difference=observed, confidence_lower=lo, confidence_upper=hi)
    result["valid_resamples"] = len(differences)
    return result


def nonoverlap_positions(mask, horizon):
    selected, next_allowed = [], 0
    for position in np.flatnonzero(mask):
        if position >= next_allowed:
            selected.append(int(position))
            next_allowed = position + horizon + 1
    return selected


def group_summary(data, mask, horizon):
    valid = np.asarray(mask, bool) & data.bank_return.notna().to_numpy()
    group = data.loc[valid]
    positions = nonoverlap_positions(valid, horizon)
    sample = data.iloc[positions]
    state_mask = np.asarray(mask, bool)
    episodes = int((state_mask & ~np.r_[False, state_mask[:-1]]).sum())
    return {
        "days": int(valid.sum()),
        "state_days": int(state_mask.sum()),
        "state_episodes": episodes,
        "mean_bank_return": group.bank_return.mean(),
        "median_bank_return": group.bank_return.median(),
        "mean_active_return": group.active_return.mean(),
        "positive_fraction": (group.bank_return > 0).mean(),
        "mean_max_drawdown": group.max_drawdown.mean(),
        "mean_minimum_return": group.minimum_return.mean(),
        "return_p10": group.bank_return.quantile(0.1),
        "nonoverlap_count": len(positions),
        "nonoverlap_mean_bank_return": sample.bank_return.mean(),
        "nonoverlap_mean_active_return": sample.active_return.mean(),
    }


def analyse(folder):
    spec_path = folder / "experiment_spec.json"
    spec = json.loads(spec_path.read_bytes())
    fm = json.loads((folder / "feature_manifest.json").read_bytes())
    if sha(spec_path) != fm["experiment_sha256"]:
        raise ValueError("frozen experiment spec modified")
    for name, digest in fm["files"].items():
        if sha(folder / name) != digest:
            raise ValueError("frozen features modified")
    pack = Path(spec["source_pack"])
    if sha(pack / "manifest.json") != spec["source_manifest_sha256"]:
        raise ValueError("source manifest modified")
    verify_files(pack, json.loads((pack / "manifest.json").read_bytes()))
    features = pd.read_parquet(folder / "features.parquet").set_index("session")
    if features.index.max() > pd.Timestamp(spec["released_end"]):
        raise ValueError("feature beyond released scope")
    # Separate label runtime; no label result is ever sent back to feature builders.
    basket = pd.read_parquet(pack / "bank_equal_basket_daily.parquet")
    benchmark = pd.read_parquet(pack / "benchmark_daily.parquet")
    labels = forward_labels(basket, benchmark, features.index, spec["horizons"])
    labels.to_parquet(folder / "labels.parquet", index=False)
    definitions = spec["factor_sources"] + spec["additional_diagnostics"]
    rows, states, combinations, annual, groups, cutpoints = [], [], [], [], [], {}
    split = features.index <= pd.Timestamp("2022-12-31")
    for definition in definitions:
        fid = definition["factor_id"]
        values = features.loc[split, fid].dropna()
        cutpoints[fid] = np.unique(values.quantile([0.2, 0.4, 0.6, 0.8]).to_numpy()).tolist()
    for horizon in spec["horizons"]:
        outcome = labels[labels.horizon == horizon].set_index("session").reindex(features.index)
        for definition in definitions:
            fid = definition["factor_id"]
            for target in spec["targets"]:
                regression = hac_regression(features[fid], outcome[target], features.index, horizon + 20)
                good = features[fid].notna() & outcome[target].notna()
                ranks = pd.concat([features[fid][good].rank(), outcome[target][good].rank()], axis=1)
                correlation = ranks.iloc[:, 0].corr(ranks.iloc[:, 1]) if len(ranks) >= 3 else np.nan
                rows.append(
                    {
                        "test_id": f"single/{fid}/{horizon}/{target}",
                        "type": "SINGLE",
                        "factor_id": fid,
                        "name": definition["name"],
                        "direction": definition["direction"],
                        "horizon": horizon,
                        "target": target,
                        "spearman": correlation,
                        **regression,
                    }
                )
            # Bins are only an explanatory diagnostic; no sign or parameter is selected here.
            for period, period_mask in [("2020-2022", split), ("2023-2025", ~split)]:
                known = features[fid].notna().to_numpy()
                bins = np.searchsorted(cutpoints[fid], features[fid].to_numpy(), side="right")
                contained = outcome.exit_session.dt.year.le(features.index.year).fillna(False).to_numpy()
                for group in sorted(set(bins[known & period_mask])):
                    mask = known & period_mask & contained & (bins == group)
                    groups.append(
                        {
                            "factor_id": fid,
                            "name": definition["name"],
                            "horizon": horizon,
                            "period": period,
                            "group": int(group),
                            **group_summary(outcome, mask, horizon),
                        }
                    )
        for state, name in STATES.items():
            mask = features.state.eq(state).to_numpy()
            states.append({"horizon": horizon, "state": state, "name": name, **group_summary(outcome, mask, horizon)})
        primary_t = features.state.eq(0).to_numpy()
        primary_c = features.state.eq(1).to_numpy()
        secondary_t = primary_t & features[ACTIVITY].gt(1).to_numpy()
        secondary_c = primary_t & features[ACTIVITY].le(1).to_numpy()
        for name, treated, control in [
            ("PRIMARY_TREND", primary_t, primary_c),
            ("SECONDARY_ACTIVITY", secondary_t, secondary_c),
        ]:
            exposure = np.where(treated, 1.0, np.where(control, 0.0, np.nan))
            for target in spec["targets"]:
                regression = hac_regression(exposure, outcome[target], features.index, horizon + 20, binary=True)
                bootstrap = bootstrap_contrast(
                    outcome[target], treated, control, max(60, horizon + 20), spec["statistics"]["seed"] + horizon
                )
                tsummary, csummary = group_summary(outcome, treated, horizon), group_summary(outcome, control, horizon)
                item = {
                    "test_id": f"combo/{name}/{horizon}/{target}",
                    "type": "COMBINATION",
                    "name": name,
                    "horizon": horizon,
                    "target": target,
                    "direction": "HIGH",
                    "treated": tsummary,
                    "control": csummary,
                    "bootstrap": bootstrap,
                    **regression,
                }
                combinations.append(item)
                for period, period_mask in [
                    ("2020-2022", split),
                    ("2023-2025", ~split),
                    *[(str(y), features.index.year == y) for y in range(2020, 2026)],
                ]:
                    contained = outcome.exit_session.dt.year.le(features.index.year).fillna(False).to_numpy()
                    pm = period_mask & contained
                    a = group_summary(outcome, treated & pm, horizon)
                    b = group_summary(outcome, control & pm, horizon)
                    field = "mean_" + target
                    annual.append(
                        {
                            "combination": name,
                            "horizon": horizon,
                            "target": target,
                            "period": period,
                            "treated_days": a["days"],
                            "control_days": b["days"],
                            "treated_mean": a[field],
                            "control_mean": b[field],
                            "difference": a[field] - b[field],
                        }
                    )
    tests = rows + combinations
    if len(tests) != spec["statistics"]["family_size"]:
        raise ValueError("registered family size changed")
    previous_tests=[]
    for item in spec.get('related_prior_results',[]):
        path=Path(item['path'])
        if sha(path)!=item['sha256']:raise ValueError('Related research evidence changed')
        old=json.loads(path.read_bytes())
        previous_tests += old.get('tests',old.get('single_tests',[])+old.get('combinations',[]))
    prior_count=len(previous_tests)
    local=holm_adjust([r['p_value'] for r in tests])
    joint=holm_adjust([r['p_value'] for r in previous_tests+tests])[prior_count:]
    for row, adjusted, local_p in zip(tests, joint, local, strict=True):
        row["holm_p"] = adjusted
        row['local_holm_p']=local_p
    latest = {
        d["factor_id"]: {
            "name": d["name"],
            "value": features[d["factor_id"]].iloc[-1],
            "valid_days": int(features[d["factor_id"]].notna().sum()),
        }
        for d in definitions
    }
    result = {
        "status": "EXPLORATORY_SIGNAL_RESEARCH_ONLY",
        "spec_sha256": sha(spec_path),
        "source_pack_id": spec["source_pack_id"],
        "start": spec["start"],
        "end": spec["end"],
        "features": len(features),
        "latest": latest,
        "single_tests": rows,
        "combinations": combinations,
        "state_groups": states,
        "chronological": annual,
        "factor_groups": groups,
        "frozen_cutpoints": cutpoints,
        "label_status_counts": labels.groupby(["horizon", "status"])
        .size()
        .rename("count")
        .reset_index()
        .to_dict("records"),
        "feature_correlations": features[[d["factor_id"] for d in definitions]].corr(method="spearman").to_dict(),
        "family_size": len(tests),
        "joint_family_size": len(tests)+prior_count,
        "holm_significant_tests": sum(r["p_value"] is not None and r["holm_p"] <= 0.05 for r in tests),
        "limitations": spec["limitations"],
        "tests_evidence": {
            "costs_modelled": False,
            "m4_run": False,
            "untouched_oos": False,
            "parameter_search": False,
            "source_integrity_verified": True,
        },
    }
    write_json(folder / "results.json", result)
    for name, records in [
        ("single_factor_statistics", rows),
        ("four_state_statistics", states),
        ("chronological_statistics", annual),
        ("factor_group_statistics", groups),
    ]:
        pd.DataFrame(serial(records)).to_csv(folder / (name + ".csv"), index=False, encoding="utf-8-sig")
    render_report(folder, serial(result), spec, features)
    print(
        json.dumps(
            {
                "status": result["status"],
                "report": str(folder / "银行板块研究报告.html"),
                "family_size": len(tests),
                "holm_significant_tests": result["holm_significant_tests"],
                "primary": serial([r for r in combinations if r["name"] == "PRIMARY_TREND"]),
            },
            ensure_ascii=False,
        )
    )
    return result


def fmt(value, kind="pct"):
    if value is None or not np.isfinite(value):
        return "—"
    return f"{value * 100:+.2f}%" if kind == "pct" else f"{value:.3f}"


def make_charts(folder, features, result):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    from matplotlib.ticker import PercentFormatter

    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if font_path.exists():
        plt.rcParams["font.family"] = FontProperties(fname=str(font_path)).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.size"] = 10
    plt.rcParams["axes.spines.top"] = False
    plt.rcParams["axes.spines.right"] = False
    colors = ["#147956", "#cf9942", "#607aab", "#9b697a"]
    paths = []
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True, constrained_layout=True)
    axes[0].plot(features.index, features[PB], color=colors[0], lw=1.2)
    axes[0].axhline(0.3, color=colors[1], ls="--", label="本轮低估阈值30%")
    axes[0].set_ylabel("PB历史分位均值")
    axes[0].yaxis.set_major_formatter(PercentFormatter(1))
    axes[0].legend(loc="upper left")
    axes[1].plot(features.index, features[TREND], color=colors[2], label="趋势覆盖面", lw=1.1)
    axes[1].plot(features.index, features[TREND_CHANGE], color=colors[0], label="20日变化", lw=0.9)
    axes[1].axhline(0, color="#aaa", lw=0.7)
    axes[1].yaxis.set_major_formatter(PercentFormatter(1))
    axes[1].set_ylabel("趋势及变化")
    axes[1].legend(loc="upper left", ncol=2)
    axes[2].plot(features.index, features[ACTIVITY], color=colors[0], lw=1)
    axes[2].axhline(1, color=colors[1], ls="--")
    axes[2].set_ylabel("换手率20日/60日")
    axes[2].set_xlabel("研究信号日期（不是可成交账户净值）")
    fig.suptitle("估值、趋势改善与交易活跃度：只使用当时及此前观测", fontsize=14)
    p = folder / "signals.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(p)
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), constrained_layout=True)
    for ax, horizon in zip(axes, HORIZONS, strict=True):
        rows = [r for r in result["state_groups"] if r["horizon"] == horizon]
        for i, row in enumerate(rows):
            value = row["mean_bank_return"]
            ax.bar(i, np.nan if value is None else value, color=colors[i], alpha=0.9)
            if value is not None:
                ax.annotate(
                    f"{value * 100:.1f}%\nn={row['days']}",
                    (i, value),
                    textcoords="offset points",
                    xytext=(0, 5 if value >= 0 else -24),
                    ha="center",
                    fontsize=9,
                )
        ax.axhline(0, color="#777", lw=0.8)
        ax.set_xticks(range(4), ["低估\n趋势改善", "低估\n未改善", "非低估\n趋势改善", "非低估\n未改善"])
        ax.set_title(f"未来{horizon}交易日")
        ax.yaxis.set_major_formatter(PercentFormatter(1))
        present = [r["mean_bank_return"] for r in rows if r["mean_bank_return"] is not None]
        low, high = min(0, min(present)), max(0, max(present))
        span = max(high - low, 0.01)
        ax.set_ylim(low - 0.28 * span, high + 0.30 * span)
    fig.suptitle("四种状态的后续平均银行收益：重叠观察，非策略年化收益", fontsize=13)
    p = folder / "states.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(p)
    definitions = result["latest"]
    keys = list(definitions)
    matrix = pd.DataFrame(result["feature_correlations"]).reindex(index=keys, columns=keys).to_numpy(float)
    labels = [definitions[k]["name"].removeprefix("银行板块·") for k in keys]
    fig, ax = plt.subplots(figsize=(12, 9), constrained_layout=True)
    image = ax.imshow(matrix, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(keys)), labels, rotation=55, ha="right", fontsize=9)
    ax.set_yticks(range(len(keys)), labels, fontsize=9)
    for i in range(len(keys)):
        for j in range(len(keys)):
            if np.isfinite(matrix[i, j]):
                ax.text(
                    j,
                    i,
                    f"{matrix[i, j]:.2f}",
                    ha="center",
                    va="center",
                    fontsize=8,
                    color="white" if abs(matrix[i, j]) > 0.6 else "#222",
                )
    fig.colorbar(image, ax=ax, shrink=0.8, label="Spearman相关；用于冗余诊断")
    ax.set_title("指标之间有多少重复信息？", pad=16)
    p = folder / "correlations.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths.append(p)
    return paths


def render_report(folder, result, spec, features, evidence_folder=None):
    evidence_folder = evidence_folder or folder
    paths = make_charts(folder, features, result)
    primary = [r for r in result["combinations"] if r["name"] == "PRIMARY_TREND" and r["target"] == "bank_return"]
    secondary = [
        r for r in result["combinations"] if r["name"] == "SECONDARY_ACTIVITY" and r["target"] == "bank_return"
    ]
    positive = sum(r["coefficient"] is not None and r["coefficient"] > 0 for r in primary)
    supported = sum(r["coefficient"] is not None and r["coefficient"] > 0 and r["holm_p"] <= 0.05 for r in primary)
    if supported:
        conclusion = (
            f"固定规则的历史检验中，{supported}/3个周期出现正向且通过本轮多重检验的趋势改善关联。"
            "这支持继续研究该组合，但已暴露历史和数据认证限制使其尚不能成为已验证的入场策略。"
        )
    else:
        conclusion = (
            f"低估时趋势改善，在{positive}/3个周期的年度控制模型中方向为正，"
            "未出现通过本轮多重检验的正向收益优势。目前不能认定“低估＋热度上升”就是可靠进场点。"
        )
    negative = [r for r in primary if r["coefficient"] is not None and r["coefficient"] < 0 and r["holm_p"] <= 0.05]
    if negative:
        conclusion += " " + "、".join(str(r["horizon"]) + "日" for r in negative) + "出现通过本轮校正的负向关联。"
        for row in negative:
            conclusion += (
                f"但{row['horizon']}日两组不重叠观察仅{row['treated']['nonoverlap_count']}和"
                f"{row['control']['nonoverlap_count']}次，不能据此推出反向交易规则。"
            )
    notes = [
        conclusion,
        "这轮衡量的是信号之后的收益与路径风险，没有生成交易账户净值或投资仓位建议。",
        "成交活跃不能区分买入追捧与卖出抛压；本轮把交易升温和趋势改善分别检验。",
    ]
    positive_activity = sum(r["coefficient"] is not None and r["coefficient"] > 0 for r in secondary)
    notes.append(
        f"交易升温的增量：在已经低估且趋势改善的日期中，年度控制后的收益差{positive_activity}/3个周期为正；详见下表，不能仅凭放量认定机会更好。"
    )
    last_year = int(features.index.year.max())
    cheap_last_year = int(((features.index.year == last_year) & (features[PB] <= spec["pb_ceiling"])).sum())
    if cheap_last_year == 0:
        notes.append(
            f"固定30%门槛在{last_year}年没有触发低估状态，分年表中该年两组为空不是漏算；"
            "说明这条规则没有覆盖所有行情，不能把缺少信号解释成成功避险。"
        )
    notes.append(
        "部分估值指标的原始相关与控制年度后的方向不同，说明年度行情影响很大；"
        "不能只挑有利的统计口径认定有效。"
    )
    images = [
        "<img class='chart' alt='"
        + html.escape(p.stem)
        + "' src='data:image/png;base64,"
        + base64.b64encode(p.read_bytes()).decode()
        + "'>"
        for p in paths
    ]
    state_tables, combo_tables, single_tables, annual_tables, bin_tables = [], [], [], [], []
    for horizon in HORIZONS:
        state_rows = [r for r in result["state_groups"] if r["horizon"] == horizon]
        state_tables.append(
            f"<div data-horizon='{horizon}'><table><thead><tr><th>状态</th><th>有效信号日</th>"
            "<th>平均收益</th><th>相对宽基差</th><th>平均区间回撤</th><th>区间最低浮动收益</th>"
            "<th>不重叠观察数</th><th>不重叠平均收益</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{r['name']}</td><td>{r['days']}</td><td>{fmt(r['mean_bank_return'])}</td>"
                f"<td>{fmt(r['mean_active_return'])}</td><td>{fmt(r['mean_max_drawdown'])}</td>"
                f"<td>{fmt(r['mean_minimum_return'])}</td><td>{r['nonoverlap_count']}</td>"
                f"<td>{fmt(r['nonoverlap_mean_bank_return'])}</td></tr>"
                for r in state_rows
            )
            + "</tbody></table></div>"
        )
        combo_rows = [r for r in result["combinations"] if r["horizon"] == horizon]
        combo_tables.append(
            f"<div data-horizon='{horizon}'><table><thead><tr><th>比较</th><th>目标</th><th>两组观察日</th>"
            "<th>原始均值差</th><th>控制年度后差值</th><th>年度控制/HAC 95%区间</th>"
            "<th>块重采样原始差95%区间</th><th>Holm校正p</th></tr></thead><tbody>"
            + "".join(combo_row(r) for r in combo_rows)
            + "</tbody></table></div>"
        )
        sr = [r for r in result["single_tests"] if r["horizon"] == horizon]
        single_tables.append(
            f"<div data-horizon='{horizon}'><table><thead><tr><th>指标</th><th>目标</th><th>有效日</th>"
            "<th>预设方向</th><th>秩相关</th><th>年度控制斜率</th><th>Holm校正p</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{html.escape(r['name'])}</td><td>{target_name(r['target'])}</td>"
                f"<td>{r['n']}</td><td>{direction_name(r['direction'])}</td><td>{fmt(r['spearman'], 'num')}</td>"
                f"<td>{fmt(r['coefficient'], 'num')}</td><td>{fmt(r['holm_p'], 'num')}</td></tr>"
                for r in sr
            )
            + "</tbody></table></div>"
        )
        ar = [
            r
            for r in result["chronological"]
            if r["horizon"] == horizon and r["target"] == "bank_return" and r["combination"] == "PRIMARY_TREND"
        ]
        annual_tables.append(
            f"<div data-horizon='{horizon}'><table><thead><tr><th>信号时期</th><th>趋势改善日</th>"
            "<th>未改善日</th><th>趋势改善收益</th><th>未改善收益</th><th>差值</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{r['period']}</td><td>{r['treated_days']}</td><td>{r['control_days']}</td>"
                f"<td>{fmt(r['treated_mean'])}</td><td>{fmt(r['control_mean'])}</td>"
                f"<td>{fmt(r['difference'])}</td></tr>"
                for r in ar
            )
            + "</tbody></table></div>"
        )
        br = [r for r in result["factor_groups"] if r["horizon"] == horizon]
        bin_tables.append(
            f"<div data-horizon='{horizon}'><table><thead><tr><th>指标</th><th>时期</th><th>冻结组别</th>"
            "<th>有效日</th><th>平均银行收益</th><th>相对宽基差</th><th>平均区间回撤</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{html.escape(r['name'])}</td><td>{r['period']}</td><td>{r['group'] + 1}</td>"
                f"<td>{r['days']}</td><td>{fmt(r['mean_bank_return'])}</td>"
                f"<td>{fmt(r['mean_active_return'])}</td><td>{fmt(r['mean_max_drawdown'])}</td></tr>"
                for r in br
            )
            + "</tbody></table></div>"
        )
    coverage = (
        "<table><thead><tr><th>指标</th><th>有效研究日 / 1455</th><th>末日数值</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{html.escape(v['name'])}</td><td>{v['valid_days']} / {result['features']}</td>"
            f"<td>{fmt(v['value'], 'num')}</td></tr>"
            for v in result["latest"].values()
        )
        + "</tbody></table>"
    )
    label_table = (
        "<table><thead><tr><th>周期</th><th>收益标签状态</th><th>日期数</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{r['horizon']}日</td><td>{label_status_name(r['status'])}</td><td>{r['count']}</td></tr>"
            for r in result["label_status_counts"]
        )
        + "</tbody></table>"
    )
    markdown = (
        "# 银行板块研究报告：估值、趋势与交易活跃度\n\n"
        f"研究范围：{result['start']} 至 {result['end']}。报告日期：{spec['created_at'][:10]}。\n\n"
        + "\n\n".join(notes)
        + "\n\n"
        + "| 周期 | 低估且趋势改善：平均收益 | 低估且未改善：平均收益 | 年度控制差值 | Holm p |\n"
        + "| --- | ---: | ---: | ---: | ---: |\n"
        + "\n".join(
            f"| {r['horizon']}日 | {fmt(r['treated']['mean_bank_return'])} | "
            f"{fmt(r['control']['mean_bank_return'])} | {fmt(r['coefficient'])} | {fmt(r['holm_p'], 'num')} |"
            for r in primary
        )
        + "\n\n数据和方法限制：\n\n"
        + "\n".join("- " + t for t in chinese_limits())
        + "\n\n完整图表与全量结果见同目录《银行板块研究报告.html》及results.json。\n"
    )
    with (folder / "report.md").open("x", encoding="utf-8") as stream:
        stream.write(markdown)
    document = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>银行板块研究报告｜估值、趋势与交易活跃度</title><style>
:root{--green:#147956;
--ink:#243c32;
--muted:#677a70}
*{box-sizing:border-box}
body{margin:0;
background:#f3f4ed;
color:var(--ink);
font:15px/1.75 'Microsoft YaHei',system-ui,sans-serif}

main{max-width:1260px;
margin:0 auto;
padding:36px 26px 72px}
header{background:linear-gradient(125deg,#123d2d,#226f4e);
color:white;
padding:34px;
border-radius:20px}

.eyebrow{font-size:12px;
letter-spacing:2px;
color:#acd5b5}
h1{font-size:30px;
margin:12px 0}
h2{font-size:23px;
margin:0 0 18px}
h3{font-size:18px;
margin-top:24px}

.meta{color:#d3e4d8}
section{background:white;
border:1px solid #dce6dd;
border-radius:18px;
padding:28px;
margin-top:24px}
.lead{font-size:18px;
font-weight:600;
color:#145b3d}

.notice{background:#f6efd9;
border-left:4px solid #c59634;
padding:16px 20px}
.chart{display:block;
width:100%;
height:auto;
margin:20px auto;
border-radius:8px}

table{border-collapse:collapse;
width:100%;
font-size:13px}
th{background:#edf4ee;
color:#1d6242}
th,td{border-bottom:1px solid #e3e9e4;
padding:10px 9px;
text-align:right;
white-space:nowrap}
th:first-child,td:first-child{text-align:left;
white-space:normal;
min-width:125px}

.scroll{overflow-x:auto}
.muted{color:var(--muted);
font-size:13px}
.controls{position:sticky;
top:0;
z-index:5;
background:#f3f4edf5;
border-bottom:1px solid #d3ded4;
padding:13px 5px;
display:flex;
gap:10px;
align-items:center;
flex-wrap:wrap}

button{border:1px solid #bfd5c5;
background:white;
color:var(--green);
border-radius:8px;
padding:9px 20px;
font:inherit;
cursor:pointer}
button.active{background:var(--green);
color:white}

ul{padding-left:22px}
li{margin:8px 0}
a{color:#147956}
details{margin:18px 0}
summary{cursor:pointer;
font-weight:600}
code{word-break:break-all;
white-space:normal;
font-size:12px}

[data-horizon]{display:none}
[data-horizon='60']{display:block}
@media print{body{background:white}
.controls{display:none}
section{break-inside:avoid;
border:0;
padding:15px}
main{padding:0}
.chart{max-height:700px;
object-fit:contain}
[data-horizon]{display:block!important}
header{background:#fff;
color:#222}
.meta,.eyebrow{color:#555}
}

</style></head><body><main>"""
    document += (
        f"<header><div class='eyebrow'>BANK SECTOR RESEARCH · 历史信号研究</div>"
        f"<h1>银行板块：便宜之后，怎样判断进场节奏？</h1><p class='meta'>研究范围 {result['start']} — {result['end']}"
        f" · 生成日期 {spec['created_at'][:10]}</p><p>估值 × 趋势改善 × 交易活跃度</p></header>"
        "<div class='controls'><b>后续观察周期：</b><button data-select='20'>20交易日</button>"
        "<button data-select='60' class='active'>60交易日</button><button data-select='120'>120交易日</button>"
        "<span class='muted'>切换下方全部结果表；属于历史条件收益，不是仓位回测。</span></div>"
        "<section><h2>先看结论</h2><p class='lead'>"
        + html.escape(notes[0])
        + "</p><ul>"
        + "".join("<li>" + html.escape(n) + "</li>" for n in notes[1:])
        + "</ul><div class='notice'>末日截面是2025-12-31，不能当作今天的交易信号。"
        "2026年6月底只是用户举例，不是本轮专门研究对象。</div></section>"
        "<section><h2>本轮怎样定义“低估”和“热度上升”</h2><ul>"
        "<li><b>低估：</b>各银行自身PB历史分位的板块均值≤30%。这是本轮事先固定的研究阈值，未搜索最优值。</li>"
        "<li><b>趋势改善：</b>站上120日均线的银行比例，比20个交易日前更高。比较含分红趋势，避免除息机械干扰。</li>"
        "<li><b>交易升温：</b>逐家计算换手率近20日均值÷近60日均值，再取银行中位数；大于1表示近期相对活跃。"
        "至少15/20、45/60个有效观测；板块至少10家且覆盖70%，缺失不填充。</li>"
        "<li><b>后续表现：</b>信号日之后第一个交易日收盘参考水平，到再经过20/60/120交易日的参考水平。"
        "两边统一税前含分红；窗口跨真实停牌断点或到达未观察尾部就留空。</li></ul>"
        + images[0]
        + "</section><section><h2>四种状态：后续收益与过程风险</h2><p class='muted'>"
        "同一段行情可能贡献许多连续信号日，这些不是独立机会。表中同时保留按时间先后抽取的不重叠观察；它也不等于完全独立样本。</p>"
        + images[1]
        + "<div class='scroll'>"
        + "".join(state_tables)
        + "</div></section>"
        "<section><h2>核心检验：低估时，趋势改善是否有额外帮助？</h2><p>第一项只比较同样低估的日期；第二项在低估且趋势改善日期内，"
        "比较交易升温与未升温。年度控制用于减少某个年份整体涨跌造成的混淆，仍不能证明因果。</p><div class='scroll'>"
        + "".join(combo_tables)
        + "</div><p class='muted'>原始均值差与年度控制差值回答不同问题，可能方向不同。"
        "HAC处理重叠及序列相关；块重采样保留完整时间序列缺口。全部90项计划检验一起作Holm校正，"
        "未能检验的项目保留p=1。渐近检验在有效行情阶段较少时仍不可靠。</p></section>"
        "<section><h2>是否只在某几年成立？</h2><p>低估条件内趋势改善与未改善的比较。"
        "每个标签必须在信号所属年份内结束；因此120日会少于20日。早期和后期都已被查看，属于时间稳定性诊断，不是未见样本外验证。</p>"
        "<div class='scroll'>" + "".join(annual_tables) + "</div></section>"
        "<section><h2>完整指标检验：不只展示最好的结果</h2><p class='muted'>原有10项板块指标及3项本轮衍生观察。"
        "秩相关是未控制年度的描述；斜率控制年度，单位是指标增加1单位时的收益变化，不用于直接比较指标排名。"
        "“观察”表示没有预先规定交易方向。</p><div class='scroll'>" + "".join(single_tables) + "</div>"
        "<details><summary>各指标分组表现：早期分界固定后，查看后期是否仍有规律</summary>"
        "<p>只用2020—2022的指标数值确定五组分界，固定后应用到2023—2025。组别从低值到高值；"
        "遇到大量相同值时合并重复分界，因此不保证五组样本均匀，也不保证每组都有观察。"
        "分组用于解释，没有据此选参数。所有收益标签均要求在信号所属年份内结束。</p><div class='scroll'>"
        + "".join(bin_tables)
        + "</div></details>"
        "<details><summary>指标信息重复程度</summary>" + images[2] + "<p>相关指标不宜简单叠加权重；"
        "低PB、高股息和低估高息占比本来就共享信息。</p></details></section>"
        "<section><h2>覆盖、缺口与当前截面</h2><p>普通股净资产26/42的独立核实缺口，没有替代本轮已有日PB。"
        "质量池依各历史日期变化；末日32家不用于倒筛全部历史。本轮普通板块观察使用当天银行范围。</p>"
        "<div class='scroll'>"
        + coverage
        + "</div><h3>后续收益窗口为什么会缺失</h3><div class='scroll'>"
        + label_table
        + "</div><p class='muted'>银行参考篮子遇到所持银行真实停牌会中断，本轮严格丢弃跨断点收益窗口。"
        "这可能产生选择偏差；下一阶段应先完善可执行篮子的停牌持仓估值规则，不能把断点插值后声称有效。</p></section>"
        "<section><h2>这轮能支持什么，下一步做什么</h2><ul><li>先按上述证据保留或否定简单信号假设，不因某一周期收益好就选择它。</li>"
        "<li>若值得继续，冻结信号和持仓篮子后，再比较动态额度、长期持有和相近平均仓位的固定额度。</li>"
        "<li>可执行回测需另计分红到账/税、费用、滑点、停牌估值、换手和踏空风险；当前分析篮子收益不能当作账户收益。</li></ul>"
        "<h3>仍然存在的边界</h3><ul>"
        + "".join("<li>" + html.escape(t) + "</li>" for t in chinese_limits())
        + "</ul><details><summary>证据与可复现信息</summary><p>基础包：<code>"
        + spec["source_pack_id"]
        + "</code></p><p>实验规格哈希：<code>"
        + result["spec_sha256"]
        + "</code></p><p>同目录保存冻结实验、特征、收益标签、全量结果、分年表和分组表。"
        "原始归档、旧因子发布和旧报告均保留。</p></details>"
        "<p class='muted'>方法参考：<a href='https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf'>Bailey等：回测过拟合</a>；"
        "<a href='https://www.cfainstitute.org/insights/professional-learning/"
        "refresher-readings/2026/backtesting-and-simulation'>CFA Institute：回测中的前视及生存偏差</a>。"
        "本报告结论来自本项目结构化结果，不是上述来源对银行择时有效性的背书。</p></section></main>"
        "<script>document.querySelectorAll('[data-select]').forEach(b=>b.addEventListener('click',()=>{"
        "document.querySelectorAll('[data-select]').forEach(x=>x.classList.toggle('active',x===b));"
        "document.querySelectorAll('[data-horizon]').forEach(x=>x.style.display=x.dataset.horizon===b.dataset.select?'block':'none');}));</script>"
        "</body></html>"
    )
    with (folder / "银行板块研究报告.html").open("x", encoding="utf-8") as stream:
        stream.write(document)
    write_json(
        folder / "report_manifest.json",
        {
            "report_sha256": sha(folder / "银行板块研究报告.html"),
            "results_sha256": sha(evidence_folder / "results.json"),
            "spec_sha256": sha(evidence_folder / "experiment_spec.json"),
            "feature_sha256": sha(evidence_folder / "features.parquet"),
            "labels_sha256": sha(evidence_folder / "labels.parquet"),
            "charts": {p.name: sha(p) for p in paths},
            "report_status": result["status"],
            "evidence_folder": str(evidence_folder.resolve()),
            "report_script_sha256": sha(__file__),
        },
    )


def target_name(target):
    return "银行收益" if target == "bank_return" else "相对宽基收益差"


def direction_name(direction):
    return {"HIGH": "高值有利假设", "LOW": "低值有利假设", "DIAGNOSTIC": "观察"}[direction]


def label_status_name(status):
    return {
        "READY_ANALYTICAL": "完整分析窗口",
        "BANK_PATH_BREAK": "银行篮子连续路径中断",
        "BENCHMARK_PATH_BREAK": "宽基连续路径中断",
        "RELEASED_TAIL_UNOBSERVED": "区间尾部尚无完整后续数据",
    }[status]


def combo_row(row):
    b = row["bootstrap"]
    name = "低估内：趋势改善 − 未改善" if row["name"] == "PRIMARY_TREND" else "低估且趋势改善内：交易升温 − 未升温"
    return (
        f"<tr><td>{name}</td><td>{target_name(row['target'])}</td>"
        f"<td>{row['treated']['days']} / {row['control']['days']}</td>"
        f"<td>{fmt(b['mean_difference'])}</td><td>{fmt(row['coefficient'])}</td>"
        f"<td>{fmt(row['confidence_lower'])} ～ {fmt(row['confidence_upper'])}</td>"
        f"<td>{fmt(b['confidence_lower'])} ～ {fmt(b['confidence_upper'])}</td>"
        f"<td>{fmt(row['holm_p'], 'num')}</td></tr>"
    )


def chinese_limits():
    return [
        "2020—2025已经暴露；本轮没有未见样本外证据，所有结论是探索性历史关联。",
        "当前42家来源样本经过历史范围过滤，但完整历史行业成员和财务首次披露版本认证尚不完备。",
        "含分红序列采用税前经济再投资口径，区别于实际分红到账、税后及可成交账户净值。",
        "真实停牌造成篮子断点；丢弃跨断点窗口可能影响研究代表性，不能解释为停牌期间真实收益缺失。",
        "资本充足率跨监管口径的可比性尚未完整认证，综合经营指标只作研究观察。",
        "换手率来源保留原始归档，但历史可用时间与修订版本仍未独立全面认证。",
        "银行相对宽基走强可能只是跌得更少，不足以说明整个市场进入某种风格。",
        "没有执行仓位回测、成本/容量检验或M4认证；不生成当前投资建议。",
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "analyse", "render"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args.root, args.root / "reports/bank_sector_research")
    elif args.phase == "analyse":
        if not args.run_dir:
            parser.error("analyse requires --run-dir")
        analyse(args.run_dir)
    else:
        if not args.run_dir:
            parser.error("render requires --run-dir")
        evidence = args.run_dir.resolve()
        result = json.loads((evidence / "results.json").read_bytes())
        spec = json.loads((evidence / "experiment_spec.json").read_bytes())
        old_manifest = json.loads((evidence / "report_manifest.json").read_bytes())
        for key, name in [
            ("results_sha256", "results.json"),
            ("spec_sha256", "experiment_spec.json"),
            ("feature_sha256", "features.parquet"),
            ("labels_sha256", "labels.parquet"),
        ]:
            if sha(evidence / name) != old_manifest[key]:
                raise ValueError("report evidence changed")
        output = evidence / ("report-" + sha(__file__)[:12])
        output.mkdir(exist_ok=False)
        features = pd.read_parquet(evidence / "features.parquet").set_index("session")
        render_report(output, result, spec, features, evidence_folder=evidence)
        print(
            json.dumps(
                {
                    "status": "REPORT_REGENERATED_FROM_SAME_EVIDENCE",
                    "report": str(output / "银行板块研究报告.html"),
                    "reason": "clarify negative test result and improve chart label spacing; no new searches",
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
