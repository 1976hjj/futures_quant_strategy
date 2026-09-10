"""Configurable factor-combination selection and continuous-account backtest."""

from __future__ import annotations

import bisect
import json
import math
import os
import threading
from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Literal

import duckdb
from pydantic import Field, model_validator

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import Digest, FrozenSpec, Identifier
from alpha_research_os.portfolio.execution import (
    DailyBarExecutionSpec,
    DailyBarLiquidity,
    FillStatus,
    OrderIntent,
    OrderSide,
    simulate_daily_bar_fill,
)


class ScoreRule(FrozenSpec):
    factor_id: Identifier
    release_id: Digest
    direction: Literal["HIGH", "LOW"] = "HIGH"
    weight: float = Field(gt=0)
    transform: Literal["PERCENTILE"] = "PERCENTILE"


class FilterRule(FrozenSpec):
    factor_id: Identifier
    release_id: Digest
    mode: Literal["EXCLUDE_HIGH", "EXCLUDE_LOW"]
    fraction: float = Field(gt=0, lt=0.5)
    missing_policy: Literal["EXCLUDE", "KEEP"] = "EXCLUDE"


class StrategyBacktestRequest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    name: str = Field(min_length=1, max_length=100)
    start: date
    end: date
    universe_id: Literal["ALL-A-PIT"] = "ALL-A-PIT"
    score_rules: tuple[ScoreRule, ...] = Field(min_length=1, max_length=12)
    filter_rules: tuple[FilterRule, ...] = Field(default=(), max_length=12)
    exclude_st: bool = True
    minimum_listed_sessions: int = Field(default=60, ge=0, le=1250)
    target_count: int = Field(default=50, ge=1, le=500)
    retention_rank: int = Field(default=75, ge=1, le=1000)
    rebalance_sessions: int = Field(default=5, ge=1, le=60)
    initial_cash_cny: float = Field(default=1_000_000, gt=0)
    minimum_cash_fraction: float = Field(default=0.02, ge=0, lt=0.5)
    buy_commission_bps: float = Field(default=3, ge=0, le=100)
    sell_commission_bps: float = Field(default=3, ge=0, le=100)
    sell_stamp_duty_bps: float = Field(default=5, ge=0, le=100)
    historical_sell_stamp_duty_bps: float = Field(default=10, ge=0, le=100)
    minimum_commission_cny: float = Field(default=5, ge=0, le=100)
    transfer_fee_bps: float = Field(default=0.1, ge=0, le=10)
    historical_transfer_fee_bps: float = Field(default=0.2, ge=0, le=10)
    base_slippage_bps: float = Field(default=2, ge=0, le=100)
    square_root_impact_bps: float = Field(default=20, ge=0, le=500)
    maximum_slippage_bps: float = Field(default=100, ge=0, le=1000)
    maximum_participation_rate: float = Field(default=0.10, gt=0, le=1)

    @model_validator(mode="after")
    def valid_request(self) -> StrategyBacktestRequest:
        if self.end < self.start:
            raise ValueError("end must not precede start")
        if self.retention_rank < self.target_count:
            raise ValueError("retention_rank must be at least target_count")
        if len({rule.factor_id for rule in self.score_rules}) != len(self.score_rules):
            raise ValueError("score factors must be unique")
        if len({rule.factor_id for rule in self.filter_rules}) != len(self.filter_rules):
            raise ValueError("filter factors must be unique")
        return self

    @property
    def config_id(self) -> Digest:
        return content_hash(self)


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_path(value: Path) -> str:
    return value.resolve().as_posix().replace("'", "''")


def _manifest(project_root: Path, release_id: str) -> tuple[dict[str, Any], Path]:
    release_key = release_id.removeprefix("sha256:")
    manifest_path = project_root / "data" / "factor_store" / "releases" / release_key / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"factor release does not exist: {release_id}")
    payload = json.loads(manifest_path.read_bytes())
    if payload.get("release_id") != release_id:
        raise ValueError(f"factor release identity mismatch: {release_id}")
    parquet = project_root / "data" / "factor_store" / payload["parquet_relative_path"]
    if not parquet.exists():
        raise ValueError(f"factor value file is missing: {release_id}")
    return payload, parquet


def _factor_release_candidates(project_root: Path, factor_id: str) -> list[dict[str, Any]]:
    """List every intact published release that contains one requested factor."""
    releases_root = project_root / "data" / "factor_store" / "releases"
    candidates: list[dict[str, Any]] = []
    for manifest_path in releases_root.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_bytes())
            verification_path = manifest_path.parent / "accuracy_verification.json"
            if verification_path.exists() and json.loads(verification_path.read_bytes()).get("status") == "FAIL":
                continue
            factors = manifest["request"]["factors"]
            if not any(item["factor_id"] == factor_id for item in factors):
                continue
            start = date.fromisoformat(manifest["request"]["start"])
            end = date.fromisoformat(manifest["request"]["end"])
            release_id = str(manifest["release_id"])
            parquet = project_root / "data" / "factor_store" / manifest["parquet_relative_path"]
            if not parquet.exists():
                continue
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        candidates.append(
            {
                "factor_id": factor_id,
                "release_id": release_id,
                "start": start,
                "end": end,
                "created_at": str(manifest.get("created_at") or ""),
                "parquet": parquet,
            }
        )
    return candidates


def _resolve_inputs(project_root: Path, request: StrategyBacktestRequest) -> list[dict[str, Any]]:
    """Choose the widest published release that fully covers the requested range.

    The browser's stored release ID is deliberately not a hard dependency. It is
    an audit hint from when the user configured the strategy; the strategy itself
    requires a factor value series that covers every requested trading day.
    """
    by_factor: dict[str, dict[str, Any]] = {}
    for factor_id in {rule.factor_id for rule in (*request.score_rules, *request.filter_rules)}:
        compatible = [
            item
            for item in _factor_release_candidates(project_root, factor_id)
            if item["start"] <= request.start and item["end"] >= request.end
        ]
        if not compatible:
            available = _factor_release_candidates(project_root, factor_id)
            ranges = ", ".join(
                f"{item['start']}..{item['end']}"
                for item in sorted(available, key=lambda item: item["end"], reverse=True)[:3]
            ) or "none"
            raise ValueError(
                f"{factor_id} has no published release covering {request.start}..{request.end}; available: {ranges}"
            )
        by_factor[factor_id] = max(
            compatible,
            key=lambda item: (
                (item["end"] - item["start"]).days,
                item["end"].toordinal(),
                -item["start"].toordinal(),
                item["created_at"],
                item["release_id"],
            ),
        )
    return [by_factor[rule.factor_id] for rule in (*request.score_rules, *request.filter_rules)]


