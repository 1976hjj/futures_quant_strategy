"""Read bank warehouses into immutable feature evidence; never query future returns/labels."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from alpha_research_os.factors.bank import (
    MAX_REPORT_AGE_DAYS,
    cash_per_current_share,
    finite,
    quality_gate,
    quality_score,
    roe_history_statistics,
)

METRIC_MAP = {
    "tushare_roe_waa_pct": "roe_weighted",
    "tushare_eps": "ordinary_eps_basic",
    "tushare_parent_profit": "parent_profit",
    "valuation_common_bvps": "common_bvps",
}
METRICS = {
    "roe_weighted",
    "ordinary_eps_basic",
    "parent_profit",
    "common_bvps",
    "npl_ratio",
    "provision_coverage_ratio",
    "cet1_ratio",
    "net_interest_margin",
    "profit_growth_direct",
}


def daily_pb_inputs(root: Path, start: date, end: date):
    """Read existing daily PB with historical membership and immutable source IDs."""
    settings = json.loads((root / "config/bank_timing_data.json").read_bytes())
    if end < start or end > date.fromisoformat(settings["released_feature_end"]):
        raise ValueError("PB window is outside released feature data; no holdout access")
    with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as connection:
        frame = connection.execute("""SELECT d.trade_date AS session,d.ts_code AS instrument_id,
            d.close,d.pb AS pb_daily,d.source_snapshot_id,d.source_payload_artifact_id
            FROM research.daily_basic d WHERE d.trade_date BETWEEN ? AND ?
            AND EXISTS (SELECT 1 FROM research.sw_industry_membership i
                WHERE i.ts_code=d.ts_code AND i.l1_name='银行' AND i.in_date<=d.trade_date
                AND (i.out_date IS NULL OR d.trade_date<i.out_date))
            ORDER BY d.ts_code,d.trade_date""", [start, end]).df()
    if frame.empty or frame.duplicated(["session", "instrument_id"]).any():
        raise ValueError("银行日PB数据为空或日期/银行键重复")
    if frame[["source_snapshot_id", "source_payload_artifact_id"]].isna().any().any():
        raise ValueError("银行日PB缺少原始归档血缘")
    frame["session"] = pd.to_datetime(frame.session).dt.date
    valid = frame.pb_daily.map(finite) & (frame.pb_daily > 0)
    frame["pb_daily"] = frame.pb_daily.where(valid)
    frame["pb_status"] = valid.map({True: "READY", False: "MISSING_OR_NONPOSITIVE_DAILY_PB"})
    frame["pb_basis"] = "archived_daily_basic_reported_pb; distinct_from_ordinary_equity_book_to_price"
    frame["available_at"] = pd.to_datetime(frame.session.astype(str), utc=True) + pd.Timedelta(hours=9)
    return frame


def eligible_bank_market(connection, start: date, end: date):
    """Push the bank key filter below the full-market universe's ranking windows."""
    codes = [row[0] for row in connection.execute(
        "SELECT DISTINCT ts_code FROM research.sw_industry_membership WHERE l1_name='银行' ORDER BY ts_code"
    ).fetchall()]
    if not codes:
        raise ValueError("没有银行行业成员数据")
    placeholders = ','.join('?' for _ in codes)
    return connection.execute("""SELECT u.trade_date AS session,u.ts_code AS instrument_id,m.close
        FROM research.universe_daily u JOIN research.market_daily m USING(trade_date,ts_code)
        WHERE u.eligible_for_signal AND u.trade_date BETWEEN ? AND ? AND u.ts_code IN (""" + placeholders + """)
        AND EXISTS (SELECT 1 FROM research.sw_industry_membership i WHERE i.ts_code=u.ts_code
            AND i.l1_name='银行' AND i.in_date<=u.trade_date AND (i.out_date IS NULL OR u.trade_date<i.out_date))
        ORDER BY u.ts_code,u.trade_date""", [start, end, *codes]).df()


