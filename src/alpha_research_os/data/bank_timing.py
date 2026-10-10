"""Pure bank allocation data transforms, with explicit gaps and evidence references."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from alpha_research_os.factors.bank import MAX_REPORT_AGE_DAYS, finite
from alpha_research_os.kernel.canonical import content_hash


def same_period_change(known: pd.DataFrame, metric: str, session: date, select_fact):
    """No shifting daily copies of financial statements and no future revisions."""
    current = select_fact(known, metric)
    if not current or not 0 <= (session - current["report_date"]).days <= MAX_REPORT_AGE_DAYS:
        return None, "NO_FRESH_CURRENT_FACT", current, None
    prior_day = (pd.Timestamp(current["report_date"]) - pd.DateOffset(years=1)).date()
    prior = select_fact(known, metric, prior_day)
    if not prior:
        return None, "NO_SAME_PERIOD_PRIOR", current, None
    if current["basis"] != prior["basis"] or current["unit"] != prior["unit"]:
        return None, "INCOMPARABLE_FACTS", current, prior
    if current["unit"] != "percent":
        return None, "UNEXPECTED_UNIT", current, prior
    if not finite(current["value"]) or not finite(prior["value"]):
        return None, "NONFINITE_FACT", current, prior
    return current["value"] - prior["value"], "READY", current, prior


def fact_reference(fact: dict | None) -> str | None:
    if fact is None:
        return None
    return content_hash({
        key: None if pd.isna(fact.get(key)) else str(fact.get(key))
        for key in ("code", "metric", "report_date", "available_at", "value", "unit", "basis",
                    "source_id", "source_sha256")
    })


def disclosure_changes(features: pd.DataFrame, facts: pd.DataFrame, select_fact):
    """Return two new bank-day inputs plus source and prior-period lineage."""
    additions, lineage = [], []
    for code, rows in features.groupby("instrument_id", sort=True):
        source = facts[facts.code == code].sort_values("available_at").reset_index(drop=True)
        source["available_at"] = pd.to_datetime(source.available_at, utc=True).astype("datetime64[ns, UTC]")
        available = source.available_at.astype("int64").to_numpy()
        cache = {}
        for row in rows.sort_values("session").itertuples():
            cutoff = pd.Timestamp(row.available_at)
            size = int(np.searchsorted(available, cutoff.value, side="right"))
            known = source.iloc[:size]
            expired = int((known.report_date < row.session - timedelta(days=MAX_REPORT_AGE_DAYS)).sum())
            key = (size, expired)
            if key not in cache:
                cache[key] = {
                    field: same_period_change(known, metric, row.session, select_fact)
                    for field, metric in (("provision_change_yoy", "provision_coverage_ratio"),
                                          ("cet1_change_yoy", "cet1_ratio"))
                }
            record = {"session": row.session, "instrument_id": code}
            for field, (value, status, current, prior) in cache[key].items():
                record[field] = value
                record[field + "_status"] = status
                lineage.append({
                    "session": row.session, "instrument_id": code, "field": field, "status": status,
                    "value": value, "unit": "percentage_point",
                    "current_fact_id": fact_reference(current), "prior_fact_id": fact_reference(prior),
                    "current_report_date": current["report_date"] if current else None,
                    "prior_report_date": prior["report_date"] if prior else None,
                    "current_available_at": current["available_at"] if current else None,
                    "prior_available_at": prior["available_at"] if prior else None,
                    "current_source_id": current["source_id"] if current else None,
                    "prior_source_id": prior["source_id"] if prior else None,
                    "current_basis": current["basis"] if current else None,
                    "prior_basis": prior["basis"] if prior else None,
                    "signal_cutoff": cutoff,
                })
            additions.append(record)
    return pd.DataFrame(additions), pd.DataFrame(lineage)


def stock_total_return_inputs(market: pd.DataFrame, actions: pd.DataFrame, calendar: list[date]):
    """Forward-built gross TR segments; invalid adjustments never join two segments."""
    action_map = {}
    for (code, day), rows in actions.groupby(["ts_code", "effective_date"]):
        if len(rows) != 1:
            action_map[(code, day)] = None
            continue
        action_map[(code, day)] = rows.iloc[0].to_dict()
    session_position = {day: index for index, day in enumerate(calendar)}
    records = []
    for code, rows in market.groupby("ts_code", sort=True):
        previous_close = None
        previous_day = None
        level = 100.0
        segment = 0
        observations = 0
        for row in rows.sort_values("trade_date").itertuples():
            day = row.trade_date
            status, gross_return = "READY", None
            cash, stock = 0.0, 0.0
            event_key = (code, day)
            event = action_map.get(event_key)
            valid_price = finite(row.close) and row.close > 0 and bool(row.is_valid_close)
            if not valid_price:
                status = "INVALID_PRICE"
            elif previous_close is None:
                status = "SEGMENT_START"
            elif session_position[day] != session_position[previous_day] + 1:
                status = "MISSING_MARKET_SESSION"
            elif event_key in action_map:
                if event is None or not bool(event["approved_for_dividend_adjustment"]):
                    status = "UNRESOLVED_EVENT"
                elif (pd.isna(event["first_available_date"]) or pd.isna(event["last_available_date"])
                      or event["first_available_date"] >= day or event["last_available_date"] >= day):
                    status = "EVENT_VERSION_NOT_KNOWN"
                else:
                    cash, stock = float(event["cash_dividend_per_share"]), float(event["stock_dividend_ratio"])
                    if not finite(cash) or not finite(stock) or cash < 0 or stock < 0:
                        status = "INVALID_EVENT_VALUE"
            if status == "READY":
                expected = (previous_close - cash) / (1 + stock)
                if not finite(row.pre_close) or abs(expected - row.pre_close) > 0.011:
                    status = "UNRESOLVED_REFERENCE_CHANGE"
                else:
                    gross_return = (row.close * (1 + stock) + cash) / previous_close - 1
                    level *= 1 + gross_return
                    observations += 1
            if status != "READY":
                segment += 1
                level = 100.0
                observations = 1 if valid_price else 0
            records.append({
                "session": day, "instrument_id": code, "economic_daily_return": gross_return,
                "bank_total_return_index": level if valid_price else None,
                "total_return_segment": segment, "segment_observations": observations, "tr_status": status,
                "cash_dividend_per_share": cash if status == "READY" else None,
                "stock_dividend_ratio": stock if status == "READY" else None,
                "market_source_snapshot_id": row.source_snapshot_id,
                "available_at": pd.Timestamp(day, tz="Asia/Shanghai") + pd.Timedelta(hours=15),
            })
            previous_close = float(row.close) if valid_price else None
            previous_day = day
    return pd.DataFrame(records)


def equal_bank_basket(universe: pd.DataFrame, technical: pd.DataFrame, calendar: list[date], lag: int = 2):
    """Analytical equal-weight TR; missing held returns invalidate the whole date."""
    if lag < 2:
        raise ValueError("basket membership must be known before the prior-close execution reference")
    pools = universe.groupby("session").instrument_id.apply(set).to_dict()
    returns = technical.set_index(["session", "instrument_id"]).economic_daily_return.to_dict()
    level, segment, observations = 100.0, 0, 0
    records, weights = [], []
    for position, day in enumerate(calendar):
        decision = calendar[position - lag] if position >= lag else None
        codes = sorted(pools.get(decision, set()))
        values = [returns.get((day, code)) for code in codes]
        valid = [value for value in values if finite(value)]
        good = bool(codes) and len(valid) == len(codes)
        daily = float(np.mean(valid)) if good else None
        if good:
            level *= 1 + daily
            observations += 1
        else:
            segment += 1
            level, observations = 100.0, 0
        records.append({
            "session": day, "basket_decision_session": decision, "held_bank_count": len(codes),
            "valid_return_count": len(valid), "daily_return": daily,
            "bank_equal_total_return_index": level if good else None,
            "basket_segment": segment, "segment_returns": observations,
            "status": "READY" if good else "NO_LAGGED_UNIVERSE" if not codes else "MISSING_HELD_RETURN",
        })
        weights.extend({"session": day, "decision_session": decision, "instrument_id": code,
                        "weight": 1 / len(codes)} for code in codes)
    return pd.DataFrame(records), pd.DataFrame(weights)


def daily_pb_history_statistics(frame, calendar, window=756, minimum=504):
    """Rank positive daily PB with pre-display history, without future fill."""
    source = frame.copy()
    source["session"] = pd.to_datetime(source.session).dt.date
    source = source[source.session <= max(calendar)]
    if source.duplicated(["session", "instrument_id"]).any():
        raise ValueError("银行PB历史键重复")
    full_calendar = sorted(set(calendar) | set(source.session))
    parts = []
    for code, group in source.groupby("instrument_id", sort=True):
        values = pd.to_numeric(group.set_index("session").pb_daily, errors="coerce").reindex(full_calendar)
        values = values.where(np.isfinite(values) & (values > 0))

        def rank(array):
            current = array[-1]
            known = array[np.isfinite(array)]
            if not np.isfinite(current) or len(known) < minimum:
                return np.nan
            return (np.sum(known < current) + .5 * np.sum(known == current)) / len(known)

        parts.append(pd.DataFrame({"session": full_calendar, "instrument_id": code,
                                   "pb_daily": values.to_numpy(),
                                   "pb_history_count": values.notna().astype(int).rolling(window, min_periods=1)
                                   .sum().astype(int).to_numpy(),
                                   "pb_percentile": values.rolling(window, min_periods=minimum)
                                   .apply(rank, raw=True).to_numpy()}))
    return pd.concat(parts, ignore_index=True)


def history_readiness(panel: pd.DataFrame, calendar: list[date], window: int = 756, pb_history=None, minimum=504,
                      valuation_history=None):
    """Counts only, not percentile values: missing calendar dates consume the window."""
    result = panel.copy()
    source_panel = panel if valuation_history is None else valuation_history[
        valuation_history.session <= max(calendar)]
    full_calendar = sorted(set(calendar) | set(source_panel.session))
    for source, output in (("book_to_price", "pb_history_count"), ("cash_yield365", "yield_history_count"),
                           ("annual_earnings_yield", "earnings_history_count")):
        counts = []
        for code, group in panel.groupby("instrument_id", sort=True):
            history = source_panel[source_panel.instrument_id == code]
            series = pd.to_numeric(history.set_index("session")[source], errors="coerce").reindex(full_calendar)
            valid = np.isfinite(series) & (series > 0 if source == "book_to_price" else True)
            count = valid.astype(int).rolling(window, min_periods=1).sum()
            counts.extend({"session": day, "instrument_id": code, output: int(count.loc[day])}
                          for day in group.session)
        result = result.merge(pd.DataFrame(counts), on=["session", "instrument_id"], validate="one_to_one")
    if pb_history is not None:
        pb = daily_pb_history_statistics(pb_history, calendar, window, minimum)
        result = result.drop(columns="pb_history_count").merge(pb, on=["session", "instrument_id"],
                                                               how="left", validate="one_to_one")
        result["pb_history_count"] = result.pb_history_count.fillna(0).astype(int)
    return result


def benchmark_timeline(frame: pd.DataFrame, calendar: list[date]):
    """Preserve benchmark gaps; rolling returns require endpoints in one segment."""
    if frame.session.duplicated().any() or not set(frame.session).issubset(calendar):
        raise ValueError("Benchmark keys disagree with market calendar")
    levels = pd.to_numeric(frame.gross_total_return_index, errors="coerce")
    if not np.isfinite(levels).all() or (levels <= 0).any():
        raise ValueError("Invalid gross TR benchmark level")
    result = frame.set_index("session").reindex(calendar).rename_axis("session").reset_index()
    segment, count = 0, 0
    segments, counts, statuses = [], [], []
    for value in result.gross_total_return_index:
        if finite(value):
            count += 1
            status = "READY"
        else:
            segment += 1
            count = 0
            status = "MISSING_SOURCE_SESSION"
        segments.append(segment)
        counts.append(count)
        statuses.append(status)
    result["benchmark_segment"] = segments
    result["segment_observations"] = counts
    result["benchmark_status"] = statuses
    return result


def latest_input_gaps(panel: pd.DataFrame, history_min: int, trend_min: int):
    """A bounded bank/field work queue; no substitute values or source claims."""
    records = []
    latest = panel[panel.session == panel.session.max()]
    for row in latest.sort_values("instrument_id").to_dict("records"):
        for field, counter, required in (
            ("pb_daily" if "pb_daily" in panel else "book_to_price", "pb_history_count", history_min),
            ("cash_yield365", "yield_history_count", history_min),
            ("annual_earnings_yield", "earnings_history_count", history_min),
            ("bank_total_return_index", "segment_observations", trend_min),
            ("quality_gate", None, None), ("nim_change", None, None),
            ("profit_growth", None, None), ("npl_improvement", None, None),
            ("provision_change_yoy", None, None), ("cet1_change_yoy", None, None),
        ):
            valid = finite(row[field]) and (row[field] > 0 if field in {"book_to_price", "pb_daily"} else True)
            count = row[counter] if counter else None
            if valid and (counter is None or count >= required):
                continue
            status = "INSUFFICIENT_VALID_HISTORY" if valid else "MISSING_CURRENT_RAW_INPUT"
            evidence = row.get(field + "_status")
            task = "BACKFILL_COMPARABLE_KNOWN_AT_HISTORY" if valid else "RECONCILE_SOURCE_FACTS_AND_AVAILABILITY"
            if field == "book_to_price" and not valid:
                anchor = row.get("common_bvps")
                status = ("PER_SHARE_ANCHOR_OR_EVENT_RECONCILIATION" if finite(anchor)
                          else "ORDINARY_BVPS_SOURCE_UNAVAILABLE")
                task = "RECONCILE_ORDINARY_SHARE_BVPS_REPORTS_AND_CAPITAL_EVENTS"
            records.append({"session": row["session"], "instrument_id": row["instrument_id"],
                            "field": field, "gap_status": status, "source_status": evidence,
                            "valid_history_count": count, "required_history_count": required,
                            "next_task": task, "priority": 1 if field == "book_to_price" else 2})
    return pd.DataFrame(records, columns=["session", "instrument_id", "field", "gap_status", "source_status",
                                         "valid_history_count", "required_history_count", "next_task", "priority"])
