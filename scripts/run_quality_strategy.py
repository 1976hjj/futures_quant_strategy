"""Compute a recorded strategy from local inputs, never call a data provider."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from alpha_research_os.portfolio.quality_strategies import atomic_json, chart_history, fingerprint, read_json, quarterly_signals


def local_engine():
    # Each worker gets its own module namespace; adapters cannot leak into the
    # regular API or a concurrent strategy calculation.
    path = ROOT / "src/alpha_research_os/portfolio/strategy_backtest.py"
    spec = importlib.util.spec_from_file_location("quality_strategy_engine", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prepare_factors(root, request, folder, as_of, progress):
    from alpha_research_os.portfolio.quality_inputs import build_inputs
    return build_inputs(root, request, folder, as_of, progress)


def non_bse_external(connection, start, end, health):
    import pandas as pd
    n = health.external_breadth_return_sessions
    warm = connection.execute("SELECT min(cal_date) FROM (SELECT cal_date FROM research.trading_calendar WHERE exchange='SSE' AND is_open AND cal_date<=? ORDER BY cal_date DESC LIMIT ?)", [start, max(health.external_trend_sessions, n) + 10]).fetchone()[0]
    frame = connection.execute(f"""WITH base AS (
      SELECT m.ts_code,m.trade_date,m.close/m.pre_close-1 AS ret,ln(m.close/m.pre_close) AS log_ret
      FROM research.market_daily m JOIN research.security_session_state u USING(trade_date,ts_code)
      WHERE m.trade_date BETWEEN ? AND ? AND m.close>0 AND m.pre_close>0 AND m.is_valid_ohlc
        AND u.eligible_for_signal AND (m.ts_code LIKE '%.SH' OR m.ts_code LIKE '%.SZ')
    ), rolling AS (SELECT *,sum(log_ret) OVER w AS ret_n,count(*) OVER w AS obs_n FROM base
      WINDOW w AS(PARTITION BY ts_code ORDER BY trade_date ROWS BETWEEN {n-1} PRECEDING AND CURRENT ROW))
    SELECT trade_date,avg(ret) market_return,avg(CASE WHEN obs_n={n} THEN CAST(ret_n>0 AS DOUBLE) END) external_breadth
    FROM rolling GROUP BY trade_date ORDER BY trade_date""", [warm, end]).df()
    frame["trade_date"] = pd.to_datetime(frame.trade_date)
    frame["market_index"] = (1 + frame.market_return.fillna(0)).cumprod()
    frame["market_ma"] = frame.market_index.rolling(health.external_trend_sessions, min_periods=health.external_trend_sessions).mean()
    frame["market_trend_gap"] = frame.market_index / frame.market_ma - 1
    frame = frame[frame.trade_date.dt.date.ge(start)].reset_index(drop=True)
    if frame.empty or frame[["market_ma", "external_breadth"]].isna().any().any():
        raise ValueError("影子策略趋势或市场广度预热历史不足")
    return frame


def run(root: Path, jid: str):
    folder = root / "data/quality_strategies/jobs"
    job = read_json(folder / f"{jid}.request.json")
    definition, parameters = job["definition"], job["parameters"]
    state_path = folder / f"{jid}.state.json"
    def progress(update):
        state = read_json(state_path)
        state.update({k: v for k, v in update.items() if k in ["progress", "phase", "current_session"]})
        atomic_json(state_path, state)
    source = (root / definition["source_path"]).resolve()
    if not source.is_relative_to(root) or fingerprint(source) != definition["source_sha256"]:
        raise ValueError("归档策略证据校验失败")
    with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as connection:
        latest = connection.execute("SELECT max(trade_date) FROM research.market_daily").fetchone()[0]
        desired = date.fromisoformat(parameters["as_of"]) if parameters["as_of"] else latest
        if desired > latest:
            raise ValueError(f"请求日期 {desired} 没有本地行情；本地截止 {latest}")
        as_of = connection.execute("SELECT max(cal_date) FROM research.trading_calendar WHERE exchange='SSE' AND is_open AND cal_date<=?", [desired]).fetchone()[0]
        config = {**definition["request"], "end": as_of.isoformat(), "selection_sequence_mode": "ACTUAL_POSITIONS"}
        if definition.get("fixed_exposure") is not None:
            config["shadow_health"] = {**config["shadow_health"], "experiment_variant": "S0"}
        engine = local_engine()
        request = engine.StrategyBacktestRequest.model_validate(config)
        calendar = [r[0] for r in connection.execute("SELECT cal_date FROM research.trading_calendar WHERE exchange='SSE' AND is_open AND cal_date BETWEEN ? AND ? ORDER BY cal_date", [request.start, as_of]).fetchall()]
        if not calendar:
            raise ValueError("策略起始日之后没有可用交易日")
        schedule = None
        if definition["schedule"] == "QUARTER":
            # Use the complete calendar, not the last observed price date, to
            # distinguish a real quarter end from a partial-quarter data cutoff.
            future = [r[0] for r in connection.execute("SELECT cal_date FROM research.trading_calendar WHERE exchange='SSE' AND is_open AND cal_date BETWEEN ? AND ? ORDER BY cal_date", [request.start, date(as_of.year, 12, 31)]).fetchall()]
            calendar_end = connection.execute("SELECT max(cal_date) FROM research.trading_calendar WHERE exchange='SSE'").fetchone()[0]
            schedule = quarterly_signals(future, calendar_end, as_of)
    resolved = prepare_factors(root, request, folder / f"{jid}.inputs", as_of, progress)
    bindings = {row["factor_id"]: row for row in resolved}
    request = request.model_copy(update={"score_rules": tuple(r.model_copy(update={"release_id": bindings[r.factor_id]["release_id"]}) for r in request.score_rules), "filter_rules": tuple(r.model_copy(update={"release_id": bindings[r.factor_id]["release_id"]}) for r in request.filter_rules)})
    engine._resolve_inputs = lambda *args: resolved
    engine._signal_cache_path = lambda *args: None
    if definition["category"] == "FACTOR_WEIGHTED":
        engine.build_external_market_frame = non_bse_external
    result = engine.run_backtest(root, request, progress_callback=progress, include_selection_history=True, calendar_signal_sessions=schedule, position_weighting=definition["allocation"], constant_exposure=definition.get("fixed_exposure"), conservative_delisting=definition["category"] == "FACTOR_WEIGHTED")
    if result.get("status") != "PASS":
        raise ValueError("模型参考账户复算未通过")
    atomic_json(folder / f"{jid}.backtest.json", result)
    last = result["daily"][-1]
    latest_selection = result["selection_history"][-1]
    positions = result["latest_positions"]
    current = {p["ts_code"]: p["quantity"] for p in parameters["current_holdings"]}
    if abs(sum(p["market_value_cny"] for p in positions) + last["cash"] - last["nav"]) > .05:
        raise ValueError("模型末日持仓与净值对账失败")
    # Historical accounts cannot execute a final-close signal inside their own
    # window. Calculate that pending decision separately from the past P&L.
    stock_signal = as_of in (schedule if schedule is not None else set(calendar[::request.rebalance_sessions]))
    shadow_changes = [c for c in result["shadow_health"].get("changes", []) if c["signal_session"] == as_of.isoformat()]
    shadow_signal = bool(shadow_changes)
    nominal = float(shadow_changes[-1]["to_exposure"]) if shadow_changes else float(last["target_shadow_exposure"])
    exposure = (1 - request.minimum_cash_fraction) * nominal
    if stock_signal:
        with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as connection:
            retained = {c for c, qty in current.items() if qty > 0} if current else {p["ts_code"] for p in positions}
            decision_inputs = [{**b, "parquet": b.get("live_parquet", b["parquet"])} for b in resolved]
            current_selection = engine.select_portfolio(engine._signal_rows(connection, decision_inputs, request, as_of), request, retained)
        latest_selection = {"signal_session": as_of.isoformat(), "holdings": current_selection["holdings"]}
    due = stock_signal or shadow_signal
    if stock_signal:
        holdings = latest_selection["holdings"]
        from alpha_research_os.portfolio.quality_strategies import inverse_risk_weights
        weights = inverse_risk_weights({h["ts_code"]: h["factor_values"]["jqdata-resvol"] for h in holdings}) if definition["allocation"] == "INVERSE_RESVOL" else {h["ts_code"]: 1 / len(holdings) for h in holdings}
    else:
        total = sum(p["market_value_cny"] for p in positions)
        weights = {p["ts_code"]: p["market_value_cny"] / total for p in positions} if total else {}
    codes = sorted(set(weights) | set(current) | {p["ts_code"] for p in positions})
    quotes = {}
    with duckdb.connect(str(root / "data/warehouse/alpha_research.duckdb"), read_only=True) as connection:
        for code in codes:
            row = connection.execute("SELECT m.close,u.security_name,u.is_st,u.eligible_for_signal,m.amount_cny,m.is_tradeable_bar,u.is_suspended FROM research.market_daily m LEFT JOIN research.security_session_state u USING(trade_date,ts_code) WHERE m.trade_date=? AND m.ts_code=?", [as_of, code]).fetchone()
            if row:
                quotes[code] = row
        next_day = connection.execute("SELECT min(cal_date) FROM research.trading_calendar WHERE exchange='SSE' AND is_open AND cal_date>?", [as_of]).fetchone()[0]
    warnings = ["历史与本次复算均属于已暴露研究；财务修订版本和公司行为仍未全面认证。", "数量按截至日收盘价取整；实际开盘价、费用、停牌、涨跌停和可用资金可能改变成交。"]
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if as_of < today:
        warnings.append(f"当前日期 {today}，本地收盘数据截至 {as_of}；尚未核验之后的行情，实盘前应更新并重算。")
    if next_day is None:
        warnings.append(f"交易日历只截至 {as_of}，下一合法成交日尚不能核对；请更新日历后再核对实际执行日期。")
    if len(weights) < request.target_count:
        warnings.append(f"当前目标仅有 {len(weights)} 只，少于设定 {request.target_count} 只；未填补缺失股票。")
    if not current:
        warnings.append("未提供实盘持仓，数量差额仅表示新建仓参考，不代表你的实际账户。")
    if not due:
        warnings.append("当前不是调仓信号日：下表展示参考持仓目标，不应据此提前执行调仓。")
    if definition["evidence_version"] == "LEGACY_365":
        warnings.append("原归档回测是365天股息口径；本次采用周期股息率，属于不同数据版本，收益须分别比较。")
    for binding in resolved:
        if "live_information_cutoff" in binding:
            warnings.append(f"{binding['factor_id']}：历史收盘后口径末日有效 {binding['latest_historical_valid_count']} 家；本日收盘后截至 {binding['live_information_cutoff']} 有效 {binding['latest_live_valid_count']} 家。已统一历史与实时收盘后决策时钟，最早次交易日成交；晚采财务不回填采集前历史；2026年历史质量覆盖缺口影响曲线，不能据此断言策略在2026年真实失效。")
    import math
    for code, qty in current.items():
        quote = quotes.get(code)
        if qty > 0 and (quote is None or quote[0] is None or not math.isfinite(quote[0]) or quote[0] <= 0):
            raise ValueError(f"当前持仓 {code} 缺少有效价格，无法核对账户权益")
    current_value = sum(qty * quotes[code][0] for code, qty in current.items() if qty > 0)
    if current_value > parameters["capital_cny"] + .05:
        raise ValueError("当前持仓市值超过输入的账户总权益；请填写总权益，而不是仅填写可用现金")
    orders, targets = [], []
    for code in codes:
        quote = quotes.get(code)
        if code in weights and (quote is None or quote[0] is None or not math.isfinite(quote[0]) or quote[0] <= 0):
            raise ValueError(f"{code} 缺少有效截至日价格，不能计算股数")
        weight = weights.get(code, 0.0) * exposure
        qty = engine._target_share_quantity(code, parameters["capital_cny"] * weight, quote[0]) if weight else 0
        record = dict(ts_code=code, security_name=quote[1] if quote else code, close=quote[0] if quote else None, weight=weight, current_quantity=current.get(code, 0), target_quantity=qty, delta_quantity=qty-current.get(code, 0))
        delta = record["delta_quantity"]
        legal_delta = engine._buy_order_quantity(code, delta) if delta > 0 else -engine._sell_order_quantity(code, current.get(code, 0), qty)
        record["legal_delta_quantity"] = legal_delta
        if legal_delta != delta:
            warnings.append(f"{code} 的数量差额 {delta} 股按交易单位只能形成 {legal_delta} 股参考委托，余量保留；仍需券商核验。")
        if quote and abs(record["delta_quantity"]) * quote[0] > (quote[4] or 0) * request.maximum_participation_rate:
            warnings.append(f"{code} 的参考数量超过截至日成交额的 {request.maximum_participation_rate:.0%}；不能假设下一日一次成交。")
        if quote and (not quote[5] or quote[6]):
            warnings.append(f"{code} 截至日不可正常交易，数量差额不能直接当作可成交订单。")
        orders.append(record)
        if weight:
            targets.append(record)
    for p in positions:
        p["security_name"] = quotes.get(p["ts_code"], [None, p["ts_code"]])[1]
    output = dict(as_of=as_of.isoformat(), signal_date=latest_selection["signal_session"], next_execution_date=str(next_day) if next_day else None, due=due, capital_cny=parameters["capital_cny"], current_holdings=parameters["current_holdings"], target_exposure=exposure, model_positions=positions, targets=targets, orders=orders, warnings=warnings, execution_note="收盘信号最早下一合法交易日执行；本页生成参考数量，不向券商发送订单。模型仓位来自固定起点的连续账户，不等于你的实盘仓位。", input_bindings=[{**{k: str(v) for k, v in b.items() if k != "parquet"}, "parquet": str(b["parquet"].relative_to(root))} for b in resolved], historical_replay=chart_history(result), definition_sha256=__import__('hashlib').sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest(), engine_sha256=fingerprint(root / "src/alpha_research_os/portfolio/strategy_backtest.py"), source_backtest_sha256=fingerprint(folder / f"{jid}.backtest.json"), created_at=datetime.now(timezone.utc).isoformat())
    from alpha_research_os.portfolio.quality_inputs import CALCULATION_MODE, source_signature
    signature = resolved[0]["source_signature"]
    if source_signature(root) != signature:
        raise RuntimeError("源数据或公式在重算期间发生变化，结果不作为最新信号发布；请重新计算")
    output.update(calculation_mode=CALCULATION_MODE, source_signature=signature)
    atomic_json(folder / f"{jid}.result.json", output)
    state = read_json(state_path)
    state.update(status="PASS", progress=100, phase="信号、持仓与数量对账完成", finished_at=datetime.now(timezone.utc).isoformat())
    atomic_json(state_path, state)


def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    ROOT = root
    try:
        run(root, args.job_id)
    except Exception as exc:
        path = root / "data/quality_strategies/jobs" / f"{args.job_id}.state.json"
        state = read_json(path)
        state.update(status="FAIL", phase="计算失败", error=str(exc), finished_at=datetime.now(timezone.utc).isoformat())
        atomic_json(path, state)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