def _effective_request(request: StrategyBacktestRequest, resolved: list[dict[str, Any]]) -> StrategyBacktestRequest:
    """Record the automatically resolved releases so reports remain reproducible."""
    release_by_factor = {item["factor_id"]: item["release_id"] for item in resolved}
    return request.model_copy(
        update={
            "score_rules": tuple(
                rule.model_copy(update={"release_id": release_by_factor[rule.factor_id]})
                for rule in request.score_rules
            ),
            "filter_rules": tuple(
                rule.model_copy(update={"release_id": release_by_factor[rule.factor_id]})
                for rule in request.filter_rules
            ),
        }
    )


def preflight(project_root: Path, request: StrategyBacktestRequest) -> dict[str, Any]:
    resolved = _resolve_inputs(project_root, request)
    effective_request = _effective_request(request, resolved)
    common_start = max(item["start"] for item in resolved)
    common_end = min(item["end"] for item in resolved)
    if common_end < common_start:
        raise ValueError("selected factor releases do not have a common date range")
    if request.start < common_start or request.end > common_end:
        raise ValueError(f"backtest range must stay inside factor common range {common_start}..{common_end}")
    database = project_root / "data" / "warehouse" / "alpha_research.duckdb"
    with duckdb.connect(str(database), read_only=True) as connection:
        session_count = connection.execute(
            """SELECT count(*) FROM research.trading_calendar
            WHERE exchange='SSE' AND is_open AND cal_date BETWEEN ? AND ?""",
            [request.start, request.end],
        ).fetchone()[0]
    if session_count < 2:
        raise ValueError("backtest range must contain at least two trading sessions")
    warnings = ["当前区间已经用于研究，回测结果属于历史诊断。"]
    if request.rebalance_sessions == 1:
        warnings.append("每日调仓通常会放大成本影响，请重点检查净收益和换手。")
    return {
        "status": "READY",
        "config_id": effective_request.config_id,
        "common_range": {"start": common_start.isoformat(), "end": common_end.isoformat()},
        "session_count": session_count,
        "factor_count": len(resolved),
        "estimated_rebalances": max(1, (session_count - 1) // request.rebalance_sessions),
        "warnings": warnings,
    }


def _percentiles(values: dict[str, float], high_is_good: bool) -> dict[str, float]:
    ordered = sorted(values.values())
    count = len(ordered)
    if count <= 1:
        return {key: 0.5 for key in values}
    result: dict[str, float] = {}
    for key, value in values.items():
        left = bisect.bisect_left(ordered, value)
        right = bisect.bisect_right(ordered, value)
        percentile = ((left + right - 1) / 2) / (count - 1)
        result[key] = percentile if high_is_good else 1 - percentile
    return result


def _signal_rows(
    connection: duckdb.DuckDBPyConnection,
    resolved: list[dict[str, Any]],
    request: StrategyBacktestRequest,
    signal_date: date,
    *,
    prepared: bool = False,
) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {
        (item["factor_id"], item["release_id"]): item for item in resolved
    }
    joins: list[str] = []
    values: list[str] = []
    for index, (_key, item) in enumerate(unique.items()):
        alias = f"f{index}"
        joins.append(
            f"LEFT JOIN read_parquet('{_sql_path(item['parquet'])}') {alias} "
            f"ON {alias}.session=u.trade_date AND {alias}.instrument_id=u.ts_code "
            f"AND {alias}.factor_id={_sql_string(item['factor_id'])} AND {alias}.variant='RAW'"
        )
        values.append(f"{alias}.value AS value_{index}")
    if prepared:
        rows = connection.execute(
            "SELECT * EXCLUDE (trade_date) FROM strategy_signal_values WHERE trade_date=? ORDER BY ts_code",
            [signal_date],
        ).fetchall()
    else:
        rows = connection.execute(
            f"""SELECT u.ts_code, u.security_name, u.is_st, u.listed_session_number,
            {', '.join(values)} FROM research.universe_daily u {' '.join(joins)}
            WHERE u.trade_date=? AND u.eligible_for_signal ORDER BY u.ts_code""",
            [signal_date],
        ).fetchall()
    columns = [item[0] for item in connection.description]
    output = [dict(zip(columns, row, strict=True)) for row in rows]
    for item in output:
        factor_values: dict[tuple[str, str], float | None] = {}
        for index, key in enumerate(unique):
            value = item.pop(f"value_{index}")
            factor_values[key] = float(value) if value is not None and math.isfinite(value) else None
        item["factor_values"] = factor_values
    return output


def _prepare_signal_table(
    connection: duckdb.DuckDBPyConnection,
    resolved: list[dict[str, Any]],
    signal_sessions: set[date],
    cache_path: Path | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    unique: dict[tuple[str, str], dict[str, Any]] = {
        (item["factor_id"], item["release_id"]): item for item in resolved
    }
    joins: list[str] = []
    values: list[str] = []
    for index, item in enumerate(unique.values()):
        alias = f"f{index}"
        joins.append(
            f"LEFT JOIN read_parquet('{_sql_path(item['parquet'])}') {alias} "
            f"ON {alias}.session=u.trade_date AND {alias}.instrument_id=u.ts_code "
            f"AND {alias}.factor_id={_sql_string(item['factor_id'])} AND {alias}.variant='RAW'"
        )
        values.append(f"{alias}.value AS value_{index}")
    if cache_path is not None and cache_path.exists():
        connection.execute(
            f"""CREATE OR REPLACE TEMP TABLE strategy_signal_values AS
            SELECT * FROM read_parquet('{_sql_path(cache_path)}')"""
        )
        return
    dates = ",".join(f"DATE {_sql_string(item.isoformat())}" for item in sorted(signal_sessions))
    finished = threading.Event()

    def monitor_query() -> None:
        while not finished.wait(1.5):
            try:
                query_percent = float(connection.query_progress())
            except (RuntimeError, TypeError, ValueError):
                continue
            if progress_callback and query_percent >= 0:
                progress_callback(
                    {
                        "phase": "准备因子信号表",
                        "progress": min(17, 10 + round(query_percent / 100 * 7)),
                        "query_progress": query_percent,
                    }
                )

    monitor = threading.Thread(target=monitor_query, daemon=True)
    monitor.start()
    try:
        connection.execute(
            f"""CREATE OR REPLACE TEMP TABLE strategy_signal_values AS
            SELECT u.trade_date, u.ts_code, u.security_name, u.is_st, u.listed_session_number,
            {', '.join(values)} FROM research.universe_daily u {' '.join(joins)}
            WHERE u.eligible_for_signal AND u.trade_date IN ({dates})"""
        )
    finally:
        finished.set()
        monitor.join(timeout=2)
    if cache_path is not None:
        # This cache is an exact materialization of the already-created temporary
        # table. A cache-write failure must never fail or alter a backtest.
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
            connection.execute(
                f"COPY strategy_signal_values TO '{_sql_path(temporary)}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            if not cache_path.exists():
                os.replace(temporary, cache_path)
            else:
                temporary.unlink(missing_ok=True)
        except OSError:
            # An antivirus scanner or a concurrent command can hold the cache
            # briefly. It is only a performance artifact, so the live result wins.
            pass


def _signal_cache_path(
    project_root: Path, resolved: list[dict[str, Any]], signal_sessions: set[date]
) -> Path:
    """Return a cache path that is invalidated when factor or warehouse inputs change."""
    warehouse = project_root / "data" / "warehouse" / "alpha_research.duckdb"
    fingerprint = warehouse.stat()
    cache_key = content_hash(
        {
            "schema": "strategy-signal-cache-v1",
            "factors": [
                {"factor_id": item["factor_id"], "release_id": item["release_id"]}
                for item in resolved
            ],
            "signal_sessions": [item.isoformat() for item in sorted(signal_sessions)],
            "warehouse_size": fingerprint.st_size,
            "warehouse_mtime_ns": fingerprint.st_mtime_ns,
        }
    ).removeprefix("sha256:")
    return project_root / "data" / "strategy_backtest_cache" / f"signals-{cache_key}.parquet"


def select_portfolio(
    rows: list[dict[str, Any]],
    request: StrategyBacktestRequest,
    existing_holdings: set[str] | None = None,
    *,
    industry_by_code: dict[str, str] | None = None,
    maximum_industry_weight: float | None = None,
    missing_industry_policy: Literal["UNKNOWN_BUCKET", "EXCLUDE"] = "UNKNOWN_BUCKET",
) -> dict[str, Any]:
    existing_holdings = existing_holdings or set()
    base = [
        row
        for row in rows
        if (not request.exclude_st or not row["is_st"])
        and (row["listed_session_number"] or 0) >= request.minimum_listed_sessions
        and not (
            industry_by_code is not None
            and missing_industry_policy == "EXCLUDE"
            and row["ts_code"] not in industry_by_code
        )
    ]
    base_count = len(base)
    survivors = base
    filter_counts: list[dict[str, Any]] = []
    for rule in request.filter_rules:
        key = (rule.factor_id, rule.release_id)
        finite = {row["ts_code"]: row["factor_values"][key] for row in base if row["factor_values"][key] is not None}
        ranked = _percentiles(finite, high_is_good=True)
        kept: list[dict[str, Any]] = []
        excluded = 0
        for row in survivors:
            value = row["factor_values"][key]
            if value is None:
                drop = rule.missing_policy == "EXCLUDE"
            elif rule.mode == "EXCLUDE_HIGH":
                drop = ranked[row["ts_code"]] >= 1 - rule.fraction
            else:
                drop = ranked[row["ts_code"]] <= rule.fraction
            if drop:
                excluded += 1
            else:
                kept.append(row)
        survivors = kept
        filter_counts.append({"factor_id": rule.factor_id, "excluded": excluded, "remaining": len(survivors)})

    score_ready = [
        row
        for row in survivors
        if all(row["factor_values"][(rule.factor_id, rule.release_id)] is not None for rule in request.score_rules)
    ]
    total_weight = sum(rule.weight for rule in request.score_rules)
    scores = {row["ts_code"]: 0.0 for row in score_ready}
    for rule in request.score_rules:
        key = (rule.factor_id, rule.release_id)
        values = {row["ts_code"]: float(row["factor_values"][key]) for row in score_ready}
        ranked = _percentiles(values, high_is_good=rule.direction == "HIGH")
        for ts_code in scores:
            scores[ts_code] += ranked[ts_code] * rule.weight / total_weight
    ranked_rows = sorted(score_ready, key=lambda row: (-scores[row["ts_code"]], row["ts_code"]))
    rank_by_code = {row["ts_code"]: index + 1 for index, row in enumerate(ranked_rows)}
    retained = sorted(
        (code for code in existing_holdings if rank_by_code.get(code, 10**9) <= request.retention_rank),
        key=rank_by_code.__getitem__,
    )
    selected: list[str] = []
    industry_counts: dict[str, int] = {}
    industry_limit_excluded = 0
    maximum_per_industry = (
        max(1, math.floor(request.target_count * maximum_industry_weight))
        if industry_by_code is not None and maximum_industry_weight is not None
        else None
    )

    def add_if_allowed(code: str) -> bool:
        nonlocal industry_limit_excluded
        if code in selected:
            return False
        industry = (industry_by_code or {}).get(code, "UNKNOWN")
        if maximum_per_industry is not None and industry_counts.get(industry, 0) >= maximum_per_industry:
            industry_limit_excluded += 1
            return False
        selected.append(code)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        return True

    for code in retained:
        if len(selected) >= request.target_count:
            break
        add_if_allowed(code)
    for row in ranked_rows:
        if len(selected) >= request.target_count:
            break
        add_if_allowed(row["ts_code"])
    by_code = {row["ts_code"]: row for row in ranked_rows}
    holdings = [
        {
            "rank": rank_by_code[code],
            "ts_code": code,
            "security_name": by_code[code]["security_name"],
            "score": scores[code],
            "retained": code in retained,
            "industry_code": (industry_by_code or {}).get(code),
            "factor_values": {
                rule.factor_id: by_code[code]["factor_values"][(rule.factor_id, rule.release_id)]
                for rule in request.score_rules
            },
        }
        for code in selected
    ]
    return {
        "base_count": base_count,
        "after_filters": len(survivors),
        "score_ready": len(score_ready),
        "missing_score_excluded": len(survivors) - len(score_ready),
        "filter_counts": filter_counts,
        "industry_limit_excluded": industry_limit_excluded,
        "industry_counts": industry_counts,
        "holdings": holdings,
    }


def _pit_industry_by_code(
    connection: duckdb.DuckDBPyConnection, signal_date: date, ts_codes: list[str]
) -> dict[str, str]:
    if not ts_codes:
        return {}
    codes = sorted(set(ts_codes))
    placeholders = ",".join("?" for _ in codes)
    rows = connection.execute(
        f"""SELECT ts_code, l1_code
        FROM research.sw_industry_membership
        WHERE in_date <= ? AND (out_date IS NULL OR ? < out_date)
          AND ts_code IN ({placeholders})
        QUALIFY row_number() OVER (
          PARTITION BY ts_code ORDER BY in_date DESC, source_snapshot_id DESC, l3_code DESC
        )=1""",
        [signal_date, signal_date, *codes],
    ).fetchall()
    return {str(ts_code): str(industry_code) for ts_code, industry_code in rows}


def preview(project_root: Path, request: StrategyBacktestRequest, signal_date: date) -> dict[str, Any]:
    checked = preflight(project_root, request)
    if not request.start <= signal_date <= request.end:
        raise ValueError("preview date must stay inside the backtest range")
    resolved = _resolve_inputs(project_root, request)
    request = _effective_request(request, resolved)
    database = project_root / "data" / "warehouse" / "alpha_research.duckdb"
    with duckdb.connect(str(database), read_only=True) as connection:
        available = connection.execute(
            "SELECT is_open FROM research.trading_calendar WHERE exchange='SSE' AND cal_date=?",
            [signal_date],
        ).fetchone()
        if not available or not available[0]:
            raise ValueError("preview date must be a trading session")
        selected = select_portfolio(_signal_rows(connection, resolved, request, signal_date), request)
    return {"status": "READY", "signal_date": signal_date.isoformat(), "preflight": checked, **selected}


def _market_rows(
    connection: duckdb.DuckDBPyConnection, session: date, securities: set[str]
) -> dict[str, dict[str, Any]]:
    if not securities:
        return {}
    placeholders = ",".join("?" for _ in securities)
    rows = connection.execute(
        f"""SELECT u.ts_code, u.security_name, m.open, m.close, m.amount_cny, m.is_tradeable_bar,
        u.is_suspended, a.adj_factor, l.up_limit, l.down_limit, u.delist_date,
        ca.stock_dividend_ratio, ca.cash_dividend_per_share
        FROM research.universe_daily u
        LEFT JOIN research.market_daily m USING (trade_date, ts_code)
        LEFT JOIN research.adj_factor a USING (trade_date, ts_code)
        LEFT JOIN raw.m2e_stk_limit l USING (trade_date, ts_code)
        LEFT JOIN research.corporate_action_reconciliation_approved ca
          ON ca.effective_date=u.trade_date AND ca.ts_code=u.ts_code
        WHERE u.trade_date=? AND u.ts_code IN ({placeholders})""",
        [session, *sorted(securities)],
    ).fetchall()
    names = (
        "ts_code", "security_name", "open", "close", "amount", "tradeable", "suspended", "adj", "up", "down",
        "delist_date", "stock_dividend_ratio", "cash_dividend_per_share",
    )
    return {
        row[0]: dict(zip(names, row, strict=True))
        for row in rows
    }


def _prefetch_market_rows(
    connection: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    securities: set[str],
    cache: dict[str, dict[date, dict[str, Any]]],
) -> None:
    """Fetch the remaining daily bars for newly selected securities in one query.

    The account simulation itself deliberately remains sequential: cash and holdings
    on day N are inputs to day N+1.  What is independent is the database lookup of
    each selected security's future daily bars.  Keeping the cache keyed by security
    also lets us evict names once they are no longer held or pending, bounding memory
    use to the live portfolio rather than the whole universe.
    """
    if not securities or end < start:
        return
    placeholders = ",".join("?" for _ in securities)
    rows = connection.execute(
        f"""SELECT u.ts_code, u.trade_date, u.security_name, m.open, m.close, m.amount_cny, m.is_tradeable_bar,
        u.is_suspended, a.adj_factor, l.up_limit, l.down_limit, u.delist_date,
        ca.stock_dividend_ratio, ca.cash_dividend_per_share
        FROM research.universe_daily u
        LEFT JOIN research.market_daily m USING (trade_date, ts_code)
        LEFT JOIN research.adj_factor a USING (trade_date, ts_code)
        LEFT JOIN raw.m2e_stk_limit l USING (trade_date, ts_code)
        LEFT JOIN research.corporate_action_reconciliation_approved ca
          ON ca.effective_date=u.trade_date AND ca.ts_code=u.ts_code
        WHERE u.trade_date BETWEEN ? AND ? AND u.ts_code IN ({placeholders})""",
        [start, end, *sorted(securities)],
    ).fetchall()
    names = (
        "ts_code", "trade_date", "security_name", "open", "close", "amount", "tradeable", "suspended", "adj", "up",
        "down", "delist_date", "stock_dividend_ratio", "cash_dividend_per_share",
    )
    for row in rows:
        payload = dict(zip(names, row, strict=True))
        code = payload.pop("ts_code")
        session = payload.pop("trade_date")
        cache.setdefault(code, {})[session] = payload


def _configure_backtest_connection(connection: duckdb.DuckDBPyConnection) -> None:
    """Use available compute without allowing one job to force Windows to page."""
    # This application admits one active job.  Twelve workers works well for the
    # 16 logical processors available on the development machine, while leaving
    # capacity for the browser/API and avoiding the E-core oversubscription of a
    # 12600KF.  The memory cap is intentionally conservative because the desktop
    # is already carrying editor and browser workloads.
    connection.execute(f"SET threads={min(12, os.cpu_count() or 1)}")
    connection.execute("SET memory_limit='8GB'")
    connection.execute("SET parquet_metadata_cache=true")
    connection.execute("SET preserve_insertion_order=false")


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def _maximum_drawdown_period(daily: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Describe the worst peak-to-trough episode and whether it recovered."""
    if not daily:
        return None
    peak_index = 0
    trough_index = 0
    worst = 0.0
    worst_peak_index = 0
    for index, row in enumerate(daily):
        if row["nav"] > daily[peak_index]["nav"]:
            peak_index = index
        drawdown = row["nav"] / daily[peak_index]["nav"] - 1
        if drawdown < worst:
            worst = drawdown
            trough_index = index
            worst_peak_index = peak_index
    recovery_index = next(
        (
            index
            for index in range(trough_index + 1, len(daily))
            if daily[index]["nav"] >= daily[worst_peak_index]["nav"]
        ),
        None,
    )
    peak_session = date.fromisoformat(daily[worst_peak_index]["session"])
    trough_session = date.fromisoformat(daily[trough_index]["session"])
    recovery_session = date.fromisoformat(daily[recovery_index]["session"]) if recovery_index is not None else None
    return {
        "drawdown": worst,
        "peak_session": peak_session.isoformat(),
        "trough_session": trough_session.isoformat(),
        "recovery_session": recovery_session.isoformat() if recovery_session else None,
        "peak_to_trough_sessions": trough_index - worst_peak_index,
        "recovery_sessions": recovery_index - trough_index if recovery_index is not None else None,
        "peak_to_recovery_sessions": recovery_index - worst_peak_index if recovery_index is not None else None,
        "peak_to_trough_calendar_days": (trough_session - peak_session).days,
        "recovery_calendar_days": (recovery_session - trough_session).days if recovery_session else None,
        "peak_to_recovery_calendar_days": (recovery_session - peak_session).days if recovery_session else None,
        "recovered": recovery_index is not None,
    }


def _csi300_benchmark(project_root: Path, sessions: list[date]) -> dict[str, Any] | None:
    """Normalize local CSI 300 closes to the same starting point as the strategy."""
    source_path = project_root / "data" / "benchmarks" / "csi300_daily.json"
    if not source_path.exists():
        return None
    try:
        source = json.loads(source_path.read_bytes())
        closes = {
            date.fromisoformat(item["session"]): float(item["close"])
            for item in source.get("daily", [])
            if float(item["close"]) > 0
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if any(session not in closes for session in sessions):
        return None
    initial_close = closes[sessions[0]]
    daily = [
        {
            "session": session.isoformat(),
            "close": closes[session],
            "return": closes[session] / initial_close - 1,
        }
        for session in sessions
    ]
    return {
        "benchmark_id": source.get("benchmark_id", "CSI300"),
        "name": source.get("name", "沪深300"),
        "symbol": source.get("symbol", "000300.SH"),
        "pricing": source.get("pricing", "close_price_index"),
        "source": source.get("source"),
        "daily": daily,
        "summary": {"total_return": daily[-1]["return"]},
    }


def _cash(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _post_trade_exposure(
    positions: dict[str, int], cash: float, marks: dict[str, float], traded_code: str
) -> dict[str, float]:
    """Value the account immediately after one fill using contemporaneous marks."""

    position_values = {
        code: _cash(quantity * max(float(marks.get(code, 0.0)), 0.0))
        for code, quantity in positions.items()
    }
    invested_value = _cash(sum(position_values.values()))
    account_value = _cash(cash + invested_value)
    security_value = position_values.get(traded_code, 0.0)
    return {
        "post_security_value_cny": security_value,
        "post_invested_value_cny": invested_value,
        "post_account_value_cny": account_value,
        "post_security_weight": security_value / account_value if account_value > 0 else 0.0,
        "post_total_position_weight": invested_value / account_value if account_value > 0 else 0.0,
    }


def _is_star_market(ts_code: str) -> bool:
    return ts_code.endswith(".SH") and ts_code.split(".", 1)[0].startswith(("688", "689"))


def _target_share_quantity(ts_code: str, target_value: float, price: float) -> int:
    """Convert a target value to a valid A-share buyable position size."""

    if price <= 0 or target_value <= 0:
        return 0
    raw = int(Decimal(str(target_value / price)).to_integral_value(rounding=ROUND_FLOOR))
    if _is_star_market(ts_code):
        return raw if raw >= 200 else 0
    return raw // 100 * 100


def _sell_order_quantity(ts_code: str, current: int, target: int) -> int:
    desired = max(0, current - target)
    if desired == 0:
        return 0
    if target == 0:
        return current
    if _is_star_market(ts_code):
        return desired if desired >= 200 else 0
    return desired // 100 * 100


def _buy_order_quantity(ts_code: str, desired: int) -> int:
    if _is_star_market(ts_code):
        return desired if desired >= 200 else 0
    return desired // 100 * 100


def run_backtest(
    project_root: Path,
    request: StrategyBacktestRequest,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    *,
    market_data_mode: Literal["prefetch", "legacy"] = "prefetch",
    target_schedule: dict[date, dict[str, float]] | None = None,
    include_selection_history: bool = False,
    maximum_industry_weight: float | None = None,
    missing_industry_policy: Literal["UNKNOWN_BUCKET", "EXCLUDE"] = "UNKNOWN_BUCKET",
) -> dict[str, Any]:
    checked = preflight(project_root, request)
    resolved = _resolve_inputs(project_root, request)
    request = _effective_request(request, resolved)
    database = project_root / "data" / "warehouse" / "alpha_research.duckdb"
    execution = DailyBarExecutionSpec(
        buy_commission_bps=request.buy_commission_bps,
        sell_commission_bps=request.sell_commission_bps,
        sell_stamp_duty_bps=request.sell_stamp_duty_bps,
        historical_sell_stamp_duty_bps=request.historical_sell_stamp_duty_bps,
        minimum_commission_cny=request.minimum_commission_cny,
        transfer_fee_bps=request.transfer_fee_bps,
        historical_transfer_fee_bps=request.historical_transfer_fee_bps,
        base_slippage_bps=request.base_slippage_bps,
        square_root_impact_bps=request.square_root_impact_bps,
        maximum_slippage_bps=request.maximum_slippage_bps,
        maximum_participation_rate=request.maximum_participation_rate,
    )
    with duckdb.connect(str(database), read_only=True) as connection:
        _configure_backtest_connection(connection)
        sessions = [
            row[0]
            for row in connection.execute(
                """SELECT cal_date FROM research.trading_calendar
                WHERE exchange='SSE' AND is_open AND cal_date BETWEEN ? AND ? ORDER BY cal_date""",
                [request.start, request.end],
            ).fetchall()
        ]
        signal_sessions = (
            set(target_schedule)
            if target_schedule is not None
            else set(sessions[:: request.rebalance_sessions])
        )
        invalid_schedule_dates = signal_sessions - set(sessions)
        if invalid_schedule_dates:
            raise ValueError("target schedule contains dates outside the backtest sessions")
        if target_schedule is not None:
            for signal_session, weights in target_schedule.items():
                if any(not math.isfinite(weight) or weight < 0 for weight in weights.values()):
                    raise ValueError(f"target schedule has invalid weights on {signal_session}")
                if weights and not math.isclose(sum(weights.values()), 1.0, rel_tol=0, abs_tol=1e-9):
                    raise ValueError(f"target schedule weights must sum to one on {signal_session}")
        signal_cache = (
            None
            if target_schedule is not None
            else _signal_cache_path(project_root, resolved, signal_sessions)
        )
        if progress_callback:
            progress_callback(
                {
                    "phase": "准备因子信号表",
                    "progress": 10,
                    "processed_sessions": 0,
                    "total_sessions": len(sessions),
                    "current_session": None,
                    "rebalance_count": 0,
                    "position_count": 0,
                }
            )
        if target_schedule is None:
            _prepare_signal_table(connection, resolved, signal_sessions, signal_cache, progress_callback)
        if progress_callback:
            progress_callback({"phase": "连续账户回放", "progress": 18})
        positions: dict[str, int] = {}
        position_costs: dict[str, float] = {}
        position_dividends: dict[str, float] = {}
        last_marks: dict[str, float] = {}
        cash = request.initial_cash_cny
        pending_target: dict[str, float] | None = None
        selection_history: list[dict[str, Any]] = []
        daily: list[dict[str, Any]] = []
        # Keep only completed transactions.  Failed orders are already represented
        # in rejection_counts, while persisting each of them would needlessly grow
        # every historical backtest report.
        trades: list[dict[str, Any]] = []
        trade_attempt_count = 0
        rejection_counts: dict[str, int] = {}
        total_cost = 0.0
        total_turnover_notional = 0.0
        total_realized_pnl = 0.0
        total_dividend_cash = 0.0
        rebalance_count = 0
        delisting_liquidations = 0
        market_cache: dict[str, dict[date, dict[str, Any]]] = {}

        for session_index, session in enumerate(sessions, start=1):
            target_set = set(pending_target or [])
            relevant = set(positions) | target_set
            if market_data_mode == "legacy":
                bars = _market_rows(connection, session, relevant)
            else:
                # A defensive per-name fallback retains exactly the old lookup
                # semantics when a source table has no cached universe row.
                bars = {
                    code: market_cache.get(code, {}).get(session, {})
                    for code in relevant
                }
                missing = {code for code, bar in bars.items() if not bar}
                if missing:
                    bars.update(_market_rows(connection, session, missing))
            day_dividend_cash = 0.0
            for code, quantity in list(positions.items()):
                bar = bars.get(code, {})
                stock_ratio = float(bar.get("stock_dividend_ratio") or 0.0)
                cash_dividend = float(bar.get("cash_dividend_per_share") or 0.0)
                if stock_ratio > 0:
                    positions[code] = int(round(quantity * (1 + stock_ratio)))
                if cash_dividend > 0:
                    credited = _cash(quantity * cash_dividend)
                    cash = _cash(cash + credited)
                    position_dividends[code] = _cash(position_dividends.get(code, 0.0) + credited)
                    day_dividend_cash = _cash(day_dividend_cash + credited)
                    total_dividend_cash = _cash(total_dividend_cash + credited)

            open_nav = cash + sum(
                quantity * ((bars.get(code, {}).get("open") or 0) or last_marks.get(code, 0))
                for code, quantity in positions.items()
            )
            intraday_marks = {
                code: float((bars.get(code, {}).get("open") or 0) or last_marks.get(code, 0))
                for code in relevant
            }
            day_notional = 0.0
            day_cost = 0.0

            if pending_target is not None:
                rebalance_count += 1
                target_quantities = {
                    code: _target_share_quantity(
                        code,
                        open_nav * (1 - request.minimum_cash_fraction) * pending_target[code],
                        float((bars.get(code, {}).get("open") or 0) or last_marks.get(code, 0)),
                    )
                    for code in target_set
                }
                sell_orders = {
                    code: _sell_order_quantity(code, quantity, target_quantities.get(code, 0))
                    for code, quantity in positions.items()
                }
                for code, quantity in sorted(sell_orders.items()):
                    if quantity <= 0:
                        continue
                    bar = bars.get(code, {})
                    fill = simulate_daily_bar_fill(
                        OrderIntent(side=OrderSide.SELL, quantity=quantity),
                        DailyBarLiquidity(
                            reference_price=bar.get("open"), traded_amount_cny=bar.get("amount"),
                            up_limit=bar.get("up"), down_limit=bar.get("down"),
                            is_suspended=bool(bar.get("suspended", True)),
                            is_tradeable_bar=bool(bar.get("tradeable", False)),
                        ),
                        execution,
                        session,
                    )
                    trade_attempt_count += 1
                    if fill.status is FillStatus.FILLED and fill.fill_price is not None:
                        current_quantity = positions[code]
                        sold = min(current_quantity, quantity)
                        proceeds = _cash(sold * fill.fill_price)
                        allocated_cost = _cash(position_costs.get(code, 0.0) * sold / current_quantity)
                        allocated_dividend = _cash(
                            position_dividends.get(code, 0.0) * sold / current_quantity
                        )
                        trading_pnl = _cash(proceeds - fill.total_cost_cny - allocated_cost)
                        realized_pnl = _cash(trading_pnl + allocated_dividend)
                        realized_pnl_pct = realized_pnl / allocated_cost if allocated_cost else None
                        positions[code] -= sold
                        position_costs[code] = _cash(position_costs.get(code, 0.0) - allocated_cost)
                        position_dividends[code] = _cash(
                            position_dividends.get(code, 0.0) - allocated_dividend
                        )
                        if positions[code] == 0:
                            positions.pop(code)
                            position_costs.pop(code, None)
                            position_dividends.pop(code, None)
                        cash = _cash(cash + proceeds - fill.total_cost_cny)
                        intraday_marks[code] = fill.fill_price
                        day_notional = _cash(day_notional + proceeds)
                        day_cost = _cash(day_cost + fill.total_cost_cny)
                        total_realized_pnl = _cash(total_realized_pnl + realized_pnl)
                        trades.append(
                            {
                                "session": session.isoformat(), "rebalance_id": rebalance_count,
                                "ts_code": code, "security_name": bar.get("security_name") or code,
                                "side": "SELL", "quantity": sold, "price": fill.fill_price,
                                "amount_cny": proceeds, "commission_cny": fill.commission_cny,
                                "stamp_duty_cny": fill.stamp_duty_cny,
                                "transfer_fee_cny": fill.transfer_fee_cny,
                                "total_cost_cny": fill.total_cost_cny,
                                "cost_basis_cny": allocated_cost,
                                "allocated_dividend_cny": allocated_dividend,
                                "trading_realized_pnl_cny": trading_pnl,
                                "realized_pnl_cny": realized_pnl,
                                "realized_pnl_pct": realized_pnl_pct,
                                "post_quantity": positions.get(code, 0),
                                **_post_trade_exposure(positions, cash, intraday_marks, code),
                            }
                        )
                    else:
                        rejection_counts[fill.status.value] = rejection_counts.get(fill.status.value, 0) + 1

                for code in pending_target:
                    bar = bars.get(code, {})
                    desired = _buy_order_quantity(
                        code, target_quantities.get(code, 0) - positions.get(code, 0)
                    )
                    affordable = max(0.0, cash - request.initial_cash_cny * request.minimum_cash_fraction)
                    if desired <= 0 or affordable <= 0:
                        continue
                    market = DailyBarLiquidity(
                        reference_price=bar.get("open"), traded_amount_cny=bar.get("amount"),
                        up_limit=bar.get("up"), down_limit=bar.get("down"),
                        is_suspended=bool(bar.get("suspended", True)),
                        is_tradeable_bar=bool(bar.get("tradeable", False)),
                    )
                    quantity = desired
                    fill = simulate_daily_bar_fill(
                        OrderIntent(side=OrderSide.BUY, quantity=quantity), market, execution, session
                    )
                    for _ in range(3):
                        if fill.status is not FillStatus.FILLED or fill.fill_price is None:
                            break
                        required_cash = _cash(quantity * fill.fill_price + fill.total_cost_cny)
                        if required_cash <= affordable:
                            break
                        affordable_shares = int(affordable / fill.fill_price)
                        quantity = _buy_order_quantity(code, min(quantity - 1, affordable_shares))
                        if quantity <= 0:
                            break
                        fill = simulate_daily_bar_fill(
                            OrderIntent(side=OrderSide.BUY, quantity=quantity), market, execution, session
                        )
                    trade_attempt_count += 1
                    if quantity > 0 and fill.status is FillStatus.FILLED and fill.fill_price is not None:
                        amount = _cash(quantity * fill.fill_price)
                        if _cash(amount + fill.total_cost_cny) > affordable:
                            rejection_counts["INSUFFICIENT_CASH"] = rejection_counts.get("INSUFFICIENT_CASH", 0) + 1
                            continue
                        positions[code] = positions.get(code, 0) + quantity
                        position_costs[code] = _cash(position_costs.get(code, 0.0) + amount + fill.total_cost_cny)
                        cash = _cash(cash - amount - fill.total_cost_cny)
                        intraday_marks[code] = fill.fill_price
                        day_notional = _cash(day_notional + amount)
                        day_cost = _cash(day_cost + fill.total_cost_cny)
                        trades.append(
                            {
                                "session": session.isoformat(), "rebalance_id": rebalance_count,
                                "ts_code": code, "security_name": bar.get("security_name") or code,
                                "side": "BUY", "quantity": quantity, "price": fill.fill_price,
                                "amount_cny": amount, "commission_cny": fill.commission_cny,
                                "stamp_duty_cny": fill.stamp_duty_cny,
                                "transfer_fee_cny": fill.transfer_fee_cny,
                                "total_cost_cny": fill.total_cost_cny,
                                "cost_basis_cny": None,
                                "allocated_dividend_cny": None,
                                "trading_realized_pnl_cny": None,
                                "realized_pnl_cny": None,
                                "realized_pnl_pct": None,
                                "post_quantity": positions[code],
                                **_post_trade_exposure(positions, cash, intraday_marks, code),
                            }
                        )
                    else:
                        rejection_counts[fill.status.value] = rejection_counts.get(fill.status.value, 0) + 1
                pending_target = None

            closing_value = 0.0
            missing_marks = 0
            for code, quantity in list(positions.items()):
                bar = bars.get(code, {})
                close_price = float(bar.get("close") or 0)
                if bar.get("delist_date") == session and close_price > 0:
                    proceeds = _cash(quantity * close_price)
                    allocated_cost = position_costs.get(code, 0.0)
                    allocated_dividend = position_dividends.get(code, 0.0)
                    trading_pnl = _cash(proceeds - allocated_cost)
                    realized_pnl = _cash(trading_pnl + allocated_dividend)
                    cash = _cash(cash + proceeds)
                    total_realized_pnl = _cash(total_realized_pnl + realized_pnl)
                    day_notional = _cash(day_notional + proceeds)
                    positions.pop(code)
                    position_costs.pop(code, None)
                    position_dividends.pop(code, None)
                    last_marks.pop(code, None)
                    intraday_marks.pop(code, None)
                    trades.append(
                        {
                            "session": session.isoformat(), "rebalance_id": rebalance_count,
                            "ts_code": code, "security_name": bar.get("security_name") or code,
                            "side": "SELL", "quantity": quantity, "price": close_price,
                            "amount_cny": proceeds, "commission_cny": 0.0,
                            "stamp_duty_cny": 0.0, "transfer_fee_cny": 0.0,
                            "total_cost_cny": 0.0, "cost_basis_cny": allocated_cost,
                            "allocated_dividend_cny": allocated_dividend,
                            "trading_realized_pnl_cny": trading_pnl,
                            "realized_pnl_cny": realized_pnl,
                            "realized_pnl_pct": realized_pnl / allocated_cost if allocated_cost else None,
                            "post_quantity": 0, "execution_reason": "DELISTING_LIQUIDATION",
                            **_post_trade_exposure(positions, cash, intraday_marks, code),
                        }
                    )
                    delisting_liquidations += 1
                    continue
                if close_price > 0:
                    last_marks[code] = close_price
                else:
                    missing_marks += 1
                closing_value += quantity * last_marks.get(code, 0)
            nav = _cash(cash + closing_value)
            previous_nav = daily[-1]["nav"] if daily else request.initial_cash_cny
            daily_return = nav / previous_nav - 1 if previous_nav else 0.0
            total_cost = _cash(total_cost + day_cost)
            total_turnover_notional = _cash(total_turnover_notional + day_notional)
            daily.append(
                {
                    "session": session.isoformat(), "nav": nav, "cash": cash,
                    "positions": len(positions), "daily_return": daily_return,
                    "turnover": day_notional / open_nav if open_nav else 0.0,
                    "cost": day_cost, "dividend_cash": day_dividend_cash,
                    "missing_marks": missing_marks,
                }
            )

            if session in signal_sessions and session != sessions[-1]:
                if target_schedule is not None:
                    pending_target = dict(target_schedule[session])
                    selected_holdings = [
                        {"ts_code": code, "weight": weight}
                        for code, weight in pending_target.items()
                    ]
                else:
                    signal_rows = _signal_rows(connection, resolved, request, session, prepared=True)
                    industry_by_code = (
                        _pit_industry_by_code(
                            connection,
                            session,
                            [str(item["ts_code"]) for item in signal_rows],
                        )
                        if maximum_industry_weight is not None
                        else None
                    )
                    selection = select_portfolio(
                        signal_rows,
                        request,
                        set(positions),
                        industry_by_code=industry_by_code,
                        maximum_industry_weight=maximum_industry_weight,
                        missing_industry_policy=missing_industry_policy,
                    )
                    selected_holdings = selection["holdings"]
                    count = len(selected_holdings)
                    pending_target = {
                        item["ts_code"]: 1 / count for item in selected_holdings
                    } if count else {}
                if include_selection_history:
                    selection_history.append(
                        {
                            "signal_session": session.isoformat(),
                            "execution_session": sessions[session_index].isoformat(),
                            "holdings": selected_holdings,
                        }
                    )
                if market_data_mode == "prefetch" and session_index < len(sessions):
                    next_session = sessions[session_index]
                    uncached = set(pending_target) - set(market_cache)
                    _prefetch_market_rows(
                        connection, next_session, sessions[-1], uncached, market_cache
                    )

            if market_data_mode == "prefetch":
                # Once a name has neither a live position nor a pending order,
                # its future bars cannot affect this continuous-account run.
                live_codes = set(positions) | set(pending_target or [])
                for code in set(market_cache) - live_codes:
                    market_cache.pop(code)

            if progress_callback:
                progress_callback(
                    {
                        "phase": "连续账户回放",
                        "progress": min(92, 18 + round(session_index / len(sessions) * 74)),
                        "processed_sessions": session_index,
                        "total_sessions": len(sessions),
                        "current_session": session.isoformat(),
                        "rebalance_count": rebalance_count,
                        "position_count": len(positions),
                    }
                )

    if progress_callback:
        progress_callback({"phase": "汇总绩效指标", "progress": 95})
    navs = [row["nav"] for row in daily]
    drawdown_period = _maximum_drawdown_period(daily)
    benchmark = _csi300_benchmark(project_root, sessions)
    returns = [row["daily_return"] for row in daily[1:]]
    years = max(len(returns) / 252, 1 / 252)
    total_return = navs[-1] / request.initial_cash_cny - 1
    annualized_return = (navs[-1] / request.initial_cash_cny) ** (1 / years) - 1
    volatility = pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    sharpe = (fmean(returns) / pstdev(returns) * math.sqrt(252)) if len(returns) > 1 and pstdev(returns) else None
    annual: list[dict[str, Any]] = []
    for year in sorted({date.fromisoformat(row["session"]).year for row in daily}):
        year_returns = [
            row["daily_return"] for row in daily if date.fromisoformat(row["session"]).year == year
        ]
        annual.append({"year": year, "return": math.prod(1 + value for value in year_returns) - 1})
    result = {
        "status": "PASS",
        "run_id": request.config_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "config": request.model_dump(mode="json"),
        "preflight": checked,
        "summary": {
            "total_return": total_return, "annualized_return": annualized_return,
            "maximum_drawdown": _max_drawdown(navs), "annualized_volatility": volatility,
            "sharpe": sharpe, "ending_nav": navs[-1], "total_cost": total_cost,
            "total_commission": _cash(sum(float(item.get("commission_cny") or 0) for item in trades)),
            "total_stamp_duty": _cash(sum(float(item.get("stamp_duty_cny") or 0) for item in trades)),
            "total_transfer_fee": _cash(sum(float(item.get("transfer_fee_cny") or 0) for item in trades)),
            "total_realized_pnl": total_realized_pnl,
            "total_dividend_cash": total_dividend_cash,
            "turnover": total_turnover_notional / request.initial_cash_cny,
            "rebalance_count": rebalance_count,
            "trade_count": trade_attempt_count,
            "filled_trade_count": len(trades),
            "delisting_liquidations": delisting_liquidations,
            "rejection_counts": rejection_counts,
        },
        "annual": annual,
        "execution_model": {
            "version": "2.0.0",
            "spec_hash": execution.spec_hash,
            "price_basis": "UNADJUSTED",
            "position_basis": "INTEGER_REAL_SHARES",
            "cost_basis_method": "MOVING_AVERAGE_INCLUDING_BUY_FEES",
        },
        "trades": trades,
        "daily": daily,
        "benchmark": benchmark,
        "drawdown_period": drawdown_period,
        "latest_holdings": sorted(positions),
    }
    if include_selection_history:
        result["selection_history"] = selection_history
    return result
