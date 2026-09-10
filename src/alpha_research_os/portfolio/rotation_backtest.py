"""Contracts and point-in-time controls for generic multi-portfolio rotation."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import Field, model_validator

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import Digest, FrozenSpec, Identifier
from alpha_research_os.portfolio.strategy_backtest import (
    FilterRule,
    ScoreRule,
    StrategyBacktestRequest,
    _effective_request,
    _pit_industry_by_code,
    _resolve_inputs,
    _signal_rows,
    run_backtest,
    select_portfolio,
)


class RotationCandidateSpec(FrozenSpec):
    """One independently formed factor portfolio used by the rotation signal."""

    candidate_id: Identifier
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["FACTOR"] = "FACTOR"
    score_rules: tuple[ScoreRule, ...] = Field(min_length=1, max_length=12)
    filter_rules: tuple[FilterRule, ...] = Field(default=(), max_length=12)
    exclude_st: bool = True
    minimum_listed_sessions: int = Field(default=60, ge=0, le=1250)
    target_count: int = Field(default=50, ge=1, le=500)
    retention_rank: int = Field(default=75, ge=1, le=1000)
    rebalance_sessions: int = Field(default=5, ge=1, le=60)
    industry_control: Literal["NONE", "CAP"] = "CAP"
    maximum_industry_weight: float = Field(default=0.25, gt=0, le=1)
    missing_industry_policy: Literal["UNKNOWN_BUCKET", "EXCLUDE"] = "UNKNOWN_BUCKET"

    @model_validator(mode="after")
    def valid_candidate(self) -> RotationCandidateSpec:
        if self.retention_rank < self.target_count:
            raise ValueError("retention_rank must be at least target_count")
        if len({rule.factor_id for rule in self.score_rules}) != len(self.score_rules):
            raise ValueError("score factors must be unique within a candidate")
        if len({rule.factor_id for rule in self.filter_rules}) != len(self.filter_rules):
            raise ValueError("filter factors must be unique within a candidate")
        return self


class RotationSignalSpec(FrozenSpec):
    metric: Literal["TRAILING_RETURN", "EXCESS_RETURN", "RISK_ADJUSTED_RETURN"] = (
        "TRAILING_RETURN"
    )
    lookback_sessions: int = Field(default=20, ge=2, le=252)
    decision_interval_sessions: int = Field(default=1, ge=1, le=60)
    switch_threshold: float = Field(default=0.01, ge=0, le=0.5)
    confirmation_periods: int = Field(default=1, ge=1, le=20)
    minimum_hold_periods: int = Field(default=1, ge=0, le=252)


class RotationAllocationSpec(FrozenSpec):
    mode: Literal["WINNER_TAKE_ALL", "WINNER_TILT", "SCORE_WEIGHTED"] = "WINNER_TAKE_ALL"
    winner_weight: float = Field(default=0.70, gt=0.5, le=1)
    minimum_cash_fraction: float = Field(default=0.02, ge=0, lt=0.5)


class RotationBacktestRequest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    strategy_type: Literal["ROTATION"] = "ROTATION"
    name: str = Field(min_length=1, max_length=100)
    start: date
    end: date
    universe_id: Literal["ALL-A-PIT"] = "ALL-A-PIT"
    candidates: tuple[RotationCandidateSpec, ...] = Field(min_length=2, max_length=8)
    signal: RotationSignalSpec = RotationSignalSpec()
    allocation: RotationAllocationSpec = RotationAllocationSpec()
    initial_cash_cny: float = Field(default=1_000_000, gt=0)
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
    def valid_request(self) -> RotationBacktestRequest:
        if self.end < self.start:
            raise ValueError("end must not precede start")
        ids = [item.candidate_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate_id values must be unique")
        return self

    @property
    def config_id(self) -> Digest:
        return content_hash(self)


class RotationState(FrozenSpec):
    active_candidate_id: str | None = None
    pending_candidate_id: str | None = None
    confirmation_count: int = Field(default=0, ge=0)
    periods_since_switch: int = Field(default=0, ge=0)


def _allocation(candidate_ids: list[str], leader: str, spec: RotationAllocationSpec) -> dict[str, float]:
    investable = 1 - spec.minimum_cash_fraction
    if spec.mode == "WINNER_TAKE_ALL" or len(candidate_ids) == 1:
        return {candidate_id: investable if candidate_id == leader else 0.0 for candidate_id in candidate_ids}
    if spec.mode == "WINNER_TILT":
        winner = investable * spec.winner_weight
        other = (investable - winner) / (len(candidate_ids) - 1)
        return {candidate_id: winner if candidate_id == leader else other for candidate_id in candidate_ids}
    # Negative scores receive no capital. If every score is non-positive the
    # caller falls back to the selected leader, avoiding unstable signed weights.
    raise ValueError("SCORE_WEIGHTED allocations require the score-aware decision path")


def advance_rotation(
    state: RotationState,
    scores: dict[str, float],
    signal: RotationSignalSpec,
    allocation: RotationAllocationSpec,
) -> dict[str, Any]:
    """Advance the hysteresis state machine by one scheduled decision period."""

    finite_scores = {key: float(value) for key, value in scores.items() if math.isfinite(value)}
    if len(finite_scores) < 2:
        raise ValueError("at least two finite candidate scores are required")
    ordered = sorted(finite_scores, key=lambda key: (-finite_scores[key], key))
    leader, runner_up = ordered[:2]
    advantage = finite_scores[leader] - finite_scores[runner_up]
    active = state.active_candidate_id
    pending = state.pending_candidate_id
    confirmation = state.confirmation_count
    periods_since_switch = state.periods_since_switch + 1
    switched = False
    reason = "HOLD"

    if active is None:
        active = leader
        pending = None
        confirmation = 0
        periods_since_switch = 0
        switched = True
        reason = "INITIAL_SELECTION"
    elif leader == active or advantage <= signal.switch_threshold:
        pending = None
        confirmation = 0
        reason = "ACTIVE_LEADS" if leader == active else "BELOW_THRESHOLD"
    elif periods_since_switch < signal.minimum_hold_periods:
        pending = None
        confirmation = 0
        reason = "MINIMUM_HOLD"
    else:
        confirmation = confirmation + 1 if pending == leader else 1
        pending = leader
        if confirmation >= signal.confirmation_periods:
            active = leader
            pending = None
            confirmation = 0
            periods_since_switch = 0
            switched = True
            reason = "SWITCH_CONFIRMED"
        else:
            reason = "AWAITING_CONFIRMATION"

    next_state = RotationState(
        active_candidate_id=active,
        pending_candidate_id=pending,
        confirmation_count=confirmation,
        periods_since_switch=periods_since_switch,
    )
    if allocation.mode == "SCORE_WEIGHTED":
        positives = {key: max(value, 0.0) for key, value in finite_scores.items()}
        total = sum(positives.values())
        if total:
            investable = 1 - allocation.minimum_cash_fraction
            weights = {key: investable * positives[key] / total for key in ordered}
        else:
            weights = _allocation(ordered, active, allocation.model_copy(update={"mode": "WINNER_TAKE_ALL"}))
    else:
        weights = _allocation(ordered, active, allocation)
    return {
        "state": next_state,
        "leader": leader,
        "advantage": advantage,
        "switched": switched,
        "reason": reason,
        "weights": weights,
    }


def pit_industry_snapshot(
    connection: duckdb.DuckDBPyConnection, signal_date: date, ts_codes: list[str] | None = None
) -> dict[str, Any]:
    """Return one deterministic SW2021 L1 industry per security as known on a date."""

    parameters: list[Any] = [signal_date, signal_date]
    restriction = ""
    if ts_codes is not None:
        if not ts_codes:
            return {"signal_date": signal_date.isoformat(), "memberships": {}, "missing": [], "overlaps": 0}
        restriction = "AND ts_code IN (" + ",".join("?" for _ in ts_codes) + ")"
        parameters.extend(sorted(set(ts_codes)))
    rows = connection.execute(
        f"""WITH valid AS (
            SELECT *, count(*) OVER (PARTITION BY ts_code) AS valid_count,
                   row_number() OVER (
                     PARTITION BY ts_code
                     ORDER BY in_date DESC, source_snapshot_id DESC, l3_code DESC
                   ) AS row_number
            FROM research.sw_industry_membership
            WHERE in_date <= ? AND (out_date IS NULL OR ? < out_date) {restriction}
        )
        SELECT ts_code, l1_code, l1_name, l2_code, l2_name, l3_code, l3_name,
               in_date, out_date, valid_count
        FROM valid WHERE row_number=1 ORDER BY ts_code""",
        parameters,
    ).fetchall()
    columns = (
        "ts_code", "l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name",
        "in_date", "out_date", "valid_count",
    )
    memberships = {row[0]: dict(zip(columns[1:], row[1:], strict=True)) for row in rows}
    requested = sorted(set(ts_codes or memberships))
    return {
        "signal_date": signal_date.isoformat(),
        "memberships": memberships,
        "missing": [code for code in requested if code not in memberships],
        "overlaps": sum(1 for item in memberships.values() if item["valid_count"] > 1),
    }


def _candidate_request(parent: RotationBacktestRequest, candidate: RotationCandidateSpec) -> StrategyBacktestRequest:
    return StrategyBacktestRequest(
        name=f"{parent.name} / {candidate.name}",
        start=parent.start,
        end=parent.end,
        universe_id=parent.universe_id,
        score_rules=candidate.score_rules,
        filter_rules=candidate.filter_rules,
        exclude_st=candidate.exclude_st,
        minimum_listed_sessions=candidate.minimum_listed_sessions,
        target_count=candidate.target_count,
        retention_rank=candidate.retention_rank,
        rebalance_sessions=candidate.rebalance_sessions,
        initial_cash_cny=parent.initial_cash_cny,
        minimum_cash_fraction=parent.allocation.minimum_cash_fraction,
        buy_commission_bps=parent.buy_commission_bps,
        sell_commission_bps=parent.sell_commission_bps,
        sell_stamp_duty_bps=parent.sell_stamp_duty_bps,
        historical_sell_stamp_duty_bps=parent.historical_sell_stamp_duty_bps,
        minimum_commission_cny=parent.minimum_commission_cny,
        transfer_fee_bps=parent.transfer_fee_bps,
        historical_transfer_fee_bps=parent.historical_transfer_fee_bps,
        base_slippage_bps=parent.base_slippage_bps,
        square_root_impact_bps=parent.square_root_impact_bps,
        maximum_slippage_bps=parent.maximum_slippage_bps,
        maximum_participation_rate=parent.maximum_participation_rate,
    )


def preflight_rotation(project_root: Path, request: RotationBacktestRequest) -> dict[str, Any]:
    """Validate factor coverage and PIT industry readiness before an expensive run."""

    project_root = project_root.resolve()
    candidate_results: list[dict[str, Any]] = []
    all_releases: set[str] = set()
    for candidate in request.candidates:
        candidate_request = _candidate_request(request, candidate)
        resolved = _resolve_inputs(project_root, candidate_request)
        effective = _effective_request(candidate_request, resolved)
        all_releases.update(item["release_id"] for item in resolved)
        candidate_results.append(
            {
                "candidate_id": candidate.candidate_id,
                "name": candidate.name,
                "factor_count": len(resolved),
                "config_id": effective.config_id,
                "common_range": {
                    "start": max(item["start"] for item in resolved).isoformat(),
                    "end": min(item["end"] for item in resolved).isoformat(),
                },
            }
        )
    database = project_root / "data" / "warehouse" / "alpha_research.duckdb"
    with duckdb.connect(str(database), read_only=True) as connection:
        sessions = connection.execute(
            """SELECT cal_date FROM research.trading_calendar
            WHERE exchange='SSE' AND is_open AND cal_date BETWEEN ? AND ? ORDER BY cal_date""",
            [request.start, request.end],
        ).fetchall()
        if len(sessions) < request.signal.lookback_sessions + 1:
            raise ValueError("backtest range is shorter than the rotation lookback window")
        end_session = sessions[-1][0]
        codes = [
            row[0]
            for row in connection.execute(
                """SELECT ts_code FROM research.universe_daily
                WHERE trade_date=? AND eligible_for_signal ORDER BY ts_code""",
                [end_session],
            ).fetchall()
        ]
        industry = pit_industry_snapshot(connection, end_session, codes)
    return {
        "status": "READY",
        "config_id": request.config_id,
        "strategy_type": request.strategy_type,
        "session_count": len(sessions),
        "estimated_decisions": max(
            1,
            (len(sessions) - request.signal.lookback_sessions)
            // request.signal.decision_interval_sessions,
        ),
        "candidate_count": len(request.candidates),
        "release_count": len(all_releases),
        "candidates": candidate_results,
        "industry": {
            "as_of": end_session.isoformat(),
            "universe_count": len(codes),
            "mapped_count": len(industry["memberships"]),
            "missing_count": len(industry["missing"]),
            "overlap_resolved_count": industry["overlaps"],
            "pit_rule": "latest in_date among records valid on signal date",
        },
        "warnings": (
            [f"行业归属缺失 {len(industry['missing'])} 只，将按候选组合的缺失行业规则处理。"]
            if industry["missing"]
            else []
        ),
    }


def preview_rotation(
    project_root: Path, request: RotationBacktestRequest, signal_date: date
) -> dict[str, Any]:
    """Preview every candidate with the same PIT selection rules used by a full run."""

    if not request.start <= signal_date <= request.end:
        raise ValueError("preview date must stay inside the backtest range")
    checked = preflight_rotation(project_root, request)
    database = project_root / "data" / "warehouse" / "alpha_research.duckdb"
    previews: list[dict[str, Any]] = []
    holdings_by_candidate: dict[str, set[str]] = {}
    with duckdb.connect(str(database), read_only=True) as connection:
        available = connection.execute(
            """SELECT is_open FROM research.trading_calendar
            WHERE exchange='SSE' AND cal_date=?""",
            [signal_date],
        ).fetchone()
        if not available or not available[0]:
            raise ValueError("preview date must be a trading session")
        for candidate in request.candidates:
            candidate_request = _candidate_request(request, candidate)
            resolved = _resolve_inputs(project_root, candidate_request)
            effective = _effective_request(candidate_request, resolved)
            rows = _signal_rows(connection, resolved, effective, signal_date)
            industries = _pit_industry_by_code(
                connection,
                signal_date,
                [str(item["ts_code"]) for item in rows],
            )
            selected = select_portfolio(
                rows,
                effective,
                industry_by_code=industries,
                maximum_industry_weight=(
                    candidate.maximum_industry_weight if candidate.industry_control == "CAP" else None
                ),
                missing_industry_policy=candidate.missing_industry_policy,
            )
            codes = {str(item["ts_code"]) for item in selected["holdings"]}
            holdings_by_candidate[candidate.candidate_id] = codes
            previews.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "name": candidate.name,
                    **selected,
                    "industry_missing_count": sum(
                        1 for item in selected["holdings"] if item["industry_code"] is None
                    ),
                }
            )
    overlaps = []
    for left_index, left in enumerate(request.candidates):
        for right in request.candidates[left_index + 1 :]:
            shared = holdings_by_candidate[left.candidate_id] & holdings_by_candidate[right.candidate_id]
            union = holdings_by_candidate[left.candidate_id] | holdings_by_candidate[right.candidate_id]
            overlaps.append(
                {
                    "left_candidate_id": left.candidate_id,
                    "right_candidate_id": right.candidate_id,
                    "shared_count": len(shared),
                    "jaccard": len(shared) / len(union) if union else 0.0,
                }
            )
    return {
        "status": "READY",
        "signal_date": signal_date.isoformat(),
        "preflight": checked,
        "candidates": previews,
        "overlaps": overlaps,
        "execution_session": "NEXT_ELIGIBLE_OPEN",
    }


def _trailing_score(
    metric: str,
    daily: list[dict[str, Any]],
    index: int,
    lookback: int,
    benchmark_returns: dict[str, float],
) -> float:
    start = float(daily[index - lookback]["nav"])
    end = float(daily[index]["nav"])
    trailing_return = end / start - 1 if start else 0.0
    if metric == "TRAILING_RETURN":
        return trailing_return
    if metric == "EXCESS_RETURN":
        end_benchmark = benchmark_returns.get(str(daily[index]["session"]))
        start_benchmark = benchmark_returns.get(str(daily[index - lookback]["session"]))
        benchmark_return = (
            (1 + end_benchmark) / (1 + start_benchmark) - 1
            if end_benchmark is not None and start_benchmark is not None and 1 + start_benchmark
            else 0.0
        )
        return trailing_return - benchmark_return
    returns = [float(item["daily_return"]) for item in daily[index - lookback + 1 : index + 1]]
    volatility = math.sqrt(sum((value - sum(returns) / len(returns)) ** 2 for value in returns) / len(returns))
    return (sum(returns) / len(returns)) / volatility if volatility else 0.0


def run_rotation_backtest(
    project_root: Path,
    request: RotationBacktestRequest,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Run costed shadow portfolios, then execute their net targets in one account."""

    checked = preflight_rotation(project_root, request)
    shadow_results: dict[str, dict[str, Any]] = {}
    selection_histories: dict[str, list[dict[str, Any]]] = {}
    candidate_requests: dict[str, StrategyBacktestRequest] = {}
    candidate_count = len(request.candidates)
    for candidate_index, candidate in enumerate(request.candidates):
        candidate_request = _candidate_request(request, candidate)
        candidate_requests[candidate.candidate_id] = candidate_request

        def shadow_progress(detail: dict[str, Any], *, index: int = candidate_index) -> None:
            if progress_callback:
                source_progress = float(detail.get("progress") or 0)
                progress_callback(
                    {
                        "phase": f"构建影子组合：{request.candidates[index].name}",
                        "progress": round(5 + (index + source_progress / 100) / candidate_count * 40),
                    }
                )

        shadow = run_backtest(
            project_root,
            candidate_request,
            shadow_progress,
            include_selection_history=True,
            maximum_industry_weight=(
                candidate.maximum_industry_weight if candidate.industry_control == "CAP" else None
            ),
            missing_industry_policy=candidate.missing_industry_policy,
        )
        selection_histories[candidate.candidate_id] = shadow.pop("selection_history")
        shadow_results[candidate.candidate_id] = shadow

    first_shadow = shadow_results[request.candidates[0].candidate_id]
    sessions = [date.fromisoformat(item["session"]) for item in first_shadow["daily"]]
    benchmark_returns = {
        item["session"]: float(item["return"])
        for item in (first_shadow.get("benchmark") or {}).get("daily", [])
    }
    histories_by_date = {
        candidate_id: {
            date.fromisoformat(item["signal_session"]): item["holdings"]
            for item in history
        }
        for candidate_id, history in selection_histories.items()
    }
    current_holdings: dict[str, list[dict[str, Any]]] = {
        candidate.candidate_id: [] for candidate in request.candidates
    }
    state = RotationState()
    target_schedule: dict[date, dict[str, float]] = {}
    decisions: list[dict[str, Any]] = []
    lookback = request.signal.lookback_sessions
    interval = request.signal.decision_interval_sessions
    for session_index, session in enumerate(sessions):
        for candidate_id, history in histories_by_date.items():
            if session in history:
                current_holdings[candidate_id] = history[session]
        if session_index == 0:
            warmup_weights: dict[str, float] = {}
            available = [holdings for holdings in current_holdings.values() if holdings]
            for holdings in available:
                holding_weight = 1 / len(available) / len(holdings)
                for holding in holdings:
                    code = holding["ts_code"]
                    warmup_weights[code] = warmup_weights.get(code, 0.0) + holding_weight
            if warmup_weights:
                target_schedule[session] = warmup_weights
                decisions.append(
                    {
                        "signal_session": session.isoformat(),
                        "execution_session": sessions[1].isoformat(),
                        "scores": None,
                        "leader": None,
                        "advantage": None,
                        "active_candidate_id": None,
                        "pending_candidate_id": None,
                        "confirmation_count": 0,
                        "switched": False,
                        "reason": "WARMUP_EQUAL_WEIGHT",
                        "candidate_weights": {
                            candidate.candidate_id: 1 / len(available)
                            if current_holdings[candidate.candidate_id]
                            else 0.0
                            for candidate in request.candidates
                        },
                        "target_stock_count": len(warmup_weights),
                    }
                )
        if session_index < lookback or (session_index - lookback) % interval or session == sessions[-1]:
            continue
        scores = {
            candidate.candidate_id: _trailing_score(
                request.signal.metric,
                shadow_results[candidate.candidate_id]["daily"],
                session_index,
                lookback,
                benchmark_returns,
            )
            for candidate in request.candidates
        }
        decision = advance_rotation(state, scores, request.signal, request.allocation)
        state = decision["state"]
        stock_weights: dict[str, float] = {}
        candidate_weight_total = sum(decision["weights"].values())
        for candidate_id, candidate_weight in decision["weights"].items():
            holdings = current_holdings[candidate_id]
            if candidate_weight <= 0 or not holdings:
                continue
            normalized_candidate_weight = candidate_weight / candidate_weight_total
            holding_weight = normalized_candidate_weight / len(holdings)
            for holding in holdings:
                code = holding["ts_code"]
                stock_weights[code] = stock_weights.get(code, 0.0) + holding_weight
        total_stock_weight = sum(stock_weights.values())
        if total_stock_weight:
            stock_weights = {code: weight / total_stock_weight for code, weight in stock_weights.items()}
        target_schedule[session] = stock_weights
        decisions.append(
            {
                "signal_session": session.isoformat(),
                "execution_session": sessions[session_index + 1].isoformat(),
                "scores": scores,
                "leader": decision["leader"],
                "advantage": decision["advantage"],
                "active_candidate_id": state.active_candidate_id,
                "pending_candidate_id": state.pending_candidate_id,
                "confirmation_count": state.confirmation_count,
                "switched": decision["switched"],
                "reason": decision["reason"],
                "candidate_weights": decision["weights"],
                "target_stock_count": len(stock_weights),
            }
        )

    def joint_progress(detail: dict[str, Any]) -> None:
        if progress_callback:
            source_progress = float(detail.get("progress") or 0)
            progress_callback(
                {
                    **detail,
                    "phase": "联合账户净额成交",
                    "progress": round(50 + source_progress / 100 * 48),
                }
            )

    joint = run_backtest(
        project_root,
        candidate_requests[request.candidates[0].candidate_id],
        joint_progress,
        target_schedule=target_schedule,
    )
    joint.update(
        {
            "run_id": request.config_id,
            "strategy_type": "ROTATION",
            "config": request.model_dump(mode="json"),
            "preflight": checked,
            "rotation": {
                "signal": request.signal.model_dump(mode="json"),
                "allocation": request.allocation.model_dump(mode="json"),
                "decision_count": len(decisions),
                "switch_count": sum(1 for item in decisions if item["switched"]),
                "decisions": decisions,
                "candidate_shadows": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "name": candidate.name,
                        "summary": shadow_results[candidate.candidate_id]["summary"],
                        "daily": shadow_results[candidate.candidate_id]["daily"],
                    }
                    for candidate in request.candidates
                ],
            },
        }
    )
    if progress_callback:
        progress_callback({"phase": "轮动回测完成", "progress": 100})
    return joint