def source_inputs(root: Path, start: date, end: date):
    config = json.loads((root / "config/bank_factors.json").read_text(encoding="utf-8"))
    frames = []
    for relative in config["original_fact_files"]:
        frame = pd.read_parquet(root / relative)
        valid = frame.publication_evidence_verified.fillna(False).astype(bool) | frame.get(
            "certified", pd.Series(False, index=frame.index)
        ).fillna(False).astype(bool)
        frame = frame[valid].copy()
        frame["source_id"] = "issuer_original_report"
        frame["source_sha256"] = frame.source_sha256.fillna(frame.get("sha256"))
        frame["basis"] = frame.definition_id.fillna(frame.metric + "_annual_original")
        frame["source_priority"] = 1
        frame["historical_grade"] = "original_document_publication_reconciled_not_exhaustive_certification"
        frames.append(frame)
    for relative in config.get("original_growth_files", []):
        frame = pd.read_parquet(root / relative).copy()
        frame["metric"] = "profit_growth_direct"
        frame["value"] = frame.parent_profit_yoy_current_report
        frame["unit"] = "ratio"
        frame["source_id"] = "issuer_original_report_formula"
        frame["basis"] = "same_report_parent_profit_yoy"
        frame["source_priority"] = 1
        frame["historical_grade"] = "same_document_comparative_only_known_at_current_publication"
        frames.append(frame)
    with duckdb.connect(str(root / "data/warehouse/bank_token.duckdb"), read_only=True) as c:
        cutoff = pd.Timestamp(end, tz="Asia/Shanghai") + pd.Timedelta(hours=15)
        lineage = c.execute("""SELECT * FROM bank_metric_lineage WHERE try_cast(report_date AS DATE)<=?
            AND try_cast(available_at AS TIMESTAMPTZ)<=?
            AND (historical_pit_verified OR try_cast(retrieved_at AS TIMESTAMPTZ)<=?)""",
                            [end, cutoff.to_pydatetime(), cutoff.to_pydatetime()]).df()
        events = c.execute("SELECT * FROM bank_dividend_events WHERE try_cast(ex_date AS DATE)<=?", [end]).df()
        coverage = {row[0] for row in c.execute("SELECT DISTINCT ts_code FROM dividend").fetchall()}
    lineage["metric"] = lineage.metric.replace(METRIC_MAP)
    # Generic BPS includes other equity tools; only explicit ordinary BVPS is comparable.
    lineage = lineage[lineage.metric.isin(METRICS)].copy()
    lineage["value"] = lineage.normalized_value
    lineage["unit"] = lineage.normalized_unit
    lineage["basis"] = "warehouse:" + lineage.metric + ":" + lineage.report_scope.fillna("unknown")
    lineage["source_priority"] = lineage.source_id.map(
        lambda s: 0 if s == "tushare_compatible_token" else 1 if "issuer" in s else 2
    )
    lineage["historical_grade"] = lineage.pit_grade
    # Existing lineage has conservative collection timestamps for unverified revisions.
    # Never substitute announcement dates for them.
    lineage["available_at"] = pd.to_datetime(lineage.available_at, utc=True, errors="coerce")
    retrieved = pd.to_datetime(lineage.retrieved_at, utc=True, errors="coerce")
    uncertain = ~lineage.historical_pit_verified.fillna(False)
    lineage.loc[uncertain, "available_at"] = pd.concat(
        [lineage.loc[uncertain, "available_at"], retrieved.loc[uncertain]], axis=1
    ).max(axis=1)
    frames.append(lineage)
    columns = [
        "code",
        "report_date",
        "metric",
        "value",
        "unit",
        "available_at",
        "source_id",
        "source_sha256",
        "source_file",
        "source_url",
        "pdf_page",
        "basis",
        "source_priority",
        "historical_grade",
    ]
    facts = pd.concat([f.reindex(columns=columns) for f in frames], ignore_index=True)
    facts["report_date"] = pd.to_datetime(facts.report_date, errors="coerce").dt.date
    facts["available_at"] = pd.to_datetime(facts.available_at, utc=True, errors="coerce")
    facts["value"] = pd.to_numeric(facts.value, errors="coerce")
    facts = facts[
        facts.metric.isin(METRICS) & facts.value.notna() & facts.available_at.notna() & facts.report_date.notna()
    ].drop_duplicates(["code", "report_date", "metric", "available_at", "source_id", "value", "basis"])
    # The ROC/EPS anchors explicitly use annual reports; other stock/YTD fields retain their report period.
    annual = facts.report_date.map(lambda d: d.month == 12 and d.day == 31)
    facts = facts[~facts.metric.isin(["roe_weighted", "ordinary_eps_basic", "common_bvps"]) | annual]
    expected_unit = facts.metric.map(
        lambda m: (
            "percent"
            if m in {"roe_weighted", "npl_ratio", "provision_coverage_ratio", "cet1_ratio", "net_interest_margin"}
            else "CNY"
            if m == "parent_profit"
            else "ratio"
            if m == "profit_growth_direct"
            else "CNY/share"
        )
    )
    unit = facts.unit.replace(
        {"CNY_per_ordinary_share": "CNY/share", "CNY_per_weighted_average_ordinary_share": "CNY/share"}
    )
    facts = facts[unit == expected_unit].sort_values(["code", "available_at", "report_date", "metric"])
    for column in ["ex_date", "imp_ann_date", "end_date", "ann_date", "pay_date", "record_date"]:
        events[column] = pd.to_datetime(events[column], errors="coerce").dt.date
        events[column] = events[column].astype(object).where(events[column].notna(), None)
    events = reconcile_dividend_events(events)
    database = root / "data/warehouse/alpha_research.duckdb"
    with duckdb.connect(str(database), read_only=True) as c:
        market = eligible_bank_market(c, start, end)
        membership = c.execute("""SELECT * FROM research.sw_industry_membership WHERE l1_name='银行'
                                  ORDER BY ts_code,in_date,out_date,source_snapshot_id""").df()
        # Prices here are feature-domain observations, not forward return labels.
        capitals = c.execute(
            """SELECT m.ts_code,m.trade_date,m.close,m.pre_close
          FROM research.market_daily m WHERE m.trade_date<=? AND EXISTS
          (SELECT 1 FROM research.sw_industry_membership i WHERE i.ts_code=m.ts_code AND i.l1_name='银行')
          ORDER BY m.ts_code,m.trade_date""",
            [end],
        ).df()
    market["session"] = pd.to_datetime(market.session).dt.date
    capitals["trade_date"] = pd.to_datetime(capitals.trade_date).dt.date
    return facts.reset_index(drop=True), events, market, membership, capitals, coverage


