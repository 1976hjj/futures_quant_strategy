"""Frozen exploratory dividend-yield rebound diagnostics, not a trading backtest.

Separate prepare/analyse processes. Only released history through 2025 is used;
the previously chart-authorized 2026 observations are excluded from both stages.
"""
from __future__ import annotations

import argparse
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
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.research.research_bank_sector_timing import (  # noqa: E402
    bootstrap_contrast, holm_adjust, nonoverlap_positions, serial, write_json,
)

SOURCE = ROOT / "reports/bank_sector_research/20261011-235644-dividend-price"
PREVIOUS = ROOT / "reports/bank_sector_research/20261011-215220"
HORIZONS = (20, 60, 120)
RULES = ("high80", "high90", "high80_cash_not_shrinking")
RULE_NAMES = {"high80": "历史最高20%", "high90": "历史最高10%",
              "high80_cash_not_shrinking": "最高20%且滚动分红同比未减少"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_daily(source, end, columns):
    # Predicate is applied inside the reader; no 2026 outcome values leave it.
    with duckdb.connect() as c:
        return c.execute(f"SELECT {','.join(columns)} FROM read_csv_auto(?) "
                         "WHERE session <= CAST(? AS DATE) ORDER BY session",
                         [str(source / "银行平均股息率与价格指数.csv"), end]).df().set_index("session")


def past_rank(series, window=756, minimum=504):
    """Compare current observation with preceding observations only; midrank ties."""
    a = np.asarray(series, float)
    out = np.full(len(a), np.nan)
    for i, value in enumerate(a):
        history = a[max(0, i-window):i]
        history = history[np.isfinite(history)]
        if np.isfinite(value) and len(history) >= minimum:
            out[i] = ((history < value).sum() + .5*(history == value).sum()) / len(history)
    return pd.Series(out, index=series.index)


def decomposition(current_cash, current_price, old_cash, old_price):
    """All four inputs must be on the same share basis and matched bank cohort."""
    return old_cash/current_price - old_cash/old_price, (current_cash-old_cash)/current_price


def cash_features(panel, events, calendar):
    # Pairs use the actual common cohort, never changing constituent averages.
    cash = panel.pivot(index="session", columns="ts_code", values="cash_per_current_share_365").reindex(calendar)
    price = panel.pivot(index="session", columns="ts_code", values="close").reindex(calendar)
    scales = pd.DataFrame(1., index=calendar, columns=cash.columns)
    for e in events.itertuples():
        if e.ts_code in scales and pd.notna(e.stk_div) and e.stk_div > 0:
            # Event happened and implementation announcement was already known.
            known = max(pd.Timestamp(e.ex_date), pd.Timestamp(e.imp_ann_date)+pd.Timedelta(days=1))
            scales.loc[scales.index >= known, e.ts_code] *= 1+e.stk_div
    rows = []
    for i, day in enumerate(calendar):
        row = {"session": day}
        for span in (20, 252):
            if i < span:
                continue
            old_scale = scales.iloc[i-span].to_numpy()/scales.iloc[i].to_numpy()
            dc = cash.iloc[i].to_numpy(); pc = price.iloc[i].to_numpy()
            ds = cash.iloc[i-span].to_numpy()*old_scale
            ps = price.iloc[i-span].to_numpy()*old_scale
            valid = np.isfinite(dc) & np.isfinite(ds) & np.isfinite(pc) & np.isfinite(ps) & (pc > 0) & (ps > 0)
            required = max(10, math.ceil(.7*int(cash.iloc[i].notna().sum())))
            row[f"matched_banks_{span}"] = int(valid.sum())
            if valid.sum() < required:
                continue
            if span == 20:
                p, d = decomposition(dc[valid], pc[valid], ds[valid], ps[valid])
                actual = np.mean(dc[valid]/pc[valid]-ds[valid]/ps[valid])
                if not np.isclose(actual, p.mean()+d.mean(), atol=1e-12):
                    raise ValueError("Matched yield-change decomposition failed")
                row.update(price_contribution20=p.mean(), cash_contribution20=d.mean(), matched_yield_change20=actual)
            elif ds[valid].sum() > 0:
                # Analytical rolling cash amount, not a forecast or announced cut.
                row["cash_yoy252"] = dc[valid].sum()/ds[valid].sum()-1
        rows.append(row)
    return pd.DataFrame(rows).set_index("session")


def prepare(source_chart=SOURCE, sector_reference=PREVIOUS):
    global SOURCE, PREVIOUS
    SOURCE, PREVIOUS = source_chart.resolve(), sector_reference.resolve()
    end = json.loads((ROOT/"config/bank_timing_data.json").read_bytes())["released_feature_end"]
    if end > "2025-12-31":
        raise ValueError("This experiment is explicitly bounded at 2025-12-31")
    parent = json.loads((SOURCE/"audit.json").read_bytes())
    for name in ("银行平均股息率与价格指数.csv", "individual_yield_inputs.parquet", "implemented_dividend_events.parquet"):
        if sha(SOURCE/name) != parent["files"][name]:
            raise ValueError(f"Changed archived chart source: {name}")
    previous = json.loads((PREVIOUS/"experiment_spec.json").read_bytes())
    pack = Path(previous["source_pack"])
    if previous["end"] > end or sha(pack/"manifest.json") != previous["source_manifest_sha256"]:
        raise ValueError("Invalid prior released basket pack")
    folder = ROOT/"reports/bank_sector_research"/(datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")+"-dividend-rebound")
    folder.mkdir(parents=True, exist_ok=False)
    prior_paths=[ROOT/'reports/bank_sector_research/20261011-215220/results.json',
                 ROOT/'reports/bank_sector_research/20261012-072739-dividend-rebound/results.json']
    if PREVIOUS/'results.json' not in prior_paths:prior_paths.append(PREVIOUS/'results.json')
    prior_specs=[];prior_count=0
    for p in prior_paths:
        old=json.loads(p.read_bytes())
        count=len(old.get('tests',old.get('single_tests',[])+old.get('combinations',[])))
        prior_count+=count;prior_specs.append({'path':str(p),'sha256':sha(p),'count':count})
    cycle=parent.get('yield_policy')=='annual_dividend_cycle_2.0.0'
    spec = {
        "version": "bank-dividend-rebound-2.0.0", "end": end, "feature_start": "2016-01-04",
        "status": "EXPOSED_HISTORY_EXPLORATORY_RESEARCH_ONLY_NOT_OOS_NOT_TRADABLE",
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "hypothesis": "Historically high implemented cash dividend yield precedes a bank price rebound",
        "primary": {"rule": "high80", "horizon": 60, "target": "price", "model": "raw"},
        "yield": "equal-bank mean of implemented annual dividend-cycle cash/raw close" if cycle else "legacy trailing365-calendar-day cash/raw close",
        "yield_field": "cash_yield_cycle" if cycle else "cash_yield365",
        "cash_field": "cash_per_current_share_cycle" if cycle else "cash_per_current_share_365",
        "past_percentile": {"window": 756, "minimum": 504, "exclude_current": True, "ties": "midrank"},
        "rules": list(RULES), "cutpoints": [.8, .9], "horizons": list(HORIZONS),
        "targets": ["price", "basket_total"], "models": ["raw", "controlled"],
        "labels": "signal T close; analytical entry T+1 close; exit T+1+h close; no unobserved tail or broken segment",
        "controls": "calendar-year and month fixed effects, prior60-session price return and volatility",
        "family_size": 36, "related_prior_tests": prior_count, "combined_family_size": prior_count+36,
        "related_results": prior_specs,
        "multiple_testing": "Holm current36 and all related previous tests including superseded365-cycle studies; unavailable p=1; manual chart selection not quantifiable",
        "hac": "Bartlett horizon+20 lags; calendar gaps preserved; asymptotic two-sided p",
        "bootstrap": {"resamples": 2999, "block": "max(60,horizon+20)", "seed": 20261012},
        "nonoverlap": "greedy chronological sampling each signal separately, spacing h+1; descriptive only",
        "chronology": "2018-2021 and 2022-2025; labels must end within each subperiod; not virgin OOS",
        "absolute_yield_buckets": [0,.04,.05,.06,1], "buckets_use": "descriptive only; no best-cut selection",
        "cash_diagnostic": "matched-bank 20-session exact numerator/denominator decomposition; prior cash and prices rescaled for free stock distributions; 252-session cash同比",
        "source_chart": str(SOURCE), "source_sha256": parent["files"],
        "basket_path": str(pack/"bank_equal_basket_daily.parquet"),
        "basket_sha256": previous["source_files"]["bank_equal_basket_daily.parquet"],
        "previous_results": str(PREVIOUS/"results.json"), "previous_sha256": sha(PREVIOUS/"results.json"),
        "constitution_sha256": sha(ROOT/"RESEARCH_CONSTITUTION.md"), "script_sha256": sha(__file__),
        "limitations": ["2026 only authorized for chart; no2026 features/forward returns here",
            "historical dividend snapshots and industry first-disclosure versions not fully certified",
            "current42 archive subset with historical membership; missing/unverified cycle is excluded, not zero",
            "cycle cash reference can lag in frequency transitions; stale/ambiguous cycles are flagged",
            "equal-bank yield and market-cap index weights differ; total-return basket is a different analytical target",
            "rights/other non-free capital changes incompletely normalized",
            "no fees, tax, slippage, tradable fills or capacity; price index excludes cash dividends",
            "already viewed prices and yields; not an independent confirmatory test"],
    }
    write_json(folder/"experiment_spec.json", spec)  # Before any label calculation.
    d = read_daily(SOURCE, end, ["session", "mean_yield", "median_yield", "valid_banks", "priced_banks", "price_index"])
    d.index = pd.DatetimeIndex(d.index)
    if not d.index.is_unique or not d.index.is_monotonic_increasing:
        raise ValueError("Invalid calendar")
    with duckdb.connect() as c:
        panel = c.execute("SELECT * FROM read_parquet(?) WHERE session<=CAST(? AS DATE)",
                          [str(SOURCE/"individual_yield_inputs.parquet"), end]).df()
        events = c.execute("SELECT * FROM read_parquet(?) WHERE ex_date<=CAST(? AS DATE) AND imp_ann_date IS NOT NULL",
                           [str(SOURCE/"implemented_dividend_events.parquet"), end]).df()
    panel["session"] = pd.to_datetime(panel.session)
    actual = panel.groupby("session")[spec['yield_field']].mean().reindex(d.index)
    if not np.allclose(actual, d.mean_yield, atol=1e-12, equal_nan=True):
        raise ValueError("Panel/chart mean differs")
    known = (d.valid_banks >= 10) & (d.valid_banks/d.priced_banks >= .7)
    f = d.drop(columns="price_index").copy()
    f["yield_rank"] = past_rank(d.mean_yield.where(known))
    f["median_rank"] = past_rank(d.median_yield.where(known))
    f["past_return60"] = d.price_index/d.price_index.shift(60)-1
    f["past_vol60"] = d.price_index.pct_change(fill_method=None).rolling(60, min_periods=60).std()
    if cycle:panel=panel.rename(columns={spec['cash_field']:'cash_per_current_share_365'})
    f = f.join(cash_features(panel, events, d.index))
    for key, threshold in (("high80", .8), ("high90", .9)):
        f[key] = (f.yield_rank >= threshold).astype(float).where(f.yield_rank.notna())
    f["high80_cash_not_shrinking"] = (f.high80.eq(1) & f.cash_yoy252.ge(0)).astype(float).where(f.high80.notna() & f.cash_yoy252.notna())
    f.to_parquet(folder/"features.parquet")
    write_json(folder/"feature_manifest.json", {"experiment_sha256": sha(folder/"experiment_spec.json"),
        "features_sha256": sha(folder/"features.parquet"), "rows": len(f),
        "ready_rows": int(f.yield_rank.notna().sum()), "first_ready": str(f[f.yield_rank.notna()].index.min().date()),
        "maximum_date_read": str(d.index.max().date()), "no2026_values": True, "forward_labels_computed": False,
        "matched_decomposition_checked": True, "aggregate_recomputed_from_bank_panel": True})
    print(json.dumps({"run_dir": str(folder), "phase": "PREPARED", "ready_rows": int(f.yield_rank.notna().sum())}, ensure_ascii=False))


def labels(levels, sessions, horizons=HORIZONS, segments=None, ready=None):
    levels = np.asarray(levels, float)
    segments = np.zeros(len(levels)) if segments is None else np.asarray(segments)
    ready = np.ones(len(levels), bool) if ready is None else np.asarray(ready, bool)
    records = []
    for i, day in enumerate(sessions):
        for h in horizons:
            a, b = i+1, i+1+h
            row = {"session": day, "horizon": h, "entry": pd.NaT, "exit": pd.NaT,
                   "return": np.nan, "drawdown": np.nan, "min_return": np.nan, "max_return": np.nan, "status": "TAIL"}
            if b < len(levels):
                row.update(entry=sessions[a], exit=sessions[b])
                path = levels[a:b+1]
                if np.isfinite(path).all() and (path > 0).all() and ready[a:b+1].all() and len(set(segments[a:b+1])) == 1:
                    row.update({"return": path[-1]/path[0]-1,
                        "drawdown": np.min(path/np.maximum.accumulate(path)-1), "min_return": np.min(path/path[0]-1),
                        "max_return": np.max(path/path[0]-1), "status": "READY"})
                else:
                    row["status"] = "PATH_BREAK"
            records.append(row)
    return pd.DataFrame(records)


def regression(signal, outcome, f, lag, controlled):
    x, y = np.asarray(signal, float), np.asarray(outcome, float)
    design = [np.ones(len(f)), x]
    if controlled:
        eligible = np.isfinite(x) & np.isfinite(y) & f.past_return60.notna().to_numpy() & f.past_vol60.notna().to_numpy()
        years = sorted(set(f.index.year[eligible]))
        design += [(f.index.year == year).astype(float) for year in years[1:]]
        design += [(f.index.month == month).astype(float) for month in range(2,13)]
        design += [f.past_return60.to_numpy(), f.past_vol60.to_numpy()]
    design = np.column_stack(design)
    valid = np.isfinite(y) & np.isfinite(design).all(axis=1)
    n = valid.sum()
    result = {"n": int(n), "coefficient": None, "p_value": None, "se": None, "ci_low": None, "ci_high": None}
    if n < 80 or min((x[valid] == 1).sum(), (x[valid] == 0).sum()) < 30:
        return result
    # Remove fixed effects that are identically zero after feature warmup.
    active = np.r_[True, True, (np.abs(design[valid,2:]).sum(axis=0) > 0)]
    design = design[:,active]; xx = design[valid]
    if np.linalg.matrix_rank(xx) < xx.shape[1]:
        return result
    inv = np.linalg.inv(xx.T@xx); beta = inv@xx.T@y[valid]
    scores = np.zeros_like(design); scores[valid] = xx*(y[valid]-xx@beta)[:,None]
    meat = scores.T@scores
    for offset in range(1,min(lag,len(f)-1)+1):
        cross = scores[offset:].T@scores[:-offset]
        meat += (1-offset/(lag+1))*(cross+cross.T)
    cov = inv@meat@inv*n/(n-xx.shape[1])
    se = math.sqrt(max(0,cov[1,1])); b = float(beta[1])
    result.update(coefficient=b, se=se, p_value=math.erfc(abs(b/se)/math.sqrt(2)) if se > 0 else 1.,
                  ci_low=b-1.96*se, ci_high=b+1.96*se)
    return result


def summary(y, signal, h):
    mask = np.asarray(signal, bool) & y["return"].notna().to_numpy()
    g = y.loc[mask]; positions = nonoverlap_positions(mask, h)
    if not len(g):
        return {"days": 0, "nonoverlap_n": 0}
    selected = y.iloc[positions]
    return {"days": len(g), "mean_return": g["return"].mean(), "median_return": g["return"].median(),
            "win_rate": g["return"].gt(0).mean(), "mean_drawdown": g.drawdown.mean(),
            "mean_min_return": g.min_return.mean(), "tail_p10": g["return"].quantile(.1),
            "reached_plus5": g.max_return.ge(.05).mean(), "nonoverlap_n": len(positions),
            "nonoverlap_mean": selected["return"].mean(), "nonoverlap_win": selected["return"].gt(0).mean()}


def analyse(folder):
    spec = json.loads((folder/"experiment_spec.json").read_bytes())
    fm = json.loads((folder/"feature_manifest.json").read_bytes())
    if sha(folder/"experiment_spec.json") != fm["experiment_sha256"] or sha(folder/"features.parquet") != fm["features_sha256"]:
        raise ValueError("Frozen experiment/features changed")
    if sha(__file__) != spec["script_sha256"]:
        raise ValueError("Script changed after pre-registration; prepare a new experiment")
    f = pd.read_parquet(folder/"features.parquet")
    source_csv = Path(spec["source_chart"])/"银行平均股息率与价格指数.csv"
    if sha(source_csv) != spec["source_sha256"][source_csv.name]:
        raise ValueError("Archived price/yield source changed after feature freeze")
    d = read_daily(Path(spec["source_chart"]), spec["end"], ["session", "price_index"])
    d.index = pd.DatetimeIndex(d.index)
    if not d.index.equals(f.index) or d.index.max() > pd.Timestamp(spec["end"]):
        raise ValueError("Outcome calendar/release mismatch")
    if sha(spec["basket_path"]) != spec["basket_sha256"]:
        raise ValueError("Total-return basket changed")
    basket = pd.read_parquet(spec["basket_path"])
    basket = basket.set_index(pd.to_datetime(basket.session)).reindex(f.index)
    outcomes = {"price": labels(d.price_index, f.index),
        "basket_total": labels(basket.bank_equal_total_return_index, f.index,
            segments=basket.basket_segment, ready=basket.status.eq("READY"))}
    tests, groups, chronology, events, buckets = [], [], [], [], []
    for target, lab in outcomes.items():
        lab.assign(target=target).to_parquet(folder/f"labels_{target}.parquet", index=False)
        for h in HORIZONS:
            y = lab[lab.horizon.eq(h)].set_index("session").reindex(f.index)
            known = f.yield_rank.notna()
            groups.append(dict(target=target, horizon=h, rule="all", group="all", **summary(y,known,h)))
            for rule in RULES:
                high = f[rule].eq(1); low = f[rule].eq(0)
                for name, mask in (("high",high),("other",low)):
                    groups.append(dict(target=target, horizon=h, rule=rule, group=name, **summary(y,mask,h)))
                boot = bootstrap_contrast(y["return"], high, low, max(60,h+20), 20261012+h+RULES.index(rule)*100,2999)
                for model in ("raw","controlled"):
                    test = regression(f[rule], y["return"], f, h+20, model=="controlled")
                    tests.append(dict(target=target, horizon=h, rule=rule, model=model, **test,
                                      bootstrap=boot if model=="raw" else None))
                for start,end in (("2018-01-01","2021-12-31"),("2022-01-01","2025-12-31")):
                    part = f.index.to_series().between(start,end).to_numpy() & y.exit.le(pd.Timestamp(end)).to_numpy()
                    chronology.append(dict(target=target,horizon=h,rule=rule,period=start[:4]+"-"+end[:4],
                        high=summary(y,high&part,h),other=summary(y,low&part,h)))
                # Threshold-entry episodes, separated by a minimum h+1 sessions.
                crosses = high & ~high.shift(1,fill_value=False)
                positions = nonoverlap_positions(crosses.to_numpy() & y["return"].notna().to_numpy(),h)
                for i in positions:
                    events.append({"target":target,"rule":rule,"session":f.index[i],
                        "mean_yield":f.mean_yield.iloc[i],"rank":f.yield_rank.iloc[i],"cash_yoy252":f.cash_yoy252.iloc[i],
                        **y.iloc[i].to_dict()})
            if target == "price":
                edges = spec["absolute_yield_buckets"]
                for a,b in zip(edges[:-1],edges[1:]):
                    mask = known & f.mean_yield.ge(a) & f.mean_yield.lt(b)
                    buckets.append(dict(horizon=h,lower=a,upper=b,**summary(y,mask,h)))
    if len(tests) != 36:
        raise ValueError("Frozen testing family incomplete")
    if sha(spec["previous_results"]) != spec["previous_sha256"]:
        raise ValueError("Related prior results changed")
    prior_tests=[]
    for old_spec in spec['related_results']:
        p=Path(old_spec['path'])
        if sha(p)!=old_spec['sha256']:raise ValueError('Related evidence changed')
        old=json.loads(p.read_bytes())
        prior_tests+=old.get('tests',old.get('single_tests',[])+old.get('combinations',[]))
    if len(prior_tests) != spec['related_prior_tests']:
        raise ValueError("Related testing family size differs")
    local = holm_adjust([t["p_value"] for t in tests])
    joint = holm_adjust([t["p_value"] for t in prior_tests]+[t["p_value"] for t in tests])[len(prior_tests):]
    for t,p,q in zip(tests,local,joint):
        t.update(holm_current36=p, holm_joint=q)
    groupframe = pd.DataFrame(groups); groupframe.to_csv(folder/"group_statistics.csv", index=False,encoding="utf-8-sig")
    pd.DataFrame([{k:v for k,v in t.items() if k!="bootstrap"} for t in tests]).to_csv(folder/"hypothesis_statistics.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(events).to_csv(folder/"entry_episodes.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(buckets).to_csv(folder/"absolute_yield_buckets.csv",index=False,encoding="utf-8-sig")
    primary = next(t for t in tests if all(t[k]==v for k,v in spec["primary"].items()))
    results = {"status": spec["status"], "spec_sha256": sha(folder/"experiment_spec.json"),
        'joint_family_size':spec['combined_family_size'],'yield_policy':parent_policy(spec),
        "first_signal": fm["first_ready"], "last_outcome": str(d.index.max().date()),
        "primary": primary, "tests": tests, "groups": groups, "chronology": chronology,
        "entry_episodes": events, "absolute_buckets": buckets,
        "price_label_status": outcomes["price"].status.value_counts().to_dict(),
        "basket_label_status": outcomes["basket_total"].status.value_counts().to_dict(),
        "matched_decomposition_rows": int(f.matched_yield_change20.notna().sum()),
        "mean_rank_median_rank_correlation": f[["yield_rank","median_rank"]].corr().iloc[0,1],
        "source_limitations": spec["limitations"],
        "statistics_references": ["https://www.statsmodels.org/dev/generated/statsmodels.stats.sandwich_covariance.cov_hac.html"]}
    write_json(folder/"results.json",results)
    render_report(folder,results,f,outcomes["price"])
    write_json(folder/"audit.json", {"maximum_date_read": str(d.index.max().date()),"no2026_values": True,
        "frozen_spec_verified": True, "family_size": 36, "joint_family_size":spec['combined_family_size'],
        "script_sha256":sha(__file__),"source_archive_unchanged":True,
        "files":{p.name:sha(p) for p in folder.iterdir() if p.is_file()}})
    print(json.dumps(serial({"run_dir":str(folder),"primary":primary,"groups":groups[:7]}),ensure_ascii=False))


def parent_policy(spec):
    return 'annual_dividend_cycle_2.0.0' if spec['yield_field']=='cash_yield_cycle' else 'legacy365'


def render_report(folder,r,f,lab):
    def pct(x):
        return "不足" if x is None or not np.isfinite(x) else f"{100*x:.2f}%"
    lines = ["# 银行股息率高位与后续反弹：统计研究", "",
        f"信号从{r['first_signal']}起可用，所有未来收益截至{r['last_outcome']}。仅已查看历史的探索研究；不属于策略回测或独立样本外验证。", "",
        "## 事前固定的检验", "",
        "新版周期口径：年度一次计一次，中期与末期按所属期间覆盖一年；首次增加中期不叠加此前整个年度。缺期、来源异常与过期参考留空。高位按前756交易日最高20%定义，至少504天历史。主检验是未来60日，T+1收盘作起点。", "",
        "| 期限 | 高位平均价格收益 | 其余日期平均收益 | 高位上涨比例 | 其余上涨比例 | 高位平均期间最大回撤 | 高位非重叠窗口数 |",
        "|---|---:|---:|---:|---:|---:|---:|"]
    for h in HORIZONS:
        a = next(g for g in r["groups"] if (g["target"],g["horizon"],g["rule"],g["group"]) == ("price",h,"high80","high"))
        b = next(g for g in r["groups"] if (g["target"],g["horizon"],g["rule"],g["group"]) == ("price",h,"high80","other"))
        lines.append(f"| {h}交易日 | {pct(a.get('mean_return'))} | {pct(b.get('mean_return'))} | {pct(a.get('win_rate'))} | {pct(b.get('win_rate'))} | {pct(a.get('mean_drawdown'))} | {a['nonoverlap_n']} |")
    p = r["primary"]; boot = p["bootstrap"]
    controlled = next(t for t in r["tests"] if t["target"]=="price" and t["rule"]=="high80" and t["horizon"]==60 and t["model"]=="controlled")
    lines += ["", "## 信号强度及不确定性", "",
        f"主检验高位与其他日期的均值差：{pct(p['coefficient'])}（百分比点）；循环时间块自助法95%区间[{pct(boot['confidence_lower'])}, {pct(boot['confidence_upper'])}]。",
        f"原始HAC p={p['p_value']:.4g}；本轮36项Holm校正p={p['holm_current36']:.4g}；保留此前研究的{r['joint_family_size']}项联合校正p={p['holm_joint']:.4g}。",
        f"控制年份、月份、此前60日收益与波动后，高位效应{pct(controlled['coefficient'])}，合并校正p={controlled['holm_joint']:.4g}。",
        "每日收益窗口会重叠，不能把几百个高位日视为几百次独立机会。HAC保留原日历缺口；时间块区间处理序列相关。非重叠窗口也不能保证完全独立。",
        "统计方法参考：[statsmodels HAC官方说明](https://www.statsmodels.org/dev/generated/statsmodels.stats.sandwich_covariance.cov_hac.html)。", "",
        "## 前后时期是否一致", "",
        "| 历史子时期 | 高位日期数 | 高位60日平均收益 | 其他日期平均收益 | 高位上涨比例 |",
        "|---|---:|---:|---:|---:|"]
    for c in r["chronology"]:
        if c["target"]=="price" and c["rule"]=="high80" and c["horizon"]==60:
            lines.append(f"| {c['period']} | {c['high']['days']} | {pct(c['high'].get('mean_return'))} | {pct(c['other'].get('mean_return'))} | {pct(c['high'].get('win_rate'))} |")
    lines += ["", "## 是否只是分红和除息造成的", "",
        "同一批银行的20日股息率变化按股价和一年分红周期参考金额分解，送转股统一每股单位。新版不会因为旧分红超过365天而自动删除；频率过渡与未知期间单独标记。分红参考仍不是未来分红预测。", "",
        "| 检查 | 高位60日均值 | 其他日期均值 | 高位样本日数 | 合并校正p（原始模型） |", "|---|---:|---:|---:|---:|"]
    for rule,target in (("high90","price"),("high80_cash_not_shrinking","price"),("high80","basket_total")):
        a = next(g for g in r["groups"] if (g["target"],g["horizon"],g["rule"],g["group"]) == (target,60,rule,"high"))
        b = next(g for g in r["groups"] if (g["target"],g["horizon"],g["rule"],g["group"]) == (target,60,rule,"other"))
        t = next(t for t in r["tests"] if (t["target"],t["horizon"],t["rule"],t["model"]) == (target,60,rule,"raw"))
        name = RULE_NAMES[rule] if target=="price" else "等权含现金分红篮子（2020–2025，有断点）"
        lines.append(f"| {name} | {pct(a.get('mean_return'))} | {pct(b.get('mean_return'))} | {a['days']} | {t['holm_joint']:.4g} |")
    lines += ["", "含分红篮子是另外一条等权分析序列，并非399986官方全收益指数。只使用无断点且完整可观察的窗口；样本覆盖更短，不能混称同一个指数。", "",
        "## 原始数据、计算边界及交付", ""]
    lines += [f"- {s}" for s in r["source_limitations"]]
    lines += ["", "来源：上一张图的冻结日度股息率及个股明细，bank_token.duckdb的股价与已实施分红归档；中证银行399986价格指数来自东方财富。股息率等权，官方价格指数权重不同。", "",
        "已有图形观察与此前研究会影响假设提出。本轮与之前90项合并校正仍不能完全计量人工选择，不将显著p值等同于可盈利策略。", "",
        "结构化结果：results.json；每项检验：hypothesis_statistics.csv；样本与收益：group_statistics.csv；所有预设条件的非重叠入区事件：entry_episodes.csv。", "",
        "![研究图](股息率高位反弹研究.png)"]
    (folder/"研究报告.md").write_text("\n".join(lines),encoding="utf-8")
    # Standalone plain HTML, numbers generated from the same structured results.
    body = "<pre>"+html.escape("\n".join(lines[:-2]))+"</pre>"
    (folder/"银行股息率反弹研究.html").write_text('<!doctype html><meta charset="utf-8"><title>银行股息率反弹研究</title><style>body{max-width:1100px;margin:40px auto;background:#f7f8f4;color:#24382e;font:16px/1.7 system-ui}pre{white-space:pre-wrap}img{width:100%}</style>'+body+'<img src="股息率高位反弹研究.png">',encoding="utf-8")
    draw(folder,r,f)


def draw(folder,r,f):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.font_manager import FontProperties
    plt.rcParams["font.family"] = FontProperties(fname="C:/Windows/Fonts/msyh.ttc").get_name()
    plt.rcParams["axes.unicode_minus"] = False
    fig,axes = plt.subplots(2,1,figsize=(14,9),gridspec_kw={"height_ratios":[1.4,1]})
    fig.patch.set_facecolor("#f7f8f4")
    fig.suptitle("银行股息率高位与后续收益：历史统计",x=.08,y=.965,ha="left",fontsize=21,fontweight="bold")
    fig.text(.08,.915,"2016–2025历史；高位=过去三年最高20%；T+1收盘起算；尚未经过独立样本外验证",fontsize=11,color="#5b6c60")
    colors=["#166b56","#bb7546"]
    x=np.arange(3); width=.34
    for j,group in enumerate(("high","other")):
        gs=[next(g for g in r["groups"] if (g["target"],g["horizon"],g["rule"],g["group"])==("price",h,"high80",group)) for h in HORIZONS]
        values=np.array([g["mean_return"]*100 for g in gs])
        bars=axes[0].bar(x+(j-.5)*width,values,width,color=colors[j],label="股息率高位" if j==0 else "其他日期")
        for bar,g in zip(bars,gs):
            axes[0].text(bar.get_x()+bar.get_width()/2,bar.get_height()+.2,f"{g['mean_return']*100:.2f}%\n上涨{g['win_rate']*100:.0f}%",ha="center",fontsize=10)
    axes[0].set_xticks(x,[f"未来{h}个交易日" for h in HORIZONS]);axes[0].set_ylabel("平均价格收益（%）")
    axes[0].axhline(0,color="#889488",lw=.8);axes[0].grid(axis="y",alpha=.15);axes[0].legend(frameon=False,loc="upper left")
    axes[0].margins(y=.35)
    axes[1].plot(f.index,f.mean_yield*100,color=colors[0],lw=1.2,label="平均已实施股息率")
    y=f.mean_yield*100
    axes[1].fill_between(f.index,0,y,where=f.high80.eq(1),alpha=.2,color=colors[0],label="过去三年最高20%区间")
    axes[1].set_ylim(0,y.max()*1.18);axes[1].set_ylabel("股息率（%）");axes[1].xaxis.set_major_locator(mdates.YearLocator());axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"));axes[1].legend(frameon=False,ncol=2)
    p=r["primary"];b=p["bootstrap"]
    fig.text(.08,.073,f"60日高位效应：{p['coefficient']*100:+.2f}个百分点；时间块95%区间 [{b['confidence_lower']*100:+.2f}, {b['confidence_upper']*100:+.2f}]；{r['joint_family_size']}项校正p={p['holm_joint']:.3g}",fontsize=11)
    fig.text(.08,.044,"日度窗口重叠，已处理序列相关。等权股息率与官方指数权重不同；收益不含股息、手续费、税及滑点。",fontsize=10,color="#5b6c60")
    fig.subplots_adjust(left=.08,right=.96,top=.85,bottom=.14,hspace=.3)
    fig.savefig(folder/"股息率高位反弹研究.png",dpi=160,facecolor=fig.get_facecolor());plt.close(fig)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    p=sub.add_parser("prepare");p.add_argument('--source-chart',type=Path,default=SOURCE);p.add_argument('--sector-reference',type=Path,default=PREVIOUS)
    a=sub.add_parser("analyse");a.add_argument("run_dir",type=Path)
    args=parser.parse_args()
    if args.command=="prepare": prepare(args.source_chart,args.sector_reference)
    else: analyse(args.run_dir.resolve())