def reconcile_dividend_events(events):
    """Preserve ambiguous same-day variants as evidence and abstain rather than sum twice."""
    records = []
    for _, group in events.groupby(["ts_code", "ex_date"], dropna=False, sort=False):
        row = group.iloc[0].to_dict()
        row["source_event_rows"] = group.to_json(orient="records", date_format="iso", default_handler=str)
        row["event_conflict"] = len(group) > 1
        if len(group) > 1:
            row["cash_div_tax"] = None
            row["stk_div"] = None
            # The conflict is visible only once all these event versions are known.
            announcements = group.imp_ann_date.dropna()
            row["imp_ann_date"] = announcements.max() if len(announcements) == len(group) else None
        records.append(row)
    return pd.DataFrame(records)


def select_fact(known: pd.DataFrame, metric: str, report_date: date | None = None):
    rows = known[known.metric == metric]
    if report_date is not None:
        rows = rows[rows.report_date == report_date]
    if rows.empty:
        return None
    rows = rows[rows.report_date == rows.report_date.max()]
    rows = rows[rows.source_priority == rows.source_priority.min()]
    rows = rows[rows.available_at == rows.available_at.max()]
    if rows.basis.nunique() != 1 or rows.value.max() - rows.value.min() > 1e-8 * max(1, abs(rows.value.iloc[0])):
        return None
    return rows.sort_values(["source_id", "source_sha256"], na_position="last").iloc[0].to_dict()


def financial_state(known: pd.DataFrame, session: date):
    selected = {m: select_fact(known, m) for m in sorted(METRICS)}
    selected = {
        m: f if f and 0 <= (session - f["report_date"]).days <= MAX_REPORT_AGE_DAYS else None
        for m, f in selected.items()
    }
    out = {m: f["value"] if f else None for m, f in selected.items()}

    def change(metric, ratio=False):
        current = selected[metric]
        if not current:
            return None
        previous_date = (pd.Timestamp(current["report_date"]) - pd.DateOffset(years=1)).date()
        prior = select_fact(known, metric, previous_date)
        if not prior or prior["basis"] != current["basis"]:
            return None
        if ratio:
            return current["value"] / prior["value"] - 1 if prior["value"] > 0 else None
        return current["value"] - prior["value"]

    out["npl_improvement"] = change("npl_ratio")
    if out["npl_improvement"] is not None:
        out["npl_improvement"] *= -1
    out["nim_change"] = change("net_interest_margin")
    profit = selected["parent_profit"]
    direct = select_fact(known, "profit_growth_direct", profit["report_date"]) if profit else None
    out["profit_growth"] = direct["value"] if direct else change("parent_profit", True)
    if direct:
        selected["profit_growth_direct"] = direct
    roe = selected["roe_weighted"]
    history = []
    if roe:
        for year in range(roe["report_date"].year - 2, roe["report_date"].year + 1):
            f = select_fact(known, "roe_weighted", date(year, 12, 31))
            if f and f["basis"] == roe["basis"]:
                history.append((f["report_date"], f["value"]))
    out["roe_median3"], out["roe_volatility3"] = (
        roe_history_statistics(history, roe["report_date"]) if roe else (None, None)
    )
    out["quality_score"] = quality_score(out["roe_weighted"], out["npl_ratio"], out["provision_coverage_ratio"])
    out["quality_gate"] = quality_gate(
        out["roe_weighted"],
        out["npl_ratio"],
        out["provision_coverage_ratio"],
        npl_change=-out["npl_improvement"] if out["npl_improvement"] is not None else None,
        cet1=out["cet1_ratio"],
        profit_growth=out["profit_growth"],
    )
    return out, selected


def normalize_per_share(fact, events, invalidations, session):
    if not fact:
        return None
    after = [
        e
        for e in events
        if fact["report_date"] < e["ex_date"] <= session
        and e["imp_ann_date"] is not None
        and e["imp_ann_date"] < session
    ]
    if any(not finite(e.get("stk_div")) or e["stk_div"] < 0 for e in after):
        return None
    scale = 1.0
    for event in after:
        scale *= 1 + event["stk_div"]
    # Non-free price-reference adjustment (rights etc.) is observable on the event day.
    # Abstain until a report with a post-event reporting date supplies a new per-share anchor.
    if any(fact["report_date"] < d <= session for d in invalidations):
        return None
    return fact["value"] / scale


def per_share_invalidations(bars, events):
    result = []
    event_by_day = {e["ex_date"]: e for e in events}
    previous = None
    for bar in bars.itertuples():
        if previous is not None:
            event = event_by_day.get(bar.trade_date)
            cash, stock = 0.0, 0.0
            if event:
                if (
                    event.get("imp_ann_date") is None
                    or event["imp_ann_date"] >= bar.trade_date
                    or not finite(event.get("cash_div_tax"))
                    or not finite(event.get("stk_div"))
                    or event["stk_div"] < 0
                ):
                    result.append(bar.trade_date)
                    previous = bar.close
                    continue
                cash, stock = event["cash_div_tax"], event["stk_div"]
            expected = (previous - cash) / (1 + stock)
            if not finite(bar.pre_close) or abs(expected - bar.pre_close) > 0.011:
                result.append(bar.trade_date)
        previous = bar.close
    return result


def build_features(facts, events, market, capitals, coverage):
    records = []
    for code, bars in market.groupby("instrument_id", sort=True):
        ff = facts[facts.code == code].sort_values('available_at').reset_index(drop=True)
        available = pd.to_datetime(ff.available_at, utc=True).astype('datetime64[ns, UTC]').astype('int64').to_numpy()
        report_days = pd.to_datetime(ff.report_date).to_numpy(dtype='datetime64[D]').astype('int64')
        ev = events[(events.ts_code == code) & events.ex_date.notna()].to_dict("records")
        cap = capitals[capitals.ts_code == code]
        invalidations = per_share_invalidations(cap, ev)
        cache = {}
        for bar in bars.itertuples():
            day = bar.session
            cut = pd.Timestamp(day).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
            size = int(np.searchsorted(available, cut.value, side='right'))
            day_number = (day - date(1970, 1, 1)).days
            fresh = ((report_days[:size] >= day_number - MAX_REPORT_AGE_DAYS)
                     & (report_days[:size] <= day_number))
            key = (size, tuple(np.flatnonzero(fresh)))
            if key not in cache:
                cache[key] = financial_state(ff.iloc[:size], day)
            state, selected = cache[key]
            row = dict(state)
            cash = cash_per_current_share(ev, day, code in coverage)
            eps = normalize_per_share(selected["ordinary_eps_basic"], ev, invalidations, day)
            bps = normalize_per_share(selected["common_bvps"], ev, invalidations, day)
            price = float(bar.close)
            row.update(
                session=day,
                instrument_id=code,
                close=price,
                cash_yield365=cash / price if finite(cash) and price > 0 else None,
                annual_earnings_yield=eps / price if finite(eps) and price > 0 else None,
                book_to_price=bps / price if finite(bps) and bps > 0 and price > 0 else None,
                available_at=cut.tz_convert("UTC"),
                optional_capital_unknown=not finite(row["cet1_ratio"]),
                optional_profit_trend_unknown=not finite(row["profit_growth"]),
                optional_npl_trend_unknown=not finite(row["npl_improvement"]),
            )
            row["gated_cash_yield365"] = row["cash_yield365"] if row["quality_gate"] == 1 else None
            records.append(row)
        print(f"bank={code} rows={len(bars)}", flush=True)
    return pd.DataFrame(records)
